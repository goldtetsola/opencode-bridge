#!/usr/bin/env python3
r"""A3-open live burn-in pack — varied investigations with metrics tracking.

Run:
  set -a; source .codex-oss/env/opencode-go.env; set +a; LIVE_BURNIN=1 python3 tests/test_a3_open_burnin_pack.py

Categories:
  1. Contradiction/blocked-source investigations
  2. Evidence-kind with required_shapes
  3. Ambiguous multi-file investigations
  4. Agenda-guided exploration investigations
  5. Exploration policy + optional check investigations

Metrics per run:
  - status (COMPLETE | PARTIAL | ESCALATE | FAILED)
  - closure_source (model | runtime_answer_graph | prefetch)
  - required_sources_covered / required_total
  - required_evidence_shapes_covered
  - optional_exploration_count
  - contradiction_search_done
  - false_COMPLETE (COMPLETE with missing required evidence)
  - raw_dump_incidents
  - GPT_cleanup_rating (subjective: minor|moderate|major)
"""

from __future__ import annotations

import json
import os
import sys
import time
import http.client
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)
BASE_URL = os.getenv("OSS_LIVE_BURNIN_BASE_URL", "http://127.0.0.1:4000/v1").rstrip("/")
AUTH = os.getenv("PROXY_API_KEY") or os.getenv("LITELLM_MASTER_KEY") or "sk-local-codex-bridge"
TIMEOUT = float(os.getenv("OSS_LIVE_BURNIN_TIMEOUT", "180"))
MODEL = os.getenv("OSS_LIVE_BURNIN_MODEL", "mission-a3-kimi")
MISSIONS_ROOT = os.path.join(ROOT, ".codex-oss", "missions")

REQUIRED_OUTPUTS = [
    "files_inspected", "commands_run", "findings", "uncertainties",
    "confidence", "caveats", "escalation_recommendation",
]


@dataclass
class BurninCase:
    name: str
    category: str
    mission: dict
    expected_requires_closure: str = ""
    tolerance_for_false_complete: bool = False
    # Multi-axis scenario expectations
    expected_answer_statuses: list = field(default_factory=list)
    expected_evidence_statuses: list = field(default_factory=list)
    expected_verification_statuses: list = field(default_factory=list)
    expected_closure_statuses: list = field(default_factory=list)
    expected_final_statuses: list = field(default_factory=list)
    requires_native_feeling: bool = False


@dataclass
class BurninResult:
    case: str = ""
    category: str = ""
    case_id: str = ""
    mission_id: str = ""
    status: str = ""
    closure_source: str = ""
    required_sources_covered: int = 0
    required_total: int = 0
    evidence_shapes_covered: int = 0
    optional_exploration: int = 0
    contradiction_search: bool = False
    runtime_finalized: bool = False
    false_complete: bool = False
    raw_dump_incidents: int = 0
    caveats: list[str] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    passed: bool = False
    elapsed: float = 0
    artifact_dir: str = ""


def _make_mission(name: str, objective: str, **overrides) -> dict:
    base = {
        "schema_version": "oss_agent_mission.v1",
        "mission_id": f"mission_a3_open_burnin_{name}",
        "tier": "A3",
        "mode": "managed_investigation",
        "objective": objective,
        "risk_tier": "low",
        "write_allowed": False,
        "allowed_roots": [],
        "allow_broad_read_scope": True,
        "allowed_paths": [],
        "allowed_tool_classes": ["read", "search", "list"],
        "tool_budget": 8,
        "time_budget_seconds": 120,
        "stop_conditions": ["valid_report", "budget_exhausted", "deadline_reached"],
        "report_schema": "managed_investigation_report.v1",
        "required_outputs": REQUIRED_OUTPUTS,
        "objective_style": "open_investigation",
        "evidence_collection_mode": "agenda_guided",
        "exploration_policy": {
            "after_required_floor": "allow_model_exploration",
            "min_optional_actions_after_floor": 1,
            "max_optional_actions_after_floor": 3,
            "require_contradiction_search": True,
        },
        "sufficiency_policy": {
            "required_source_coverage": 1.0,
            "allow_closure_when_missing_sources": False,
        },
    }
    base.update(overrides)
    return base


def _run_mission_id(base_mission_id: str, idx: int, run_id: str) -> str:
    return f"{base_mission_id}_{idx}__{run_id}"


