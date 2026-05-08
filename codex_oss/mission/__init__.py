"""MissionV1 parser and tier classifier."""

from __future__ import annotations

import json
import math
import os
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

JSON = Dict[str, Any]


# ── Tier constants ──

TIER_A0 = "A0"  # No-tool exact output
TIER_A1 = "A1"  # Context-pack compatibility
TIER_A2 = "A2"  # Guided exploration
TIER_A3 = "A3"  # Managed investigation
TIER_A4 = "A4"  # Patch proposal (future)
TIER_A5 = "A5"  # Bounded implementation (future)
TIER_A6 = "A6"  # Critical autonomous engineer (future)

VALID_TIERS_V1 = {TIER_A0, TIER_A1, TIER_A2, TIER_A3, TIER_A4, TIER_A5, TIER_A6}
TOOL_AUTONOMY_TIERS = {TIER_A2, TIER_A3, TIER_A4, TIER_A5, TIER_A6}
IMPLEMENTATION_TIERS = {TIER_A4, TIER_A5, TIER_A6}

# ── Mode constants ──

MODE_NO_TOOL_EXACT = "no_tool_exact"
MODE_CONTEXT_PACK = "context_pack_report"
MODE_GUIDED_EXPLORATION = "guided_exploration"
MODE_MANAGED_INVESTIGATION = "managed_investigation"
MODE_PATCH_PROPOSAL = "patch_proposal"
MODE_BOUNDED_IMPLEMENTATION = "bounded_implementation"
MODE_CRITICAL_IMPLEMENTATION = "critical_implementation"
MODE_BOUNDED_WRITE_EXACT = "bounded_write_exact"
MODE_BOUNDED_WRITE_PATCH = "bounded_write_patch"
MODE_ESCALATE = "escalate"
MODE_INVALID_HANDOFF = "invalid_handoff"

TIER_MODE_MAP = {
    TIER_A0: MODE_NO_TOOL_EXACT,
    TIER_A1: MODE_CONTEXT_PACK,
    TIER_A2: MODE_GUIDED_EXPLORATION,
    TIER_A3: MODE_MANAGED_INVESTIGATION,
    TIER_A4: MODE_PATCH_PROPOSAL,
    TIER_A5: MODE_BOUNDED_IMPLEMENTATION,
    TIER_A6: MODE_CRITICAL_IMPLEMENTATION,
}

# ── Tool classes ──

TOOL_CLASS_READ = "read"
TOOL_CLASS_SEARCH = "search"
TOOL_CLASS_LIST = "list"
TOOL_CLASS_SAFE_GIT = "safe_git"

ALLOWED_TOOL_CLASSES_V1 = {TOOL_CLASS_READ, TOOL_CLASS_SEARCH, TOOL_CLASS_LIST, TOOL_CLASS_SAFE_GIT}

# ── Default deny roots ──

DEFAULT_DENY_ROOTS = [
    ".env", "*.env", "secrets/", ".git/", "node_modules/",
    ".codex-oss/env/", ".codex-oss/state/", ".codex-oss/logs/",
]

DEFAULT_DENY_PATTERNS = [
    "private key", "-----BEGIN", "sk-", "OPENCODE_GO_API_KEY",
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DATABASE_URL",
]


