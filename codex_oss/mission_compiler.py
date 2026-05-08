"""MissionCompilerV1 for building explicit MissionV1 contracts from CLI inputs."""

from __future__ import annotations

from typing import Any


def compile_mission_v1(
    *,
    mission_id: str,
    objective: str,
    tier: str,
    risk_tier: str,
    allowed_roots: list[str],
    allowed_paths: list[str],
    owned_paths: list[str],
    read_only_paths: list[str],
    objective_type: str,
    required_test_names: list[str],
    required_changed_files: list[str],
    required_source_files: list[str],
    required_test_files: list[str],
    required_symbols: list[str],
    verification_commands: list[list[str]],
    apply_mode: str,
    allow_broad_read_scope: bool = False,
    critical_path_read_allowed: bool = False,
    critical_path_reason: str | None = None,
    critical_path_write_allowed: bool = False,
    workspace_apply_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tier = tier.upper()
    mode_map = {
        "A2": "guided_exploration",
        "A3": "managed_investigation",
        "A4": "patch_proposal",
        "A5": "bounded_implementation",
        "A6": "critical_implementation",
    }
    if tier not in mode_map:
        raise ValueError(f"unsupported tier for compiler: {tier}")

    mission: dict[str, Any] = {
        "schema_version": "oss_agent_mission.v1",
        "mission_id": mission_id,
        "tier": tier,
        "mode": mode_map[tier],
        "objective": objective,
        "risk_tier": risk_tier,
        "write_allowed": tier in {"A5", "A6"},
        "allowed_roots": list(allowed_roots),
        "allowed_paths": list(allowed_paths),
        "owned_paths": list(owned_paths),
        "read_only_paths": list(read_only_paths),
        "forbidden_roots": [],
        "allowed_tool_classes": ["read", "search", "list", "safe_git"],
        "tool_budget": 10 if tier == "A2" else 20,
        "time_budget_seconds": 90 if tier == "A2" else 180,
        "stop_conditions": ["valid_report", "budget_exhausted", "deadline_reached"],
        "allow_broad_read_scope": bool(allow_broad_read_scope),
        "critical_path_read_allowed": bool(critical_path_read_allowed),
        "critical_path_reason": critical_path_reason,
        "critical_path_write_allowed": bool(critical_path_write_allowed),
    }

    if tier in {"A2", "A3"}:
        mission["report_schema"] = "managed_investigation_report.v1"
        mission["required_outputs"] = [
            "files_inspected",
            "commands_run",
            "findings",
            "uncertainties",
            "confidence",
            "caveats",
            "escalation_recommendation",
        ]
    else:
        mission["report_schema"] = "patch_validation_report.v1" if tier == "A4" else "implementation_report.v1"
        mission["required_outputs"] = ["patch", "changed_files", "risk_assessment", "verification_plan", "evidence_refs"]
        mission["max_files_changed"] = max(1, len(required_changed_files) or len(owned_paths) or 1)
        mission["max_patch_bytes"] = 12000
        mission["verification_policy"] = {
            "allowed_commands": verification_commands,
            "max_commands": len(verification_commands) if verification_commands else 0,
            "timeout_seconds": 60,
        }
        mission["apply_mode"] = apply_mode
        if workspace_apply_policy:
            policy = dict(workspace_apply_policy)
            if policy.get("certification_required") and "require_gpt_review" not in policy:
                policy["require_gpt_review"] = True
            if policy.get("certification_required") and not policy.get("min_reviewer_approvals"):
                policy["min_reviewer_approvals"] = max(1, len(list(policy.get("reviewer_models", []) or [])))
            mission["workspace_apply_policy"] = policy

    if objective_type:
        mission["objective_spec"] = _objective_spec(
            objective_type=objective_type,
            required_test_names=required_test_names,
            required_changed_files=required_changed_files,
            required_source_files=required_source_files,
            required_test_files=required_test_files,
            required_symbols=required_symbols,
        )
    return mission


def _objective_spec(
    *,
    objective_type: str,
    required_test_names: list[str],
    required_changed_files: list[str],
    required_source_files: list[str],
    required_test_files: list[str],
    required_symbols: list[str],
) -> dict[str, Any]:
    if objective_type == "implementation_test_only":
        test_file = required_test_files[0] if required_test_files else (required_changed_files[0] if required_changed_files else "")
        return {
            "schema_version": "objective_spec.v1",
            "objective_type": "implementation_test_only",
            "target": {
                "test_file": test_file,
                "source_files": list(required_source_files),
                "required_test_names": list(required_test_names),
            },
            "required_outputs": ["changed_test_file"],
            "required_evidence_shapes": ["test_definition"],
            "completion_criteria": ["required_test_present", "source_files_unchanged"],
        }
    if objective_type in {"implementation_patch", "critical_path_patch"}:
        parsed_symbols = []
        for entry in required_symbols:
            parts = entry.split(":", 2)
            if len(parts) == 3:
                path, kind, name = parts
            elif len(parts) == 2:
                path, name = parts
                kind = "function"
            else:
                path = required_source_files[0] if required_source_files else ""
                kind = "function"
                name = entry
            parsed_symbols.append({"path": path, "kind": kind, "name": name})
        return {
            "schema_version": "objective_spec.v1",
            "objective_type": objective_type,
            "target": {
                "required_changed_files": list(required_changed_files),
                "required_source_files": list(required_source_files),
                "required_test_files": list(required_test_files),
                "required_symbols": parsed_symbols,
                "required_test_names": list(required_test_names),
            },
            "required_outputs": ["changed_source_file"] if required_source_files else ["changed_files"],
            "required_evidence_shapes": ["source_change"],
            "completion_criteria": ["required_symbols_present", "verification_required"],
        }
    if objective_type == "function_location":
        symbol = required_symbols[0] if required_symbols else ""
        return {
            "schema_version": "objective_spec.v1",
            "objective_type": "function_location",
            "target": {"symbol": symbol},
            "required_outputs": ["file_path", "function_definition"],
            "required_evidence_shapes": ["function_definition"],
            "completion_criteria": ["function_definition_present"],
        }
    return {
        "schema_version": "objective_spec.v1",
        "objective_type": objective_type,
        "target": {},
        "required_outputs": [],
        "required_evidence_shapes": [],
        "completion_criteria": [],
    }