def build_burnin_cases() -> list[BurninCase]:
    cases: list[BurninCase] = []

    # Category 1: Contradiction/blocked-source investigations
    cases.append(BurninCase(
        name="blocked_source_missing_file",
        category="contradiction_blocked",
        mission=_make_mission("blocked_missing",
            objective="Investigate whether a non-existent required file can block completion.",
            allowed_paths=["codex_oss/nonexistent.py"],
            allowed_tool_classes=["read"],
            answer_obligations=[{
                "id": "q1", "question": "What does the nonexistent file contain?",
                "required": True,
                "source_hints": ["codex_oss/nonexistent.py"],
                "source_requirements": [{"path": "codex_oss/nonexistent.py", "evidence_kind": "source_access", "required": True, "prefetch": False}],
            }],
            must_inspect=["codex_oss/nonexistent.py"],
        ),
        expected_requires_closure="PARTIAL",
        expected_answer_statuses=["BLOCKED"],
        expected_evidence_statuses=["BLOCKED"],
        expected_final_statuses=["PARTIAL", "ESCALATE"],
        expected_closure_statuses=["RUNTIME_CLOSED"],
        tolerance_for_false_complete=True,
    ))

    cases.append(BurninCase(
        name="contradiction_marker_in_source",
        category="contradiction_blocked",
        mission=_make_mission("contradiction_marker",
            objective="Check if contradiction markers block COMPLETE status.",
            allowed_paths=["codex_oss/raw_lane.py"],
            allowed_tool_classes=["read"],
            answer_obligations=[{
                "id": "q1", "question": "Is the raw lane free of probe flag references?",
                "required": True,
                "source_hints": ["codex_oss/raw_lane.py"],
                "source_requirements": [{
                    "path": "codex_oss/raw_lane.py",
                    "evidence_kind": "contradiction_check",
                    "required": True, "prefetch": False,
                    "contradiction_markers": ["probe_flag", "raw_cert_accepts_flags"],
                }],
            }],
            must_inspect=["codex_oss/raw_lane.py"],
        ),
        expected_requires_closure="PARTIAL",
        tolerance_for_false_complete=True,
    ))

    cases.append(BurninCase(
        name="required_source_readable_no_contradiction",
        category="contradiction_blocked",
        mission=_make_mission("required_readable",
            objective="Verify the audit module is readable and returns expected fields.",
            exploration_policy={"after_required_floor": "close_immediately", "min_optional_actions_after_floor": 0, "max_optional_actions_after_floor": 0, "require_contradiction_search": False},
            allowed_paths=["codex_oss/audit.py"],
            allowed_tool_classes=["read"],
            answer_obligations=[{
                "id": "q1", "question": "Does audit.py define audit_mission and _check?",
                "required": True,
                "source_hints": ["codex_oss/audit.py"],
                "source_requirements": [{"path": "codex_oss/audit.py", "evidence_kind": "function_defs", "required": True, "prefetch": False, "contradiction_markers": ["TOKEN_AUDIT_FAILED"]}],
            }],
            must_inspect=["codex_oss/audit.py"],
        ),
        expected_requires_closure="COMPLETE",
    ))

    # Category 2: Evidence-kind with required_shapes
    cases.append(BurninCase(
        name="shape_function_definition",
        category="evidence_kind",
        mission=_make_mission("shape_func_def",
            exploration_policy={"after_required_floor": "close_immediately", "min_optional_actions_after_floor": 0, "max_optional_actions_after_floor": 0, "require_contradiction_search": False},
            objective="Find which files define the build_implementation_readiness_graph function.",
            allowed_paths=["codex_oss/implementation_graph.py", "codex_oss/implementation.py"],
            allowed_tool_classes=["read", "search"],
            answer_obligations=[{
                "id": "q1", "question": "Which file defines build_implementation_readiness_graph?",
                "required": True,
                "source_hints": ["codex_oss/implementation_graph.py"],
                "source_requirements": [{
                    "path": "codex_oss/implementation_graph.py",
                    "evidence_kind": "function_definition",
                    "required": True, "prefetch": False,
                    "required_shapes": ["function_definition"],
                }],
            }],
            must_inspect=["codex_oss/implementation_graph.py"],
        ),
        expected_requires_closure="COMPLETE",
    ))

    cases.append(BurninCase(
        name="shape_config_value",
        category="evidence_kind",
        mission=_make_mission("shape_config",
            exploration_policy={"after_required_floor": "close_immediately", "min_optional_actions_after_floor": 0, "max_optional_actions_after_floor": 0, "require_contradiction_search": False},
            objective="Find the fallback budget value in the deadline policy.",
            allowed_paths=["codex_oss/runtime/policy.py"],
            allowed_tool_classes=["read"],
            answer_obligations=[{
                "id": "q1", "question": "What is the fallback_budget in DeadlinePolicy?",
                "required": True,
                "source_hints": ["codex_oss/runtime/policy.py"],
                "source_requirements": [{
                    "path": "codex_oss/runtime/policy.py",
                    "evidence_kind": "config_value",
                    "required": True, "prefetch": False,
                    "required_shapes": ["config_value", "class_definition"],
                }],
            }],
            must_inspect=["codex_oss/runtime/policy.py"],
        ),
        expected_requires_closure="COMPLETE",
    ))

    # Category 3: Ambiguous multi-file investigations
    cases.append(BurninCase(
        name="ambiguous_which_model_aliases",
        category="ambiguous_multifile",
        mission=_make_mission("ambiguous_model_aliases",
            exploration_policy={"after_required_floor": "close_immediately", "min_optional_actions_after_floor": 0, "max_optional_actions_after_floor": 0, "require_contradiction_search": False},
            objective="Find all model alias mappings used in the runtime and report how they work.",
            allowed_paths=[
                "codex_oss/runtime/loop.py",
                "codex_oss/runtime/policy.py",
                "codex_oss/managed_bridge.py",
            ],
            allowed_tool_classes=["read", "search"],
            answer_obligations=[{
                "id": "q1", "question": "How are OSS model aliases mapped to provider-specific model names?",
                "required": True,
                "source_hints": [
                    "codex_oss/runtime/loop.py",
                    "codex_oss/managed_bridge.py",
                ],
                "source_requirements": [
                    {"path": "codex_oss/runtime/loop.py", "evidence_kind": "model_alias_logic", "required": True, "prefetch": False},
                    {"path": "codex_oss/managed_bridge.py", "evidence_kind": "bridge_mapping", "required": True, "prefetch": False},
                ],
            }],
            must_inspect=["codex_oss/runtime/loop.py", "codex_oss/managed_bridge.py"],
        ),
        expected_requires_closure="COMPLETE",
    ))

    cases.append(BurninCase(
        name="ambiguous_artifact_pipeline",
        category="ambiguous_multifile",
        mission=_make_mission("artifact_pipeline",
            exploration_policy={"after_required_floor": "close_immediately", "min_optional_actions_after_floor": 0, "max_optional_actions_after_floor": 0, "require_contradiction_search": False},
            objective="Trace how implementation artifacts flow from patch proposal through persistence. Which files write which artifacts?",
            allowed_paths=[
                "codex_oss/implementation.py",
                "codex_oss/implementation_graph.py",
                "codex_oss/audit.py",
            ],
            allowed_tool_classes=["read", "search"],
            answer_obligations=[{
                "id": "q1", "question": "What artifacts are persisted during implementation (A4/A5/A6), and which functions write them?",
                "required": True,
                "source_hints": ["codex_oss/implementation.py"],
                "source_requirements": [
                    {"path": "codex_oss/implementation.py", "evidence_kind": "artifact_persistence", "required": True, "prefetch": False},
                ],
            }],
            must_inspect=["codex_oss/implementation.py"],
        ),
        expected_requires_closure="COMPLETE",
    ))

    # Category 4: Agenda-guided exploration
    cases.append(BurninCase(
        name="agenda_guided_explore_before_close",
        category="agenda_guided",
        mission=_make_mission("agenda_explore",
            objective="Explore the coverage model: what does CoverageGraphV1 track beyond just source_read?",
            evidence_collection_mode="agenda_guided",
            exploration_policy={
                "after_required_floor": "allow_model_exploration",
                "min_optional_actions_after_floor": 2,
                "max_optional_actions_after_floor": 4,
                "require_contradiction_search": True,
            },
            allowed_paths=[
                "codex_oss/answer_graph.py",
                "codex_oss/claim_graph.py",
                "codex_oss/answer_graph.py",
            ],
            allowed_tool_classes=["read", "search"],
            answer_obligations=[{
                "id": "q1", "question": "What does CoverageGraphV1 track, and how does it relate to answer obligations?",
                "required": True,
                "source_hints": ["codex_oss/answer_graph.py", "codex_oss/answer_graph.py"],
                "source_requirements": [
                    {"path": "codex_oss/answer_graph.py", "evidence_kind": "coverage_model", "required": True, "prefetch": False},
                    {"path": "codex_oss/answer_graph.py", "evidence_kind": "obligation_model", "required": True, "prefetch": False},
                ],
            }],
            must_inspect=["codex_oss/answer_graph.py", "codex_oss/answer_graph.py"],
        ),
        expected_requires_closure="PARTIAL",
        expected_answer_statuses=["ANSWERED"],
        expected_verification_statuses=["INCOMPLETE"],
        expected_final_statuses=["PARTIAL"],
    ))

    # Category 5: Exploration policy + close_immediately
    cases.append(BurninCase(
        name="close_immediately_deterministic",
        category="exploration_policy",
        mission=_make_mission("close_immediate",
            objective="Find and report the exact value of LANE_CAPACITY['read_pool'] in codex_oss/runtime/policy.py.",
            evidence_collection_mode="prefetch_floor",
            exploration_policy={"after_required_floor": "close_immediately"},
            allowed_paths=["codex_oss/runtime/policy.py"],
            allowed_tool_classes=["read", "search"],
            answer_obligations=[{
                "id": "q1", "question": "What is LANE_CAPACITY['read_pool']?",
                "required": True,
                "source_hints": ["codex_oss/runtime/policy.py"],
                "source_requirements": [{
                    "path": "codex_oss/runtime/policy.py",
                    "evidence_kind": "config_value",
                    "required": True, "prefetch": True,
                    "required_shapes": ["config_value", "mapping_assignment"],
                }],
            }],
            must_inspect=["codex_oss/runtime/policy.py"],
        ),
        expected_requires_closure="COMPLETE",
    ))

    cases.append(BurninCase(
        name="model_led_experimental",
        category="exploration_policy",
        mission=_make_mission("model_led_experiment",
            objective="Investigate how the decision trace works during investigations. What does append_decision record?",
            evidence_collection_mode="model_led",
            exploration_policy={
                "after_required_floor": "allow_model_exploration",
                "min_optional_actions_after_floor": 0,
                "max_optional_actions_after_floor": 5,
                "require_contradiction_search": False,
            },
            allowed_paths=["codex_oss/decision_trace.py", "codex_oss/runtime/loop.py"],
            allowed_tool_classes=["read", "search"],
            answer_obligations=[{
                "id": "q1", "question": "How does the decision trace record investigation progress?",
                "required": True,
                "source_hints": ["codex_oss/decision_trace.py"],
                "source_requirements": [
                    {"path": "codex_oss/decision_trace.py", "evidence_kind": "trace_logic", "required": True, "prefetch": False},
                ],
            }],
            must_inspect=["codex_oss/decision_trace.py"],
        ),
        expected_requires_closure="COMPLETE",
        tolerance_for_false_complete=True,
    ))

    cases.append(BurninCase(
        name="two_source_contradiction",
        category="contradiction_blocked",
        mission=_make_mission("two_src_contradiction",
            objective="Check if two sources have contradictory definitions of the same function. Compare codex_oss/implementation.py and codex_oss/implementation_graph.py for build_implementation_readiness_graph.",
            exploration_policy={"after_required_floor": "close_immediately", "min_optional_actions_after_floor": 0, "max_optional_actions_after_floor": 0, "require_contradiction_search": False},
            allowed_paths=["codex_oss/implementation.py", "codex_oss/implementation_graph.py"],
            allowed_tool_classes=["read", "search"],
            answer_obligations=[{
                "id": "q1", "question": "Do both files define build_implementation_readiness_graph consistently?",
                "required": True,
                "source_hints": ["codex_oss/implementation.py", "codex_oss/implementation_graph.py"],
                "source_requirements": [
                    {"path": "codex_oss/implementation.py", "evidence_kind": "function_def", "required": True, "prefetch": False, "contradiction_markers": ["conflicting_definition"]},
                    {"path": "codex_oss/implementation_graph.py", "evidence_kind": "function_def", "required": True, "prefetch": False, "contradiction_markers": ["conflicting_definition"]},
                ],
            }],
            must_inspect=["codex_oss/implementation.py", "codex_oss/implementation_graph.py"],
        ),
        expected_requires_closure="COMPLETE",
    ))

    cases.append(BurninCase(
        name="secret_redaction_blocked_source",
        category="contradiction_blocked",
        mission=_make_mission("secret_redaction",
            objective="Investigate whether .codex-oss/env/opencode-go.env is accessible and what it contains. The file should be blocked or redacted.",
            allowed_paths=[".codex-oss/env/opencode-go.env"],
            allowed_tool_classes=["read"],
            answer_obligations=[{
                "id": "q1", "question": "Can the environment file be read, and is secret content redacted?",
                "required": True,
                "source_hints": [".codex-oss/env/opencode-go.env"],
                "source_requirements": [{"path": ".codex-oss/env/opencode-go.env", "evidence_kind": "secret_check", "required": True, "prefetch": False}],
            }],
            must_inspect=[".codex-oss/env/opencode-go.env"],
        ),
        expected_requires_closure="PARTIAL",
        tolerance_for_false_complete=True,
    ))

    cases.append(BurninCase(
        name="shape_mapping_and_config",
        category="evidence_kind",
        mission=_make_mission("shape_mapping_config",
            exploration_policy={"after_required_floor": "close_immediately", "min_optional_actions_after_floor": 0, "max_optional_actions_after_floor": 0, "require_contradiction_search": False},
            objective="Find the RUNTIME_AUTONOMY_PROFILES dict in codex_oss/managed_bridge.py and report the max_tool_budget for mission-a5-kimi.",
            allowed_paths=["codex_oss/managed_bridge.py"],
            allowed_tool_classes=["read"],
            answer_obligations=[{
                "id": "q1", "question": "What is the max_tool_budget for mission-a5-kimi in RUNTIME_AUTONOMY_PROFILES?",
                "required": True,
                "source_hints": ["codex_oss/managed_bridge.py"],
                "source_requirements": [{
                    "path": "codex_oss/managed_bridge.py",
                    "evidence_kind": "config_mapping",
                    "required": True, "prefetch": False,
                    "required_shapes": ["mapping_assignment", "config_value"],
                }],
            }],
            must_inspect=["codex_oss/managed_bridge.py"],
        ),
        expected_requires_closure="COMPLETE",
    ))

    cases.append(BurninCase(
        name="shape_zero_match_search",
        category="evidence_kind",
        mission=_make_mission("shape_zero_match",
            exploration_policy={"after_required_floor": "close_immediately", "min_optional_actions_after_floor": 0, "max_optional_actions_after_floor": 0, "require_contradiction_search": False},
            objective="Search for a non-existent function name 'definitely_not_in_codebase' in codex_oss/. Verify zero-match evidence.",
            allowed_paths=["codex_oss/answer_graph.py", "codex_oss/claim_graph.py", "codex_oss/runtime/loop.py"],
            allowed_tool_classes=["read", "search"],
            answer_obligations=[{
                "id": "q1", "question": "Does 'definitely_not_in_codebase' appear anywhere in codex_oss/?",
                "required": True,
                "source_hints": ["codex_oss/"],
                "source_requirements": [{
                    "path": "codex_oss/runtime/loop.py",
                    "evidence_kind": "zero_match",
                    "evidence_plane": "command_result",
                    "required": True, "prefetch": False,
                    "required_shapes": ["zero_match"],
                }],
            }],
            must_inspect=["codex_oss/runtime/loop.py"],
        ),
        expected_requires_closure="COMPLETE",
    ))

    cases.append(BurninCase(
        name="shape_flag_detection",
        category="evidence_kind",
        mission=_make_mission("shape_flag",
            exploration_policy={"after_required_floor": "close_immediately", "min_optional_actions_after_floor": 0, "max_optional_actions_after_floor": 0, "require_contradiction_search": False},
            objective="Find any flag_parameter or flag_read shapes in codex_oss/raw_lane.py. Check for probe flags or behavior derivation patterns.",
            allowed_paths=["codex_oss/raw_lane.py"],
            allowed_tool_classes=["read"],
            answer_obligations=[{
                "id": "q1", "question": "Does codex_oss/raw_lane.py contain flag parameters or behavior derivation patterns?",
                "required": True,
                "source_hints": ["codex_oss/raw_lane.py"],
                "source_requirements": [{
                    "path": "codex_oss/raw_lane.py",
                    "evidence_kind": "flag_behavior_check",
                    "required": True, "prefetch": False,
                    "required_shapes": ["flag_parameter", "flag_read", "behavior_derivation"],
                    "shape_match_policy": "any",
                }],
            }],
            must_inspect=["codex_oss/raw_lane.py"],
        ),
        expected_requires_closure="COMPLETE",
    ))

    cases.append(BurninCase(
        name="scheduler_read_pool_concurrency",
        category="scheduler",
        mission=_make_mission("scheduler_read_pool",
            exploration_policy={"after_required_floor": "close_immediately", "min_optional_actions_after_floor": 0, "max_optional_actions_after_floor": 0, "require_contradiction_search": False},
            objective="Investigate how the read-pool scheduler works. Find the scheduler/concurrency policy in the runtime.",
            allowed_paths=["codex_oss/runtime/policy.py", "codex_oss/runtime/loop.py"],
            allowed_tool_classes=["read", "search"],
            answer_obligations=[{
                "id": "q1", "question": "How does LANE_CAPACITY limit concurrent read-only missions?",
                "required": True,
                "source_hints": ["codex_oss/runtime/policy.py"],
                "source_requirements": [
                    {"path": "codex_oss/runtime/policy.py", "evidence_kind": "concurrency_config", "required": True, "prefetch": False, "required_shapes": ["config_value", "mapping_assignment"]},
                ],
            }],
            must_inspect=["codex_oss/runtime/policy.py"],
        ),
        expected_requires_closure="COMPLETE",
    ))

    cases.append(BurninCase(
        name="scheduler_mission_slot_tracking",
        category="scheduler",
        mission=_make_mission("scheduler_slot",
            exploration_policy={"after_required_floor": "close_immediately", "min_optional_actions_after_floor": 0, "max_optional_actions_after_floor": 0, "require_contradiction_search": False},
            objective="Find how acquire_mission_slot and release_mission_slot work in the runtime scheduler.",
            allowed_paths=["codex_oss/runtime/policy.py", "codex_oss/runtime/loop.py"],
            allowed_tool_classes=["read", "search"],
            answer_obligations=[{
                "id": "q1", "question": "How does the runtime prevent overlapping workspace missions?",
                "required": True,
                "source_hints": ["codex_oss/runtime/policy.py"],
                "source_requirements": [
                    {"path": "codex_oss/runtime/policy.py", "evidence_kind": "scheduler_logic", "required": True, "prefetch": False, "required_shapes": ["function_definition"]},
                ],
            }],
            must_inspect=["codex_oss/runtime/policy.py"],
        ),
        expected_requires_closure="COMPLETE",
    ))

    cases.append(BurninCase(
        name="scheduler_lane_capacity",
        category="scheduler",
        mission=_make_mission("scheduler_lane",
            exploration_policy={"after_required_floor": "close_immediately", "min_optional_actions_after_floor": 0, "max_optional_actions_after_floor": 0, "require_contradiction_search": False},
            objective="Find the lane capacity configuration for read_pool, patch_pool, isolated_apply, and workspace_apply.",
            allowed_paths=["codex_oss/runtime/policy.py"],
            allowed_tool_classes=["read"],
            answer_obligations=[{
                "id": "q1", "question": "What are the capacity limits for each scheduler lane?",
                "required": True,
                "source_hints": ["codex_oss/runtime/policy.py"],
                "source_requirements": [{"path": "codex_oss/runtime/policy.py", "evidence_kind": "lane_config", "required": True, "prefetch": False, "required_shapes": ["mapping_assignment", "config_value"]}],
            }],
            must_inspect=["codex_oss/runtime/policy.py"],
        ),
        expected_requires_closure="COMPLETE",
    ))

    cases.append(BurninCase(
        name="patch_pipeline_artifact_flow",
        category="patch_pipeline",
        mission=_make_mission("patch_artifact_flow",
            exploration_policy={"after_required_floor": "close_immediately", "min_optional_actions_after_floor": 0, "max_optional_actions_after_floor": 0, "require_contradiction_search": False},
            objective="Trace how implementation artifacts flow from validate_patch_proposal through apply_patch_in_isolated_worktree.",
            allowed_paths=["codex_oss/implementation.py"],
            allowed_tool_classes=["read", "search"],
            answer_obligations=[{
                "id": "q1", "question": "What artifacts are written and where during the implementation pipeline?",
                "required": True,
                "source_hints": ["codex_oss/implementation.py"],
                "source_requirements": [
                    {"path": "codex_oss/implementation.py", "evidence_kind": "artifact_persistence", "required": True, "prefetch": False, "required_shapes": ["function_definition"]},
                ],
            }],
            must_inspect=["codex_oss/implementation.py"],
        ),
        expected_requires_closure="COMPLETE",
    ))

    cases.append(BurninCase(
        name="patch_pipeline_rollback_investigation",
        category="patch_pipeline",
        mission=_make_mission("patch_rollback",
            exploration_policy={"after_required_floor": "close_immediately", "min_optional_actions_after_floor": 0, "max_optional_actions_after_floor": 0, "require_contradiction_search": False},
            objective="Find how rollback artifacts are generated in the implementation pipeline. Look for rollback_diff and git apply -R references.",
            allowed_paths=["codex_oss/implementation.py"],
            allowed_tool_classes=["read", "search"],
            answer_obligations=[{
                "id": "q1", "question": "How does the implementation pipeline generate and apply rollback?",
                "required": True,
                "source_hints": ["codex_oss/implementation.py"],
                "source_requirements": [{"path": "codex_oss/implementation.py", "evidence_kind": "rollback_logic", "required": True, "prefetch": False}],
            }],
            must_inspect=["codex_oss/implementation.py"],
        ),
        expected_requires_closure="COMPLETE",
    ))

    cases.append(BurninCase(
        name="patch_pipeline_verification_scope",
        category="patch_pipeline",
        mission=_make_mission("patch_verification",
            exploration_policy={"after_required_floor": "close_immediately", "min_optional_actions_after_floor": 0, "max_optional_actions_after_floor": 0, "require_contradiction_search": False},
            objective="Find how verification_policy is enforced during isolated implementation. Which function validates verification commands?",
            allowed_paths=["codex_oss/implementation.py"],
            allowed_tool_classes=["read", "search"],
            answer_obligations=[{
                "id": "q1", "question": "How is verification_policy enforced in the implementation pipeline?",
                "required": True,
                "source_hints": ["codex_oss/implementation.py"],
                "source_requirements": [{"path": "codex_oss/implementation.py", "evidence_kind": "verification_logic", "required": True, "prefetch": False, "required_shapes": ["function_definition"]}],
            }],
            must_inspect=["codex_oss/implementation.py"],
        ),
        expected_requires_closure="COMPLETE",
    ))

    cases.append(BurninCase(
        name="raw_lane_cert_investigation",
        category="raw_lane",
        mission=_make_mission("raw_lane_cert",
            exploration_policy={"after_required_floor": "close_immediately", "min_optional_actions_after_floor": 0, "max_optional_actions_after_floor": 0, "require_contradiction_search": False},
            objective="Investigate the raw_lane module. How does it differ from the runtime-backed managed_bridge path?",
            allowed_paths=["codex_oss/raw_lane.py", "codex_oss/managed_bridge.py"],
            allowed_tool_classes=["read", "search"],
            answer_obligations=[{
                "id": "q1", "question": "How does the raw lane differ from the managed bridge path?",
                "required": True,
                "source_hints": ["codex_oss/raw_lane.py", "codex_oss/managed_bridge.py"],
                "source_requirements": [
                    {"path": "codex_oss/raw_lane.py", "evidence_kind": "raw_lane_impl", "required": True, "prefetch": False},
                    {"path": "codex_oss/managed_bridge.py", "evidence_kind": "managed_bridge_impl", "required": True, "prefetch": False},
                ],
            }],
            must_inspect=["codex_oss/raw_lane.py", "codex_oss/managed_bridge.py"],
        ),
        expected_requires_closure="COMPLETE",
        tolerance_for_false_complete=True,
    ))

    cases.append(BurninCase(
        name="raw_lane_probe_flags",
        category="raw_lane",
        mission=_make_mission("raw_lane_probe",
            exploration_policy={"after_required_floor": "close_immediately", "min_optional_actions_after_floor": 0, "max_optional_actions_after_floor": 0, "require_contradiction_search": False},
            objective="Check if codex_oss/raw_lane.py uses probe flags or behavior flags that differentiate raw cert from runtime-backed cert.",
            allowed_paths=["codex_oss/raw_lane.py"],
            allowed_tool_classes=["read"],
            answer_obligations=[{
                "id": "q1", "question": "Does raw_lane.py use probe or behavior flags?",
                "required": True,
                "source_hints": ["codex_oss/raw_lane.py"],
                "source_requirements": [{
                    "path": "codex_oss/raw_lane.py",
                    "evidence_kind": "flag_check",
                    "required": True, "prefetch": False,
                    "required_shapes": ["flag_parameter", "flag_read"],
                }],
            }],
            must_inspect=["codex_oss/raw_lane.py"],
        ),
        expected_requires_closure="COMPLETE",
        tolerance_for_false_complete=True,
    ))

    return cases


