"""AdaptiveAutonomyBudgetV1.

This module decides whether a proposed action is productive enough to execute.
It is deliberately separate from the runtime loop so autonomy limits are visible
policy instead of scattered if-statements.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProgressAssessment:
    decision: str = "allow"  # allow, redirect, finalize
    reason: str = ""
    phase: str = "EXPLORE"
    novelty: str = "unknown"
    specificity: str = "unknown"
    evidence_linkage: bool = False
    broad_searches_used: int = 0
    low_information_actions_used: int = 0
    duplicate_actions_used: int = 0
    suggested_message: str = ""
    risk_flags: list[str] = field(default_factory=list)


def assess_action_progress(mission: Any, ledger: Any, action: Any, deadline: Any) -> ProgressAssessment:
    """Apply adaptive autonomy limits to a normalized action before execution."""
    phase = mission_phase(ledger)
    if not bool(getattr(mission, "progress_policy_enabled", True)):
        return ProgressAssessment(phase=phase)

    tool_name = str(getattr(action, "tool_name", "") or "")
    args = getattr(action, "arguments", {}) if isinstance(getattr(action, "arguments", {}), dict) else {}
    duplicate_count = int(getattr(ledger, "duplicate_actions_blocked", 0) or 0)
    low_info_count = _low_information_count(ledger)
    broad_count = _broad_search_count(ledger)

    max_duplicates = int(getattr(mission, "max_duplicate_actions", 2))
    max_low_info = int(getattr(mission, "max_low_information_actions", 3))
    max_broad = int(getattr(mission, "max_broad_searches", 6))

    if duplicate_count >= max_duplicates:
        return ProgressAssessment(
            decision="finalize",
            reason=f"duplicate action budget exhausted ({duplicate_count}/{max_duplicates})",
            phase=phase,
            duplicate_actions_used=duplicate_count,
            low_information_actions_used=low_info_count,
            broad_searches_used=broad_count,
        )

    if low_info_count >= max_low_info:
        return ProgressAssessment(
            decision="finalize",
            reason=f"low-information action budget exhausted ({low_info_count}/{max_low_info})",
            phase=phase,
            duplicate_actions_used=duplicate_count,
            low_information_actions_used=low_info_count,
            broad_searches_used=broad_count,
        )

    has_file_evidence = bool(getattr(ledger, "files_inspected", {}) or {})
    has_any_evidence = has_file_evidence or bool(getattr(ledger, "commands_run", []) or [])
    rationale_present = _rationale_present(action)
    evidence_linked = _evidence_linked(action)

    if tool_name == "rtk_grep":
        broad = _is_broad_search(args)
        if broad and broad_count >= max_broad:
            return ProgressAssessment(
                decision="redirect",
                reason=f"broad search budget exhausted ({broad_count}/{max_broad})",
                phase=phase,
                novelty=_search_novelty(ledger, args),
                specificity="broad",
                evidence_linkage=evidence_linked,
                duplicate_actions_used=duplicate_count,
                low_information_actions_used=low_info_count,
                broad_searches_used=broad_count,
                suggested_message=(
                    "Broad exploration budget is exhausted. Use existing evidence, request a targeted read, "
                    "or return final_report."
                ),
            )

        if has_file_evidence and phase in ("VERIFY", "REPORT"):
            novelty = _search_novelty(ledger, args)
            if novelty == "repeated" and not evidence_linked:
                return ProgressAssessment(
                    decision="redirect",
                    reason="post-evidence search repeats prior pattern/path without evidence-linked rationale",
                    phase=phase,
                    novelty=novelty,
                    specificity="broad" if broad else "targeted",
                    evidence_linkage=evidence_linked,
                    duplicate_actions_used=duplicate_count,
                    low_information_actions_used=low_info_count,
                    broad_searches_used=broad_count,
                    suggested_message=(
                        "A similar search already ran after evidence was gathered. Use cached evidence or "
                        "return final_report unless you can name a new uncertainty."
                    ),
                )
            if not rationale_present:
                return ProgressAssessment(
                    decision="redirect",
                    reason="post-evidence search lacks hypothesis/expected_information_gain/why_not_report_yet",
                    phase=phase,
                    novelty=novelty,
                    specificity="broad" if broad else "targeted",
                    evidence_linkage=evidence_linked,
                    duplicate_actions_used=duplicate_count,
                    low_information_actions_used=low_info_count,
                    broad_searches_used=broad_count,
                    suggested_message=(
                        "After file evidence exists, another search must explain the hypothesis, expected "
                        "information gain, and why a report is not ready."
                    ),
                )

    remaining = float(deadline.remaining()) if deadline else 999
    low_budget = int(getattr(ledger, "tool_budget_remaining", 0)) <= max(1, int(getattr(mission, "tool_budget", 1)) // 4)
    if has_any_evidence and (remaining < 20 or low_budget) and phase in ("VERIFY", "REPORT"):
        return ProgressAssessment(
            decision="finalize",
            reason="evidence exists and adaptive deadline/budget policy requires closure",
            phase=phase,
            duplicate_actions_used=duplicate_count,
            low_information_actions_used=low_info_count,
            broad_searches_used=broad_count,
        )

    return ProgressAssessment(
        decision="allow",
        reason="action passes adaptive progress policy",
        phase=phase,
        novelty=_search_novelty(ledger, args) if tool_name == "rtk_grep" else "unknown",
        specificity="broad" if tool_name == "rtk_grep" and _is_broad_search(args) else "targeted",
        evidence_linkage=evidence_linked,
        duplicate_actions_used=duplicate_count,
        low_information_actions_used=low_info_count,
        broad_searches_used=broad_count,
    )


def mission_phase(ledger: Any) -> str:
    if getattr(ledger, "files_inspected", {}) and getattr(ledger, "claims", []):
        return "REPORT"
    if getattr(ledger, "files_inspected", {}):
        return "VERIFY"
    if getattr(ledger, "commands_run", []):
        return "NARROW"
    return "EXPLORE"


def _rationale_present(action: Any) -> bool:
    return all(
        str(getattr(action, field, "") or "").strip()
        for field in ("hypothesis", "expected_information_gain", "why_not_report_yet")
    )


def _evidence_linked(action: Any) -> bool:
    text = "\n".join(
        str(getattr(action, field, "") or "")
        for field in ("reason", "hypothesis", "expected_information_gain", "why_not_report_yet")
    ).lower()
    return any(term in text for term in ("verify", "uncertainty", "finding", "evidence", "confirm", "test", "call"))


def _is_broad_search(args: dict) -> bool:
    path = str(args.get("path", "") or "").strip().rstrip("/")
    pattern = str(args.get("pattern", "") or "")
    if path in ("", ".", "src", "docs", "scripts", "codex_oss"):
        return True
    if len(pattern.strip()) <= 3:
        return True
    if not args.get("include_glob") and not args.get("max_results"):
        return True
    return False


def _search_novelty(ledger: Any, args: dict) -> str:
    pattern = str(args.get("pattern", "") or "")
    path = str(args.get("path", "") or "")
    for command in getattr(ledger, "commands_run", []) or []:
        if getattr(command, "tool", "") != "rtk_grep":
            continue
        prev = getattr(command, "args", {}) or {}
        if str(prev.get("pattern", "") or "") == pattern and str(prev.get("path", "") or "") == path:
            return "repeated"
    return "novel"


def _broad_search_count(ledger: Any) -> int:
    count = 0
    for command in getattr(ledger, "commands_run", []) or []:
        if getattr(command, "tool", "") == "rtk_grep" and _is_broad_search(getattr(command, "args", {}) or {}):
            count += 1
    return count


def _low_information_count(ledger: Any) -> int:
    count = 0
    for trace in getattr(ledger, "action_trace", []) or []:
        if getattr(trace, "runtime_decision", "") in ("blocked", "duplicate", "repaired", "redirected"):
            count += 1
        elif getattr(trace, "information_gain", "") in ("none", "low"):
            count += 1
    return count