@dataclass
class MissionV1:
    mission_id: str
    tier: str
    mode: str
    objective: str
    risk_tier: str = "low"
    write_allowed: bool = False
    allowed_roots: List[str] = field(default_factory=list)
    allowed_paths: List[str] = field(default_factory=list)
    forbidden_roots: List[str] = field(default_factory=list)
    forbidden_topics: List[str] = field(default_factory=list)
    critical_path_read_allowed: bool = False
    critical_path_reason: Optional[str] = None
    allow_broad_read_scope: bool = False
    max_files_to_inspect: int = 20
    max_total_observation_bytes: int = 500000
    tool_budget: int = 20
    time_budget_seconds: int = 180
    allowed_tool_classes: List[str] = field(default_factory=list)
    stop_conditions: List[str] = field(default_factory=list)
    report_schema: str = "managed_investigation_report.v1"
    required_outputs: List[str] = field(default_factory=list)
    fallback_policy: str = "fallback_policy.v1"
    deadline_policy: str = "deadline_policy.v1"
    objective_spec: Optional[dict] = None
    allow_heuristic_objective: bool = True
    objective_style: str = "deterministic_lookup"
    sufficiency_policy: dict = field(default_factory=dict)
    answer_obligations: List[dict] = field(default_factory=list)
    must_inspect: List[str] = field(default_factory=list)

    # AdaptiveAutonomyBudgetV1
    max_model_calls: int = 25
    max_duplicate_actions: int = 2
    max_broad_searches: int = 6
    max_low_information_actions: int = 3
    phase_policy_enabled: bool = True
    progress_policy_enabled: bool = True

    # Budget clamping metadata
    tool_budget_requested: int = 0
    tool_budget_effective: int = 0
    budget_reduction_reason: str = ""

    # ImplementationMissionV1/A4-A5
    owned_paths: List[str] = field(default_factory=list)
    read_only_paths: List[str] = field(default_factory=list)
    critical_path_write_allowed: bool = False
    max_files_changed: int = 1
    max_patch_bytes: int = 12000
    verification_policy: dict = field(default_factory=dict)
    apply_mode: str = "none"
    workspace_apply_policy: dict = field(default_factory=dict)


def parse_mission_v1(handoff_text: str) -> MissionV1:
    """Parse a MissionV1 from OSS_HANDOFF_JSON block or structured JSON."""
    block = _extract_handoff_json_block(handoff_text)
    if not block:
        raise InvalidHandoffError("No OSS_HANDOFF_JSON block found")

    try:
        raw = json.loads(block)
    except json.JSONDecodeError as e:
        raise InvalidHandoffError(f"Malformed JSON: {e}")

    return _build_mission(raw)


def _extract_handoff_json_block(text: str) -> Optional[str]:
    """Extract exactly one OSS_HANDOFF_JSON block from text."""
    import re
    blocks = re.findall(r"<OSS_HANDOFF_JSON>(.*?)</OSS_HANDOFF_JSON>", text, re.DOTALL)
    if not blocks:
        return None
    if len(blocks) > 1:
        raise InvalidHandoffError("Multiple OSS_HANDOFF_JSON blocks found — exactly one required")
    return blocks[0].strip()