def call_bridge(payload: dict) -> dict:
    """Submit a payload to the bridge and return the response JSON."""
    body = {
        "model": MODEL,
        "stream": False,
        "input": [
            {"role": "user", "content": "<OSS_HANDOFF_JSON>\n" + json.dumps(payload) + "\n</OSS_HANDOFF_JSON>"},
        ],
    }
    data = json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {AUTH}",
    }
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                f"{BASE_URL.rstrip('/')}/responses",
                data=data, headers=headers, method="POST",
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8")[:200]
            except Exception:
                pass
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            return {"error": f"HTTP {e.code}: {err_body}", "bridge_unreachable": True}
        except (urllib.error.URLError, http.client.HTTPException, OSError, TimeoutError) as e:
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            return {"error": str(e)[:200], "bridge_unreachable": True}
    return {"error": "all attempts failed", "bridge_unreachable": True}


def extract_text(payload: dict) -> str:
    parts = []
    for output in payload.get("output", []) or []:
        if isinstance(output, dict):
            for msg in output.get("content", []) or []:
                if isinstance(msg, dict) and msg.get("type") == "output_text":
                    parts.append(str(msg.get("text", "") or ""))
    return "\n".join(parts)


def extract_metrics(mission: dict, response: dict) -> BurninResult:
    """Extract burn-in metrics from a mission run response."""
    result = BurninResult(case=str(mission.get("mission_id", "")))
    result.category = mission.get("mission_id", "")

    if response.get("bridge_unreachable"):
        result.passed = False
        return result
    if response.get("error"):
        result.passed = False
        return result

    content = extract_text(response) if isinstance(response, dict) else str(response)
    if not content:
        result.passed = False
        return result

    if "OSS_REPORT_BEGIN" in content and "OSS_REPORT_END" in content:
        report_section = content.split("OSS_REPORT_BEGIN", 1)[1].split("OSS_REPORT_END", 1)[0]
        result.status = "COMPLETE" if '"COMPLETE"' in report_section or "COMPLETE" in report_section[:500] else "PARTIAL"
    elif "OSS_IMPLEMENTATION_REPORT_BEGIN" in content:
        report_section = content.split("OSS_IMPLEMENTATION_REPORT_BEGIN", 1)[1].split("OSS_IMPLEMENTATION_REPORT_END", 1)[0]
        result.status = "COMPLETE" if '"COMPLETE"' in report_section else "VERIFIED"
    else:
        result.status = "UNKNOWN"

    if "runtime_answer_graph" in content.lower():
        result.closure_source = "runtime_answer_graph"
        result.runtime_finalized = True
    elif "OSS_REPORT_JSON" in content and '"report_source": "runtime_answer_graph"' in content:
        result.closure_source = "runtime_answer_graph"
        result.runtime_finalized = True
    else:
        result.closure_source = "model"

    if '"optional_exploration_count"' in content:
        import re
        m = re.search(r'"optional_exploration_count":\s*(\d+)', content)
        if m:
            result.optional_exploration = int(m.group(1))

    result.contradiction_search = '"contradiction_search_done": true' in content.lower() or '"contradiction_search_done":true' in content.lower()

    try:
        report_json_start = content.index("OSS_REPORT_JSON:")
        report_json_end = content.index("OSS_REPORT_END", report_json_start)
        report_str = content[report_json_start:report_json_end]
        brace_start = report_str.index("{")
        brace_end = report_str.rindex("}") + 1
        report = json.loads(report_str[brace_start:brace_end])
        result.required_sources_covered = int(report.get("answer_graph_summary", {}).get("required_answered", 0))
        result.required_total = int(report.get("answer_graph_summary", {}).get("required_total", 0))
        if result.required_total > 0 and result.required_sources_covered < result.required_total:
            if result.status == "COMPLETE":
                result.false_complete = True
        missing = list(report.get("missing_required_sources", []) or [])
        result.missing_evidence = missing
        result.caveats = list(report.get("caveats", []) or [])
        result.evidence_shapes_covered = int(report.get("answer_graph_summary", {}).get("required_evidence_shapes_covered", 0))
    except (ValueError, KeyError, json.JSONDecodeError):
        pass

    if "raw dump" in content.lower() or "RAW_DUMP" in content:
        result.raw_dump_incidents = 1

    result.passed = not result.false_complete and result.raw_dump_incidents == 0
    if result.status == "COMPLETE":
        result.passed = result.passed or not result.false_complete

    return result


