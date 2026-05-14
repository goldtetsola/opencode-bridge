"""Closure Reliability V2 — runtime-owned canon, model-narrated draft, runtime merge.

The runtime builds canonical answer + report skeleton from evidence graphs.
The model fills only a constrained CloserDraftV1 (narrative layer).
The runtime merges, validates, publishes.

Closure types:
  MODEL_CLOSED                     — model produced full valid report (legacy)
  CLOSER_MODEL_CLOSED              — dedicated closer produced full report
  MODEL_NARRATED_RUNTIME_CLOSED    — model draft + runtime merge (target)
  RUNTIME_CLOSED                   — runtime fallen without model narrative
  DETERMINISTIC_FAST_PATH          — deterministic extraction
  FAILED                            — mission failed
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

JSON = dict[str, Any]


def build_canonical_answer(
    mission: Any,
    answer_graph: JSON,
    completion_envelope: JSON,
    ledger: Any,
) -> JSON:
    """Build the runtime's canonical answer from evidence graphs."""
    envelope = completion_envelope or {}
    sufficiency = answer_graph.get("sufficiency", {}) or {}
    obligations = (
        list(answer_graph.get("required_obligations", []) or [])
        + list(answer_graph.get("optional_obligations", []) or [])
    )
    answered = [o for o in obligations if o.get("status") == "answered"]

    required_findings = []
    for i, o in enumerate(answered):
        refs = list(o.get("evidence_refs", []) or [])[:4]
        required_findings.append({
            "finding_id": f"finding_{i + 1:03d}",
            "obligation_id": str(o.get("id", "") or ""),
            "claim": str(o.get("question", "") or f"Obligation {i + 1}")[:200],
            "evidence_refs": refs,
            "confidence": envelope.get("evidence_status", "SUFFICIENT"),
        })

    missing_sources = sufficiency.get("missing_required_sources", []) or []
    contradicted = sufficiency.get("contradicted_obligations", []) or []
    blocked = sufficiency.get("blocked_obligations", []) or []
    insufficient = sufficiency.get("insufficient_evidence_obligations", []) or []

    required_caveats = []
    if missing_sources:
        required_caveats.append(f"Required sources not inspected: {', '.join(str(s) for s in missing_sources[:4])}")
    if contradicted:
        required_caveats.append(f"Contradiction unresolved: {', '.join(str(c) for c in contradicted[:4])}")
    if blocked:
        required_caveats.append(f"Blocked sources: {', '.join(str(b) for b in blocked[:4])}")
    if insufficient:
        required_caveats.append(f"Required evidence shapes not found: {', '.join(str(i) for i in insufficient[:4])}")
    if envelope.get("verification_status") == "INCOMPLETE":
        required_caveats.append("The core answer was found, but post-floor verification was not completed.")

    allowed = [envelope.get("final_status", "PARTIAL")]
    if envelope.get("final_status", "PARTIAL") == "COMPLETE":
        allowed = ["COMPLETE"]
    elif envelope.get("final_status", "PARTIAL") == "PARTIAL":
        allowed = ["PARTIAL"]

    return {
        "schema_version": "canonical_answer.v1",
        "mission_id": str(getattr(mission, "mission_id", "") or ""),
        "recommended_status": envelope.get("final_status", "PARTIAL"),
        "confidence_cap": sufficiency.get("confidence_cap", "MEDIUM"),
        "answer_status": envelope.get("answer_status", "ANSWERED"),
        "evidence_status": envelope.get("evidence_status", "SUFFICIENT"),
        "verification_status": envelope.get("verification_status", "NOT_REQUIRED"),
        "closure_entitlement": {
            "can_return_complete": sufficiency.get("closure_entitlement", {}).get("can_return_complete", False),
            "reason_codes": sufficiency.get("reason_code", ""),
        },
        "required_findings": required_findings,
        "required_caveats": required_caveats,
        "missing_fields": list(missing_sources + contradicted + blocked),
        "unanswered_obligations": [
            {"id": str(o.get("id", "")), "question": str(o.get("question", ""))}
            for o in obligations
            if o.get("status") not in ("answered", "blocked", "out_of_scope")
        ],
        "blocked_sources": list(blocked),
        "contradictions": list(contradicted),
        "allowed_statuses": allowed,
        "forbidden_statuses": ["COMPLETE"] if "COMPLETE" not in allowed else [],
    }


def build_report_skeleton(canonical_answer: JSON) -> JSON:
    """Build the skeleton the model may narrate, never override."""
    return {
        "schema_version": "report_skeleton.v1",
        "mission_id": canonical_answer.get("mission_id", ""),
        "allowed_statuses": canonical_answer.get("allowed_statuses", ["PARTIAL"]),
        "status": canonical_answer.get("recommended_status", "PARTIAL"),
        "confidence": canonical_answer.get("confidence_cap", "MEDIUM"),
        "required_findings": [
            {"claim": f["claim"], "evidence_refs": f["evidence_refs"]}
            for f in (canonical_answer.get("required_findings", []) or [])
        ],
        "required_caveats": list(canonical_answer.get("required_caveats", []) or []),
        "required_sections": ["findings", "evidence", "caveats", "verification_summary"],
        "forbidden_moves": [
            "Do not change status to COMPLETE." if "COMPLETE" in canonical_answer.get("forbidden_statuses", []) else "",
            "Do not remove required findings.",
            "Do not invent evidence refs.",
            "Do not claim verification was completed if it was not.",
        ],
    }