def _build_mission(raw: dict) -> MissionV1:
    schema_version = raw.get("schema_version", "")
    if schema_version != "oss_agent_mission.v1":
        raise InvalidHandoffError(f"Expected schema_version 'oss_agent_mission.v1', got '{schema_version}'")

    mission_id = raw.get("mission_id", "")
    if not mission_id:
        mission_id = f"mission_{uuid.uuid4().hex[:12]}"

    tier = str(raw.get("tier", "")).upper()
    if tier not in VALID_TIERS_V1:
        raise InvalidHandoffError(f"Invalid tier '{tier}'. v1 supports: {sorted(VALID_TIERS_V1)}")

    mode = str(raw.get("mode", "")).lower()
    expected_mode = TIER_MODE_MAP.get(tier, "")
    if mode and mode != expected_mode:
        raise InvalidHandoffError(f"Tier {tier} requires mode '{expected_mode}', got '{mode}'")
    mode = expected_mode

    write_allowed = bool(raw.get("write_allowed", False))
    if tier in (TIER_A0, TIER_A1, TIER_A2, TIER_A3, TIER_A4) and write_allowed:
        raise InvalidHandoffError(f"{tier} missions require write_allowed=false")
    if tier in (TIER_A5, TIER_A6) and not write_allowed:
        raise InvalidHandoffError(f"{tier} missions require explicit write_allowed=true")

    objective = str(raw.get("objective", ""))
    if not objective:
        raise InvalidHandoffError("objective is required")
    objective_spec = _validate_objective_spec(raw.get("objective_spec"))
    allow_heuristic_objective = bool(raw.get("allow_heuristic_objective", True))
    objective_style = _validate_objective_style(raw.get("objective_style"), tier, objective_spec)
    sufficiency_policy = _validate_sufficiency_policy(raw.get("sufficiency_policy"), objective_style)
    answer_obligations = _validate_answer_obligations(raw.get("answer_obligations"), objective_style)
    must_inspect = _validate_must_inspect(raw.get("must_inspect"))

    risk_tier = str(raw.get("risk_tier", "low")).lower()
    if risk_tier not in ("low", "medium", "critical"):
        risk_tier = "low"
    if tier == TIER_A3 and not objective_spec and not allow_heuristic_objective:
        raise InvalidHandoffError("A3 missions require objective_spec when allow_heuristic_objective=false")

    allowed_roots = _as_str_list(raw.get("allowed_roots", []))
    allowed_paths = _as_str_list(raw.get("allowed_paths", []))
    if tier in TOOL_AUTONOMY_TIERS and not (allowed_roots or allowed_paths):
        raise InvalidHandoffError("A2/A3 missions require allowed_roots or allowed_paths")
    forbidden_roots = _as_str_list(raw.get("forbidden_roots", []))
    forbidden_topics = _as_str_list(raw.get("forbidden_topics", []))
    owned_paths = _as_str_list(raw.get("owned_paths", []))
    read_only_paths = _as_str_list(raw.get("read_only_paths", []))
    if tier in IMPLEMENTATION_TIERS and not owned_paths:
        raise InvalidHandoffError("A4/A5 missions require non-empty owned_paths")

    critical_path_read_allowed = bool(raw.get("critical_path_read_allowed", False))
    critical_path_reason = raw.get("critical_path_reason")
    critical_path_write_allowed = bool(raw.get("critical_path_write_allowed", False))

    tool_budget = int(raw.get("tool_budget", 20))
    time_budget_seconds = int(raw.get("time_budget_seconds", 180))
    if tier in TOOL_AUTONOMY_TIERS and "allowed_tool_classes" not in raw:
        raise InvalidHandoffError("A2/A3 missions require allowed_tool_classes")
    allowed_tool_classes = _as_str_list(raw.get("allowed_tool_classes", []))
    if tier in TOOL_AUTONOMY_TIERS and not allowed_tool_classes:
        raise InvalidHandoffError("A2/A3 missions require non-empty allowed_tool_classes")

    # Validate tool classes
    for tc in allowed_tool_classes:
        if tc not in ALLOWED_TOOL_CLASSES_V1:
            raise InvalidHandoffError(f"Invalid tool class '{tc}'. v1 allows: {sorted(ALLOWED_TOOL_CLASSES_V1)}")

    # Budget clamping
    tier_max_budget = 10 if tier == TIER_A2 else 20
    tier_max_time = 90 if tier == TIER_A2 else 180
    request_deadline = float(os.getenv("REQUEST_DEADLINE_SECONDS", "90"))
    reserve = float(os.getenv("DETERMINISTIC_PARTIAL_RESERVE_SECONDS", "5"))
    estimated_turn = float(os.getenv("ESTIMATED_A3_TURN_SECONDS", "8"))
    deadline_budget = max(1, math.floor(max(0, request_deadline - reserve) / max(1, estimated_turn)))
    effective_budget = min(tool_budget, tier_max_budget, deadline_budget)
    effective_time = min(time_budget_seconds, tier_max_time)

    # Abusive values fail closed
    if tool_budget > 1000 or time_budget_seconds > 3600:
        raise InvalidHandoffError(f"Abusive budget values: tool_budget={tool_budget}, time={time_budget_seconds}")

    stop_conditions = _as_str_list(raw.get("stop_conditions", []))
    if not stop_conditions:
        stop_conditions = ["valid_report", "budget_exhausted", "deadline_reached"]

    if tier in TOOL_AUTONOMY_TIERS and "required_outputs" not in raw:
        raise InvalidHandoffError("A2/A3 missions require required_outputs")
    required_outputs = _as_str_list(raw.get("required_outputs", []))
    if tier in TOOL_AUTONOMY_TIERS and not required_outputs:
        raise InvalidHandoffError("A2/A3 missions require non-empty required_outputs")
    if not required_outputs:
        required_outputs = ["files_inspected", "commands_run", "findings", "uncertainties",
                           "confidence", "caveats", "escalation_recommendation"]

    budget_reduction_reason = ""
    if effective_budget < tool_budget:
        reasons = []
        if tier_max_budget < tool_budget:
            reasons.append("tier_cap")
        if deadline_budget < min(tool_budget, tier_max_budget):
            reasons.append("request_deadline")
        budget_reduction_reason = "+".join(reasons) or "budget_cap"
    if effective_time < time_budget_seconds:
        budget_reduction_reason = "tier_cap" if not budget_reduction_reason else budget_reduction_reason + "+tier_cap"

    max_model_calls = int(raw.get("max_model_calls", 25))
    max_duplicate_actions = int(raw.get("max_duplicate_actions", 2))
    max_broad_searches = int(raw.get("max_broad_searches", 3 if tier == TIER_A2 else 6))
    max_low_information_actions = int(raw.get("max_low_information_actions", 2 if tier == TIER_A2 else 3))
    for name, value in {
        "max_model_calls": max_model_calls,
        "max_duplicate_actions": max_duplicate_actions,
        "max_broad_searches": max_broad_searches,
        "max_low_information_actions": max_low_information_actions,
    }.items():
        if value < 0 or value > 100:
            raise InvalidHandoffError(f"{name} must be between 0 and 100")

    max_files_changed = int(raw.get("max_files_changed", 1))
    max_patch_bytes = int(raw.get("max_patch_bytes", 12000))
    if max_files_changed < 1 or max_files_changed > 20:
        raise InvalidHandoffError("max_files_changed must be between 1 and 20")
    if max_patch_bytes < 1 or max_patch_bytes > 250000:
        raise InvalidHandoffError("max_patch_bytes must be between 1 and 250000")
    verification_policy = _validate_verification_policy(raw.get("verification_policy", {}))
    workspace_apply_policy = _validate_workspace_apply_policy(raw.get("workspace_apply_policy", {}))
    apply_mode = str(raw.get("apply_mode", "isolated_worktree" if tier in (TIER_A5, TIER_A6) else "none"))
    allowed_apply_modes = {
        TIER_A4: {"none"},
        TIER_A5: {"isolated_worktree", "temp_project", "workspace", "workspace_low_risk", "workspace_explicit"},
        TIER_A6: {"isolated_worktree", "temp_project", "critical_workspace_certified"},
    }
    if tier in allowed_apply_modes and apply_mode not in allowed_apply_modes[tier]:
        raise InvalidHandoffError(
            f"{tier} v1 supports apply_mode in {sorted(allowed_apply_modes[tier])}, got '{apply_mode}'"
        )

    return MissionV1(
        mission_id=mission_id,
        tier=tier,
        mode=mode,
        objective=objective,
        risk_tier=risk_tier,
        write_allowed=write_allowed,
        allowed_roots=allowed_roots,
        allowed_paths=allowed_paths,
        forbidden_roots=forbidden_roots + DEFAULT_DENY_ROOTS,
        forbidden_topics=forbidden_topics,
        critical_path_read_allowed=critical_path_read_allowed,
        critical_path_reason=critical_path_reason,
        allow_broad_read_scope=bool(raw.get("allow_broad_read_scope", False)),
        max_files_to_inspect=int(raw.get("max_files_to_inspect", 20)),
        max_total_observation_bytes=int(raw.get("max_total_observation_bytes", 500000)),
        tool_budget=effective_budget,
        time_budget_seconds=effective_time,
        allowed_tool_classes=allowed_tool_classes if allowed_tool_classes else list(ALLOWED_TOOL_CLASSES_V1),
        stop_conditions=stop_conditions,
        report_schema=str(raw.get("report_schema", "managed_investigation_report.v1")),
        required_outputs=required_outputs,
        objective_spec=objective_spec,
        allow_heuristic_objective=allow_heuristic_objective,
        objective_style=objective_style,
        sufficiency_policy=sufficiency_policy,
        answer_obligations=answer_obligations,
        must_inspect=must_inspect,
        max_model_calls=max_model_calls,
        max_duplicate_actions=max_duplicate_actions,
        max_broad_searches=max_broad_searches,
        max_low_information_actions=max_low_information_actions,
        phase_policy_enabled=bool(raw.get("phase_policy_enabled", True)),
        progress_policy_enabled=bool(raw.get("progress_policy_enabled", True)),
        tool_budget_requested=tool_budget,
        tool_budget_effective=effective_budget,
        budget_reduction_reason=budget_reduction_reason,
        owned_paths=owned_paths,
        read_only_paths=read_only_paths,
        critical_path_write_allowed=critical_path_write_allowed,
        max_files_changed=max_files_changed,
        max_patch_bytes=max_patch_bytes,
        verification_policy=verification_policy,
        apply_mode=apply_mode,
        workspace_apply_policy=workspace_apply_policy,
    )


