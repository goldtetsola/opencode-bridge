"""Deterministic promotion-proof generation for runtime-backed OSS lanes."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from typing import Any, Callable

from .implementation import run_implementation_mission
from .managed_bridge import run_managed_mission_from_body
from .mission import _build_mission


def refresh_claim_proofs(project_root: str, suite: str = "proof") -> dict[str, Any]:
    generated: list[str] = []
    if suite in {"proof", "all"}:
        generated.extend(_refresh_deterministic_proofs(project_root))
    if suite in {"operational", "all"}:
        generated.extend(_refresh_operational_burnin(project_root))
    return {
        "proof_refresh_version": "1.0",
        "project_root": project_root,
        "suite": suite,
        "generated_missions": generated,
    }


def _refresh_deterministic_proofs(project_root: str) -> list[str]:
    return [
        _generate_a3_proof(project_root),
        _generate_a3_open_proof(project_root),
        _generate_a4_proof(project_root),
        _generate_a5_workspace_proof(project_root),
        _generate_a6_certified_proof(project_root),
    ]


def _refresh_operational_burnin(project_root: str) -> list[str]:
    return [
        _generate_operational_a3_readme(project_root),
        _generate_operational_a3_impl_site(project_root),
        _generate_operational_a3_open_runtime(project_root),
        _generate_operational_a4_docs_patch(project_root),
        _generate_operational_a5_workspace_docs(project_root),
        _generate_operational_a5_multifile_runtime(project_root),
        _generate_operational_a6_certified_runtime(project_root),
    ]


def _generate_a3_proof(project_root: str) -> str:
    mission_id = "proof_a3_readonly"
    with tempfile.TemporaryDirectory(prefix="oss_proof_a3_") as root:
        note_dir = os.path.join(root, "notes")
        os.makedirs(note_dir, exist_ok=True)
        with open(os.path.join(note_dir, "example.txt"), "w", encoding="utf-8") as handle:
            handle.write("hello proof\n")

        call_count = {"n": 0}

        def call_payload(payload, timeout):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {
                    "choices": [{
                        "message": {
                            "content": (
                                '{"action_type":"tool_call","tool_name":"rtk_read",'
                                '"arguments":{"path":"notes/example.txt"},'
                                '"reason":"Read proof file","hypothesis":"The file contains the answer.",'
                                '"expected_information_gain":"Gather file evidence.",'
                                '"why_not_report_yet":"Need evidence first."}'
                            )
                        }
                    }]
                }
            return {
                "choices": [{
                    "message": {
                        "content": (
                            '{"action_type":"final_report","report":'
                            '{"oss_report_version":"1.0","mission_id":"proof_a3_readonly",'
                            '"status":"COMPLETE","confidence":"LOW",'
                            '"files_inspected":[{"path":"notes/example.txt","complete":true}],'
                            '"commands_run":[{"tool":"rtk_read","args":{"path":"notes/example.txt"}}],'
                            '"findings":[{"claim":"The proof file was inspected.","evidence_refs":["command:0"],"confidence":"LOW"}],'
                            '"uncertainties":[],"caveats":["proof refresh"],'
                            '"escalation_recommendation":"GPT-5.5 review required",'
                            '"missing_fields":[]}}'
                        )
                    }
                }]
            }

        body = {
            "input": [{
                "role": "user",
                "content": (
                    "<OSS_HANDOFF_JSON>\n"
                    '{"schema_version":"oss_agent_mission.v1","mission_id":"proof_a3_readonly",'
                    '"tier":"A3","mode":"managed_investigation","objective":"Inspect one file and report.",'
                    '"risk_tier":"low","write_allowed":false,"allowed_roots":[],"allowed_paths":["notes/example.txt"],'
                    '"tool_budget":2,"time_budget_seconds":30,"allowed_tool_classes":["read"],'
                    '"stop_conditions":["valid_report"],"report_schema":"managed_investigation_report.v1",'
                    '"required_outputs":["files_inspected","commands_run","findings","uncertainties","confidence","caveats","escalation_recommendation"]}'
                    "\n</OSS_HANDOFF_JSON>"
                ),
            }]
        }
        with _cwd(root):
            result = run_managed_mission_from_body(
                body,
                "mission-a3-kimi",
                lambda *args, **kwargs: None,
                call_payload,
                lambda model: model,
                request_deadline=30,
            )
            if not result.handled or result.status != "COMPLETE":
                raise RuntimeError(f"A3 proof generation failed: {result}")
        _copy_proof_artifacts(root, project_root, mission_id, proof_kind="A3")
    return mission_id


def _generate_a3_open_proof(project_root: str) -> str:
    mission_id = "proof_a3_open"
    with tempfile.TemporaryDirectory(prefix="oss_proof_a3_open_") as root:
        src_dir = os.path.join(root, "src")
        tests_dir = os.path.join(root, "tests")
        os.makedirs(src_dir, exist_ok=True)
        os.makedirs(tests_dir, exist_ok=True)
        with open(os.path.join(src_dir, "runtime.py"), "w", encoding="utf-8") as handle:
            handle.write(
                "def persist_readonly_artifacts():\n"
                "    return ['mission.json', 'report.json', 'trace.jsonl']\n"
            )
        with open(os.path.join(tests_dir, "test_runtime.py"), "w", encoding="utf-8") as handle:
            handle.write(
                "def test_runtime_artifact_contract():\n"
                "    assert 'trace.jsonl' in ['mission.json', 'report.json', 'trace.jsonl']\n"
            )

        call_count = {"n": 0}

        def call_payload(payload, timeout):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {
                    "choices": [{
                        "message": {
                            "content": (
                                '{"action_type":"tool_call","phase":"EXPLORE","tool_name":"rtk_grep",'
                                '"arguments":{"pattern":"trace.jsonl","path":"src/runtime.py"},'
                                '"reason":"Start with a search to discover where the artifact contract appears.",'
                                '"hypothesis":"The readonly artifact contract should surface in both implementation and tests.",'
                                '"target_question":"Which files mention trace.jsonl as part of the readonly contract?",'
                                '"expected_information_gain":"Find candidate files before narrowing to concrete reads.",'
                                '"why_not_report_yet":"Need to discover the relevant files before inspecting them directly."}'
                            )
                        }
                    }]
                }
            if call_count["n"] == 2:
                return {
                    "choices": [{
                        "message": {
                            "content": (
                                '{"action_type":"tool_call","phase":"NARROW","tool_name":"rtk_read",'
                                '"arguments":{"path":"src/runtime.py"},'
                                '"reason":"Inspect the implementation after the search identified the artifact string.",'
                                '"hypothesis":"The runtime file shows which readonly artifacts are persisted.",'
                                '"target_question":"Which artifacts does the runtime persist?",'
                                '"expected_information_gain":"Find implementation evidence for persisted artifact names.",'
                                '"why_not_report_yet":"Need implementation evidence before drawing a conclusion."}'
                            )
                        }
                    }]
                }
            if call_count["n"] == 3:
                return {
                    "choices": [{
                        "message": {
                            "content": (
                                '{"action_type":"tool_call","phase":"VERIFY","tool_name":"rtk_read",'
                                '"arguments":{"path":"tests/test_runtime.py"},'
                                '"reason":"Check whether tests reinforce the artifact contract.",'
                                '"hypothesis":"The tests should confirm trace.jsonl is expected.",'
                                '"target_question":"Is there a test that reinforces the artifact contract?",'
                                '"expected_information_gain":"Corroborate the implementation claim with test evidence.",'
                                '"why_not_report_yet":"Need a second source before closing."}'
                            )
                        }
                    }]
                }
            return {
                "choices": [{
                    "message": {
                        "content": (
                            '{"action_type":"final_report","report":'
                            '{"oss_report_version":"1.0","mission_id":"proof_a3_open",'
                            '"status":"COMPLETE","confidence":"LOW",'
                            '"files_inspected":[{"path":"src/runtime.py","complete":true},{"path":"tests/test_runtime.py","complete":true}],'
                            '"commands_run":[{"tool":"rtk_grep","args":{"pattern":"trace.jsonl","path":"src/runtime.py"}},{"tool":"rtk_read","args":{"path":"src/runtime.py"}},{"tool":"rtk_read","args":{"path":"tests/test_runtime.py"}}],'
                            '"findings":[{"claim":"The runtime persists read-only artifacts and the test fixture reinforces that trace.jsonl is part of the contract.","evidence_refs":["command:0","command:1","command:2"],"confidence":"LOW"}],'
                            '"uncertainties":["Only two fixture files were inspected."],'
                            '"caveats":["open investigation proof"],'
                            '"escalation_recommendation":"GPT-5.5 review recommended",'
                            '"missing_fields":[]}}'
                        )
                    }
                }]
            }

        body = {
            "input": [{
                "role": "user",
                "content": (
                    "<OSS_HANDOFF_JSON>\n"
                    '{"schema_version":"oss_agent_mission.v1","mission_id":"proof_a3_open",'
                    '"tier":"A3","mode":"managed_investigation","objective":"Investigate how readonly artifacts are persisted and reinforced.",'
                    '"objective_style":"open_investigation",'
                    '"sufficiency_policy":{"min_main_claims":1,"min_evidence_refs_per_claim":1,"must_list_uninspected_areas":true,"confidence_cap_if_partial_extracts":"MEDIUM"},'
                    '"answer_obligations":[{"id":"q1","question":"Which implementation file persists readonly artifacts?","required":true,"source_hints":["src/runtime.py"],"source_requirements":[{"path":"src/runtime.py","evidence_kind":"implementation_logic","required":true,"prefetch":true}]},{"id":"q2","question":"Which test file reinforces the readonly artifact contract?","required":true,"source_hints":["tests/test_runtime.py"],"source_requirements":[{"path":"tests/test_runtime.py","evidence_kind":"test_enforcement","required":true,"prefetch":true}]},{"id":"q3","question":"What conclusion is justified from the inspected evidence?","required":true,"source_hints":["src/runtime.py","tests/test_runtime.py"]},{"id":"q4","question":"What remains unproven?","required":true,"source_hints":["src/runtime.py","tests/test_runtime.py"]}],'
                    '"must_inspect":["src/runtime.py","tests/test_runtime.py"],'
                    '"risk_tier":"low","write_allowed":false,"allowed_roots":[],"allowed_paths":["src/runtime.py","tests/test_runtime.py"],'
                    '"tool_budget":5,"time_budget_seconds":45,"allowed_tool_classes":["read","search"],'
                    '"stop_conditions":["valid_report"],"report_schema":"managed_investigation_report.v1",'
                    '"required_outputs":["files_inspected","commands_run","findings","uncertainties","confidence","caveats","escalation_recommendation"]}'
                    "\n</OSS_HANDOFF_JSON>"
                ),
            }]
        }
        with _cwd(root):
            result = run_managed_mission_from_body(
                body,
                "mission-a3-kimi",
                lambda *args, **kwargs: None,
                call_payload,
                lambda model: model,
                request_deadline=45,
            )
            if not result.handled or result.status != "COMPLETE":
                raise RuntimeError(f"A3 open proof generation failed: {result}")
        _copy_proof_artifacts(root, project_root, mission_id, proof_kind="A3_OPEN")
    return mission_id


def _generate_a4_proof(project_root: str) -> str:
    mission_id = "proof_a4_proposal"
    with tempfile.TemporaryDirectory(prefix="oss_proof_a4_") as root:
        tests_dir = os.path.join(root, "tests")
        os.makedirs(tests_dir, exist_ok=True)
        original = "def test_existing():\n    assert True\n"
        target = os.path.join(tests_dir, "test_config.py")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(original)
        mission = _build_mission({
            "schema_version": "oss_agent_mission.v1",
            "mission_id": mission_id,
            "tier": "A4",
            "mode": "patch_proposal",
            "objective": "Propose a low-risk test patch.",
            "risk_tier": "low",
            "write_allowed": False,
            "allowed_roots": [],
            "allowed_paths": ["tests/test_config.py"],
            "owned_paths": ["tests/test_config.py"],
            "read_only_paths": ["tests/test_config.py"],
            "forbidden_roots": [".env", ".git", ".codex-oss/"],
            "allowed_tool_classes": ["read", "search"],
            "tool_budget": 4,
            "time_budget_seconds": 60,
            "stop_conditions": ["valid_patch", "deadline_reached"],
            "report_schema": "patch_validation_report.v1",
            "required_outputs": ["patch", "changed_files", "risk_assessment", "verification_plan", "evidence_refs"],
            "max_files_changed": 1,
            "max_patch_bytes": 12000,
            "apply_mode": "none",
            "verification_policy": {
                "allowed_commands": [["python3", "-m", "py_compile", "tests/test_config.py"]],
                "max_commands": 1,
                "timeout_seconds": 20,
            },
            "objective_spec": {
                "schema_version": "objective_spec.v1",
                "objective_type": "implementation_test_only",
                "target": {
                    "test_file": "tests/test_config.py",
                    "source_files": [],
                    "required_test_names": ["test_proof_patch"],
                },
                "required_outputs": ["changed_test_file"],
                "required_evidence_shapes": ["test_definition"],
                "completion_criteria": ["required_test_present"],
            },
        })
        diff = (
            "diff --git a/tests/test_config.py b/tests/test_config.py\n"
            "--- a/tests/test_config.py\n"
            "+++ b/tests/test_config.py\n"
            "@@ -1,2 +1,5 @@\n"
            " def test_existing():\n"
            "     assert True\n"
            "+\n"
            "+def test_proof_patch():\n"
            "+    assert True\n"
        )
        handoff = (
            "<OSS_PATCH_PROPOSAL_JSON>\n"
            + json.dumps(_proposal_dict(diff, original, "tests/test_config.py", "A4 proof patch"), indent=2)
            + "\n</OSS_PATCH_PROPOSAL_JSON>\n"
        )
        with _cwd(root):
            result = run_implementation_mission(
                mission=mission,
                raw_model_alias="mission-a4-kimi",
                handoff=handoff,
                call_model=_unused_call_model,
                timeout=30,
                project_root=root,
            )
            if result.get("status") != "VALID":
                raise RuntimeError(f"A4 proof generation failed: {result}")
        _copy_proof_artifacts(root, project_root, mission_id, proof_kind="A4")
    return mission_id


def _generate_a5_workspace_proof(project_root: str) -> str:
    mission_id = "proof_a5_workspace"
    with tempfile.TemporaryDirectory(prefix="oss_proof_a5_") as root:
        target_dir = os.path.join(root, "tmp", "proof_workspace")
        os.makedirs(target_dir, exist_ok=True)
        original = "def marker():\n    return 'base'\n"
        target = os.path.join(target_dir, "test_proof_target.py")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(original)
        mission = _build_mission({
            "schema_version": "oss_agent_mission.v1",
            "mission_id": mission_id,
            "tier": "A5",
            "mode": "bounded_implementation",
            "objective": "Apply a low-risk workspace proof patch.",
            "risk_tier": "low",
            "write_allowed": True,
            "allowed_roots": [],
            "allowed_paths": ["tmp/proof_workspace/test_proof_target.py"],
            "owned_paths": ["tmp/proof_workspace/test_proof_target.py"],
            "read_only_paths": ["tmp/proof_workspace/test_proof_target.py"],
            "forbidden_roots": [".env", ".git", ".codex-oss/"],
            "allowed_tool_classes": ["read", "search"],
            "tool_budget": 4,
            "time_budget_seconds": 60,
            "stop_conditions": ["valid_patch", "deadline_reached"],
            "report_schema": "implementation_report.v1",
            "required_outputs": ["patch", "changed_files", "risk_assessment", "verification_plan", "evidence_refs"],
            "max_files_changed": 1,
            "max_patch_bytes": 12000,
            "apply_mode": "workspace_low_risk",
            "workspace_apply_policy": {"allow_direct_workspace_apply": True},
            "verification_policy": {
                "allowed_commands": [["python3", "-m", "py_compile", "tmp/proof_workspace/test_proof_target.py"]],
                "max_commands": 1,
                "timeout_seconds": 20,
            },
            "objective_spec": {
                "schema_version": "objective_spec.v1",
                "objective_type": "implementation_patch",
                "target": {
                    "required_changed_files": ["tmp/proof_workspace/test_proof_target.py"],
                    "required_test_files": ["tmp/proof_workspace/test_proof_target.py"],
                    "required_symbols": [{"path": "tmp/proof_workspace/test_proof_target.py", "kind": "function", "name": "test_proof_helper"}],
                },
                "completion_criteria": ["verification_required"],
            },
        })
        diff = (
            "diff --git a/tmp/proof_workspace/test_proof_target.py b/tmp/proof_workspace/test_proof_target.py\n"
            "--- a/tmp/proof_workspace/test_proof_target.py\n"
            "+++ b/tmp/proof_workspace/test_proof_target.py\n"
            "@@ -1,2 +1,5 @@\n"
            " def marker():\n"
            "     return 'base'\n"
            "+\n"
            "+def test_proof_helper():\n"
            "+    return 'workspace'\n"
        )
        handoff = (
            "<OSS_PATCH_PROPOSAL_JSON>\n"
            + json.dumps(_proposal_dict(diff, original, "tmp/proof_workspace/test_proof_target.py", "A5 workspace proof"), indent=2)
            + "\n</OSS_PATCH_PROPOSAL_JSON>\n"
        )
        with _cwd(root):
            result = run_implementation_mission(
                mission=mission,
                raw_model_alias="mission-a5-kimi",
                handoff=handoff,
                call_model=_unused_call_model,
                timeout=30,
                project_root=root,
            )
            if result.get("status") != "VERIFIED":
                raise RuntimeError(f"A5 workspace proof generation failed: {result}")
        _copy_proof_artifacts(root, project_root, mission_id, proof_kind="A5")
    return mission_id


def _generate_a6_certified_proof(project_root: str) -> str:
    mission_id = "proof_a6_certified"
    with tempfile.TemporaryDirectory(prefix="oss_proof_a6_") as root:
        auth_dir = os.path.join(root, "src", "auth")
        os.makedirs(auth_dir, exist_ok=True)
        original = "def gate():\n    return True\n"
        target = os.path.join(auth_dir, "proof_gate.py")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(original)
        mission = _build_mission({
            "schema_version": "oss_agent_mission.v1",
            "mission_id": mission_id,
            "tier": "A6",
            "mode": "critical_implementation",
            "objective": "Apply a certified critical-path proof patch.",
            "risk_tier": "critical",
            "write_allowed": True,
            "allowed_roots": [],
            "allowed_paths": ["src/auth/proof_gate.py"],
            "owned_paths": ["src/auth/proof_gate.py"],
            "read_only_paths": ["src/auth/proof_gate.py"],
            "forbidden_roots": [".env", ".git", ".codex-oss/"],
            "allowed_tool_classes": ["read", "search"],
            "tool_budget": 6,
            "time_budget_seconds": 60,
            "stop_conditions": ["valid_patch", "deadline_reached"],
            "report_schema": "implementation_report.v1",
            "required_outputs": ["patch", "changed_files", "risk_assessment", "verification_plan", "evidence_refs"],
            "max_files_changed": 1,
            "max_patch_bytes": 12000,
            "apply_mode": "critical_workspace_certified",
            "critical_path_write_allowed": True,
            "critical_path_reason": "deterministic proof refresh",
            "workspace_apply_policy": {
                "allow_critical_workspace_apply": True,
                "certification_required": True,
                "require_gpt_review": True,
                "reviewer_models": ["mission-a6-kimi"],
                "min_reviewer_approvals": 1,
                "require_isolated_preflight": True,
                "require_rollback_proof": True,
                "invariant_commands": [["python3", "-c", "pass"]],
            },
            "verification_policy": {
                "allowed_commands": [["python3", "-m", "py_compile", "src/auth/proof_gate.py"]],
                "max_commands": 1,
                "timeout_seconds": 20,
            },
            "objective_spec": {
                "schema_version": "objective_spec.v1",
                "objective_type": "critical_path_patch",
                "target": {
                    "required_changed_files": ["src/auth/proof_gate.py"],
                    "required_source_files": ["src/auth/proof_gate.py"],
                    "required_symbols": [{"path": "src/auth/proof_gate.py", "kind": "function", "name": "certified_helper"}],
                },
                "completion_criteria": ["verification_required", "workspace_certification_required"],
            },
        })
        diff = (
            "diff --git a/src/auth/proof_gate.py b/src/auth/proof_gate.py\n"
            "--- a/src/auth/proof_gate.py\n"
            "+++ b/src/auth/proof_gate.py\n"
            "@@ -1,2 +1,5 @@\n"
            " def gate():\n"
            "     return True\n"
            "+\n"
            "+def certified_helper():\n"
            "+    return True\n"
        )
        handoff = (
            "<OSS_PATCH_PROPOSAL_JSON>\n"
            + json.dumps(_proposal_dict(diff, original, "src/auth/proof_gate.py", "A6 certified proof"), indent=2)
            + "\n</OSS_PATCH_PROPOSAL_JSON>\n"
        )

        def reviewer_call_model(messages, tools, timeout, model_alias_override=None):
            return {
                "choices": [{
                    "message": {
                        "content": json.dumps({
                            "certification_review_version": "1.0",
                            "approved": True,
                            "confidence": "HIGH",
                            "findings": ["Deterministic proof patch is scoped to the owned critical path."],
                            "rationale": "Validation is green and verification is targeted.",
                        })
                    }
                }]
            }

        with _cwd(root):
            result = run_implementation_mission(
                mission=mission,
                raw_model_alias="mission-a6-kimi",
                handoff=handoff,
                call_model=reviewer_call_model,
                timeout=30,
                project_root=root,
            )
            if result.get("status") != "VERIFIED":
                raise RuntimeError(f"A6 certified proof generation failed: {result}")
        _copy_proof_artifacts(root, project_root, mission_id, proof_kind="A6")
    return mission_id


def _generate_operational_a3_readme(project_root: str) -> str:
    mission_id = "operational_a3_readme"
    with tempfile.TemporaryDirectory(prefix="oss_operational_a3_") as root:
        _copy_repo_file(project_root, root, "README.md")
        call_count = {"n": 0}

        def call_payload(payload, timeout):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {
                    "choices": [{
                        "message": {
                            "content": (
                                '{"action_type":"tool_call","tool_name":"rtk_read",'
                                '"arguments":{"path":"README.md"},'
                                '"reason":"Inspect the real repo README.","hypothesis":"README contains the project overview.",'
                                '"expected_information_gain":"Gather real repo evidence.",'
                                '"why_not_report_yet":"Need direct file evidence first."}'
                            )
                        }
                    }]
                }
            return {
                "choices": [{
                    "message": {
                        "content": (
                            '{"action_type":"final_report","report":'
                            '{"oss_report_version":"1.0","mission_id":"operational_a3_readme",'
                            '"status":"COMPLETE","confidence":"LOW",'
                            '"files_inspected":[{"path":"README.md","complete":true}],'
                            '"commands_run":[{"tool":"rtk_read","args":{"path":"README.md"}}],'
                            '"findings":[{"claim":"The repository README was inspected as operational evidence.","evidence_refs":["command:0"],"confidence":"LOW"}],'
                            '"uncertainties":[],"caveats":["operational burnin"],'
                            '"escalation_recommendation":"GPT-5.5 review required",'
                            '"missing_fields":[]}}'
                        )
                    }
                }]
            }

        body = {
            "input": [{
                "role": "user",
                "content": (
                    "<OSS_HANDOFF_JSON>\n"
                    '{"schema_version":"oss_agent_mission.v1","mission_id":"operational_a3_readme",'
                    '"tier":"A3","mode":"managed_investigation","objective":"Inspect the real repo README and report.",'
                    '"risk_tier":"low","write_allowed":false,"allowed_roots":[],"allowed_paths":["README.md"],'
                    '"tool_budget":2,"time_budget_seconds":30,"allowed_tool_classes":["read"],'
                    '"stop_conditions":["valid_report"],"report_schema":"managed_investigation_report.v1",'
                    '"required_outputs":["files_inspected","commands_run","findings","uncertainties","confidence","caveats","escalation_recommendation"]}'
                    "\n</OSS_HANDOFF_JSON>"
                ),
            }]
        }
        with _cwd(root):
            result = run_managed_mission_from_body(
                body,
                "mission-a3-kimi",
                lambda *args, **kwargs: None,
                call_payload,
                lambda model: model,
                request_deadline=30,
            )
            if not result.handled or result.status != "COMPLETE":
                raise RuntimeError(f"Operational A3 burn-in failed: {result}")
        _copy_proof_artifacts(root, project_root, mission_id, proof_kind="A3", evidence_set="operational")
    return mission_id


def _generate_operational_a3_impl_site(project_root: str) -> str:
    mission_id = "operational_a3_impl_site"
    with tempfile.TemporaryDirectory(prefix="oss_operational_a3_impl_") as root:
        _copy_repo_file(project_root, root, "codex_oss/managed_bridge.py")
        call_count = {"n": 0}

        def call_payload(payload, timeout):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {
                    "choices": [{
                        "message": {
                            "content": (
                                '{"action_type":"tool_call","tool_name":"rtk_read",'
                                '"arguments":{"path":"codex_oss/managed_bridge.py"},'
                                '"reason":"Inspect a real runtime implementation site.","hypothesis":"Managed mission dispatch lives in managed_bridge.py.",'
                                '"expected_information_gain":"Gather code evidence for implementation-site discovery.",'
                                '"why_not_report_yet":"Need file evidence before reporting."}'
                            )
                        }
                    }]
                }
            return {
                "choices": [{
                    "message": {
                        "content": (
                            '{"action_type":"final_report","report":'
                            '{"oss_report_version":"1.0","mission_id":"operational_a3_impl_site",'
                            '"status":"COMPLETE","confidence":"LOW",'
                            '"files_inspected":[{"path":"codex_oss/managed_bridge.py","complete":true}],'
                            '"commands_run":[{"tool":"rtk_read","args":{"path":"codex_oss/managed_bridge.py"}}],'
                            '"findings":[{"claim":"The managed bridge file was inspected as a real implementation site.","evidence_refs":["command:0"],"confidence":"LOW"}],'
                            '"uncertainties":[],"caveats":["operational burnin"],'
                            '"escalation_recommendation":"GPT-5.5 review required",'
                            '"missing_fields":[]}}'
                        )
                    }
                }]
            }

        body = {
            "input": [{
                "role": "user",
                "content": (
                    "<OSS_HANDOFF_JSON>\n"
                    '{"schema_version":"oss_agent_mission.v1","mission_id":"operational_a3_impl_site",'
                    '"tier":"A3","mode":"managed_investigation","objective":"Inspect a real implementation site and report.",'
                    '"risk_tier":"low","write_allowed":false,"allowed_roots":[],"allowed_paths":["codex_oss/managed_bridge.py"],'
                    '"tool_budget":2,"time_budget_seconds":30,"allowed_tool_classes":["read"],'
                    '"stop_conditions":["valid_report"],"report_schema":"managed_investigation_report.v1",'
                    '"required_outputs":["files_inspected","commands_run","findings","uncertainties","confidence","caveats","escalation_recommendation"]}'
                    "\n</OSS_HANDOFF_JSON>"
                ),
            }]
        }
        with _cwd(root):
            result = run_managed_mission_from_body(
                body,
                "mission-a3-kimi",
                lambda *args, **kwargs: None,
                call_payload,
                lambda model: model,
                request_deadline=30,
            )
            if not result.handled or result.status != "COMPLETE":
                raise RuntimeError(f"Operational A3 implementation-site burn-in failed: {result}")
        _copy_proof_artifacts(root, project_root, mission_id, proof_kind="A3", evidence_set="operational")
    return mission_id


def _generate_operational_a3_open_runtime(project_root: str) -> str:
    mission_id = "operational_a3_open_runtime"
    with tempfile.TemporaryDirectory(prefix="oss_operational_a3_open_") as root:
        _copy_repo_file(project_root, root, "codex_oss/managed_bridge.py")
        _copy_repo_file(project_root, root, "codex_oss/audit.py")
        call_count = {"n": 0}

        def call_payload(payload, timeout):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {
                    "choices": [{
                        "message": {
                            "content": (
                                '{"action_type":"tool_call","phase":"EXPLORE","tool_name":"rtk_grep",'
                                '"arguments":{"pattern":"trace.jsonl|claim_graph.json|decision_trace.json","path":"codex_oss/managed_bridge.py"},'
                                '"reason":"Start with a targeted search over the runtime code.",'
                                '"hypothesis":"The readonly artifact bundle is implemented in one runtime writer and enforced in audit.",'
                                '"target_question":"Which runtime files mention readonly artifact bundle members?",'
                                '"expected_information_gain":"Discover the implementation and enforcement files before direct inspection.",'
                                '"why_not_report_yet":"Need to identify the likely files before reading them directly."}'
                            )
                        }
                    }]
                }
            if call_count["n"] == 2:
                return {
                    "choices": [{
                        "message": {
                            "content": (
                                '{"action_type":"tool_call","phase":"NARROW","tool_name":"rtk_read",'
                                '"arguments":{"path":"codex_oss/managed_bridge.py"},'
                                '"reason":"Inspect the runtime writer first.",'
                                '"hypothesis":"managed_bridge.py persists the readonly artifact bundle.",'
                                '"target_question":"Where are readonly mission artifacts written?",'
                                '"expected_information_gain":"Find the implementation site that writes readonly artifacts.",'
                                '"why_not_report_yet":"Need implementation evidence before summarizing the behavior."}'
                            )
                        }
                    }]
                }
            if call_count["n"] == 3:
                return {
                    "choices": [{
                        "message": {
                            "content": (
                                '{"action_type":"tool_call","phase":"VERIFY","tool_name":"rtk_read",'
                                '"arguments":{"path":"codex_oss/audit.py"},'
                                '"reason":"Inspect audit expectations to verify the contract.",'
                                '"hypothesis":"audit.py enforces the presence of readonly artifacts including decision traces and claim graphs.",'
                                '"target_question":"What does audit require for readonly missions?",'
                                '"expected_information_gain":"Cross-check the artifact contract from audit enforcement.",'
                                '"why_not_report_yet":"Need audit evidence to support the implementation claim."}'
                            )
                        }
                    }]
                }
            return {
                "choices": [{
                    "message": {
                        "content": (
                            '{"action_type":"final_report","report":'
                            '{"oss_report_version":"1.0","mission_id":"operational_a3_open_runtime",'
                            '"status":"COMPLETE","confidence":"LOW",'
                            '"files_inspected":[{"path":"codex_oss/managed_bridge.py","complete":true},{"path":"codex_oss/audit.py","complete":true}],'
                            '"commands_run":[{"tool":"rtk_grep","args":{"pattern":"trace.jsonl|claim_graph.json|decision_trace.json","path":"codex_oss/managed_bridge.py"}},{"tool":"rtk_read","args":{"path":"codex_oss/managed_bridge.py"}},{"tool":"rtk_read","args":{"path":"codex_oss/audit.py"}}],'
                            '"findings":[{"claim":"The runtime writes the readonly artifact bundle in managed_bridge.py and audit.py enforces that contract for readonly missions.","evidence_refs":["command:0","command:1","command:2"],"confidence":"LOW"}],'
                            '"uncertainties":["This operational open investigation inspected two files but did not traverse every helper module."],'
                            '"caveats":["operational A3-open burnin"],'
                            '"escalation_recommendation":"GPT-5.5 review recommended",'
                            '"missing_fields":[]}}'
                        )
                    }
                }]
            }

        body = {
            "input": [{
                "role": "user",
                "content": (
                    "<OSS_HANDOFF_JSON>\n"
                    '{"schema_version":"oss_agent_mission.v1","mission_id":"operational_a3_open_runtime",'
                    '"tier":"A3","mode":"managed_investigation","objective":"Investigate how readonly runtime artifacts are written and audited.",'
                    '"objective_style":"open_investigation",'
                    '"sufficiency_policy":{"min_main_claims":1,"min_evidence_refs_per_claim":1,"must_list_uninspected_areas":true,"confidence_cap_if_partial_extracts":"MEDIUM"},'
                    '"answer_obligations":[{"id":"q1","question":"Which runtime file writes readonly artifacts?","required":true,"source_hints":["codex_oss/managed_bridge.py"],"source_requirements":[{"path":"codex_oss/managed_bridge.py","evidence_kind":"implementation_logic","required":true,"prefetch":true}]},{"id":"q2","question":"Which audit file enforces the readonly artifact contract?","required":true,"source_hints":["codex_oss/audit.py"],"source_requirements":[{"path":"codex_oss/audit.py","evidence_kind":"audit_enforcement","required":true,"prefetch":true}]},{"id":"q3","question":"What conclusion is justified from the implementation and audit evidence?","required":true,"source_hints":["codex_oss/managed_bridge.py","codex_oss/audit.py"]},{"id":"q4","question":"What remains unproven?","required":true,"source_hints":["codex_oss/managed_bridge.py","codex_oss/audit.py"]}],'
                    '"must_inspect":["codex_oss/managed_bridge.py","codex_oss/audit.py"],'
                    '"risk_tier":"low","write_allowed":false,"allowed_roots":[],"allowed_paths":["codex_oss/managed_bridge.py","codex_oss/audit.py"],'
                    '"tool_budget":5,"time_budget_seconds":45,"allowed_tool_classes":["read","search"],'
                    '"stop_conditions":["valid_report"],"report_schema":"managed_investigation_report.v1",'
                    '"required_outputs":["files_inspected","commands_run","findings","uncertainties","confidence","caveats","escalation_recommendation"]}'
                    "\n</OSS_HANDOFF_JSON>"
                ),
            }]
        }
        with _cwd(root):
            result = run_managed_mission_from_body(
                body,
                "mission-a3-kimi",
                lambda *args, **kwargs: None,
                call_payload,
                lambda model: model,
                request_deadline=45,
            )
            if not result.handled or result.status != "COMPLETE":
                raise RuntimeError(f"Operational A3-open burn-in failed: {result}")
        _copy_proof_artifacts(root, project_root, mission_id, proof_kind="A3_OPEN", evidence_set="operational")
    return mission_id


def _generate_operational_a4_docs_patch(project_root: str) -> str:
    mission_id = "operational_a4_docs_patch"
    with tempfile.TemporaryDirectory(prefix="oss_operational_a4_") as root:
        _copy_repo_file(project_root, root, "README.md")
        target = os.path.join(root, "README.md")
        original = _read_text(target)
        addition = "\n## Operational Burn-in Note\nThis section exists only for runtime operational certification.\n"
        updated = original.rstrip() + addition
        mission = _build_mission({
            "schema_version": "oss_agent_mission.v1",
            "mission_id": mission_id,
            "tier": "A4",
            "mode": "patch_proposal",
            "objective": "Propose a real-repo README documentation patch.",
            "risk_tier": "low",
            "write_allowed": False,
            "allowed_roots": [],
            "allowed_paths": ["README.md"],
            "owned_paths": ["README.md"],
            "read_only_paths": ["README.md"],
            "forbidden_roots": [".env", ".git", ".codex-oss/"],
            "allowed_tool_classes": ["read", "search"],
            "tool_budget": 4,
            "time_budget_seconds": 60,
            "stop_conditions": ["valid_patch", "deadline_reached"],
            "report_schema": "patch_validation_report.v1",
            "required_outputs": ["patch", "changed_files", "risk_assessment", "verification_plan", "evidence_refs"],
            "max_files_changed": 1,
            "max_patch_bytes": 12000,
            "apply_mode": "none",
            "verification_policy": {
                "allowed_commands": [["python3", "-c", "from pathlib import Path; assert 'Operational Burn-in Note' in Path('README.md').read_text()"]],
                "max_commands": 1,
                "timeout_seconds": 20,
            },
            "objective_spec": {
                "schema_version": "objective_spec.v1",
                "objective_type": "documentation_patch",
                "target": {
                    "required_changed_files": ["README.md"],
                    "required_markdown_headings": ["Operational Burn-in Note"],
                    "required_content_substrings": ["runtime operational certification"],
                },
                "completion_criteria": ["required_heading_present"],
            },
        })
        diff = _unified_diff("README.md", original, updated)
        handoff = (
            "<OSS_PATCH_PROPOSAL_JSON>\n"
            + json.dumps(_proposal_dict(diff, original, "README.md", "Operational README proposal"), indent=2)
            + "\n</OSS_PATCH_PROPOSAL_JSON>\n"
        )
        with _cwd(root):
            result = run_implementation_mission(
                mission=mission,
                raw_model_alias="mission-a4-kimi",
                handoff=handoff,
                call_model=_unused_call_model,
                timeout=30,
                project_root=root,
            )
            if result.get("status") != "VALID":
                raise RuntimeError(f"Operational A4 burn-in failed: {result}")
        _copy_proof_artifacts(root, project_root, mission_id, proof_kind="A4", evidence_set="operational")
    return mission_id


def _generate_operational_a5_workspace_docs(project_root: str) -> str:
    mission_id = "operational_a5_workspace_docs"
    with tempfile.TemporaryDirectory(prefix="oss_operational_a5_") as root:
        _copy_repo_file(project_root, root, "docs/OSS_AGENT_RUNTIME_V1_SPEC.md")
        target = os.path.join(root, "docs", "OSS_AGENT_RUNTIME_V1_SPEC.md")
        original = _read_text(target)
        addition = "\n## Operational Workspace Proof\nThis section proves low-risk workspace mutation on a real repository document.\n"
        updated = original.rstrip() + addition
        mission = _build_mission({
            "schema_version": "oss_agent_mission.v1",
            "mission_id": mission_id,
            "tier": "A5",
            "mode": "bounded_implementation",
            "objective": "Apply a low-risk real-repo documentation patch.",
            "risk_tier": "low",
            "write_allowed": True,
            "allowed_roots": [],
            "allowed_paths": ["docs/OSS_AGENT_RUNTIME_V1_SPEC.md"],
            "owned_paths": ["docs/OSS_AGENT_RUNTIME_V1_SPEC.md"],
            "read_only_paths": ["docs/OSS_AGENT_RUNTIME_V1_SPEC.md"],
            "forbidden_roots": [".env", ".git", ".codex-oss/"],
            "allowed_tool_classes": ["read", "search"],
            "tool_budget": 4,
            "time_budget_seconds": 60,
            "stop_conditions": ["valid_patch", "deadline_reached"],
            "report_schema": "implementation_report.v1",
            "required_outputs": ["patch", "changed_files", "risk_assessment", "verification_plan", "evidence_refs"],
            "max_files_changed": 1,
            "max_patch_bytes": 12000,
            "apply_mode": "workspace_low_risk",
            "workspace_apply_policy": {"allow_direct_workspace_apply": True},
            "verification_policy": {
                "allowed_commands": [["python3", "-c", "from pathlib import Path; assert 'Operational Workspace Proof' in Path('docs/OSS_AGENT_RUNTIME_V1_SPEC.md').read_text()"]],
                "max_commands": 1,
                "timeout_seconds": 20,
            },
            "objective_spec": {
                "schema_version": "objective_spec.v1",
                "objective_type": "documentation_patch",
                "target": {
                    "required_changed_files": ["docs/OSS_AGENT_RUNTIME_V1_SPEC.md"],
                    "required_markdown_headings": ["Operational Workspace Proof"],
                    "required_content_substrings": ["low-risk workspace mutation"],
                },
                "completion_criteria": ["required_heading_present", "verification_required"],
            },
        })
        diff = _unified_diff("docs/OSS_AGENT_RUNTIME_V1_SPEC.md", original, updated)
        handoff = (
            "<OSS_PATCH_PROPOSAL_JSON>\n"
            + json.dumps(
                _proposal_dict(
                    diff,
                    original,
                    "docs/OSS_AGENT_RUNTIME_V1_SPEC.md",
                    "Operational workspace docs patch",
                    verification_command=[
                        "python3",
                        "-c",
                        "from pathlib import Path; assert 'Operational Workspace Proof' in Path('docs/OSS_AGENT_RUNTIME_V1_SPEC.md').read_text()",
                    ],
                ),
                indent=2,
            )
            + "\n</OSS_PATCH_PROPOSAL_JSON>\n"
        )
        with _cwd(root):
            result = run_implementation_mission(
                mission=mission,
                raw_model_alias="mission-a5-kimi",
                handoff=handoff,
                call_model=_unused_call_model,
                timeout=30,
                project_root=root,
            )
            if result.get("status") != "VERIFIED":
                raise RuntimeError(f"Operational A5 burn-in failed: {result}")
        _copy_proof_artifacts(root, project_root, mission_id, proof_kind="A5", evidence_set="operational")
    return mission_id


def _generate_operational_a6_certified_runtime(project_root: str) -> str:
    mission_id = "operational_a6_certified_runtime"
    with tempfile.TemporaryDirectory(prefix="oss_operational_a6_") as root:
        _copy_repo_file(project_root, root, "codex_oss/audit.py")
        target = os.path.join(root, "codex_oss", "audit.py")
        original = _read_text(target)
        updated = original.rstrip() + (
            "\n\n"
            "def _operational_certified_helper() -> bool:\n"
            "    return True\n"
        )
        mission = _build_mission({
            "schema_version": "oss_agent_mission.v1",
            "mission_id": mission_id,
            "tier": "A6",
            "mode": "critical_implementation",
            "objective": "Apply a certified operational runtime patch.",
            "risk_tier": "critical",
            "write_allowed": True,
            "allowed_roots": [],
            "allowed_paths": ["codex_oss/audit.py"],
            "owned_paths": ["codex_oss/audit.py"],
            "read_only_paths": ["codex_oss/audit.py"],
            "forbidden_roots": [".env", ".git", ".codex-oss/"],
            "allowed_tool_classes": ["read", "search"],
            "tool_budget": 6,
            "time_budget_seconds": 60,
            "stop_conditions": ["valid_patch", "deadline_reached"],
            "report_schema": "implementation_report.v1",
            "required_outputs": ["patch", "changed_files", "risk_assessment", "verification_plan", "evidence_refs"],
            "max_files_changed": 1,
            "max_patch_bytes": 16000,
            "apply_mode": "critical_workspace_certified",
            "critical_path_write_allowed": True,
            "critical_path_reason": "operational runtime burnin",
            "workspace_apply_policy": {
                "allow_critical_workspace_apply": True,
                "certification_required": True,
                "require_gpt_review": True,
                "reviewer_models": ["mission-a6-kimi"],
                "min_reviewer_approvals": 1,
                "require_isolated_preflight": True,
                "require_rollback_proof": True,
                "invariant_commands": [["python3", "-m", "py_compile", "codex_oss/audit.py"]],
            },
            "verification_policy": {
                "allowed_commands": [["python3", "-m", "py_compile", "codex_oss/audit.py"]],
                "max_commands": 1,
                "timeout_seconds": 20,
            },
            "objective_spec": {
                "schema_version": "objective_spec.v1",
                "objective_type": "critical_path_patch",
                "target": {
                    "required_changed_files": ["codex_oss/audit.py"],
                    "required_source_files": ["codex_oss/audit.py"],
                    "required_symbols": [{"path": "codex_oss/audit.py", "kind": "function", "name": "_operational_certified_helper"}],
                },
                "completion_criteria": ["verification_required", "workspace_certification_required"],
            },
        })
        diff = _unified_diff("codex_oss/audit.py", original, updated)
        handoff = (
            "<OSS_PATCH_PROPOSAL_JSON>\n"
            + json.dumps(_proposal_dict(diff, original, "codex_oss/audit.py", "Operational critical runtime patch"), indent=2)
            + "\n</OSS_PATCH_PROPOSAL_JSON>\n"
        )

        def reviewer_call_model(messages, tools, timeout, model_alias_override=None):
            return {
                "choices": [{
                    "message": {
                        "content": json.dumps({
                            "certification_review_version": "1.0",
                            "approved": True,
                            "confidence": "HIGH",
                            "findings": ["Operational runtime patch stays inside the owned critical file and passes targeted verification."],
                            "rationale": "Validation, preflight, rollback proof, and invariant checks are green.",
                        })
                    }
                }]
            }

        with _cwd(root):
            result = run_implementation_mission(
                mission=mission,
                raw_model_alias="mission-a6-kimi",
                handoff=handoff,
                call_model=reviewer_call_model,
                timeout=30,
                project_root=root,
            )
            if result.get("status") != "VERIFIED":
                raise RuntimeError(f"Operational A6 burn-in failed: {result}")
        _copy_proof_artifacts(root, project_root, mission_id, proof_kind="A6", evidence_set="operational")
    return mission_id


def _generate_operational_a5_multifile_runtime(project_root: str) -> str:
    mission_id = "operational_a5_multifile_runtime"
    with tempfile.TemporaryDirectory(prefix="oss_operational_a5_multi_") as root:
        _copy_repo_file(project_root, root, "tests/fixtures/a4_live_string_utils.py")
        _copy_repo_file(project_root, root, "tests/test_a4_live_string_utils.py")
        source_path = os.path.join(root, "tests", "fixtures", "a4_live_string_utils.py")
        test_path = os.path.join(root, "tests", "test_a4_live_string_utils.py")
        source_original = _read_text(source_path)
        test_original = _read_text(test_path)
        source_updated = source_original.rstrip() + (
            "\n\n"
            "def squash_lines(value: str) -> str:\n"
            "    return \" \".join(part for part in value.splitlines() if part.strip())\n"
        )
        test_updated = test_original.rstrip() + (
            "\n\n"
            "    def test_squash_lines(self):\n"
            "        from a4_live_string_utils import squash_lines\n"
            "        self.assertEqual(squash_lines(\"a\\n\\n b\\n\"), \"a  b\")\n"
        )
        diff = (
            _unified_diff("tests/fixtures/a4_live_string_utils.py", source_original, source_updated)
            + _unified_diff("tests/test_a4_live_string_utils.py", test_original, test_updated)
        )
        mission = _build_mission({
            "schema_version": "oss_agent_mission.v1",
            "mission_id": mission_id,
            "tier": "A5",
            "mode": "bounded_implementation",
            "objective": "Apply a medium-complexity real-repo source+test patch in isolation.",
            "risk_tier": "low",
            "write_allowed": True,
            "allowed_roots": [],
            "allowed_paths": [
                "tests/fixtures/a4_live_string_utils.py",
                "tests/test_a4_live_string_utils.py",
            ],
            "owned_paths": [
                "tests/fixtures/a4_live_string_utils.py",
                "tests/test_a4_live_string_utils.py",
            ],
            "read_only_paths": [
                "tests/fixtures/a4_live_string_utils.py",
                "tests/test_a4_live_string_utils.py",
            ],
            "forbidden_roots": [".env", ".git", ".codex-oss/"],
            "allowed_tool_classes": ["read", "search"],
            "tool_budget": 6,
            "time_budget_seconds": 90,
            "stop_conditions": ["valid_patch", "deadline_reached"],
            "report_schema": "implementation_report.v1",
            "required_outputs": ["patch", "changed_files", "risk_assessment", "verification_plan", "evidence_refs"],
            "max_files_changed": 2,
            "max_patch_bytes": 24000,
            "apply_mode": "isolated_worktree",
            "verification_policy": {
                "allowed_commands": [["python3", "tests/test_a4_live_string_utils.py"]],
                "max_commands": 1,
                "timeout_seconds": 30,
            },
            "objective_spec": {
                "schema_version": "objective_spec.v1",
                "objective_type": "implementation_patch",
                "target": {
                    "required_changed_files": [
                        "tests/fixtures/a4_live_string_utils.py",
                        "tests/test_a4_live_string_utils.py",
                    ],
                    "required_source_files": ["tests/fixtures/a4_live_string_utils.py"],
                    "required_test_files": ["tests/test_a4_live_string_utils.py"],
                    "required_symbols": [
                        {
                            "path": "tests/fixtures/a4_live_string_utils.py",
                            "kind": "function",
                            "name": "squash_lines",
                        }
                    ],
                    "required_test_names": ["test_squash_lines"],
                },
                "completion_criteria": ["verification_required"],
            },
        })
        proposal = {
            **_proposal_dict(
                diff,
                source_original,
                "tests/fixtures/a4_live_string_utils.py",
                "Operational multifile runtime patch",
                verification_command=["python3", "tests/test_a4_live_string_utils.py"],
            ),
            "changed_files": [
                {
                    "path": "tests/fixtures/a4_live_string_utils.py",
                    "change_type": "modify",
                    "reason": "Add new helper.",
                    "base_sha256": hashlib.sha256(source_original.encode()).hexdigest(),
                },
                {
                    "path": "tests/test_a4_live_string_utils.py",
                    "change_type": "modify",
                    "reason": "Add coverage for new helper.",
                    "base_sha256": hashlib.sha256(test_original.encode()).hexdigest(),
                },
            ],
            "risk_assessment": {
                "risk_tier": "low",
                "critical_paths_touched": False,
                "blast_radius": "two-file isolated runtime patch",
            },
            "evidence_refs": [
                "file:tests/fixtures/a4_live_string_utils.py#extract:normalize_label",
                "file:tests/test_a4_live_string_utils.py#extract:NormalizeLabelTests",
            ],
        }
        handoff = "<OSS_PATCH_PROPOSAL_JSON>\n" + json.dumps(proposal, indent=2) + "\n</OSS_PATCH_PROPOSAL_JSON>\n"
        with _cwd(root):
            result = run_implementation_mission(
                mission=mission,
                raw_model_alias="mission-a5-kimi",
                handoff=handoff,
                call_model=_unused_call_model,
                timeout=45,
                project_root=root,
            )
            if result.get("status") != "VERIFIED":
                raise RuntimeError(f"Operational A5 multifile burn-in failed: {result}")
        _copy_proof_artifacts(root, project_root, mission_id, proof_kind="A5", evidence_set="operational")
    return mission_id


def _copy_proof_artifacts(
    source_root: str,
    project_root: str,
    mission_id: str,
    proof_kind: str,
    evidence_set: str = "proof",
) -> None:
    source_dir = os.path.join(source_root, ".codex-oss", "missions", mission_id)
    dest_dir = os.path.join(project_root, ".codex-oss", "missions", mission_id)
    if os.path.exists(dest_dir):
        shutil.rmtree(dest_dir)
    os.makedirs(os.path.dirname(dest_dir), exist_ok=True)
    shutil.copytree(source_dir, dest_dir)
    mission_path = os.path.join(dest_dir, "mission.json")
    if os.path.exists(mission_path):
        with open(mission_path, "r", encoding="utf-8") as handle:
            mission = json.load(handle)
        mission["promotion_proof"] = evidence_set == "proof"
        mission["operational_burnin"] = evidence_set == "operational"
        mission["evidence_set"] = evidence_set
        mission["proof_kind"] = proof_kind
        with open(mission_path, "w", encoding="utf-8") as handle:
            json.dump(mission, handle, indent=2, sort_keys=True)


def _proposal_dict(
    diff: str,
    original_text: str,
    path: str,
    summary: str,
    verification_command: list[str] | None = None,
) -> dict[str, Any]:
    command = verification_command or ["python3", "-m", "py_compile", path]
    return {
        "patch_proposal_version": "1.0",
        "status": "PROPOSED",
        "summary": summary,
        "base": {"git_head": "fixture", "dirty_worktree_allowed": False},
        "changed_files": [{
            "path": path,
            "change_type": "modify",
            "reason": summary,
            "base_sha256": hashlib.sha256(original_text.encode()).hexdigest(),
        }],
        "unified_diff": diff,
        "risk_assessment": {"risk_tier": "low", "critical_paths_touched": "src/auth/" in path, "blast_radius": "test-only" if path.startswith("tests/") else "single-file"},
        "verification_plan": [{"command": command, "reason": "verification"}],
        "evidence_refs": [f"file:{path}#extract:proof"],
        "caveats": [],
    }


def _copy_repo_file(project_root: str, temp_root: str, relpath: str) -> None:
    src = os.path.join(project_root, relpath)
    if not os.path.exists(src):
        src = os.path.join(os.path.dirname(os.path.dirname(__file__)), relpath)
    dst = os.path.join(temp_root, relpath)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _unified_diff(relpath: str, original_text: str, updated_text: str) -> str:
    body = "".join(
        difflib.unified_diff(
            original_text.splitlines(keepends=True),
            updated_text.splitlines(keepends=True),
            fromfile=f"a/{relpath}",
            tofile=f"b/{relpath}",
        )
    )
    return f"diff --git a/{relpath} b/{relpath}\n{body}"


def _unused_call_model(messages, tools, timeout, model_alias_override=None):
    return {"choices": [{"message": {"content": "{}"}}]}


@contextmanager
def _cwd(path: str):
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)
