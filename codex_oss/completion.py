"""CompletionContractV1 + CompletionEnvelopeV1 — multi-axis mission completion.

Replaces the overloaded single "COMPLETE" status with orthogonal axes:
  answer_status     — was the core answer found?
  evidence_status   — was evidence sufficient?
  verification_status — was verification/contradiction/exploration completed?
  closure_status    — was closure model-owned or runtime-owned?
  quality_status    — is the result earned, truthful, suspicious, or false?

The final_status is derived from the envelope + mission's completion_contract.
"""

from __future__ import annotations

from typing import Any

JSON = dict[str, Any]


class AnswerStatus:
    ANSWERED = "ANSWERED"
    PARTIALLY_ANSWERED = "PARTIALLY_ANSWERED"
    UNANSWERED = "UNANSWERED"
    BLOCKED = "BLOCKED"


class EvidenceStatus:
    SUFFICIENT = "SUFFICIENT"
    PARTIAL = "PARTIAL"
    INSUFFICIENT = "INSUFFICIENT"
    CONTRADICTED = "CONTRADICTED"
    BLOCKED = "BLOCKED"


class VerificationStatus:
    SATISFIED = "SATISFIED"
    INCOMPLETE = "INCOMPLETE"
    NOT_REQUIRED = "NOT_REQUIRED"
    BLOCKED = "BLOCKED"


class ClosureStatus:
    MODEL_CLOSED = "MODEL_CLOSED"
    CLOSER_MODEL_CLOSED = "CLOSER_MODEL_CLOSED"
    MODEL_NARRATED_RUNTIME_CLOSED = "MODEL_NARRATED_RUNTIME_CLOSED"
    RUNTIME_CLOSED = "RUNTIME_CLOSED"
    DETERMINISTIC_FAST_PATH = "DETERMINISTIC_FAST_PATH"
    FAILED = "FAILED"


NATIVE_FEELING_CLOSURES = frozenset({
    ClosureStatus.MODEL_CLOSED,
    ClosureStatus.CLOSER_MODEL_CLOSED,
    ClosureStatus.MODEL_NARRATED_RUNTIME_CLOSED,
})


class QualityStatus:
    EARNED_COMPLETE = "EARNED_COMPLETE"
    EARNED_RUNTIME_COMPLETE = "EARNED_RUNTIME_COMPLETE"
    TRUTHFUL_PARTIAL = "TRUTHFUL_PARTIAL"
    RUNTIME_RESCUED = "RUNTIME_RESCUED"
    SUSPICIOUS_COMPLETE = "SUSPICIOUS_COMPLETE"
    FALSE_COMPLETE = "FALSE_COMPLETE"
    FAILED = "FAILED"