def _as_str_list(val: Any) -> List[str]:
    if isinstance(val, list):
        return [str(v) for v in val]
    if isinstance(val, str):
        return [v.strip() for v in val.split(",") if v.strip()]
    return []


def _validate_objective_spec(raw: Any) -> Optional[dict]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise InvalidHandoffError("objective_spec must be an object")
    schema_version = str(raw.get("schema_version", ""))
    if schema_version != "objective_spec.v1":
        raise InvalidHandoffError("objective_spec.schema_version must be objective_spec.v1")
    objective_type = str(raw.get("objective_type", ""))
    allowed_types = {
        "mapping_lookup",
        "config_value_extraction",
        "function_location",
        "zero_match_evidence",
        "implementation_site_discovery",
        "usage_location",
        "control_flow_trace",
        "policy_rule_location",
        "test_coverage_location",
        "implementation_test_only",
        "documentation_patch",
        "implementation_patch",
        "critical_path_patch",
    }
    if objective_type not in allowed_types:
        raise InvalidHandoffError(f"objective_spec.objective_type must be one of {sorted(allowed_types)}")
    target = raw.get("target", {})
    if target is not None and not isinstance(target, dict):
        raise InvalidHandoffError("objective_spec.target must be an object")
    for field_name in ("required_outputs", "required_evidence_shapes", "completion_criteria"):
        if field_name in raw and not isinstance(raw.get(field_name), list):
            raise InvalidHandoffError(f"objective_spec.{field_name} must be a list")
    if "confidence_policy" in raw and not isinstance(raw.get("confidence_policy"), dict):
        raise InvalidHandoffError("objective_spec.confidence_policy must be an object")
    return dict(raw)