def run_burnin(cases: list[BurninCase], *, mission_ids: list[str], limit: int = 25, start: int = 1):
    """Run burn-in cases against the live bridge."""
    results: list[BurninResult] = []
    total = min(limit, len(cases))

    print(f"\nA3-Open Burn-In Pack: {total} cases ({len(cases)} defined)")
    print(f"Bridge: {BASE_URL}")
    print(f"Model: {MODEL}")
    print(f"Timeout: {TIMEOUT}s")
    print("-" * 60)

    for local_idx, case in enumerate(cases[start - 1 : start - 1 + total], start=0):
        idx = start + local_idx
        mission = dict(case.mission)
        mission["mission_id"] = mission_ids[local_idx]

        print(f"\n[{idx}/{total}] {case.name} ({case.category})")
        start_time = time.time()
        response = call_bridge(mission)
        elapsed = time.time() - start_time

        if response.get("bridge_unreachable"):
            print(f"  SKIP: Bridge unreachable ({str(response.get('error', ''))[:80]})")
            continue

        if response.get("error"):
            err = str(response.get("error", "") or "")[:120]
            print(f"  ERROR: {err}")
            results.append(BurninResult(case=case.name, category=case.category, mission_id=mission["mission_id"], passed=False, elapsed=elapsed))
            continue

        result = extract_metrics(mission, response)
        result.case = case.name
        result.case_id = f"case_{idx:03d}"
        result.category = case.category
        result.mission_id = mission["mission_id"]
        result.elapsed = elapsed
        result.artifact_dir = os.path.join(MISSIONS_ROOT, mission["mission_id"])

        print(f"  Status: {result.status} | Closure: {result.closure_source} | "
              f"Sources: {result.required_sources_covered}/{result.required_total} | "
              f"Optional: {result.optional_exploration} | "
              f"Contradiction: {result.contradiction_search} | "
              f"FalseComplete: {result.false_complete} | "
              f"Raws: {result.raw_dump_incidents} | "
              f"Elapsed: {elapsed:.1f}s")
        if result.missing_evidence:
            print(f"  Missing: {result.missing_evidence[:3]}")
        if result.caveats:
            for c in result.caveats[:2]:
                print(f"  Caveat: {c[:100]}")

        results.append(result)

    print("\n" + "=" * 60)
    passed = [r for r in results if r.passed]
    false_completes = [r for r in results if r.false_complete]
    raw_dumps = [r for r in results if r.raw_dump_incidents > 0]
    completed = [r for r in results if r.status == "COMPLETE"]
    partials = [r for r in results if r.status == "PARTIAL"]

    print(f"Results: {len(results)} total")
    print(f"  Passed: {len(passed)}/{len(results)} ({_pct(len(passed), len(results))})")
    print(f"  COMPLETE: {len(completed)} | PARTIAL: {len(partials)}")
    print(f"  False COMPLETE: {len(false_completes)} | Raw dumps: {len(raw_dumps)}")
    print(f"  Runtime closures: {len([r for r in results if r.runtime_finalized])}")

    if false_completes:
        print("\nFalse COMPLETE cases:")
        for fc in false_completes:
            print(f"  - {fc.case}: sources={fc.required_sources_covered}/{fc.required_total}")
    if raw_dumps:
        print("\nRaw dump incidents:")
        for rd in raw_dumps:
            print(f"  - {rd.case}")

    by_category: dict[str, list] = {}
    for r in results:
        by_category.setdefault(r.category, []).append(r)

    print("\nBy category:")
    for cat, cat_results in sorted(by_category.items()):
        cat_passed = len([r for r in cat_results if r.passed])
        print(f"  {cat}: {cat_passed}/{len(cat_results)}")

    runtime_closures = len([r for r in results if r.runtime_finalized])
    complete_useful = len([r for r in results if r.status == "COMPLETE" and not r.false_complete])
    partial_truthful = len([r for r in results if r.status in ("PARTIAL", "ESCALATE") and not r.false_complete])
    print(f"\nGPT effort-saving estimate:")
    print(f"  Runtime closures (no GPT synthesis needed): {runtime_closures}/{len(results)} ({_pct(runtime_closures, len(results))})")
    print(f"  Useful COMPLETE reports: {complete_useful}/{len(results)}")
    print(f"  Truthful PARTIAL/ESCALATE: {partial_truthful}/{len(results)}")
    n_false = len(false_completes)
    n_raw = len(raw_dumps)
    print(f"  GPT cleanup: {'minor' if n_false == 0 and n_raw == 0 else ('moderate' if n_false <= 1 else 'major')}")

    return results


