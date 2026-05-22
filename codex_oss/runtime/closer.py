"""Runtime closer — compact report prompt, budget reservation, attempt tracking.

The closer receives a condensed summary of the investigation (obligations,
coverage, evidence refs) instead of the full trace. This makes the final
model call smaller and faster, reducing timeout risk.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

JSON = dict[str, Any]


def build_closer_payload(
    mission: Any,
    answer_graph: JSON,
    claim_graph: JSON | None,
    ledger: Any,
    *,
    max_chars: int = 12000,
) -> str:
    """Build a compact closer prompt from investigation state."""
    sufficiency = answer_graph.get("sufficiency", {}) or {}
    obligations = (
        list(answer_graph.get("required_obligations", []) or [])
        + list(answer_graph.get("optional_obligations", []) or [])
    )
    answered = [o for o in obligations if o.get("status") == "answered"]
    missing = [o for o in obligations if o.get("status") not in ("answered", "blocked", "out_of_scope")]

    lines = []
    lines.append("You are closing a completed runtime-backed OSS investigation.")
    lines.append("")
    lines.append(f"Objective: {getattr(mission, 'objective', '')}")
    lines.append(f"Mission ID: {getattr(mission, 'mission_id', '')}")
    lines.append("")

    # Obligations
    if answered:
        lines.append(f"Answered obligations ({len(answered)}):")
        for o in answered[:8]:
            q = str(o.get("question", o.get("id", "")))[:120]
            refs = ", ".join(str(r) for r in (o.get("evidence_refs", []) or [])[:4])
            lines.append(f"- {q}")
            if refs:
                lines.append(f"  Evidence: {refs}")
    if missing:
        lines.append(f"\nUnresolved obligations ({len(missing)}):")
        for o in missing[:4]:
            lines.append(f"- {o.get('question', o.get('id', ''))}")

    # Coverage
    required_answered = sufficiency.get("required_answered", 0)
    required_total = sufficiency.get("required_total", 0)
    missing_sources = sufficiency.get("missing_required_sources", []) or []
    contradictions = sufficiency.get("contradicted_obligations", []) or []
    blocked = sufficiency.get("blocked_obligations", []) or []
    recommended = sufficiency.get("recommended_status", "PARTIAL")
    confidence = sufficiency.get("confidence_cap", "LOW")

    lines.append(f"\nCoverage: {required_answered}/{required_total} obligations answered")
    if missing_sources:
        lines.append(f"Missing sources: {', '.join(str(s) for s in missing_sources[:4])}")
    if contradictions:
        lines.append(f"Contradictions: {', '.join(str(c) for c in contradictions[:4])}")
    if blocked:
        lines.append(f"Blocked: {', '.join(str(b) for b in blocked[:4])}")

    # Claims
    claims = list(claim_graph.get("claims", []) or []) if claim_graph else []
    if claims:
        lines.append(f"\nClaims ({len(claims)}):")
        for c in claims[:5]:
            text = str(c.get("text", c.get("claim", "")))[:150]
            confidence_c = c.get("confidence", "")
            lines.append(f"- [{confidence_c}] {text}")

    # Files
    files = getattr(ledger, "files_inspected", {}) or {}
    if files:
        lines.append("\nFiles inspected:")
        for path, entry in list(files.items())[:6]:
            status = "complete" if getattr(entry, "complete", False) else "partial"
            lines.append(f"- {path} [{status}]")

    # Required output
    lines.append(f"\nRecommended status: {recommended}")
    lines.append(f"Confidence cap: {confidence}")
    lines.append("")
    lines.append("Return a ValidatedReportV1 with: status, findings, uncertainties, confidence, caveats, escalation_recommendation.")
    lines.append("Do not invent evidence. Do not claim broader verification than listed above.")
    lines.append("Use only the evidence refs provided. Mark COMPLETE only if all required obligations are satisfied.")

    payload = "\n".join(lines)
    if len(payload) > max_chars:
        # Drop claims first, then files
        payload = "\n".join(lines[:lines.index("\nFiles inspected:")] if "\nFiles inspected:" in payload else lines[:len(lines)//2])
        if len(payload) > max_chars:
            payload = payload[:max_chars - 100] + "\n\n[truncated — full evidence in artifacts]"
    return payload


def should_attempt_closer(
    mission: Any,
    deadline_remaining: float,
    *,
    closer_budget_seconds: int = 30,
    terminal_reserve_seconds: int = 10,
) -> bool:
    """Check whether there is enough time for a closer model call."""
    return deadline_remaining > (closer_budget_seconds + terminal_reserve_seconds)


def compute_closure_budgets(total_seconds: float) -> JSON:
    """Compute budget split for exploration vs closure."""
    exploration = total_seconds * 0.70
    closer = total_seconds * 0.20
    reserve = total_seconds * 0.10
    return {
        "total_seconds": total_seconds,
        "exploration_budget": exploration,
        "closer_budget": closer,
        "terminal_reserve": reserve,
    }


def record_closure_attempt(
    mission_dir: str,
    attempt_id: str,
    *,
    closer_model: str = "",
    closure_strategy: str = "same_model_final_report",
    call_type: str = "",
    started_at: str = "",
    elapsed_seconds: float = 0,
    timeout_seconds: float = 0,
    payload_chars: int = 0,
    deadline_remaining_seconds: float = 0,
    result: str = "",
    error: str = "",
    error_type: str = "",
    report_valid: bool = False,
    repair_attempted: bool = False,
    final_closure_source: str = "",
):
    """Record a closure attempt to closure_attempts.jsonl."""
    path = os.path.join(mission_dir, "closure_attempts.jsonl")
    entry = {
        "attempt_id": attempt_id,
        "closer_model": closer_model,
        "closure_strategy": closure_strategy,
        "call_type": call_type,
        "started_at": started_at or str(int(time.time())),
        "elapsed_seconds": round(elapsed_seconds, 1),
        "timeout_seconds": round(timeout_seconds, 1),
        "payload_chars": payload_chars,
        "deadline_remaining_seconds": round(deadline_remaining_seconds, 1),
        "result": result,
        "error": error[:200] if error else "",
        "error_type": error_type,
        "report_valid": report_valid,
        "repair_attempted": repair_attempted,
        "final_closure_source": final_closure_source,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


def build_report_skeleton(
    mission: Any,
    answer_graph: JSON,
) -> JSON:
    """Build a report skeleton the closer fills in, not writes from scratch."""
    sufficiency = answer_graph.get("sufficiency", {}) or {}
    obligations = (
        list(answer_graph.get("required_obligations", []) or [])
        + list(answer_graph.get("optional_obligations", []) or [])
    )
    answered = [o for o in obligations if o.get("status") == "answered"]
    recommended = sufficiency.get("recommended_status", "PARTIAL")
    confidence = sufficiency.get("confidence_cap", "MEDIUM")

    required_findings = []
    for o in answered:
        refs = list(o.get("evidence_refs", []) or [])[:3]
        required_findings.append({
            "obligation_id": str(o.get("id", "") or ""),
            "claim": f"Evidence was gathered for: {str(o.get('question', ''))[:120]}",
            "evidence_refs": refs,
        })

    missing_sources = sufficiency.get("missing_required_sources", []) or []
    contradictions = sufficiency.get("contradicted_obligations", []) or []
    blocked = sufficiency.get("blocked_obligations", []) or []

    required_caveats = []
    if missing_sources:
        required_caveats.append(f"Required sources not inspected: {', '.join(str(s) for s in missing_sources[:4])}")
    if contradictions:
        required_caveats.append(f"Contradictions unresolved: {', '.join(str(c) for c in contradictions[:4])}")
    if blocked:
        required_caveats.append(f"Blocked sources: {', '.join(str(b) for b in blocked[:4])}")

    return {
        "schema_version": "report_skeleton.v1",
        "status": recommended if recommended != "ESCALATE" else "PARTIAL",
        "allowed_statuses": [recommended] if recommended in ("COMPLETE", "PARTIAL", "FAILED") else ["PARTIAL", "ESCALATE"],
        "confidence": confidence,
        "required_findings": required_findings,
        "required_caveats": required_caveats,
        "forbidden_claims": ["full repo-wide behavior was verified"],
    }


def build_skeleton_closer_payload(
    mission: Any,
    skeleton: JSON,
    *,
    max_chars: int = 6000,
) -> str:
    """Build a minimal closer prompt from a report skeleton."""
    lines = []
    lines.append("Fill in this report skeleton. Do not change status. Do not invent evidence.")
    lines.append("")
    lines.append(f"Status: {skeleton.get('status', 'PARTIAL')}")
    lines.append(f"Allowed statuses: {', '.join(skeleton.get('allowed_statuses', []))}")
    lines.append(f"Confidence: {skeleton.get('confidence', 'MEDIUM')}")
    lines.append("")

    findings = skeleton.get("required_findings", []) or []
    if findings:
        lines.append("Required findings:")
        for f in findings:
            lines.append(f"- [{f.get('obligation_id', '')}] {f.get('claim', '')}")
            refs = ", ".join(f.get("evidence_refs", []) or [])
            if refs:
                lines.append(f"  Evidence: {refs}")

    caveats = skeleton.get("required_caveats", []) or []
    if caveats:
        lines.append("\nRequired caveats:")
        for c in caveats:
            lines.append(f"- {c}")

    lines.append(f"\nForbidden: {', '.join(skeleton.get('forbidden_claims', []))}")
    lines.append("")
    lines.append("Return a ValidatedReportV1 JSON using only the provided findings and evidence refs.")
    lines.append("Do not add new claims. Do not change status upward.")

    payload = "\n".join(lines)
    if len(payload) > max_chars:
        payload = payload[:max_chars - 50] + "\n\n[truncated]"
    return payload


def build_targeted_repair_prompt(
    report: JSON,
    semantic_result: JSON,
    skeleton: JSON,
) -> str:
    """Build a repair prompt for a semantically incomplete closer report."""
    codes = semantic_result.get("reason_codes", []) or []
    repairs = semantic_result.get("required_repairs", []) or []
    lines = [
        "Your report was structurally valid but semantically incomplete.",
        "",
        f"Problems: {', '.join(codes)}",
    ]
    if repairs:
        lines.append("Required fixes:")
        for r in repairs:
            lines.append(f"- {r}")
    lines.append("")
    lines.append("Use this skeleton. Return only a corrected ValidatedReportV1 JSON.")
    lines.append(f"Status: {skeleton.get('status', 'PARTIAL')}")
    lines.append(f"Allowed statuses: {', '.join(skeleton.get('allowed_statuses', []))}")
    lines.append(f"Confidence: {skeleton.get('confidence', 'MEDIUM')}")
    findings = skeleton.get("required_findings", []) or []
    if findings:
        lines.append("Required findings to include:")
        for f in findings:
            lines.append(f"- {f.get('claim', '')}")
    lines.append("Do not invent evidence. Do not change status upward.")
    return "\n".join(lines)