def _validate_objective_style(raw: Any, tier: str, objective_spec: Optional[dict]) -> str:
    allowed = {"deterministic_lookup", "open_investigation", "implementation"}
    if raw is None or str(raw).strip() == "":
        if tier in IMPLEMENTATION_TIERS:
            return "implementation"
        return "deterministic_lookup"
    style = str(raw).strip().lower()
    if style not in allowed:
        raise InvalidHandoffError(f"objective_style must be one of {sorted(allowed)}, got '{style}'")
    return style


def _validate_sufficiency_policy(raw: Any, objective_style: str) -> dict:
    default = {
        "min_main_claims": 1 if objective_style == "open_investigation" else 0,
        "min_evidence_refs_per_claim": 1,
        "must_list_uninspected_areas": bool(objective_style == "open_investigation"),
        "confidence_cap_if_partial_extracts": "MEDIUM" if objective_style == "open_investigation" else "LOW",
        "allow_runtime_answer_graph_closure": bool(objective_style == "open_investigation"),
    }
    if raw is None:
        return default
    if not isinstance(raw, dict):
        raise InvalidHandoffError("sufficiency_policy must be an object")
    policy = dict(default)
    policy.update(raw)
    try:
        policy["min_main_claims"] = int(policy.get("min_main_claims", default["min_main_claims"]))
        policy["min_evidence_refs_per_claim"] = int(policy.get("min_evidence_refs_per_claim", default["min_evidence_refs_per_claim"]))
    except (TypeError, ValueError):
        raise InvalidHandoffError("sufficiency_policy numeric fields must be integers")
    if policy["min_main_claims"] < 0 or policy["min_evidence_refs_per_claim"] < 0:
        raise InvalidHandoffError("sufficiency_policy numeric fields must be >= 0")
    policy["must_list_uninspected_areas"] = bool(policy.get("must_list_uninspected_areas", False))
    policy["allow_runtime_answer_graph_closure"] = bool(policy.get("allow_runtime_answer_graph_closure", default["allow_runtime_answer_graph_closure"]))
    cap = str(policy.get("confidence_cap_if_partial_extracts", default["confidence_cap_if_partial_extracts"])).upper()
    if cap not in {"LOW", "MEDIUM", "HIGH"}:
        raise InvalidHandoffError("sufficiency_policy.confidence_cap_if_partial_extracts must be LOW, MEDIUM, or HIGH")
    policy["confidence_cap_if_partial_extracts"] = cap
    return policy