def build_completion_envelope(
    mission: Any,
    report: JSON,
    answer_graph: JSON,
    sufficiency: JSON | None,
    closure_attempts: list[JSON] | None,
) -> JSON:
    """Build a multi-axis completion envelope from mission state."""
    report = report or {}
    answer_graph = answer_graph or {}
    sufficiency = sufficiency or answer_graph.get("sufficiency", {}) or {}

    # ── answer_status ──
    required_answered = sufficiency.get("required_answered", 0)
    required_total = sufficiency.get("required_total", 0)
    blocked_obligations = sufficiency.get("blocked_obligations", []) or []
    if blocked_obligations:
        answer_status = AnswerStatus.BLOCKED
    elif required_total > 0 and required_answered >= required_total:
        answer_status = AnswerStatus.ANSWERED
    elif required_answered > 0:
        answer_status = AnswerStatus.PARTIALLY_ANSWERED
    else:
        answer_status = AnswerStatus.UNANSWERED

    # ── evidence_status ──
    contradicted = sufficiency.get("contradicted_obligations", []) or []
    blocked = sufficiency.get("blocked_obligations", []) or []
    insufficient = sufficiency.get("insufficient_evidence_obligations", []) or []
    missing_sources = sufficiency.get("missing_required_sources", []) or []
    if blocked:
        evidence_status = EvidenceStatus.BLOCKED
    elif contradicted:
        evidence_status = EvidenceStatus.CONTRADICTED
    elif insufficient:
        evidence_status = EvidenceStatus.INSUFFICIENT
    elif missing_sources:
        evidence_status = EvidenceStatus.PARTIAL
    elif required_answered >= required_total and required_total > 0:
        evidence_status = EvidenceStatus.SUFFICIENT
    else:
        evidence_status = EvidenceStatus.PARTIAL

    # ── verification_status ──
    exploration_policy = getattr(mission, "exploration_policy", {}) or {}
    require_contradiction = bool(exploration_policy.get("require_contradiction_search", False))
    min_optional = int(exploration_policy.get("min_optional_actions_after_floor", 0) or 0)
    contradiction_done = bool((report.get("contradiction_search_done", False)) or
                              any("contradiction" in str(a.get("result", "")).lower()
                                  for a in (closure_attempts or [])))
    optional_done = int(report.get("optional_exploration_actions", 0) or
                        report.get("optional_exploration_count", 0))
    if not require_contradiction and min_optional <= 0:
        verification_status = VerificationStatus.NOT_REQUIRED
    elif require_contradiction and not contradiction_done:
        verification_status = VerificationStatus.INCOMPLETE
    elif min_optional > 0 and optional_done < min_optional:
        verification_status = VerificationStatus.INCOMPLETE
    else:
        verification_status = VerificationStatus.SATISFIED

    # ── closure_status ──
    closure_source = str(report.get("closure_source", "") or "")
    if "deterministic" in closure_source.lower() or "fast_path" in closure_source.lower():
        closure_status = ClosureStatus.DETERMINISTIC_FAST_PATH
    elif "model_report_downgraded" in closure_source:
        closure_status = ClosureStatus.RUNTIME_CLOSED
    elif "model_report" in closure_source:
        closure_status = ClosureStatus.MODEL_CLOSED
    elif "model_narrated_runtime" in closure_source.lower():
        closure_status = ClosureStatus.MODEL_NARRATED_RUNTIME_CLOSED
    elif "runtime_answer_graph" in closure_source:
        closure_status = ClosureStatus.RUNTIME_CLOSED
    else:
        closure_status = ClosureStatus.RUNTIME_CLOSED

    # ── quality_status ──
    status = str(report.get("status", "") or "").upper()
    can_close = sufficiency.get("can_close", False)
    entitlement = sufficiency.get("closure_entitlement", {}) or {}
    can_return = entitlement.get("can_return_complete", can_close)
    is_model_closed = closure_status == ClosureStatus.MODEL_CLOSED
    is_runtime_closed = closure_status == ClosureStatus.RUNTIME_CLOSED
    is_deterministic = closure_status == ClosureStatus.DETERMINISTIC_FAST_PATH

    # False COMPLETE checks
    if status == "COMPLETE" and not can_return:
        quality_status = QualityStatus.FALSE_COMPLETE
    elif status == "COMPLETE" and missing_sources:
        quality_status = QualityStatus.FALSE_COMPLETE
    elif status == "COMPLETE" and insufficient:
        quality_status = QualityStatus.FALSE_COMPLETE
    # Suspicious checks
    elif status == "COMPLETE" and report.get("semantic_gate_evaluated") is False and not is_deterministic:
        quality_status = QualityStatus.SUSPICIOUS_COMPLETE
    elif status == "COMPLETE" and not report.get("findings"):
        quality_status = QualityStatus.SUSPICIOUS_COMPLETE
    # Earned
    elif status == "COMPLETE" and is_model_closed and can_return:
        quality_status = QualityStatus.EARNED_COMPLETE
    elif status == "COMPLETE" and is_runtime_closed and can_return:
        quality_status = QualityStatus.EARNED_RUNTIME_COMPLETE
    elif status == "COMPLETE" and is_deterministic:
        quality_status = QualityStatus.EARNED_COMPLETE
    elif status in ("PARTIAL", "ESCALATE") and (required_answered > 0 or can_close):
        quality_status = QualityStatus.TRUTHFUL_PARTIAL
    elif status in ("PARTIAL", "ESCALATE"):
        quality_status = QualityStatus.RUNTIME_RESCUED
    elif status == "FAILED":
        quality_status = QualityStatus.FAILED
    else:
        quality_status = QualityStatus.TRUTHFUL_PARTIAL

    return {
        "schema_version": "completion_envelope.v1",
        "answer_status": answer_status,
        "evidence_status": evidence_status,
        "verification_status": verification_status,
        "closure_status": closure_status,
        "quality_status": quality_status,
        "final_status": status,
    }


