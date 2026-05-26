"""Runtime-owned implementation coverage graph for A4/A5/A6 missions.

Extends ImplementationReadinessGraph with semantic coverage domains:
- target knowledge (what to change)
- source evidence (inspection of relevant files)
- change intent (desired state / patch recipe)
- verification coverage (test/verify plan)
- risk assessment (forbidden paths, critical paths)
- semantic quality (review ok)
"""

from __future__ import annotations

from typing import Any

JSON = dict[str, Any]


def build_implementation_coverage_graph(
    proposal: JSON,
    files: list[Any],
    mission: Any,
    checks: JSON,
    readiness_graph: JSON | None = None,
) -> JSON:
    spec = getattr(mission, "objective_spec", None)
    target = spec.get("target", {}) if isinstance(spec, dict) and isinstance(spec.get("target"), dict) else {}
    objective_type = str(spec.get("objective_type", "") or "") if isinstance(spec, dict) else ""
    changed_paths = {str(getattr(file, "path", "") or "") for file in files}
    verification_required = str(getattr(mission, "tier", "") or "") in {"A5", "A6"}

    categories: dict[str, JSON] = {}

    # ── target_knowledge ──
    target_files = _listify(target.get("required_changed_files", [])) + _listify(target.get("required_source_files", []))
    known_what = bool(target_files or target.get("target_behavior"))
    known_where = bool(target_files) or bool(changed_paths)
    target_obligations: list[JSON] = []
    for path in target_files:
        target_obligations.append({
            "id": f"target_file:{path}",
            "kind": "target_file",
            "required": True,
            "status": "satisfied" if path in changed_paths else "missing",
            "path": path,
            "reason": "" if path in changed_paths else f"target file not found in changed paths: {path}",
        })
    if not target_files:
        target_obligations.append({
            "id": "target_knowledge:no_files",
            "kind": "target_files_unspecified",
            "required": True,
            "status": "missing",
            "reason": "target files are not explicitly specified in objective_spec.target",
        })
    satisfied = [o for o in target_obligations if o.get("status") == "satisfied"]
    categories["target_knowledge"] = {
        "status": "covered" if satisfied else ("partial" if known_what or known_where else "missing"),
        "satisfied_count": len(satisfied),
        "total_count": max(len(target_obligations), 1),
        "obligations": target_obligations,
        "known_what_to_change": bool(target.get("target_behavior")) or bool(target_files),
        "known_where_to_change": bool(target_files) or bool(changed_paths),
        "reason": "Target knowledge is sufficient." if satisfied else "Target files or behavior are not fully specified.",
    }

    # ── source_evidence ──
    source_files = _listify(target.get("source_files", []))
    read_only_paths = _listify(getattr(mission, "read_only_paths", []))
    source_obligations: list[JSON] = []
    for path in set(source_files + read_only_paths):
        source_obligations.append({
            "id": f"source_evidence:{path}",
            "kind": "source_inspected",
            "required": True,
            "status": "satisfied" if path in changed_paths else "missing",
            "path": path,
            "reason": "" if path in changed_paths else f"source file not covered by patch: {path}",
        })
    if not source_obligations:
        source_obligations.append({
            "id": "source_evidence:no_sources",
            "kind": "source_files_unspecified",
            "required": False,
            "status": "satisfied",
            "reason": "No source files required for this mission.",
        })
    src_satisfied = [o for o in source_obligations if o.get("status") in {"satisfied", "waived"}]
    all_optional = all(not o.get("required") for o in source_obligations)
    categories["source_evidence"] = {
        "status": "covered" if (len(src_satisfied) >= len([o for o in source_obligations if o.get("required")]) or all_optional) else "partial",
        "satisfied_count": len(src_satisfied),
        "total_count": len(source_obligations),
        "obligations": source_obligations,
        "reason": "Source files are covered by patch." if src_satisfied or all_optional else "Some source files are not covered.",
    }

    # ── change_intent ──
    proposal_source = str(proposal.get("proposal_source", "") or "raw_patch_proposal_v1")
    is_runtime_built = proposal_source in {"desired_state_v1", "patch_recipe_v1", "patch_intent_v1"}
    has_desired_state = isinstance(proposal.get("desired_state"), dict)
    has_patch_recipe = isinstance(proposal.get("patch_recipe"), dict)
    has_patch_intent = (
        isinstance(proposal.get("patch_intent"), dict)
        or str(proposal.get("patch_intent_version", "") or "") == "1.0"
    )
    has_any_intent_source = has_desired_state or has_patch_recipe or has_patch_intent
    intent_required = is_runtime_built or has_any_intent_source
    intent_obligations: list[JSON] = [
        {
            "id": "change_intent:runtime_source",
            "kind": "runtime_intent_source",
            "required": intent_required,
            "status": "satisfied" if has_any_intent_source else ("missing" if intent_required else "waived"),
            "reason": "" if has_any_intent_source else ("No DesiredStateV1, PatchRecipeV1, or PatchIntentV1 source was preserved." if intent_required else "Not required for raw patch proposals."),
        },
        {
            "id": "change_intent:desired_state",
            "kind": "desired_state",
            "required": False,
            "status": "satisfied" if has_desired_state else "waived",
            "reason": "" if has_desired_state else "DesiredStateV1 was not the selected intent source.",
        },
        {
            "id": "change_intent:patch_recipe",
            "kind": "patch_recipe",
            "required": False,
            "status": "satisfied" if has_patch_recipe else "waived",
            "reason": "" if has_patch_recipe else "PatchRecipeV1 was not the selected intent source.",
        },
        {
            "id": "change_intent:patch_intent",
            "kind": "patch_intent",
            "required": False,
            "status": "satisfied" if has_patch_intent else "waived",
            "reason": "" if has_patch_intent else "PatchIntentV1 was not the selected intent source.",
        },
    ]
    intent_satisfied = [o for o in intent_obligations if o.get("status") in {"satisfied", "waived"}]
    intent_required_count = len([o for o in intent_obligations if o.get("required")])
    categories["change_intent"] = {
        "status": "covered" if len(intent_satisfied) >= intent_required_count else ("partial" if intent_satisfied else "missing"),
        "satisfied_count": len(intent_satisfied),
        "total_count": intent_required_count or 1,
        "obligations": intent_obligations,
        "has_desired_state": has_desired_state,
        "has_patch_recipe": has_patch_recipe,
        "has_patch_intent": has_patch_intent,
        "has_any_intent_source": has_any_intent_source,
        "intent_source": (
            "desired_state_v1" if has_desired_state else
            "patch_recipe_v1" if has_patch_recipe else
            "patch_intent_v1" if has_patch_intent else
            "raw_patch_proposal_v1"
        ),
        "reason": "Change intent is fully specified." if len(intent_satisfied) >= intent_required_count else "Change intent is incomplete.",
    }

    # ── verification_coverage ──
    verification_ok = bool(checks.get("verification_plan_ok", False))
    verify_obligations: list[JSON] = [
        {
            "id": "verification_coverage:plan",
            "kind": "verification_plan",
            "required": verification_required,
            "status": "satisfied" if verification_ok else ("blocked" if verification_required else "waived"),
            "reason": "" if verification_ok or not verification_required else "Verification plan is required but missing for A5/A6.",
        },
    ]
    verify_satisfied = [o for o in verify_obligations if o.get("status") == "satisfied"]
    categories["verification_coverage"] = {
        "status": "covered" if verify_satisfied or not verification_required else "missing",
        "satisfied_count": len(verify_satisfied),
        "total_count": len(verify_obligations),
        "obligations": verify_obligations,
        "verification_required": verification_required,
        "reason": "Verification is covered." if verify_satisfied or not verification_required else "Verification plan is missing.",
    }

    # ── risk_assessment ──
    forbidden = _listify(target.get("forbidden_changed_files", []))
    forbidden_violated = [f for f in forbidden if f in changed_paths]
    critical_touched = bool(checks.get("critical_paths_touched", False))
    critical_write_allowed = bool(getattr(mission, "critical_path_write_allowed", False))
    risk_obligations: list[JSON] = [
        {
            "id": "risk:forbidden_paths",
            "kind": "forbidden_path_check",
            "required": True,
            "status": "contradicted" if forbidden_violated else "satisfied",
            "reason": f"Forbidden path changed: {forbidden_violated[0]}" if forbidden_violated else "No forbidden paths changed.",
        },
        {
            "id": "risk:critical_paths",
            "kind": "critical_path_check",
            "required": True,
            "status": "contradicted" if (critical_touched and not critical_write_allowed) else "satisfied",
            "reason": "Critical paths touched without write permission." if (critical_touched and not critical_write_allowed) else ("Critical paths touched with explicit write permission." if critical_touched else "No critical paths changed."),
        },
    ]
    risk_satisfied = [o for o in risk_obligations if o.get("status") == "satisfied"]
    categories["risk_assessment"] = {
        "status": "clean" if len(risk_satisfied) == len(risk_obligations) else "contradicted",
        "satisfied_count": len(risk_satisfied),
        "total_count": len(risk_obligations),
        "obligations": risk_obligations,
        "forbidden_paths_violated": forbidden_violated,
        "critical_paths_touched": critical_touched,
        "reason": "Risk assessment is clean." if len(risk_satisfied) == len(risk_obligations) else "Risk obligations are violated.",
    }

    # ── semantic_quality ──
    semantic_ok = bool(checks.get("semantic_review_ok", False))
    semantic_score = int(checks.get("semantic_review_score", 0) or 0)
    semantic_obligations: list[JSON] = [
        {
            "id": "semantic_quality:review",
            "kind": "semantic_review",
            "required": True,
            "status": "satisfied" if semantic_ok else "contradicted",
            "score": semantic_score,
            "reason": "" if semantic_ok else f"Semantic review did not pass (score={semantic_score}).",
        },
    ]
    sem_satisfied = [o for o in semantic_obligations if o.get("status") == "satisfied"]
    categories["semantic_quality"] = {
        "status": "covered" if sem_satisfied else "contradicted",
        "satisfied_count": len(sem_satisfied),
        "total_count": len(semantic_obligations),
        "obligations": semantic_obligations,
        "semantic_review_score": semantic_score,
        "reason": "Semantic review passed." if sem_satisfied else f"Semantic review failed (score={semantic_score}).",
    }

    # ── overall assessment ──
    all_obligations = []
    for cat in categories.values():
        all_obligations.extend(cat.get("obligations", []) or [])
    required = [o for o in all_obligations if o.get("required", True)]
    required_satisfied = [o for o in required if o.get("status") in {"satisfied", "waived"}]
    missing_coverage = [
        {"category": cat_name, "obligation_id": o["id"], "reason": o.get("reason", "")}
        for cat_name, cat_data in categories.items()
        for o in (cat_data.get("obligations", []) or [])
        if o.get("status") not in {"satisfied", "waived"}
    ]

    category_statuses = {
        cat_name: str(cat_data.get("status", "") or "")
        for cat_name, cat_data in categories.items()
    }
    critical_violations = [
        cat_name for cat_name, status in category_statuses.items()
        if status in {"missing", "contradicted"}
    ]

    coverage_score = len(required_satisfied) / max(len(required), 1)

    can_propose = (
        coverage_score >= 0.6
        and not forbidden_violated
        and not critical_touched
        and category_statuses.get("semantic_quality") != "contradicted"
        and category_statuses.get("change_intent") != "missing"
        and (category_statuses.get("verification_coverage") != "missing" or not verification_required)
    )

    if not can_propose:
        if forbidden_violated or critical_touched:
            recommended_status = "ESCALATE"
        elif category_statuses.get("change_intent") == "missing":
            recommended_status = "INVALID"
        elif coverage_score < 0.6:
            recommended_status = "INVALID"
        else:
            recommended_status = "PARTIAL"
    elif coverage_score >= 0.9:
        recommended_status = "VALID"
    else:
        recommended_status = "VALID_WITH_CAVEAT"

    next_actions = []
    if category_statuses.get("change_intent") in {"missing", "partial"}:
        next_actions.append({"action": "specify_change_intent", "detail": "Add DesiredStateV1, PatchRecipeV1, or PatchIntentV1 to the proposal."})
    if category_statuses.get("source_evidence") == "missing":
        next_actions.append({"action": "inspect_source_files", "detail": "Read the relevant source files before proposing."})
    if category_statuses.get("target_knowledge") in {"missing", "partial"}:
        next_actions.append({"action": "clarify_target", "detail": "Specify target files or behavior in the objective_spec."})
    if category_statuses.get("verification_coverage") == "missing":
        next_actions.append({"action": "add_verification", "detail": "Add a verification plan (allowed commands for A5/A6)."})

    return {
        "implementation_coverage_graph_version": "1.0",
        "mission_id": str(getattr(mission, "mission_id", "") or ""),
        "tier": str(getattr(mission, "tier", "") or ""),
        "objective_type": objective_type,
        "overall_coverage_score": round(coverage_score, 2),
        "can_propose": can_propose,
        "recommended_status": recommended_status,
        "categories": categories,
        "category_statuses": category_statuses,
        "critical_violations": critical_violations,
        "missing_coverage": missing_coverage,
        "next_actions": next_actions,
    }


def _listify(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [value]
    return []
