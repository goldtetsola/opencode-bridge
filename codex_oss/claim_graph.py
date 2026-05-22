"""ClaimGraphV1 — runtime-owned draft answer state for read-only investigations."""

from __future__ import annotations

import json
import os
import hashlib
from typing import Any

from codex_oss.runtime.autonomy import mission_phase
from codex_oss.runtime.objectives import synthesize_objective_finding


def refresh_claim_graph(
    mission: Any,
    ledger: Any,
    *,
    action: Any | None = None,
    report: dict[str, Any] | None = None,
    reason: str = "observation",
    persist: bool = False,
) -> dict[str, Any]:
    graph = _base_graph(mission, ledger)
    graph["last_updated_reason"] = reason

    if action is not None:
        target_question = str(getattr(action, "target_question", "") or "").strip()
        if target_question:
            ledger.add_open_question(target_question, priority="high")
        why_not = str(getattr(action, "why_not_report_yet", "") or "").strip()
        if why_not:
            ledger.add_open_question(why_not, priority="normal")

    synthesized = synthesize_objective_finding(mission, ledger)
    if synthesized:
        ledger.add_claim(_claim_from_finding(synthesized, status="supported", source="runtime_objective"))

    if action is not None and _is_open_style(mission) and getattr(ledger, "files_inspected", {}):
        hypothesis = str(getattr(action, "hypothesis", "") or "").strip()
        latest_ref = _latest_evidence_ref(ledger)
        if hypothesis and latest_ref:
            ledger.add_claim({
                "claim_id": f"claim_{len(getattr(ledger, 'claims', []) or []) + 1:03d}",
                "text": f"Evidence gathered while testing hypothesis: {hypothesis}",
                "status": "unverified",
                "confidence": "LOW",
                "evidence_refs": [latest_ref],
                "source": "action_hypothesis",
            })

    if isinstance(report, dict):
        for finding in report.get("findings", []) or []:
            if not isinstance(finding, dict):
                continue
            ledger.add_claim(_claim_from_finding(finding, status="supported", source=str(report.get("report_source", "") or "report")))

    graph["claims"] = list(getattr(ledger, "claims", []) or [])
    graph["open_questions"] = list(getattr(ledger, "open_questions", []) or [])
    graph["inspected_paths"] = list((getattr(ledger, "files_inspected", {}) or {}).keys())
    graph["uninspected_allowed_paths"] = _uninspected_allowed_paths(mission, ledger)
    graph["phase"] = _claim_graph_phase(mission, ledger)
    graph["phase_history"] = _phase_history(ledger, graph["phase"])
    graph["sufficiency"] = evaluate_sufficiency(mission, ledger, graph=graph, report=report)
    graph["investigation_state"] = {
        "phase": graph["phase"],
        "enough_evidence_to_report": bool(graph["sufficiency"].get("enough_evidence_to_report")),
        "remaining_uncertainties": list(graph["sufficiency"].get("missing_requirements", []) or []),
    }
    ledger.claim_graph = dict(graph)
    if persist:
        write_claim_graph(os.getcwd(), getattr(mission, "mission_id", "mission_unknown"), graph)
    return graph