def _validate_answer_obligations(raw: Any, objective_style: str) -> List[dict]:
    if raw in (None, ""):
        return []
    if objective_style != "open_investigation":
        raise InvalidHandoffError("answer_obligations are only valid for objective_style=open_investigation")
    if not isinstance(raw, list):
        raise InvalidHandoffError("answer_obligations must be a list")
    obligations: list[dict] = []
    for idx, item in enumerate(raw):
        if isinstance(item, str):
            text = item.strip()
            if not text:
                raise InvalidHandoffError(f"answer_obligations[{idx}] must not be empty")
            obligations.append({"id": f"q{idx + 1}", "question": text, "required": True})
            continue
        if not isinstance(item, dict):
            raise InvalidHandoffError(f"answer_obligations[{idx}] must be a string or object")
        question = str(item.get("question", "") or "").strip()
        if not question:
            raise InvalidHandoffError(f"answer_obligations[{idx}].question is required")
        source_hints = item.get("source_hints", [])
        if source_hints not in (None, "") and (
            not isinstance(source_hints, list) or not all(isinstance(part, str) and part.strip() for part in source_hints)
        ):
            raise InvalidHandoffError(f"answer_obligations[{idx}].source_hints must be a list of strings")
        source_requirements = _validate_source_requirements(
            item.get("source_requirements"),
            field_name=f"answer_obligations[{idx}].source_requirements",
        )
        obligations.append({
            "id": str(item.get("id", f"q{idx + 1}")),
            "question": question,
            "required": bool(item.get("required", True)),
            "source_hints": [str(part) for part in (source_hints or [])],
            "source_requirements": source_requirements,
            "evidence_needed": [str(part) for part in (item.get("evidence_needed", []) or []) if str(part)],
        })
    return obligations


def _validate_must_inspect(raw: Any) -> List[str]:
    paths = _as_str_list(raw)
    return [path for path in paths if path]


def _validate_source_requirements(raw: Any, *, field_name: str) -> List[dict]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise InvalidHandoffError(f"{field_name} must be a list")
    requirements: list[dict] = []
    for idx, item in enumerate(raw):
        if isinstance(item, str):
            path = item.strip()
            if not path:
                raise InvalidHandoffError(f"{field_name}[{idx}] must not be empty")
            requirements.append({
                "path": path,
                "evidence_kind": "source_read",
                "required": True,
                "prefetch": True,
            })
            continue
        if not isinstance(item, dict):
            raise InvalidHandoffError(f"{field_name}[{idx}] must be a string or object")
        path = str(item.get("path", "") or "").strip()
        if not path:
            raise InvalidHandoffError(f"{field_name}[{idx}].path is required")
        evidence_kind = str(item.get("evidence_kind", "source_read") or "source_read").strip()
        if not evidence_kind:
            raise InvalidHandoffError(f"{field_name}[{idx}].evidence_kind must not be empty")
        requirements.append({
            "path": path,
            "evidence_kind": evidence_kind,
            "required": bool(item.get("required", True)),
            "prefetch": bool(item.get("prefetch", True)),
        })
    return requirements


