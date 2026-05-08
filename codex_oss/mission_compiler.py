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
    target_symbol: str,
    target_key: str,
    target_pattern: str,
    required_values: list[str],
    required_test_names: list[str],
    required_changed_files: list[str],
    required_source_files: list[str],
    required_test_files: list[str],
    required_symbols: list[str],
    verification_commands: list[list[str]],
    apply_mode: str,
    objective_style: str = "",
    sufficiency_policy: dict[str, Any] | None = None,
    answer_obligations: list[dict[str, Any]] | None = None,
    must_inspect: list[str] | None = None,
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
    if objective_style:
        mission["objective_style"] = objective_style
    if sufficiency_policy:
        mission["sufficiency_policy"] = dict(sufficiency_policy)
    if must_inspect:
        mission["must_inspect"] = list(must_inspect)

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
            target_symbol=target_symbol,
            target_key=target_key,
            target_pattern=target_pattern,
            required_values=required_values,
            required_test_names=required_test_names,
            required_changed_files=required_changed_files,
            required_source_files=required_source_files,
            required_test_files=required_test_files,
            required_symbols=required_symbols,
        )
    if mission.get("objective_style") == "open_investigation":
        mission["answer_obligations"] = list(answer_obligations or _default_open_answer_obligations(
            objective=objective,
            allowed_paths=allowed_paths,
            objective_spec=mission.get("objective_spec"),
        ))
        if "must_inspect" not in mission:
            mission["must_inspect"] = _default_must_inspect(
                allowed_paths=allowed_paths,
                objective_spec=mission.get("objective_spec"),
            )
    return mission


def compile_handoff_v1(mission: dict[str, Any]) -> str:
    import json

    return "<OSS_HANDOFF_JSON>\n" + json.dumps(mission, indent=2, sort_keys=True) + "\n</OSS_HANDOFF_JSON>\n"


def _objective_spec(
    *,
    objective_type: str,
    target_symbol: str,
    target_key: str,
    target_pattern: str,
    required_values: list[str],
    required_test_names: list[str],
    required_changed_files: list[str],
    required_source_files: list[str],
    required_test_files: list[str],
    required_symbols: list[str],
) -> dict[str, Any]:
    if objective_type == "mapping_lookup":
        key = target_key or target_symbol or (required_symbols[0] if required_symbols else "")
        return {
            "schema_version": "objective_spec.v1",
            "objective_type": "mapping_lookup",
            "target": {"key": key},
            "required_outputs": ["mapped_value", "mapping_file"],
            "required_evidence_shapes": ["mapping_assignment"],
            "completion_criteria": ["key_value_mapping_present"],
        }
    if objective_type == "config_value_extraction":
        key = target_key or ""
        symbol = target_symbol or ""
        return {
            "schema_version": "objective_spec.v1",
            "objective_type": "config_value_extraction",
            "target": {"key": key, "symbol": symbol},
            "required_outputs": ["file_path"] + list(required_values),
            "required_evidence_shapes": ["dictionary_entry", "literal_value"],
            "required_values": list(required_values),
            "completion_criteria": ["required_values_present"],
        }
    if objective_type == "zero_match_evidence":
        pattern = target_pattern or target_symbol or target_key
        return {
            "schema_version": "objective_spec.v1",
            "objective_type": "zero_match_evidence",
            "target": {"pattern": pattern},
            "required_outputs": ["searched_paths", "zero_match_result"],
            "required_evidence_shapes": ["grep_zero_match"],
            "completion_criteria": ["grep_executed", "matches_count_zero"],
        }
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
        symbol = target_symbol or (required_symbols[0] if required_symbols else "")
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


def _default_open_answer_obligations(*, objective: str, allowed_paths: list[str], objective_spec: dict[str, Any] | None) -> list[dict[str, Any]]:
    objective_spec = objective_spec if isinstance(objective_spec, dict) else {}
    objective_type = str(objective_spec.get("objective_type", "") or "")
    target = objective_spec.get("target", {}) if isinstance(objective_spec.get("target", {}), dict) else {}
    target_name = str(target.get("symbol") or target.get("key") or target.get("pattern") or "")
    path0 = allowed_paths[0] if allowed_paths else ""
    path1 = allowed_paths[1] if len(allowed_paths) > 1 else path0
    summary_paths = [path for path in [path0, path1] if path]
    if objective_type in {"function_location", "mapping_lookup", "config_value_extraction", "zero_match_evidence"}:
        label = target_name or objective_type
        return [
            {
                "id": "q1",
                "question": f"Where is the relevant implementation or evidence source for {label}?",
                "required": True,
                "source_hints": [path for path in [path0] if path],
                "source_requirements": _source_requirements([path0], evidence_kind="primary_evidence_source"),
                "evidence_needed": ["implementation_location"],
            },
            {
                "id": "q2",
                "question": f"What does the current evidence say about {label}?",
                "required": True,
                "source_hints": [path for path in [path1] if path],
                "source_requirements": _source_requirements([path1], evidence_kind="behavior_evidence_source"),
                "evidence_needed": ["behavior_evidence"],
            },
            {
                "id": "q3",
                "question": "What conclusion is justified from the inspected evidence?",
                "required": True,
                "source_hints": list(summary_paths),
                "evidence_needed": ["scope_of_claim"],
            },
            {
                "id": "q4",
                "question": "What remains unproven or uncertain?",
                "required": True,
                "source_hints": list(summary_paths),
                "evidence_needed": ["remaining_uncertainty"],
            },
        ]
    return [
        {
            "id": "q1",
            "question": f"What concrete evidence sources are relevant to: {objective}",
            "required": True,
            "source_hints": [path for path in [path0] if path],
            "source_requirements": _source_requirements([path0], evidence_kind="primary_evidence_source"),
            "evidence_needed": ["relevant_sources"],
        },
        {
            "id": "q2",
            "question": f"What does the inspected evidence show about: {objective}",
            "required": True,
            "source_hints": [path for path in [path1] if path],
            "source_requirements": _source_requirements([path1], evidence_kind="supporting_evidence_source"),
            "evidence_needed": ["supported_claim"],
        },
        {
            "id": "q3",
            "question": "What conclusion is justified?",
            "required": True,
            "source_hints": list(summary_paths),
            "evidence_needed": ["scope_of_claim"],
        },
        {
            "id": "q4",
            "question": "What remains unknown, blocked, or unproven?",
            "required": True,
            "source_hints": list(summary_paths),
            "evidence_needed": ["remaining_uncertainty"],
        },
    ]


def _default_must_inspect(*, allowed_paths: list[str], objective_spec: dict[str, Any] | None) -> list[str]:
    objective_spec = objective_spec if isinstance(objective_spec, dict) else {}
    target = objective_spec.get("target", {}) if isinstance(objective_spec.get("target", {}), dict) else {}
    required_paths = []
    for key in ("required_source_files", "required_test_files", "required_changed_files"):
        values = target.get(key, [])
        if isinstance(values, list):
            required_paths.extend(str(value) for value in values if str(value))
    required_paths.extend(str(path) for path in allowed_paths[:2] if str(path))
    seen: list[str] = []
    for path in required_paths:
        if path and path not in seen:
            seen.append(path)
    return seen


def _source_requirements(paths: list[str], *, evidence_kind: str) -> list[dict[str, Any]]:
    requirements: list[dict[str, Any]] = []
    for path in paths:
        if not path:
            continue
        requirements.append({
            "path": str(path),
            "evidence_kind": evidence_kind,
            "required": True,
            "prefetch": True,
        })
    return requirements
