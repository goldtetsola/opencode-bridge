"""ReportSemanticCompletenessGateV1 — ensures a COMPLETE report actually communicates the answer.

Hard rules:
  - COMPLETE cannot have empty findings (open investigations)
  - COMPLETE cannot have non-empty missing_fields
  - COMPLETE cannot have uncertainties that negate the answer
  - COMPLETE must represent required answered obligations
"""

from __future__ import annotations

from typing import Any

JSON = dict[str, Any]

NEGATING_UNCERTAINTY_PATTERNS: list[str] = [
    "target value was not retrieved",
    "file was not inspected",
    "unable to determine",
    "unable to confirm",
    "could not",
    "not found",
    "answer was not retrieved",
    "no findings were produced",
    "could not be determined",
    "not directly observed",
    "not directly inspected",
    "not directly verify",
    "not directly verified",
    "extracts were not directly inspected",
    "without verification",
    "without direct verification",
    "externally without verification",
    "supplied externally",
    "firsthand inspection",
    "prevented firsthand inspection",
    "exact content of extracts is unknown",
    "from the extracts alone",
    "from extracts alone",
    "might alter interpretation",
    "did not directly verify",
    "no actual file inspection",
    "no actual file inspection was executed",
    "evidence refs are placeholders",
    "placeholders provided in the prompt",
    "no full file inspection",
    "no full file inspection was possible",
    "cannot be fully confirmed",
    "manual review recommended",
    "human review required",
    "budget constraints forced closure",
    "forced closure before thorough",
]

ESCALATION_REQUIRED_PATTERNS: list[str] = [
    "human review required",
    "manual review recommended",
    "manual review required",
    "review required to confirm",
]

EVIDENCE_WEAKENING_SUBJECTS: list[str] = [
    "evidence",
    "extract",
    "extracts",
    "evidence ref",
    "evidence refs",
    "command output",
    "output",
]

EVIDENCE_WEAKENING_PREDICATES: list[str] = [
    "not available",
    "not detailed",
    "unknown",
    "assumed",
    "suggest",
    "placeholder",
    "without verification",
    "not directly",
    "could not",
    "not self-explanatory",
]


def build_published_answer(
    mission: Any,
    answer_graph: JSON,
    claim_graph: JSON | None,
) -> JSON:
    """Build a canonical answer object from the answer graph."""
    obligations = (
        list(answer_graph.get("required_obligations", []) or [])
        + list(answer_graph.get("optional_obligations", []) or [])
    )
    answered_obligations = [o for o in obligations if o.get("status") == "answered"]
    claims = list((claim_graph or {}).get("claims", []) or [])

    required_answers = []
    for o in answered_obligations:
        refs = list(o.get("evidence_refs", []) or [])
        answer = {
            "obligation_id": str(o.get("id", "") or ""),
            "question": str(o.get("question", "") or ""),
            "evidence_refs": refs[:6],
        }
        related_claims = [
            {"text": str(c.get("text", c.get("claim", "")))[:200], "confidence": str(c.get("confidence", ""))}
            for c in claims
            if any(r in str(c.get("evidence_refs", [])) for r in refs)
        ]
        if related_claims:
            answer["supporting_claims"] = related_claims[:3]
        required_answers.append(answer)

    sufficiency = answer_graph.get("sufficiency", {}) or {}
    return {
        "schema_version": "published_answer.v1",
        "mission_id": str(getattr(mission, "mission_id", "") or ""),
        "required_answers": required_answers,
        "required_answered": sufficiency.get("required_answered", 0),
        "required_total": sufficiency.get("required_total", 0),
        "can_close": sufficiency.get("can_close", False),
        "confidence_cap": sufficiency.get("confidence_cap", "LOW"),
        "unanswered_obligations": [
            {"id": str(o.get("id", "")), "question": str(o.get("question", ""))}
            for o in obligations
            if o.get("status") not in ("answered", "blocked", "out_of_scope")
        ],
    }


