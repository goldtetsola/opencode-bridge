"""AnswerObligationGraphV1 + CoverageGraphV1 for open investigations."""

from __future__ import annotations

import json
import hashlib
import os
import re
from typing import Any

from codex_oss.evidence_shapes import detect_registered_shapes


def build_investigation_plan(mission: Any) -> dict[str, Any]:
    obligations = list(getattr(mission, "answer_obligations", []) or [])
    if not obligations and str(getattr(mission, "objective_style", "") or "") == "open_investigation":
        obligations = _fallback_obligations(mission)
    normalized = [_normalize_obligation(item) for item in obligations]
    required = [dict(item) for item in normalized if bool(item.get("required", True))]
    optional = [dict(item) for item in normalized if not bool(item.get("required", True))]
    likely_paths = list(dict.fromkeys(
        list(getattr(mission, "must_inspect", []) or [])
        + list(getattr(mission, "allowed_paths", []) or [])
        + list(getattr(mission, "read_only_paths", []) or [])
    ))
    return {
        "investigation_plan_version": "1.0",
        "mission_id": str(getattr(mission, "mission_id", "") or ""),
        "mission_question": str(getattr(mission, "objective", "") or ""),
        "required_obligations": required,
        "optional_obligations": optional,
        "must_inspect": list(getattr(mission, "must_inspect", []) or []),
        "likely_paths": likely_paths,
    }


def refresh_answer_graph(
    mission: Any,
    ledger: Any,
    *,
    claim_graph: dict[str, Any] | None = None,
    report: dict[str, Any] | None = None,
    reason: str = "observation",
    persist: bool = False,
) -> dict[str, Any]:
    del report  # reserved for future synthesis-aware refresh hooks
    plan = build_investigation_plan(mission)
    claims = list((claim_graph or getattr(ledger, "claim_graph", {}) or {}).get("claims", []) or getattr(ledger, "claims", []) or [])
    command_refs = _command_refs(ledger)
    file_entries = getattr(ledger, "files_inspected", {}) or {}
    inspected_paths = set(file_entries.keys())
    obligation_views = []
    agenda_items = []
    declared_requirement_paths: set[str] = set()

    for raw in list(plan.get("required_obligations", []) or []) + list(plan.get("optional_obligations", []) or []):
        obligation = _normalize_obligation(raw)
        requirement_views = []
        for requirement in list(obligation.get("source_requirements", []) or []):
            requirement_views.append(_source_requirement_view(requirement, inspected_paths, command_refs, file_entries))
            declared_requirement_paths.add(str(requirement.get("path", "") or ""))
        evidence_refs = _obligation_evidence_refs(obligation, claims, ledger, command_refs, requirement_views)
        linked_claims = _obligation_claim_ids(obligation, claims, requirement_views)
        missing_evidence = _missing_evidence_for_requirements(requirement_views)
        status = _obligation_status(obligation, linked_claims, evidence_refs, missing_evidence, requirement_views, ledger)
        confidence = _obligation_confidence(status, evidence_refs)
        obligation_view = {
            **obligation,
            "claims": linked_claims,
            "evidence_refs": evidence_refs,
            "missing_evidence": missing_evidence,
            "status": status,
            "confidence": confidence,
            "source_requirements": requirement_views,
        }
        obligation_views.append(obligation_view)
        agenda_items.extend(_agenda_items_for_obligation(obligation_view))

    for path in list(plan.get("must_inspect", []) or []):
        if not path or path in declared_requirement_paths:
            continue
        agenda_items.append({
            "id": f"agenda_global_{_slug(path)}",
            "kind": "required_read",
            "path": path,
            "obligation_id": "global_must_inspect",
            "priority": "high",
            "status": "done" if path in inspected_paths else "pending",
            "source_state": "read_satisfied" if path in inspected_paths else "missing",
            "reason": "Mission declared this path as must_inspect.",
            "prefetch": True,
        })

    agenda_items = _dedupe_agenda(agenda_items)
    coverage_status = _coverage_status(obligation_views, agenda_items)
    source_states = _source_states_from_obligations(obligation_views)
    source_state_hash = _source_state_hash(source_states)
    coverage_graph = {
        "coverage_graph_version": "1.0",
        "mission_id": str(getattr(mission, "mission_id", "") or ""),
        "nodes": obligation_views,
        "next_required_actions": _next_required_actions(agenda_items),
        "coverage_status": coverage_status,
        "source_states": source_states,
        "source_state_hash": source_state_hash,
    }
    evidence_agenda = {
        "agenda_version": "1.0",
        "mission_id": str(getattr(mission, "mission_id", "") or ""),
        "items": agenda_items,
    }
    sufficiency = evaluate_answer_sufficiency(
        mission,
        obligation_views,
        ledger=ledger,
        agenda_items=agenda_items,
    )
    graph = {
        "answer_graph_version": "1.0",
        "mission_id": str(getattr(mission, "mission_id", "") or ""),
        "mission_question": plan.get("mission_question", ""),
        "required_obligations": [item for item in obligation_views if item.get("required", True)],
        "optional_obligations": [item for item in obligation_views if not item.get("required", True)],
        "must_inspect": list(plan.get("must_inspect", []) or []),
        "likely_paths": list(plan.get("likely_paths", []) or []),
        "last_updated_reason": reason,
        "coverage_graph": coverage_graph,
        "evidence_agenda": evidence_agenda,
        "sufficiency": sufficiency,
        "source_states": source_states,
        "source_state_hash": source_state_hash,
        "investigation_state": {
            "phase": _phase_from_obligations(obligation_views, agenda_items, ledger, sufficiency),
            "enough_evidence_to_report": bool(sufficiency.get("can_close")),
            "remaining_uncertainties": list(sufficiency.get("open_high_priority_questions", []) or []),
        },
    }
    ledger.answer_graph = dict(graph)
    ledger.coverage_graph = dict(coverage_graph)
    ledger.evidence_agenda = dict(evidence_agenda)
    if persist:
        project_root = os.getcwd()
        mission_id = getattr(mission, "mission_id", "mission_unknown")
        write_answer_graph(project_root, mission_id, graph)
        write_investigation_plan(project_root, mission_id, plan)
        write_coverage_graph(project_root, mission_id, coverage_graph)
        write_evidence_agenda(project_root, mission_id, evidence_agenda)
    return graph