def evaluate_sufficiency(mission: Any, ledger: Any, *, graph: dict[str, Any] | None = None, report: dict[str, Any] | None = None) -> dict[str, Any]:
    graph = graph or _base_graph(mission, ledger)
    policy = dict(getattr(mission, "sufficiency_policy", {}) or {})
    objective_style = str(getattr(mission, "objective_style", "") or "")
    supported_claims = [
        claim for claim in (graph.get("claims", []) or [])
        if str(claim.get("status", "") or "") in {"supported", "weak"}
    ]
    min_claims = int(policy.get("min_main_claims", 0) or 0)
    min_refs = int(policy.get("min_evidence_refs_per_claim", 1) or 1)
    missing: list[str] = []
    if len(supported_claims) < min_claims:
        missing.append(f"need_at_least_{min_claims}_main_claims")
    for claim in supported_claims:
        refs = list(claim.get("evidence_refs", []) or [])
        if len(refs) < min_refs:
            missing.append(f"claim_missing_evidence_refs:{claim.get('claim_id', 'unknown')}")
    if bool(policy.get("must_list_uninspected_areas", False)) and "uninspected_allowed_paths" not in graph:
        missing.append("must_list_uninspected_areas")
    partial_extracts = any(not bool(getattr(entry, "complete", False)) for entry in (getattr(ledger, "files_inspected", {}) or {}).values())
    confidence_cap = str(policy.get("confidence_cap_if_partial_extracts", "LOW") if partial_extracts else "HIGH")
    enough = False
    if objective_style == "open_investigation":
        enough = not missing and bool(supported_claims or isinstance(report, dict))
    elif isinstance(report, dict):
        enough = not missing and bool(supported_claims or report.get("findings"))
    return {
        "sufficiency_version": "1.0",
        "enough_evidence_to_report": enough,
        "main_claim_count": len(supported_claims),
        "missing_requirements": missing,
        "confidence_cap": confidence_cap,
        "partial_extracts": partial_extracts,
    }


def write_claim_graph(project_root: str, mission_id: str, graph: dict[str, Any]) -> None:
    path = os.path.join(project_root, ".codex-oss", "missions", mission_id, "claim_graph.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(graph, handle, indent=2, sort_keys=True)


def _base_graph(mission: Any, ledger: Any) -> dict[str, Any]:
    return {
        "claim_graph_version": "1.0",
        "mission_id": str(getattr(mission, "mission_id", "") or ""),
        "objective": str(getattr(mission, "objective", "") or ""),
        "objective_style": str(getattr(mission, "objective_style", "") or ""),
        "phase": _claim_graph_phase(mission, ledger),
    }


def _claim_from_finding(finding: dict[str, Any], *, status: str, source: str) -> dict[str, Any]:
    text = str(finding.get("claim", "") or "").strip()
    claim_id = "claim_" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    return {
        "claim_id": claim_id,
        "text": text,
        "status": status,
        "evidence_refs": list(finding.get("evidence_refs", []) or []),
        "confidence": str(finding.get("confidence", "LOW") or "LOW"),
        "source": source,
        "missing_evidence": [],
    }


def _claim_graph_phase(mission: Any, ledger: Any) -> str:
    if _is_open_style(mission) and not (getattr(ledger, "commands_run", []) or getattr(ledger, "files_inspected", {})):
        return "PLAN"
    return mission_phase(ledger)


def _is_open_style(mission: Any) -> bool:
    return str(getattr(mission, "objective_style", "") or "") == "open_investigation"


def _latest_evidence_ref(ledger: Any) -> str:
    files = list((getattr(ledger, "files_inspected", {}) or {}).items())
    if not files:
        return ""
    path, entry = files[-1]
    extracts = list(getattr(entry, "extracts", []) or [])
    if not extracts:
        return f"file:{path}#extract:1"
    extract = extracts[-1]
    extract_id = str(extract.get("id", "extract:1")).replace("extract:", "")
    return f"file:{path}#extract:{extract_id}"


def _uninspected_allowed_paths(mission: Any, ledger: Any) -> list[str]:
    known = list(getattr(mission, "allowed_paths", []) or []) + list(getattr(mission, "read_only_paths", []) or [])
    inspected = set((getattr(ledger, "files_inspected", {}) or {}).keys())
    return [path for path in known if path not in inspected]


def _phase_history(ledger: Any, current_phase: str) -> list[str]:
    existing = list((((getattr(ledger, "claim_graph", {}) or {}).get("phase_history")) or []))
    phase = str(current_phase or "").strip()
    if phase and (not existing or existing[-1] != phase):
        existing.append(phase)
    return existing