def evaluate_report_semantic_completeness(
    mission: Any,
    report: JSON,
    published_answer: JSON,
    answer_graph: JSON,
    answer_sufficiency: JSON | None,
) -> JSON:
    """Check whether a structurally valid report actually communicates the answer."""
    report = report or {}
    status = str(report.get("status", "") or "").upper()
    findings = report.get("findings", []) or []
    missing_fields = report.get("missing_fields", []) or []
    uncertainties = report.get("uncertainties", []) or []
    caveats = report.get("caveats", []) or []
    objective_style = str(getattr(mission, "objective_style", "") or "")
    is_open = objective_style == "open_investigation"

    reason_codes: list[str] = []
    required_repairs: list[str] = []

    # Rule 1: COMPLETE cannot have empty findings (open investigations)
    if status == "COMPLETE" and is_open and not findings:
        reason_codes.append("complete_with_empty_findings")
        required_repairs.append("Add at least one finding that states the resolved answer.")

    # Rule 2: COMPLETE cannot have non-empty missing_fields
    non_empty_missing = [f for f in missing_fields if str(f).strip()]
    if status == "COMPLETE" and non_empty_missing:
        reason_codes.append("complete_with_missing_fields")
        required_repairs.append("Remove or resolve missing_fields before claiming COMPLETE.")

    # Rule 3: COMPLETE cannot have uncertainties that negate the answer
    if status == "COMPLETE":
        escalation_text = str(report.get("escalation_recommendation", "") or "").strip().lower()
        all_text = " ".join(
            [str(u) for u in (uncertainties + caveats)]
            + [escalation_text]
            + [str(f.get("claim", f)) for f in findings if isinstance(f, dict)]
        ).lower()
        for pattern in NEGATING_UNCERTAINTY_PATTERNS:
            if pattern in all_text:
                reason_codes.append("complete_with_negating_uncertainty")
                required_repairs.append(f"Remove or qualify the uncertainty: '{pattern}' is incompatible with COMPLETE.")
                break
        for item in [str(u).lower() for u in (uncertainties + caveats)]:
            if any(subject in item for subject in EVIDENCE_WEAKENING_SUBJECTS) and any(predicate in item for predicate in EVIDENCE_WEAKENING_PREDICATES):
                if "complete_with_negating_uncertainty" not in reason_codes:
                    reason_codes.append("complete_with_negating_uncertainty")
                    required_repairs.append("Resolve evidence-weakening caveats before claiming COMPLETE.")
                break
        if escalation_text and escalation_text not in ("none", "none required", "no escalation", "not required"):
            if any(pattern in escalation_text for pattern in ESCALATION_REQUIRED_PATTERNS):
                reason_codes.append("complete_with_escalation_required")
                required_repairs.append("A COMPLETE report cannot require GPT, human, or manual review to confirm the answer.")

    # Rule 4: COMPLETE must have canonical answer evidence
    if status == "COMPLETE" and is_open:
        required_answers = published_answer.get("required_answers", []) or []
        if required_answers and not findings:
            reason_codes.append("complete_missing_required_answer")
            required_repairs.append("Include the canonical answer from the runtime in the report findings.")

    # Rule 5: COMPLETE can't exceed runtime status cap
    sufficiency = answer_sufficiency or {}
    recommended = str(sufficiency.get("recommended_status", "") or "").upper()
    if status == "COMPLETE" and recommended in ("PARTIAL", "ESCALATE", "FAILED"):
        reason_codes.append("complete_status_exceeds_runtime_cap")
        required_repairs.append(f"Downgrade status to {recommended} or lower.")

    ok = not reason_codes
    decision = "ACCEPT" if ok else ("REJECT_FOR_REPAIR" if reason_codes else "ACCEPT")
    status_cap = "PARTIAL" if not ok else None

    return {
        "schema_version": "report_semantic_completeness.v1",
        "ok": ok,
        "decision": decision,
        "reason_codes": reason_codes,
        "status_cap": status_cap,
        "required_repairs": required_repairs,
        "canonical_answer_available": bool(published_answer.get("required_answers")),
    }


def apply_semantic_status_cap(report: JSON, semantic_result: JSON) -> JSON:
    """Apply status cap from semantic completeness check to a report."""
    if not semantic_result.get("ok"):
        cap = semantic_result.get("status_cap")
        if cap:
            report["status"] = cap
        report.setdefault("caveats", []).append(
            f"Report status capped: {', '.join(semantic_result.get('reason_codes', []) or ['semantic_incomplete'])}"
        )
        report["report_source"] = "model_report_downgraded"
    return report