def check_partial_evidence_entitlement(obligation: dict[str, Any], ledger: Any) -> dict[str, Any]:
    """Evaluate whether partial evidence entitles this obligation to COMPLETE.

    Returns a dict with:
      complete_allowed: bool
      reason: str
      confidence_cap: str
      partial_sources: list of paths with partial evidence
    """
    source_requirements = list(obligation.get("source_requirements", []) or [])
    if not source_requirements:
        return {"complete_allowed": True, "reason": "No source requirements — obligation status alone determines entitlement.",
                "confidence_cap": "HIGH", "partial_sources": []}

    partial_sources = []
    for req in source_requirements:
        path = str(req.get("path", "") or "")
        if not path:
            continue
        file_entry = (getattr(ledger, "files_inspected", {}) or {}).get(path)
        if file_entry is None:
            continue
        if not getattr(file_entry, "complete", False):
            partial_sources.append({
                "path": path,
                "completeness_policy": str(req.get("completeness_policy", "shape_sufficient") or "shape_sufficient"),
                "required_shapes_found": list(req.get("detected_shapes", []) or []),
                "missing_shapes": list(req.get("missing_shapes", []) or []),
                "status": str(req.get("status", "") or ""),
            })

    if not partial_sources:
        return {"complete_allowed": True, "reason": "All source files were fully extracted.",
                "confidence_cap": "HIGH", "partial_sources": []}

    # Evaluate each partial source
    blocked = False
    for ps in partial_sources:
        policy = ps.get("completeness_policy", "shape_sufficient")
        if policy == "whole_file_required":
            # Partial extract is never enough
            return {
                "complete_allowed": False,
                "reason": f"File {ps['path']} requires whole_file_required completeness but was partially extracted.",
                "confidence_cap": "LOW",
                "partial_sources": [{"path": p["path"], "policy": p["completeness_policy"]} for p in partial_sources],
            }
        if policy == "negative_proof_required":
            return {
                "complete_allowed": False,
                "reason": f"Negative proof requires full scope search; partial extract of {ps['path']} is insufficient.",
                "confidence_cap": "LOW",
                "partial_sources": [{"path": p["path"], "policy": p["completeness_policy"]} for p in partial_sources],
            }
        if policy == "scope_search_required":
            return {
                "complete_allowed": False,
                "reason": f"Scope search required but file {ps['path']} was only partially extracted.",
                "confidence_cap": "LOW",
                "partial_sources": [{"path": p["path"], "policy": p["completeness_policy"]} for p in partial_sources],
            }
        if policy == "shape_sufficient" or policy == "range_sufficient":
            if ps.get("missing_shapes"):
                # Required shapes not found in partial extract — insufficient
                return {
                    "complete_allowed": False,
                    "reason": f"File {ps['path']} was partially extracted and missing required shapes: {ps['missing_shapes']}.",
                    "confidence_cap": "LOW",
                    "partial_sources": [{"path": p["path"], "policy": p["completeness_policy"]} for p in partial_sources],
                }
            # Shape is present — partial is sufficient but cap confidence
            continue

    if blocked:
        return {"complete_allowed": False, "reason": "Partial evidence not sufficient.",
                "confidence_cap": "LOW", "partial_sources": partial_sources}

    # All partial sources are entitled
    return {
        "complete_allowed": True,
        "reason": f"Partial evidence is entitled under shape_sufficient/range_sufficient policy (sources: {[p['path'] for p in partial_sources]}).",
        "confidence_cap": "MEDIUM",
        "partial_sources": partial_sources,
    }


