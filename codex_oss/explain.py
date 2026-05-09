"""Decision explainer — builds a unified explanation of why a mission ended with its status.

Reads mission artifacts (report, validation, coverage, semantic review, decision trace)
and produces a single coherent explanation of the mission outcome.
"""

from __future__ import annotations

import json
import os

JSON = dict


def build_explanation(project_root: str, mission_id: str) -> JSON | None:
    """Build a comprehensive explanation of a mission's outcome."""
    mission_dir = os.path.join(project_root, ".codex-oss", "missions", mission_id)
    if not os.path.isdir(mission_dir):
        return None

    report = _read_json(os.path.join(mission_dir, "report.json"))
    validation = _read_json(os.path.join(mission_dir, "validation.json"))
    trace = _read_json(os.path.join(mission_dir, "decision_trace.json"))
    coverage = _read_json(os.path.join(mission_dir, "implementation_coverage_graph.json"))
    readiness = _read_json(os.path.join(mission_dir, "implementation_readiness_graph.json"))

    if not report and not trace:
        return None

    status = (
        (report or {}).get("status", "")
        or (validation or {}).get("status", "")
        or "unknown"
    )
    closure = (report or {}).get("closure_source", "") or (
        "runtime_answer_graph"
        if "runtime_answer_graph" in str(report or {})
        else "model"
    )

    explanation: dict = {
        "mission_id": mission_id,
        "status": status,
        "closure_source": closure,
        "confidence": (report or {}).get("confidence", "unknown"),
        "phase_path": _phase_path(trace),
        "decision_count": len((trace or {}).get("decisions", []) or []),
        "key_decisions": _key_decisions(trace),
        "caveats": (report or {}).get("caveats", []) or [],
        "required_sources": {
            "covered": _int(report, "answer_graph_summary", "required_answered"),
            "total": _int(report, "answer_graph_summary", "required_total"),
        },
        "missing_evidence": (report or {}).get("missing_required_sources", []) or [],
        "blocked_obligations": (report or {}).get("blocked_obligations", []) or [],
        "contradictions": (report or {}).get("contradicted_obligations", []) or [],
        "evidence_shapes": {
            "covered": _int(report, "answer_graph_summary", "required_evidence_shapes_covered"),
            "missing": (report or {}).get("insufficient_evidence_obligations", []) or [],
        },
        "coverage_gaps": [],
        "semantic_review": {},
        "verification": {},
        "rollback": (report or {}).get("rollback"),
    }

    # Coverage gaps
    if coverage:
        explanation["coverage_gaps"] = [
            {"category": item.get("category", ""), "reason": item.get("reason", "")}
            for item in (coverage.get("missing_coverage", []) or [])
        ]
        explanation["coverage_score"] = coverage.get("overall_coverage_score", 0)
        explanation["coverage_can_propose"] = coverage.get("can_propose", False)
        explanation["coverage_critical_violations"] = coverage.get("critical_violations", []) or []

    # Semantic review
    if isinstance(validation, dict) and isinstance(validation.get("semantic_review"), dict):
        sr = validation["semantic_review"]
        explanation["semantic_review"] = {
            "decision": sr.get("decision", ""),
            "blocking_count": sr.get("blocking_count", 0),
            "repairable_count": sr.get("repairable_count", 0),
            "non_blocking_count": sr.get("non_blocking_count", 0),
            "blocking_codes": [f.get("code", "") for f in (sr.get("blocking_findings", []) or [])],
            "repairable_codes": [f.get("code", "") for f in (sr.get("repairable_findings", []) or [])],
            "file_roles": sr.get("file_roles", {}),
        }

    # Verification
    if isinstance(report, dict) or isinstance(validation, dict):
        explanation["verification"] = {
            "ok": (validation or {}).get("checks", {}).get("verification_plan_ok", False),
            "score": (validation or {}).get("checks", {}).get("verification_plan_score", 0),
            "commands_ran": len((report or {}).get("verification", []) or []),
        }

    # Readiness
    if readiness:
        cs = readiness.get("coverage_status", {}) or {}
        explanation["readiness"] = {
            "recommended": cs.get("recommended_status", ""),
            "can_propose": cs.get("can_propose", False),
            "can_apply": cs.get("can_apply", False),
            "can_verify": cs.get("can_verify", False),
        }

    # Derived explanation text
    explanation["summary"] = _build_summary(explanation)

    return explanation


def _read_json(path: str) -> JSON | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def _int(data: JSON | None, *keys: str) -> int:
    current = data or {}
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key, {}) or {}
        else:
            return 0
    try:
        return int(current or 0)
    except (TypeError, ValueError):
        return 0


def _phase_path(trace: JSON | None) -> str:
    if not trace:
        return ""
    phases = []
    for item in (trace.get("decisions", []) or []):
        if str(item.get("decision_type", "")) == "phase_transition":
            result = str(item.get("result", "") or "")
            if result:
                phases.append(result)
    return " -> ".join(phases) if phases else ""


def _key_decisions(trace: JSON | None) -> list[JSON]:
    if not trace:
        return []
    decisions: list[JSON] = []
    for item in (trace.get("decisions", []) or []):
        d_type = str(item.get("decision_type", "") or "")
        if d_type in {"phase_transition", "artifact_persistence"}:
            continue  # Not interesting for quick explanation
        decisions.append({
            "type": d_type,
            "result": str(item.get("result", "") or ""),
            "reason": str(item.get("reason", "") or ""),
            "policy": str(item.get("policy", "") or ""),
            "source": str(item.get("source_module", "") or ""),
        })
    return decisions


def _build_summary(explanation: JSON) -> str:
    status = explanation.get("status", "unknown")
    lines = []

    if status == "COMPLETE":
        lines.append("All required evidence was found and answer obligations were satisfied.")
    elif status == "PARTIAL":
        reasons = []
        if explanation.get("missing_evidence"):
            reasons.append("some required evidence was not found")
        if explanation.get("blocked_obligations"):
            reasons.append("some evidence sources were blocked")
        if explanation.get("evidence_shapes", {}).get("missing"):
            reasons.append("some required evidence shapes were not found")
        if reasons:
            lines.append(f"Report is PARTIAL because {', '.join(reasons)}.")
        else:
            lines.append("Report is PARTIAL.")
    elif status == "ESCALATE":
        lines.append("Report escalated: ")
        if explanation.get("contradictions"):
            lines.append(f"  - Contradictory evidence: {explanation['contradictions']}")
        if explanation.get("semantic_review", {}).get("blocking_codes"):
            lines.append(f"  - Semantic review blocking: {explanation['semantic_review']['blocking_codes']}")
        if explanation.get("coverage_critical_violations"):
            lines.append(f"  - Critical coverage violations: {explanation['coverage_critical_violations']}")
    elif status == "VERIFIED":
        lines.append("Patch was validated, applied in isolated worktree, and verification passed.")
    elif status == "FAILED":
        lines.append("Mission failed. Check key decisions and caveats for details.")
    else:
        lines.append(f"Mission ended with status: {status}")

    closure = explanation.get("closure_source", "")
    if closure == "runtime_answer_graph":
        lines.append("The runtime closed from the answer graph (model timeout or budget exhausted).")
    elif closure == "model":
        lines.append("The model self-closed with a final report.")

    caveats = explanation.get("caveats", []) or []
    if caveats:
        lines.append(f"Caveats: {', '.join(c[:80] for c in caveats[:3])}")

    return " ".join(lines)
