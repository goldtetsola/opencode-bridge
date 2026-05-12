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
    started_at: str = "",
    elapsed_seconds: float = 0,
    timeout_seconds: float = 0,
    payload_chars: int = 0,
    deadline_remaining_seconds: float = 0,
    result: str = "",
    error: str = "",
    report_valid: bool = False,
    final_closure_source: str = "",
):
    """Record a closure attempt to closure_attempts.jsonl."""
    path = os.path.join(mission_dir, "closure_attempts.jsonl")
    entry = {
        "attempt_id": attempt_id,
        "closer_model": closer_model,
        "closure_strategy": closure_strategy,
        "started_at": started_at or str(int(time.time())),
        "elapsed_seconds": round(elapsed_seconds, 1),
        "timeout_seconds": round(timeout_seconds, 1),
        "payload_chars": payload_chars,
        "deadline_remaining_seconds": round(deadline_remaining_seconds, 1),
        "result": result,
        "error": error[:200] if error else "",
        "report_valid": report_valid,
        "final_closure_source": final_closure_source,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