def evaluate_answer_sufficiency(
    mission: Any,
    obligations: list[dict[str, Any]],
    *,
    ledger: Any | None = None,
    agenda_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    policy = dict(getattr(mission, "sufficiency_policy", {}) or {})
    required = [item for item in obligations if item.get("required", True)]
    answered = [item for item in required if item.get("status") == "answered"]
    partial = [item for item in required if item.get("status") in {"partially_answered", "blocked_on_evidence"}]
    contradicted = [item for item in required if item.get("status") == "contradicted"]
    blocked = [item for item in required if item.get("status") == "blocked_source"]
    insufficient = [item for item in required if item.get("status") == "insufficient_evidence"]
    open_required = [item for item in required if item.get("status") not in {"answered", "blocked", "out_of_scope"}]
    agenda_items = list(agenda_items or [])
    pending_required_sources = [
        str(item.get("path", "") or "")
        for item in agenda_items
        if item.get("kind") == "required_read"
        and item.get("status") == "pending"
        and str(item.get("source_state", "") or "") == "missing"
    ]
    partial_extracts = any(
        not bool(getattr(entry, "complete", False))
        for entry in (getattr(ledger, "files_inspected", {}) or {}).values()
    ) if ledger is not None else False
    confidence_cap = str(
        policy.get("confidence_cap_if_partial_extracts", "LOW") if partial_extracts else "MEDIUM"
    ).upper()
    has_useful_evidence = bool(
        answered
        or partial
        or contradicted
        or blocked
        or insufficient
        or getattr(ledger, "claims", [])
        or getattr(ledger, "files_inspected", {})
        or getattr(ledger, "commands_run", [])
    )
    can_close = not open_required and not pending_required_sources and not contradicted and not blocked and not insufficient and has_useful_evidence
    partial_entitlement = None
    if can_close and partial_extracts:
        # Check partial evidence entitlement for answered obligations
        partial_entitlement = {}
        for obl in answered:
            entitlement = check_partial_evidence_entitlement(obl, ledger)
            if not entitlement["complete_allowed"]:
                can_close = False
                partial_entitlement = entitlement
                break
            partial_entitlement = entitlement
        if not partial_entitlement:
            partial_entitlement = {"complete_allowed": True, "reason": "No partial extracts affecting answered obligations."}
    if contradicted or blocked:
        recommended_status = "ESCALATE"
    elif can_close:
        recommended_status = "COMPLETE"
    elif has_useful_evidence:
        recommended_status = "PARTIAL"
    else:
        recommended_status = "FAILED"
    reason = "All required obligations and required sources are satisfied." if can_close else (
        "Required evidence contradicts itself." if contradicted else
        "A required evidence source is blocked or unreadable." if blocked else
        "Partial evidence does not satisfy completeness requirements." if partial_entitlement and not partial_entitlement.get("complete_allowed", True) else
        "Required evidence was read but the required evidence shape was not found." if insufficient else
        "Required evidence sources remain uninspected."
        if pending_required_sources
        else "Required answer obligations remain unanswered."
        if open_required
        else "No useful evidence gathered yet."
    )
    open_questions: list[str] = []
    reason_code = _sufficiency_reason_code(
        contradicted=contradicted, blocked=blocked, insufficient=insufficient,
        pending_required=pending_required_sources, open_required=open_required,
        partial_unentitled=partial_entitlement and not partial_entitlement.get("complete_allowed", True),
        can_close=can_close,
    )
    return {
        "sufficiency_version": "2.0",
        "can_close": can_close,
        "recommended_status": recommended_status,
        "required_answered": len(answered),
        "required_total": len(required),
        "partially_answered": len(partial),
        "missing_must_inspect": list(dict.fromkeys(pending_required_sources)),
        "missing_required_sources": list(dict.fromkeys(pending_required_sources)),
        "contradicted_obligations": [str(item.get("id", "") or "") for item in contradicted],
        "blocked_obligations": [str(item.get("id", "") or "") for item in blocked],
        "insufficient_evidence_obligations": [str(item.get("id", "") or "") for item in insufficient],
        "open_high_priority_questions": open_questions,
        "confidence_cap": partial_entitlement.get("confidence_cap", confidence_cap) if partial_entitlement else confidence_cap,
        "partial_extracts": partial_extracts,
        "partial_evidence_entitlement": partial_entitlement,
        "reason": reason,
        "reason_code": reason_code,
        "closure_entitlement": {
            "can_return_complete": can_close and recommended_status == "COMPLETE",
            "reason": reason,
            "reason_code": reason_code,
            "partial_evidence_entitled": partial_entitlement.get("complete_allowed", True) if partial_entitlement else True,
        },
        "next_required_actions": _next_required_actions(agenda_items),
    }


def build_runtime_report_from_answer_graph(
    mission: Any,
    ledger: Any,
    answer_graph: dict[str, Any],
    *,
    reason: str,
    report_source: str = "runtime_answer_graph",
) -> dict[str, Any]:
    sufficiency = dict(answer_graph.get("sufficiency", {}) or {})
    findings = _findings_from_answer_graph(answer_graph, ledger)
    obligations = list(answer_graph.get("required_obligations", []) or []) + list(answer_graph.get("optional_obligations", []) or [])
    unanswered = [
        item.get("question", "")
        for item in obligations
        if item.get("status") not in {"answered", "blocked", "out_of_scope"}
    ]
    answered_obligations = [
        {
            "id": str(item.get("id", "") or ""),
            "question": str(item.get("question", "") or ""),
            "status": str(item.get("status", "") or ""),
        }
        for item in obligations
        if item.get("status") == "answered"
    ]
    missing_sources = list(sufficiency.get("missing_required_sources", []) or [])
    contradicted = list(sufficiency.get("contradicted_obligations", []) or [])
    blocked = list(sufficiency.get("blocked_obligations", []) or [])
    insufficient = list(sufficiency.get("insufficient_evidence_obligations", []) or [])
    caveats = [str(reason)]
    if unanswered:
        caveats.append("Some answer obligations remain unresolved.")
    if missing_sources:
        caveats.append("Some required evidence sources were not inspected.")
    if contradicted:
        caveats.append("At least one required obligation has contradictory evidence.")
    if blocked:
        caveats.append("At least one required evidence source was blocked or unreadable.")
    if insufficient:
        caveats.append("At least one required evidence source was read but did not satisfy the required evidence shape.")
    if sufficiency.get("partial_extracts"):
        entitlement = sufficiency.get("partial_evidence_entitlement") or {}
        if entitlement.get("complete_allowed"):
            caveats.append("Some evidence sources were partially extracted but required evidence shapes were present.")
        else:
            caveats.append("Some evidence sources were partially extracted and did not satisfy completeness requirements.")
    if str(getattr(mission, "objective_style", "") or "") == "open_investigation":
        caveats.append("Report rendered from runtime answer graph.")
    report = {
        "oss_report_version": "1.0",
        "mission_id": str(getattr(mission, "mission_id", "") or ""),
        "status": str(sufficiency.get("recommended_status", "PARTIAL") or "PARTIAL"),
        "confidence": str(sufficiency.get("confidence_cap", "LOW") or "LOW"),
        "files_inspected": _files_payload(ledger),
        "commands_run": _commands_payload(ledger),
        "findings": findings,
        "uncertainties": unanswered,
        "answered_obligations": answered_obligations,
        "missing_required_sources": missing_sources,
        "contradicted_obligations": contradicted,
        "blocked_obligations": blocked,
        "insufficient_evidence_obligations": insufficient,
        "source_states": list(answer_graph.get("source_states", []) or []),
        "source_state_hash": str(answer_graph.get("source_state_hash", "") or ""),
        "next_required_actions": list(sufficiency.get("next_required_actions", []) or []),
        "caveats": list(dict.fromkeys(caveats)),
        "escalation_recommendation": "GPT-5.5 review required",
        "missing_fields": list(dict.fromkeys(
            list(sufficiency.get("open_high_priority_questions", []) or [])
            + [f"inspect:{path}" for path in missing_sources]
            + _typed_insufficiency_fields(answer_graph)
        )),
        "report_source": report_source,
        "closure_source": report_source,
        "answer_graph_summary": {
            "required_answered": sufficiency.get("required_answered", 0),
            "required_total": sufficiency.get("required_total", 0),
            "can_close": sufficiency.get("can_close", False),
            "reason": sufficiency.get("reason", ""),
            "partial_evidence_entitlement": sufficiency.get("partial_evidence_entitlement"),
            "source_state_hash": str(answer_graph.get("source_state_hash", "") or ""),
        },
    }
    return report


def write_answer_graph(project_root: str, mission_id: str, graph: dict[str, Any]) -> None:
    _write_json_artifact(project_root, mission_id, "answer_graph.json", graph)


def write_investigation_plan(project_root: str, mission_id: str, plan: dict[str, Any]) -> None:
    _write_json_artifact(project_root, mission_id, "investigation_plan.json", plan)


def write_coverage_graph(project_root: str, mission_id: str, graph: dict[str, Any]) -> None:
    _write_json_artifact(project_root, mission_id, "coverage_graph.json", graph)


def write_evidence_agenda(project_root: str, mission_id: str, agenda: dict[str, Any]) -> None:
    _write_json_artifact(project_root, mission_id, "evidence_agenda.json", agenda)


def pending_required_agenda_items(answer_graph: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in ((answer_graph.get("evidence_agenda", {}) or {}).get("items", []) or [])
        if str(item.get("kind", "") or "").startswith("required_") and item.get("status") != "done"
    ]


def _write_json_artifact(project_root: str, mission_id: str, filename: str, payload: dict[str, Any]) -> None:
    path = os.path.join(project_root, ".codex-oss", "missions", mission_id, filename)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)