def build_closer_draft_prompt(skeleton: JSON, *, max_chars: int = 5000) -> str:
    """Build a minimal closer prompt asking for CloserDraftV1, not full report."""
    lines = [
        "You are writing the narrative layer for a runtime-built report.",
        "",
        "The runtime has already decided status, confidence, findings, evidence refs, and caveats.",
        "You must not change them.",
        "",
        "Return only CloserDraftV1 JSON.",
        "",
        f"Required status: {skeleton.get('status', 'PARTIAL')}",
        f"Confidence cap: {skeleton.get('confidence', 'MEDIUM')}",
        "",
    ]

    findings = skeleton.get("required_findings", []) or []
    if findings:
        lines.append("Required findings:")
        for f in findings:
            lines.append(f"- {f.get('claim', '')}")
            refs = ", ".join(f.get("evidence_refs", []) or [])
            if refs:
                lines.append(f"  Evidence: {refs}")

    caveats = skeleton.get("required_caveats", []) or []
    if caveats:
        lines.append("\nRequired caveats:")
        for c in caveats:
            lines.append(f"- {c}")

    lines.append(f"\nForbidden:")
    lines.append(f"- Do not change status upward.")
    lines.append(f"- Do not invent evidence refs.")
    lines.append(f"- Do not claim verification was completed if it was not.")
    lines.append("")
    lines.append("Write a concise narrative_summary, finding_narratives, caveat_narratives, verification_summary, and confidence_rationale.")
    lines.append("Return only valid JSON. Do not include raw file content or large extracts.")

    payload = "\n".join(lines)
    if len(payload) > max_chars:
        payload = payload[:max_chars - 50] + "\n\n[truncated]"
    return payload


def merge_draft_into_report(canonical_answer: JSON, draft: JSON) -> JSON:
    """Merge a CloserDraftV1 into the canonical answer to produce the final report."""
    report = {
        "oss_report_version": "1.0",
        "mission_id": canonical_answer.get("mission_id", ""),
        "status": canonical_answer.get("recommended_status", "PARTIAL"),
        "confidence": canonical_answer.get("confidence_cap", "MEDIUM"),
        "report_source": "model_narrated_runtime_closed",
        "findings": [
            {
                "claim": draft.get("finding_narratives", [{}])[i].get("text", f["claim"])
                if i < len(draft.get("finding_narratives", []) or [])
                else f["claim"],
                "evidence_refs": f["evidence_refs"],
            }
            for i, f in enumerate(canonical_answer.get("required_findings", []) or [])
        ],
        "caveats": list(canonical_answer.get("required_caveats", []) or []),
        "missing_fields": list(canonical_answer.get("missing_fields", []) or []),
        "uncertainties": list(canonical_answer.get("unanswered_obligations", []) or []),
        "narrative_summary": str(draft.get("narrative_summary", "") or "")[:500],
        "verification_summary": str(draft.get("verification_summary", "") or "")[:500],
        "escalation_recommendation": "GPT-5.5 review required"
        if canonical_answer.get("recommended_status", "") == "ESCALATE"
        else "GPT-5.5 review recommended",
    }

    # Also include the canonical answer in the report for audit
    report["canonical_answer"] = canonical_answer
    return report


def build_runtime_report_from_canonical(canonical_answer: JSON) -> JSON:
    """Build a full report from the canonical answer without model narrative."""
    findings = canonical_answer.get("required_findings", []) or []
    return {
        "oss_report_version": "1.0",
        "mission_id": canonical_answer.get("mission_id", ""),
        "status": canonical_answer.get("recommended_status", "PARTIAL"),
        "confidence": canonical_answer.get("confidence_cap", "MEDIUM"),
        "report_source": "runtime_closed",
        "findings": [
            {"claim": f["claim"], "evidence_refs": f["evidence_refs"]}
            for f in findings
        ],
        "caveats": list(canonical_answer.get("required_caveats", []) or []),
        "missing_fields": list(canonical_answer.get("missing_fields", []) or []),
        "uncertainties": list(canonical_answer.get("unanswered_obligations", []) or []),
        "escalation_recommendation": "GPT-5.5 review required"
        if canonical_answer.get("recommended_status", "") == "ESCALATE"
        else "GPT-5.5 review recommended",
    }


def record_closure_telemetry(
    mission_dir: str,
    attempt_id: str,
    *,
    attempt_type: str = "closer_draft",
    closer_model: str = "",
    elapsed_seconds: float = 0,
    timeout_seconds: float = 0,
    payload_chars: int = 0,
    deadline_remaining_seconds: float = 0,
    skeleton_findings_count: int = 0,
    allowed_statuses: list[str] | None = None,
    result: str = "",
    error_type: str = "",
    error_message: str = "",
    draft_valid: bool = False,
    merged_report_valid: bool = False,
    closure_status: str = "",
    skip_reason: str = "",
    result_valid: bool = False,
    final_closure_source: str = "",
    repair_attempted: bool = False,
):
    """Record a closure attempt with complete telemetry. No blank fields."""
    path = os.path.join(mission_dir, "closure_attempts.jsonl")
    entry = {
        "schema_version": "closure_attempt.v2",
        "attempt_id": attempt_id,
        "attempt_type": attempt_type,
        "closer_model": closer_model,
        "started_at": str(int(time.time())),
        "elapsed_seconds": round(elapsed_seconds, 1),
        "timeout_seconds": round(timeout_seconds, 1),
        "deadline_remaining_seconds": round(deadline_remaining_seconds, 1),
        "payload_chars": payload_chars,
        "skeleton_findings_count": skeleton_findings_count,
        "allowed_statuses": allowed_statuses or [],
        "result": result or "runtime_skipped",
        "error_type": error_type,
        "draft_valid": draft_valid,
        "merged_report_valid": merged_report_valid,
        "closure_status": closure_status,
        "skip_reason": skip_reason,
        "report_valid": result_valid,
        "repair_attempted": repair_attempted,
        "final_closure_source": final_closure_source,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
