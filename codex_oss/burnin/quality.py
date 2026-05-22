"""QualityValidityV1 — classifies result quality separately from mission status.

Distinguishes earned COMPLETE from suspicious COMPLETE, runtime rescue,
deterministic fast path, and truthful partial.
"""

from __future__ import annotations

from typing import Any

JSON = dict[str, Any]


def classify_quality_validity(
    mission: JSON | None,
    report: JSON | None,
    ledger: JSON | None,
    answer_graph: JSON | None,
    decision_trace: JSON | None,
) -> JSON:
    """Classify the quality of a mission result independently of its status."""
    mission = mission or {}
    report = report or {}
    ledger = ledger or {}
    answer_graph = answer_graph or {}
    status = str(report.get("status", "") or "")
    closure_source = str(report.get("closure_source", "") or "")
    objective_style = str(mission.get("objective_style", "") or "")

    is_deterministic = closure_source == "deterministic_fast_path" or _has_deterministic_source(closure_source, status)
    is_model_closed = closure_source in ("model_report", "model_report_downgraded")
    is_runtime_closed = "runtime_answer_graph" in closure_source or "runtime" in closure_source
    has_observations = _has_ledger_observations(ledger)
    operator_asserted = _is_operator_asserted(report)
    is_suspicious = _check_suspicious(mission, report, ledger, objective_style, has_observations)
    is_false = _check_false_complete(status, answer_graph, report, mission)

    if is_false:
        grade = "failed" if status != "COMPLETE" else QualityGrade.SUSPICIOUS_COMPLETE
        earned = False
        suspicious = True
    elif status == "COMPLETE":
        if is_deterministic:
            grade = QualityGrade.DETERMINISTIC_COMPLETE
            earned = True
        elif is_suspicious:
            grade = QualityGrade.SUSPICIOUS_COMPLETE
            earned = False
        elif has_observations and not operator_asserted:
            grade = QualityGrade.EARNED_COMPLETE if is_model_closed else QualityGrade.RUNTIME_COMPLETE
            earned = True
        else:
            grade = QualityGrade.RUNTIME_COMPLETE
            earned = not operator_asserted
    elif status in ("PARTIAL", "ESCALATE"):
        if has_observations:
            grade = QualityGrade.TRUTHFUL_PARTIAL
        else:
            grade = QualityGrade.RUNTIME_RESCUED_PARTIAL
        earned = False
    elif status == "FAILED":
        grade = QualityGrade.FAILED
        earned = False
    else:
        grade = QualityGrade.FAILED
        earned = False

    return {
        "grade": grade,
        "earned_complete": earned,
        "suspicious_complete": is_suspicious,
        "false_complete": is_false,
        "model_self_closed": is_model_closed,
        "runtime_closed": is_runtime_closed,
        "operator_asserted_evidence_floor": operator_asserted,
        "tool_observations_count": _observation_count(ledger),
        "evidence_refs_resolve": _refs_resolve(report),
        "required_sources_covered": _required_covered(answer_graph),
        "raw_dump_detected": _has_raw_dump(report),
        "gpt_cleanup_rating": _cleanup_rating(report),
    }


def is_earned_complete(result: JSON) -> bool:
    qv = result.get("quality_validity", {}) if isinstance(result, dict) else {}
    return bool(qv.get("earned_complete", False))


def detect_suspicious_complete(mission: JSON, report: JSON, ledger: JSON) -> JSON:
    has_obs = _has_ledger_observations(ledger)
    style = str((mission or {}).get("objective_style", "") or "")
    return {
        "suspicious_complete": _check_suspicious(mission, report, ledger, style, has_obs),
        "reasons": _suspicious_reasons(mission, report, ledger, style, has_obs),
    }


def is_false_complete(status: str, answer_graph: JSON, report: JSON) -> bool:
    return _check_false_complete(status, answer_graph, report)


def _has_ledger_observations(ledger: JSON | None) -> bool:
    if not ledger:
        return False
    commands = ledger.get("commands", ledger.get("commands_run"))
    if isinstance(commands, list) and len(commands) > 0:
        return True
    observations = ledger.get("observations")
    if isinstance(observations, list) and len(observations) > 0:
        return True
    return False


def _observation_count(ledger: JSON | None) -> int:
    if not ledger:
        return 0
    commands = ledger.get("commands", ledger.get("commands_run"))
    count = len(commands) if isinstance(commands, list) else 0
    observations = ledger.get("observations")
    count += len(observations) if isinstance(observations, list) else 0
    return count