def _normalize_obligation(raw: dict[str, Any]) -> dict[str, Any]:
    source_hints = [str(item) for item in (raw.get("source_hints", []) or []) if str(item)]
    source_requirements = []
    for item in (raw.get("source_requirements", []) or []):
        if isinstance(item, str):
            source_requirements.append({
                "path": str(item),
                "evidence_kind": "source_read",
                "required": True,
                "prefetch": True,
            })
            continue
        if isinstance(item, dict):
            path = str(item.get("path", "") or "")
            if not path:
                continue
            source_requirements.append({
                "path": path,
                "evidence_kind": str(item.get("evidence_kind", "source_read") or "source_read"),
                "required": bool(item.get("required", True)),
                "prefetch": bool(item.get("prefetch", True)),
                "pattern": str(item.get("pattern", "") or ""),
                "command_requirement": dict(item.get("command_requirement", {}) or {}),
                "required_shapes": [str(shape) for shape in (item.get("required_shapes", []) or []) if str(shape)],
                "shape_patterns": dict(item.get("shape_patterns", {}) or {}) if isinstance(item.get("shape_patterns"), dict) else {},
                "contradiction_markers": [str(marker) for marker in (item.get("contradiction_markers", []) or []) if str(marker)],
                "completeness_policy": str(item.get("completeness_policy", "") or "") or "shape_sufficient",
                "shape_match_policy": str(item.get("shape_match_policy", "") or "") or "all",
                "evidence_plane": str(item.get("evidence_plane", "") or "") or "file_content",
            })
    return {
        "id": str(raw.get("id", "q")),
        "question": str(raw.get("question", "") or ""),
        "required": bool(raw.get("required", True)),
        "source_hints": source_hints,
        "source_requirements": source_requirements,
        "evidence_needed": [str(item) for item in (raw.get("evidence_needed", []) or []) if str(item)],
    }