def _read_json(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _artifact_bundle(mission_id: str) -> dict[str, dict[str, Any]]:
    mission_dir = os.path.join(MISSIONS_ROOT, mission_id)
    return {
        "mission": _read_json(os.path.join(mission_dir, "mission.json")),
        "report": _read_json(os.path.join(mission_dir, "report.json")),
        "ledger": _read_json(os.path.join(mission_dir, "ledger.json")),
        "answer_graph": _read_json(os.path.join(mission_dir, "answer_graph.json")),
        "decision_trace": _read_json(os.path.join(mission_dir, "decision_trace.json")),
    }


def _mission_quality(case: BurninCase, mission_id: str) -> dict[str, Any]:
    from codex_oss.burnin.quality import classify_quality_validity

    artifacts = _artifact_bundle(mission_id)
    quality = classify_quality_validity(
        mission=artifacts["mission"] or case.mission,
        report=artifacts["report"],
        ledger=artifacts["ledger"],
        answer_graph=artifacts["answer_graph"],
        decision_trace=artifacts["decision_trace"],
    )
    return quality


def _pct(part: int, total: int) -> str:
    if total == 0:
        return "0%"
    return f"{part / total * 100:.0f}%"


def _check_runtime_freshness():
    """Check that the bridge runtime is fresh before running live burn-in."""
    import urllib.request, json
    health_url = BASE_URL.rstrip("/v1").rstrip("/responses").rstrip("/") + "/health"
    try:
        req = urllib.request.Request(health_url, headers={"Authorization": f"Bearer {AUTH}"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            health = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"Bridge health unreachable: {e}")
        sys.exit(1)
    runtime_id = health.get("runtime_identity", {}) or {}
    source_tree = runtime_id.get("runtime_source_tree", {}) or {}
    if not source_tree.get("fresh", True):
        print("\nERROR: Bridge runtime is stale. Restart the bridge before running live burn-in.")
        changed = source_tree.get("changed_files", [])
        if changed:
            print(f"  Changed files: {', '.join(changed[:5])}")
        print("\n  Run: bin/codex-oss restart")
        print("  Or: LIVE_BURNIN_ALLOW_STALE_RUNTIME=1 python3 tests/test_a3_open_burnin_pack.py")
        sys.exit(1)
    print(f"Runtime source: FRESH ({source_tree.get('total_files', 0)} files)")


def main():
    from codex_oss.burnin.harness import BurninHarness

    if not os.getenv("LIVE_BURNIN"):
        print("Skipping live burn-in (set LIVE_BURNIN=1 to run).")
        print("Run: set -a; source .codex-oss/env/opencode-go.env; set +a; LIVE_BURNIN=1 python3 tests/test_a3_open_burnin_pack.py")
        return 0

    if not os.getenv("LIVE_BURNIN_ALLOW_STALE_RUNTIME"):
        _check_runtime_freshness()

    cases = build_burnin_cases()
    limit = int(os.getenv("OSS_LIVE_BURNIN_LIMIT", "25"))
    start = int(os.getenv("OSS_LIVE_BURNIN_START_INDEX", "1"))
    run_id = f"burnin_a3_open_{datetime.now(timezone.utc).strftime('%Y_%m_%d_%H%M%S')}"
    selected_cases = cases[start - 1 : start - 1 + min(limit, len(cases))]
    harness = BurninHarness(
        run_id=run_id, suite="a3_open", project_root=ROOT,
        required_aliases=["mission-a3-kimi", "mission-a3-deepseek", "mission-a2-flash"],
        bridge_url=BASE_URL.rstrip("/v1"),
        auth=AUTH,
    )

    # Register cases
    mission_ids: list[str] = []
    selected_index_by_case_id: dict[str, int] = {}
    for local_idx, case in enumerate(selected_cases, start=0):
        idx = start + local_idx
        base_mission_id = str(case.mission.get("mission_id", f"case_{idx}"))
        mission_id = _run_mission_id(base_mission_id, idx, run_id)
        case_id = f"case_{idx:03d}"
        mission_ids.append(mission_id)
        selected_index_by_case_id[case_id] = local_idx
        harness.register_case(
            case_id, mission_id,
            model_alias=MODEL, category=case.category,
            expected_outcome=case.expected_requires_closure,
            tolerance=case.tolerance_for_false_complete,
            expected_answer_statuses=case.expected_answer_statuses,
            expected_evidence_statuses=case.expected_evidence_statuses,
            expected_verification_statuses=case.expected_verification_statuses,
            expected_closure_statuses=case.expected_closure_statuses,
            expected_final_statuses=case.expected_final_statuses,
        )

    # Preflight
    preflight = harness.run_preflight()
    print(f"\nPreflight: {'PASS' if preflight.ok else 'FAIL'}")
    if preflight.failures:
        for f in preflight.failures:
            print(f"  - {f}")
        return 1

    results: list[BurninResult] = []
    final_status = "FAIL"
    try:
        results = run_burnin(selected_cases, mission_ids=mission_ids, limit=limit, start=1)
        harness.scan_missing_artifacts(MISSIONS_ROOT)

        for result in results:
            if not result.case_id or result.case_id not in selected_index_by_case_id:
                continue
            case = selected_cases[selected_index_by_case_id[result.case_id]]
            quality = _mission_quality(case, result.mission_id) if result.mission_id else {}
            harness.record_case_completion(
                result.case_id,
                elapsed=result.elapsed,
                mission_artifact_dir=result.artifact_dir,
                mission_status=result.status,
                closure_source=result.closure_source,
                quality_result=quality,
            )

        harness.scan_missing_artifacts(MISSIONS_ROOT)
    finally:
        harness.scan_missing_artifacts(MISSIONS_ROOT)
        harness.finalize()

    # Use authoritative finalizer
    from codex_oss.burnin.finalizer import finalize_burnin_run
    authoritative = finalize_burnin_run(ROOT, run_id)
    truth_safe = authoritative.get("truth_safe", {}) or {}
    scenario = authoritative.get("scenario_fidelity", {}) or {}
    native = authoritative.get("native_feeling", {}) or {}

    print(f"\nBURN-IN TRUTH_SAFE: {'PASS' if truth_safe.get('pass') else 'FAIL'}")
    print(f"BURN-IN SCENARIO_FIDELITY: {'PASS' if scenario.get('pass') else 'FAIL'}")
    print(f"BURN-IN NATIVE_FEELING: {'PASS' if native.get('pass') else 'FAIL'}")
    print(f"AUTHORITATIVE SUMMARY: {harness.output_dir}/summary.json")

    result_str = authoritative.get("result", "FAIL")
    if result_str == "PASS":
        print(f"\nBURN-IN PASSED: all required axes")
        return 0
    else:
        reasons = authoritative.get("result_reasons", [])
        print(f"\nBURN-IN FAILED: {', '.join(reasons[:5])}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