def _is_operator_asserted(report: JSON | None) -> bool:
    if not report:
        return False
    caveats = " ".join(str(c) for c in (report.get("caveats", []) or []))
    if "operator asserted" in caveats.lower() or "operator-asserted" in caveats.lower():
        return True
    if "this agent performed zero file reads" in caveats.lower():
        return True
    if "evidence floor was covered" in caveats.lower() and "zero file reads" in caveats.lower():
        return True
    return False


def _check_suspicious(mission: JSON, report: JSON, ledger: JSON, style: str, has_obs: bool) -> bool:
    status = str((report or {}).get("status", "") or "")
    if status != "COMPLETE":
        return False
    if not has_obs and not _has_deterministic_source(
        str((report or {}).get("closure_source", "") or ""), status
    ):
        return True
    obligations = (mission or {}).get("answer_obligations")
    if style == "open_investigation" and (obligations is None or obligations == []):
        return True
    if _is_operator_asserted(report):
        return True
    caveats = " ".join(str(c) for c in ((report or {}).get("caveats", []) or []))
    if "evidence floor was covered" in caveats.lower() and not has_obs:
        return True
    # Hollow COMPLETE detection
    if status == "COMPLETE":
        findings = (report or {}).get("findings", []) or []
        missing = (report or {}).get("missing_fields", []) or []
        uncertainties = " ".join(str(u) for u in ((report or {}).get("uncertainties", []) or [])).lower()
        if not findings and style == "open_investigation":
            return True
        if any(str(m).strip() for m in missing):
            return True
        for pattern in ["target value was not retrieved", "file was not inspected", "unable to determine",
                         "answer was not retrieved", "not found"]:
            if pattern in uncertainties:
                return True
    return False


def _suspicious_reasons(mission: JSON, report: JSON, ledger: JSON, style: str, has_obs: bool) -> list[str]:
    reasons: list[str] = []
    if not has_obs and not _has_deterministic_source(
        str((report or {}).get("closure_source", "") or ""), str((report or {}).get("status", "") or "")
    ):
        reasons.append("no_ledger_observations")
    obligations = (mission or {}).get("answer_obligations")
    if style == "open_investigation" and (obligations is None or obligations == []):
        reasons.append("missing_answer_obligations")
    if _is_operator_asserted(report):
        reasons.append("operator_asserted_evidence_floor")
    return reasons


def _check_false_complete(status: str, answer_graph: JSON | None, report: JSON | None, mission: JSON | None = None) -> bool:
    if status != "COMPLETE":
        return False
    if not answer_graph:
        return False
    sufficiency = (answer_graph or {}).get("sufficiency", {}) or {}
    entitlement = sufficiency.get("closure_entitlement", {}) or {}
    if entitlement.get("can_return_complete") is False:
        return True
    if sufficiency.get("can_close") is False:
        return True
    missing = sufficiency.get("missing_required_sources", []) or []
    if missing:
        return True
    insufficient = sufficiency.get("insufficient_evidence_obligations", []) or []
    if insufficient:
        return True
    return False


def _has_deterministic_source(closure_source: str, status: str) -> bool:
    return "deterministic" in closure_source.lower() or "fast_path" in closure_source.lower()


def _refs_resolve(report: JSON | None) -> bool:
    if not report:
        return False
    caveats = " ".join(str(c) for c in ((report or {}).get("caveats", []) or []))
    if "evidence ref" in caveats.lower() and ("not found" in caveats.lower() or "not resolve" in caveats.lower()):
        return False
    return True


def _required_covered(answer_graph: JSON | None) -> bool:
    if not answer_graph:
        return True
    sufficiency = (answer_graph or {}).get("sufficiency", {}) or {}
    answered = sufficiency.get("required_answered", 0)
    total = sufficiency.get("required_total", 0)
    return total == 0 or answered >= total


def _has_raw_dump(report: JSON | None) -> bool:
    if not report:
        return False
    caveats = " ".join(str(c) for c in ((report or {}).get("caveats", []) or []))
    return "raw dump" in caveats.lower()


def _cleanup_rating(report: JSON | None) -> str:
    """Subjective GPT cleanup rating based on report quality signals."""
    if not report:
        return "major"
    caveats_count = len((report or {}).get("caveats", []) or [])
    missing = (report or {}).get("missing_required_sources", []) or []
    if missing:
        return "moderate"
    if caveats_count <= 2:
        return "minor"
    if caveats_count <= 4:
        return "moderate"
    return "major"


# ── Local aliases ──

from codex_oss.burnin.models import QualityGrade  # noqa: E402