def _fallback_obligations(mission: Any) -> list[dict[str, Any]]:
    likely = list(getattr(mission, "allowed_paths", []) or [])[:2]
    return [
        {
            "id": "q1",
            "question": "What concrete evidence source is most relevant to this mission?",
            "required": True,
            "source_hints": likely[:1],
            "source_requirements": _default_source_requirements(likely[:1], "relevant_source"),
        },
        {
            "id": "q2",
            "question": "What does the current evidence show?",
            "required": True,
            "source_hints": likely[1:2] or likely[:1],
            "source_requirements": _default_source_requirements(likely[1:2] or likely[:1], "supporting_source"),
        },
        {"id": "q3", "question": "What conclusion is justified?", "required": True, "source_hints": likely},
        {"id": "q4", "question": "What remains unknown or unproven?", "required": True, "source_hints": likely},
    ]


def _source_requirement_view(requirement: dict[str, Any], inspected_paths: set[str], command_refs: list[dict[str, Any]], file_entries: dict[str, Any] | None = None) -> dict[str, Any]:
    path = str(requirement.get("path", "") or "")
    evidence_plane = _evidence_plane(requirement)
    command_requirement = dict(requirement.get("command_requirement", {}) or {})
    pattern = str(requirement.get("pattern", "") or command_requirement.get("pattern", "") or "")
    evidence_refs = []
    failed_reads = []
    successful_read = False
    for item in command_refs:
        if path and path == item.get("path"):
            if evidence_plane == "command_result":
                evidence_refs.append(str(item.get("ref")))
                continue
            if int(item.get("exit_code", 0) or 0) == 0:
                successful_read = True
                evidence_refs.append(str(item.get("ref")))
            else:
                failed_reads.append(str(item.get("ref")))
    file_entry = (file_entries or {}).get(path)
    file_read_success = bool(
        file_entry is not None
        and getattr(file_entry, "complete", False)
        and os.path.exists(path)
        and (getattr(file_entry, "cached_text", "") or getattr(file_entry, "sha256", ""))
    )
    if file_read_success:
        successful_read = True
        evidence_refs.append(f"file:{path}#extract:1")
    required_shapes = [str(shape) for shape in (requirement.get("required_shapes", []) or []) if str(shape)]
    detected_shapes = _detected_shapes_for_path(path, requirement) if evidence_plane != "command_result" else _detected_command_shapes(path, command_refs)
    shape_match_policy = str(requirement.get("shape_match_policy", "") or "") or "all"
    contradiction_markers = [str(marker) for marker in (requirement.get("contradiction_markers", []) or []) if str(marker)]
    matched_contradictions = _matched_contradiction_markers(path, contradiction_markers)
    missing_shapes = [shape for shape in required_shapes if shape not in detected_shapes]
    shape_satisfied = (
        not required_shapes or
        (shape_match_policy == "any" and bool(detected_shapes)) or
        (shape_match_policy == "all" and not missing_shapes)
    )
    if matched_contradictions:
        status = "contradicted"
        source_state = "contradicted"
    elif failed_reads and not evidence_refs:
        status = "blocked_source"
        source_state = "blocked"
    elif not evidence_refs:
        status = "missing"
        source_state = "missing"
    elif not shape_satisfied:
        status = "insufficient_evidence"
        source_state = "read_insufficient_shape"
    else:
        status = "satisfied"
        source_state = "read_satisfied"
    completeness_policy = str(requirement.get("completeness_policy", "") or "") or "shape_sufficient"
    return {
        "path": path,
        "evidence_kind": str(requirement.get("evidence_kind", "source_read") or "source_read"),
        "evidence_plane": evidence_plane,
        "pattern": pattern,
        "command_requirement": command_requirement,
        "required": bool(requirement.get("required", True)),
        "prefetch": bool(requirement.get("prefetch", True)),
        "required_shapes": required_shapes,
        "shape_patterns": dict(requirement.get("shape_patterns", {}) or {}) if isinstance(requirement.get("shape_patterns"), dict) else {},
        "detected_shapes": detected_shapes,
        "missing_shapes": missing_shapes,
        "shape_match_policy": shape_match_policy,
        "shape_satisfied": shape_satisfied,
        "contradiction_markers": contradiction_markers,
        "matched_contradictions": matched_contradictions,
        "completeness_policy": completeness_policy,
        "status": status,
        "source_state": source_state,
        "source_state_v1": {
            "schema_version": "source_state.v1",
            "path": path,
            "state": source_state,
            "status": status,
            "evidence_refs": list(dict.fromkeys(evidence_refs + failed_reads)),
            "required_shapes": required_shapes,
            "detected_shapes": detected_shapes,
            "missing_shapes": missing_shapes,
            "blocked_refs": failed_reads if source_state == "blocked" else [],
            "contradiction_markers": matched_contradictions,
        },
        "evidence_refs": list(dict.fromkeys(evidence_refs + failed_reads)),
    }


def _evidence_plane(requirement: dict[str, Any]) -> str:
    return str(requirement.get("evidence_plane", "") or "") or "file_content"


def _detected_command_shapes(path: str, command_refs: list[dict[str, Any]]) -> list[str]:
    """Detect evidence shapes from command results (rtk_grep, etc.)"""
    shapes = []
    for ref in command_refs:
        if path and path != ref.get("path"):
            continue
        tool_name = str(ref.get("tool_name", "") or "")
        if tool_name not in ("rtk_grep", "grep"):
            continue
        matches = int(ref.get("matches_count", -1))
        exit_code = int(ref.get("exit_code", 0) or 0)
        if matches == 0 or (matches < 0 and exit_code != 0):
            shapes.append("zero_match")
    return shapes