def _validate_verification_policy(raw: Any) -> dict:
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        raise InvalidHandoffError("verification_policy must be an object")
    allowed_commands = raw.get("allowed_commands", [])
    if allowed_commands and not isinstance(allowed_commands, list):
        raise InvalidHandoffError("verification_policy.allowed_commands must be a list")
    normalized_commands = []
    for command in allowed_commands:
        if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
            raise InvalidHandoffError("verification_policy.allowed_commands entries must be argv string lists")
        normalized_commands.append(list(command))
    max_commands = int(raw.get("max_commands", len(normalized_commands) or 0))
    timeout_seconds = int(raw.get("timeout_seconds", 60))
    if max_commands < 0 or max_commands > 10:
        raise InvalidHandoffError("verification_policy.max_commands must be between 0 and 10")
    if timeout_seconds < 1 or timeout_seconds > 600:
        raise InvalidHandoffError("verification_policy.timeout_seconds must be between 1 and 600")
    return {
        "allowed_commands": normalized_commands,
        "max_commands": max_commands,
        "timeout_seconds": timeout_seconds,
        "network_allowed": bool(raw.get("network_allowed", False)),
        "env_policy": str(raw.get("env_policy", "minimal")),
        "allow_broad_suite": bool(raw.get("allow_broad_suite", False)),
    }


def _validate_workspace_apply_policy(raw: Any) -> dict:
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        raise InvalidHandoffError("workspace_apply_policy must be an object")
    reviewer_models = raw.get("reviewer_models", [])
    if reviewer_models not in (None, "") and (
        not isinstance(reviewer_models, list) or not all(isinstance(item, str) for item in reviewer_models)
    ):
        raise InvalidHandoffError("workspace_apply_policy.reviewer_models must be a list of strings")
    invariant_commands = raw.get("invariant_commands", [])
    if invariant_commands not in (None, "") and not isinstance(invariant_commands, list):
        raise InvalidHandoffError("workspace_apply_policy.invariant_commands must be a list of argv lists")
    normalized_invariant_commands: list[list[str]] = []
    for command in invariant_commands or []:
        if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
            raise InvalidHandoffError("workspace_apply_policy.invariant_commands entries must be non-empty argv string lists")
        normalized_invariant_commands.append([str(part) for part in command])
    min_reviewer_approvals = int(raw.get("min_reviewer_approvals", 0) or 0)
    if min_reviewer_approvals < 0:
        raise InvalidHandoffError("workspace_apply_policy.min_reviewer_approvals must be >= 0")
    return {
        "allow_direct_workspace_apply": bool(raw.get("allow_direct_workspace_apply", False)),
        "allow_critical_workspace_apply": bool(raw.get("allow_critical_workspace_apply", False)),
        "require_clean_worktree": bool(raw.get("require_clean_worktree", False)),
        "allow_dirty_target_files": bool(raw.get("allow_dirty_target_files", False)),
        "certification_required": bool(raw.get("certification_required", False)),
        "require_gpt_review": bool(raw.get("require_gpt_review", True)),
        "reviewer_models": [str(item) for item in (reviewer_models or [])],
        "min_reviewer_approvals": min_reviewer_approvals,
        "require_isolated_preflight": bool(raw.get("require_isolated_preflight", False)),
        "require_rollback_proof": bool(raw.get("require_rollback_proof", False)),
        "invariant_commands": normalized_invariant_commands,
    }


class InvalidHandoffError(Exception):
    pass


def classify_handoff(text: str) -> tuple:
    """Classify a handoff text, returning (mission, errors) or fallback mode."""
    try:
        mission = parse_mission_v1(text)
        return mission, []
    except InvalidHandoffError as e:
        return None, [str(e)]