def derive_final_status(envelope: JSON, contract: JSON | None) -> str:
    """Derive final status from envelope + completion contract."""
    contract = contract or {}
    verification_required = bool(contract.get("verification_requirements", {}).get("contradiction_search_required", False)
                                 or contract.get("verification_requirements", {}).get("optional_exploration_required", False))
    model_closure_required = bool(contract.get("closure_requirements", {}).get("model_self_closure_required", False))
    runtime_closure_allowed = bool(contract.get("closure_requirements", {}).get("runtime_closure_allowed", True))

    answer = envelope.get("answer_status", "")
    evidence = envelope.get("evidence_status", "")
    verification = envelope.get("verification_status", "")
    closure = envelope.get("closure_status", "")

    if answer == AnswerStatus.BLOCKED or evidence == EvidenceStatus.BLOCKED:
        return "ESCALATE"
    if evidence == EvidenceStatus.CONTRADICTED:
        return "ESCALATE"
    if answer == AnswerStatus.ANSWERED and evidence == EvidenceStatus.SUFFICIENT:
        if verification_required and verification == VerificationStatus.INCOMPLETE:
            return "PARTIAL"
        if model_closure_required and closure not in (ClosureStatus.MODEL_CLOSED, ClosureStatus.CLOSER_MODEL_CLOSED):
            return "PARTIAL"
        return "COMPLETE"
    if answer == AnswerStatus.ANSWERED and not runtime_closure_allowed and closure == ClosureStatus.RUNTIME_CLOSED:
        return "PARTIAL"
    if answer in (AnswerStatus.ANSWERED, AnswerStatus.PARTIALLY_ANSWERED):
        return "PARTIAL"
    return "FAILED"


def default_completion_contract(mission: Any) -> JSON:
    """Generate a default completion contract based on mission properties."""
    style = str(getattr(mission, "objective_style", "") or "")
    exploration = getattr(mission, "exploration_policy", {}) or {}
    require_contradiction = bool(exploration.get("require_contradiction_search", False))
    min_optional = int(exploration.get("min_optional_actions_after_floor", 0) or 0)

    if style == "deterministic_lookup":
        return {
            "primary_goal": "deterministic_lookup",
            "required_axes": {"answer": True, "evidence": True, "verification": False, "model_self_closure": False},
            "verification_requirements": {"contradiction_search_required": False, "optional_exploration_required": False},
            "closure_requirements": {"runtime_closure_allowed": True, "model_self_closure_required": False},
            "status_policy": {"runtime_closure": "ALLOWED"},
        }
    if style == "open_investigation":
        return {
            "primary_goal": "answer_finding",
            "required_axes": {"answer": True, "evidence": True, "verification": require_contradiction or min_optional > 0, "model_self_closure": False},
            "verification_requirements": {"contradiction_search_required": require_contradiction, "optional_exploration_required": min_optional > 0},
            "closure_requirements": {"runtime_closure_allowed": True, "model_self_closure_required": False},
            "status_policy": {"answer_found_without_verification": "PARTIAL" if require_contradiction else "COMPLETE", "runtime_closure": "ALLOWED"},
        }
    return {
        "primary_goal": "answer_finding",
        "required_axes": {"answer": True, "evidence": True, "verification": False, "model_self_closure": False},
        "verification_requirements": {"contradiction_search_required": False, "optional_exploration_required": False},
        "closure_requirements": {"runtime_closure_allowed": True, "model_self_closure_required": False},
        "status_policy": {"runtime_closure": "ALLOWED"},
    }