def _obligation_evidence_refs(
    obligation: dict[str, Any],
    claims: list[dict[str, Any]],
    ledger: Any,
    command_refs: list[dict[str, Any]],
    requirement_views: list[dict[str, Any]],
) -> list[str]:
    refs: list[str] = []
    source_hints = list(obligation.get("source_hints", []) or [])
    text_tokens = _tokens(obligation.get("question", ""))
    for claim in claims:
        claim_text = str(claim.get("text", "") or "")
        claim_refs = [str(ref) for ref in (claim.get("evidence_refs", []) or []) if str(ref)]
        if source_hints and not any(hint in claim_text for hint in source_hints) and not any(hint in ref for hint in source_hints for ref in claim_refs):
            if not (_tokens(claim_text) & text_tokens):
                continue
        refs.extend(claim_refs)
    for requirement in requirement_views:
        refs.extend(str(ref) for ref in (requirement.get("evidence_refs", []) or []) if str(ref))
    for hint in source_hints:
        if hint in (getattr(ledger, "files_inspected", {}) or {}):
            refs.append(f"file:{hint}#extract:1")
        for item in command_refs:
            if hint and hint == item.get("path"):
                refs.append(str(item.get("ref")))
    return list(dict.fromkeys(refs))


def _obligation_claim_ids(obligation: dict[str, Any], claims: list[dict[str, Any]], requirement_views: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    source_hints = list(obligation.get("source_hints", []) or [])
    text_tokens = _tokens(obligation.get("question", ""))
    question_lower = str(obligation.get("question", "") or "").lower()
    broad_summary_question = any(term in question_lower for term in ("conclusion", "justify", "unknown", "unproven", "current evidence", "remains"))
    requirement_paths = [str(item.get("path", "") or "") for item in requirement_views if str(item.get("path", "") or "")]
    for claim in claims:
        claim_text = str(claim.get("text", "") or "")
        claim_refs = [str(ref) for ref in (claim.get("evidence_refs", []) or []) if str(ref)]
        if broad_summary_question and claim_refs:
            ids.append(str(claim.get("claim_id", "")))
            continue
        if source_hints and any(hint in claim_text for hint in source_hints):
            ids.append(str(claim.get("claim_id", "")))
            continue
        if requirement_paths and any(path in ref for path in requirement_paths for ref in claim_refs):
            ids.append(str(claim.get("claim_id", "")))
            continue
        if text_tokens and (_tokens(claim_text) & text_tokens):
            ids.append(str(claim.get("claim_id", "")))
    return [item for item in ids if item]


def _obligation_status(
    obligation: dict[str, Any],
    linked_claims: list[str],
    evidence_refs: list[str],
    missing_evidence: list[str],
    requirement_views: list[dict[str, Any]],
    ledger: Any,
) -> str:
    question_lower = str(obligation.get("question", "") or "").lower()
    summary_question = "conclusion" in question_lower or "justified" in question_lower
    uncertainty_question = any(term in question_lower for term in ("unknown", "unproven", "uncertain", "remains"))
    required_requirements = [item for item in requirement_views if bool(item.get("required", True))]
    if any(item.get("status") == "contradicted" for item in required_requirements):
        return "contradicted"
    if any(item.get("status") == "blocked_source" for item in required_requirements):
        return "blocked_source"
    if any(item.get("status") == "insufficient_evidence" for item in required_requirements):
        return "insufficient_evidence"
    all_required_sources_satisfied = all(item.get("status") == "satisfied" for item in required_requirements)
    has_any_evidence = bool(linked_claims or evidence_refs or getattr(ledger, "files_inspected", {}))
    if uncertainty_question and (linked_claims or (missing_evidence and has_any_evidence)):
        return "answered"
    if summary_question and linked_claims and all_required_sources_satisfied:
        return "answered"
    if all_required_sources_satisfied and (linked_claims or evidence_refs):
        return "answered"
    if missing_evidence and (linked_claims or evidence_refs):
        return "partially_answered"
    if missing_evidence:
        return "blocked_on_evidence"
    if linked_claims or evidence_refs:
        return "partially_answered"
    return "unanswered"


def _obligation_confidence(status: str, evidence_refs: list[str]) -> str:
    if status in {"contradicted", "blocked_source", "insufficient_evidence"}:
        return "LOW"
    if status == "answered" and len(evidence_refs) >= 2:
        return "MEDIUM"
    if status in {"answered", "partially_answered"}:
        return "LOW"
    return "LOW"


def _agenda_items_for_obligation(obligation: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for requirement in list(obligation.get("source_requirements", []) or []):
        path = str(requirement.get("path", "") or "")
        if not path or not bool(requirement.get("required", True)):
            continue
        plane = _evidence_plane(requirement)
        kind, tool_name = _agenda_kind_for_requirement(requirement, plane)
        pattern = _command_pattern_for_requirement(requirement, obligation)
        source_state = str(requirement.get("source_state", "") or "missing")
        item_status = "pending" if source_state == "missing" else "done"
        items.append({
            "id": f"agenda_{obligation.get('id', 'q')}_{_slug(path)}",
            "kind": kind,
            "tool_name": tool_name,
            "path": path,
            "pattern": pattern,
            "obligation_id": str(obligation.get("id", "") or ""),
            "priority": "high",
            "status": item_status,
            "source_state": source_state,
            "insufficiency_reasons": list(requirement.get("missing_shapes", []) or []) if source_state == "read_insufficient_shape" else [],
            "reason": f"Required source for obligation {obligation.get('id', '')}.",
            "prefetch": bool(requirement.get("prefetch", True)),
            "required_shapes": list(requirement.get("required_shapes", []) or []),
            "evidence_plane": plane,
        })
    return items


def _command_pattern_for_requirement(requirement: dict[str, Any], obligation: dict[str, Any]) -> str:
    command_requirement = dict(requirement.get("command_requirement", {}) or {})
    explicit = str(requirement.get("pattern", "") or command_requirement.get("pattern", "") or "")
    if explicit:
        return explicit
    if _evidence_plane(requirement) != "command_result":
        return ""
    evidence_kind = str(requirement.get("evidence_kind", "") or "").lower()
    if "zero_match" not in evidence_kind:
        return ""
    for text in (
        str(obligation.get("question", "") or ""),
        str(obligation.get("objective", "") or ""),
    ):
        match = re.search(r"['\"]([^'\"]+)['\"]", text)
        if match:
            return match.group(1)
    return ""


def _missing_evidence_for_requirements(requirement_views: list[dict[str, Any]]) -> list[str]:
    missing: list[str] = []
    for item in requirement_views:
        if not bool(item.get("required", True)):
            continue
        state = str(item.get("source_state", "") or "missing")
        path = str(item.get("path", "") or "")
        if state == "missing":
            missing.append(f"inspect:{path}")
        elif state == "read_insufficient_shape":
            shapes = ",".join(str(shape) for shape in (item.get("missing_shapes", []) or [])) or "unknown_shape"
            missing.append(f"insufficient_shape:{path}:{shapes}")
        elif state == "blocked":
            missing.append(f"blocked_source:{path}")
        elif state == "contradicted":
            missing.append(f"contradicted_source:{path}")
    return missing


def _source_states_from_obligations(obligations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    states: list[dict[str, Any]] = []
    for obligation in obligations:
        obligation_id = str(obligation.get("id", "") or "")
        for requirement in list(obligation.get("source_requirements", []) or []):
            state = dict(requirement.get("source_state_v1", {}) or {})
            if not state:
                continue
            state["obligation_id"] = obligation_id
            states.append(state)
    return states


def _source_state_hash(source_states: list[dict[str, Any]]) -> str:
    payload = json.dumps(source_states, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def _typed_insufficiency_fields(answer_graph: dict[str, Any]) -> list[str]:
    fields: list[str] = []
    for state in list(answer_graph.get("source_states", []) or []):
        if str(state.get("state", "") or "") != "read_insufficient_shape":
            continue
        path = str(state.get("path", "") or "")
        shapes = ",".join(str(shape) for shape in (state.get("missing_shapes", []) or [])) or "unknown_shape"
        fields.append(f"insufficient_shape:{path}:{shapes}")
    return fields


def _agenda_kind_for_requirement(requirement: dict[str, Any], plane: str) -> tuple[str, str]:
    if plane == "command_result":
        kind = str(requirement.get("evidence_kind", "") or "")
        if "zero_match" in kind.lower():
            return ("required_grep_zero_match", "rtk_grep")
        return ("required_grep", "rtk_grep")
    return ("required_read", "rtk_read")


def _dedupe_agenda(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (str(item.get("kind", "") or ""), str(item.get("path", "") or ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _coverage_status(obligations: list[dict[str, Any]], agenda_items: list[dict[str, Any]]) -> dict[str, Any]:
    required = [item for item in obligations if item.get("required", True)]
    answered = [item for item in required if item.get("status") == "answered"]
    contradicted = [item.get("id", "") for item in required if item.get("status") == "contradicted"]
    blocked = [item.get("id", "") for item in required if item.get("status") == "blocked_source"]
    insufficient = [item.get("id", "") for item in required if item.get("status") == "insufficient_evidence"]
    missing_sources = [
        str(item.get("path", "") or "")
        for item in agenda_items
        if str(item.get("kind", "") or "").startswith("required_")
        and item.get("status") == "pending"
        and str(item.get("source_state", "") or "") == "missing"
    ]
    coverage_complete = len(answered) == len(required) and not missing_sources and not contradicted and not blocked and not insufficient
    if contradicted or blocked:
        recommended_status = "ESCALATE"
    elif coverage_complete:
        recommended_status = "COMPLETE"
    elif answered or missing_sources or insufficient:
        recommended_status = "PARTIAL"
    else:
        recommended_status = "FAILED"
    return {
        "required_obligations": len(required),
        "answered_obligations": len(answered),
        "missing_required_sources": list(dict.fromkeys(missing_sources)),
        "contradicted_obligations": contradicted,
        "blocked_obligations": blocked,
        "insufficient_evidence_obligations": insufficient,
        "can_complete": coverage_complete,
        "coverage_complete": coverage_complete,
        "recommended_status": recommended_status,
    }


def _sufficiency_reason_code(
    *,
    contradicted: list,
    blocked: list,
    insufficient: list,
    pending_required: list,
    open_required: list,
    partial_unentitled: bool,
    can_close: bool,
) -> str:
    if contradicted:
        return "contradiction_unresolved"
    if blocked:
        return "blocked_source"
    if insufficient:
        return "missing_required_shape"
    if pending_required:
        return "missing_required_source"
    if open_required:
        return "unanswered_obligation"
    if partial_unentitled:
        return "partial_evidence_not_entitled"
    if can_close:
        return "all_satisfied"
    return "no_evidence"


def _next_required_actions(agenda_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    actions = []
    for item in agenda_items:
        kind = str(item.get("kind", "") or "")
        status = str(item.get("status", "") or "")
        if status == "done":
            continue
        if kind not in ("required_read", "required_grep", "required_grep_zero_match"):
            continue
        path = str(item.get("path", "") or "")
        pattern = str(item.get("pattern", "") or "")
        tool_name = str(item.get("tool_name", "") or "rtk_read")
        args: dict[str, str] = {"path": path}
        if tool_name == "rtk_grep" and pattern:
            args["pattern"] = pattern
        if not path:
            continue
        actions.append({
            "action_type": "tool_call",
            "tool_name": tool_name,
            "arguments": args,
            "reason": str(item.get("reason", "") or "Required source has not been inspected."),
            "obligation_id": str(item.get("obligation_id", "") or ""),
        })
    return actions[:3]


def _phase_from_obligations(obligations: list[dict[str, Any]], agenda_items: list[dict[str, Any]], ledger: Any, sufficiency: dict[str, Any]) -> str:
    if not ((getattr(ledger, "commands_run", []) or []) or (getattr(ledger, "files_inspected", {}) or {})):
        return "PLAN"
    pending_required = any(
        str(item.get("kind", "") or "").startswith("required_") and item.get("status") != "done"
        for item in agenda_items
    )
    required = [item for item in obligations if item.get("required", True)]
    if bool(sufficiency.get("can_close")):
        return "REPORT"
    if pending_required:
        return "NARROW"
    if any(item.get("status") in {"answered", "partially_answered"} for item in required):
        return "VERIFY"
    return "EXPLORE"


def _findings_from_answer_graph(answer_graph: dict[str, Any], ledger: Any) -> list[dict[str, Any]]:
    claims_by_id = {
        str(item.get("claim_id", "")): item
        for item in (getattr(ledger, "claims", []) or [])
        if isinstance(item, dict)
    }
    findings: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for obligation in list(answer_graph.get("required_obligations", []) or []) + list(answer_graph.get("optional_obligations", []) or []):
        if obligation.get("status") != "answered":
            continue
        claim_id = next(iter(obligation.get("claims", []) or []), "")
        claim = claims_by_id.get(claim_id, {})
        claim_text = str(claim.get("text", "") or "").strip()
        if not claim_text:
            claim_text = f"Answered obligation: {obligation.get('question', '')}"
        evidence_refs = list(obligation.get("evidence_refs", []) or claim.get("evidence_refs", []) or [])
        key = (claim_text, tuple(evidence_refs))
        if key in seen:
            continue
        seen.add(key)
        findings.append({
            "claim": claim_text,
            "evidence_refs": evidence_refs,
            "confidence": str(claim.get("confidence", obligation.get("confidence", "LOW")) or "LOW"),
        })
    return findings


def _files_payload(ledger: Any) -> list[dict[str, Any]]:
    return [
        {"path": path, "complete": bool(getattr(entry, "complete", False))}
        for path, entry in (getattr(ledger, "files_inspected", {}) or {}).items()
    ]


def _commands_payload(ledger: Any) -> list[dict[str, Any]]:
    return [
        {"tool": getattr(command, "tool", ""), "args": dict(getattr(command, "args", {}) or {})}
        for command in (getattr(ledger, "commands_run", []) or [])
    ]


def _command_refs(ledger: Any) -> list[dict[str, Any]]:
    refs = []
    for idx, command in enumerate(getattr(ledger, "commands_run", []) or []):
        args = dict(getattr(command, "args", {}) or {})
        refs.append({
            "ref": f"command:{idx}",
            "tool_name": str(getattr(command, "tool", "") or ""),
            "path": str(args.get("path", "") or ""),
            "pattern": str(args.get("pattern", "") or ""),
            "exit_code": int(getattr(command, "exit_code", 0) or 0),
            "matches_count": int(getattr(command, "matches_count", -1) or -1),
        })
    return refs


def _detected_shapes_for_path(path: str, requirement: dict[str, Any] | None = None) -> list[str]:
    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return []
    shapes = _detect_shapes(text)
    for shape, pattern in (dict((requirement or {}).get("shape_patterns", {}) or {}) if isinstance((requirement or {}).get("shape_patterns"), dict) else {}).items():
        try:
            if re.search(str(pattern), text, re.MULTILINE):
                shapes.append(str(shape))
        except re.error:
            continue
    return list(dict.fromkeys(shapes))


def _matched_contradiction_markers(path: str, markers: list[str]) -> list[str]:
    if not path or not markers:
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read().lower()
    except OSError:
        return []
    return [marker for marker in markers if marker.lower() in text]


def _detect_shapes(text: str) -> list[str]:
    return detect_registered_shapes(text)


def _default_source_requirements(paths: list[str], evidence_kind: str) -> list[dict[str, Any]]:
    return [
        {
            "path": str(path),
            "evidence_kind": evidence_kind,
            "required": True,
            "prefetch": True,
        }
        for path in paths
        if str(path)
    ]


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(text or "")).strip("_") or "item"


def _tokens(text: str) -> set[str]:
    lowered = str(text or "").lower()
    return {
        token
        for token in re.findall(r"[a-z0-9_]{4,}", lowered)
        if token not in {"what", "does", "this", "that", "with", "from", "have", "been"}
    }
