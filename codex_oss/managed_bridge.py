"""Bridge adapter for A2/A3 managed investigation missions.

This module keeps the HTTP handler out of the mission runtime. It owns only the
translation from a Responses request body to a validated runtime report.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict

from codex_oss.decision_trace import append_decision, write_decision_trace
from codex_oss.fast_path import run_deterministic_fast_path
from codex_oss.implementation import run_implementation_mission
from codex_oss.ledger import EvidenceLedger
from codex_oss.mission import InvalidHandoffError, _build_mission
from codex_oss.claim_graph import refresh_claim_graph
from codex_oss.answer_graph import build_investigation_plan, refresh_answer_graph
from codex_oss.runtime.loop import run_loop
from codex_oss.runtime.policy import (
    acquire_mission_slot,
    build_deterministic_partial_report,
    extract_single_handoff_block,
    release_mission_slot,
)
from codex_oss.validation import render_report

JSON = Dict[str, Any]

RUNTIME_MODEL_ALIASES = {
    "mission-a2-kimi": "ocg-kimi-k2.6",
    "mission-a3-kimi": "ocg-kimi-k2.6",
    "mission-a2-deepseek": "ocg-deepseek-v4-pro",
    "mission-a3-deepseek": "ocg-deepseek-v4-pro",
    "mission-a2-flash": "ocg-deepseek-v4-flash",
    "mission-a3-flash": "ocg-deepseek-v4-flash",
    "mission-a4-kimi": "ocg-kimi-k2.6",
    "mission-a4-deepseek": "ocg-deepseek-v4-pro",
    "mission-a4-flash": "ocg-deepseek-v4-flash",
    "mission-a5-kimi": "ocg-kimi-k2.6",
    "mission-a5-deepseek": "ocg-deepseek-v4-pro",
    "mission-a5-flash": "ocg-deepseek-v4-flash",
    "mission-a6-kimi": "ocg-kimi-k2.6",
    "mission-a6-deepseek": "ocg-deepseek-v4-pro",
    "mission-a6-flash": "ocg-deepseek-v4-flash",
}

RUNTIME_MODEL_FALLBACKS = {
    "mission-a2-kimi": ["ocg-deepseek-v4-flash"],
    "mission-a3-kimi": ["ocg-deepseek-v4-pro", "ocg-deepseek-v4-flash"],
    "mission-a2-deepseek": ["ocg-deepseek-v4-flash", "ocg-kimi-k2.6"],
    "mission-a3-deepseek": ["ocg-kimi-k2.6", "ocg-deepseek-v4-flash"],
    "mission-a2-flash": ["ocg-kimi-k2.6"],
    "mission-a3-flash": ["ocg-kimi-k2.6"],
    "mission-a4-kimi": ["ocg-deepseek-v4-pro", "ocg-deepseek-v4-flash"],
    "mission-a4-deepseek": ["ocg-kimi-k2.6", "ocg-deepseek-v4-flash"],
    "mission-a4-flash": ["ocg-kimi-k2.6"],
    "mission-a5-kimi": ["ocg-deepseek-v4-pro", "ocg-deepseek-v4-flash"],
    "mission-a5-deepseek": ["ocg-kimi-k2.6", "ocg-deepseek-v4-flash"],
    "mission-a5-flash": ["ocg-kimi-k2.6"],
    "mission-a6-kimi": ["ocg-deepseek-v4-pro"],
    "mission-a6-deepseek": ["ocg-kimi-k2.6"],
    "mission-a6-flash": ["ocg-kimi-k2.6"],
}

RUNTIME_AUTONOMY_PROFILES = {
    "mission-a2-flash": {"max_tool_budget": 8, "max_time_seconds": 90},
    "mission-a3-flash": {"max_tool_budget": 8, "max_time_seconds": 90},
    "mission-a2-kimi": {"max_tool_budget": 10, "max_time_seconds": 90},
    "mission-a3-kimi": {"max_tool_budget": 14, "max_time_seconds": 150},
    "mission-a2-deepseek": {"max_tool_budget": 12, "max_time_seconds": 120},
    "mission-a3-deepseek": {"max_tool_budget": 20, "max_time_seconds": 180},
    "mission-a4-kimi": {"max_tool_budget": 14, "max_time_seconds": 150},
    "mission-a4-deepseek": {"max_tool_budget": 20, "max_time_seconds": 180},
    "mission-a4-flash": {"max_tool_budget": 8, "max_time_seconds": 90},
    "mission-a5-kimi": {"max_tool_budget": 14, "max_time_seconds": 150},
    "mission-a5-deepseek": {"max_tool_budget": 20, "max_time_seconds": 180},
    "mission-a5-flash": {"max_tool_budget": 8, "max_time_seconds": 90},
    "mission-a6-kimi": {"max_tool_budget": 20, "max_time_seconds": 180},
    "mission-a6-deepseek": {"max_tool_budget": 24, "max_time_seconds": 180},
    "mission-a6-flash": {"max_tool_budget": 10, "max_time_seconds": 120},
}


@dataclass
class ManagedMissionResult:
    handled: bool
    report_text: str = ""
    status: str = "PARTIAL"
    mission_id: str = ""


def extract_handoff_from_body(body: JSON) -> str:
    """Extract system/developer/user text from a Responses request body."""
    text = ""
    for item in body.get("input", []):
        role = item.get("role", "")
        content = item.get("content", "")
        if role not in ("system", "developer", "user") or not content:
            continue
        if isinstance(content, str):
            text += content + "\n"
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    text += part.get("text", "") + "\n"
    return text


def run_managed_mission_from_body(
    body: JSON,
    raw_model_alias: str,
    log_fn: Callable[..., None],
    call_payload_fn: Callable[[JSON, float], JSON],
    map_model_fn: Callable[[str], str],
    request_deadline: float,
) -> ManagedMissionResult:
    """Run an A2/A3 MissionV1 if present; return handled=False otherwise."""
    handoff = extract_handoff_from_body(body)
    runtime_alias = raw_model_alias in RUNTIME_MODEL_ALIASES
    try:
        mission_block = extract_single_handoff_block(handoff) if handoff else None
    except ValueError as exc:
        log_fn("mission_entrypoint_invalid", error=str(exc))
        return ManagedMissionResult(
            handled=True,
            status="FAILED",
            report_text=render_report(_failure_report("unknown", f"invalid entrypoint: {exc}")),
        )

    if not mission_block or "oss_agent_mission.v1" not in mission_block:
        if runtime_alias:
            return ManagedMissionResult(
                handled=True,
                status="FAILED",
                report_text=render_report(_failure_report(
                    "unknown",
                    "runtime-controlled OSS agent requires exactly one OSS_HANDOFF_JSON MissionV1 block",
                )),
            )
        return ManagedMissionResult(handled=False)

    mission_id = "unknown"
    try:
        raw = json.loads(mission_block)
        mission = _build_mission(raw)
        mission_id = mission.mission_id
        mission.decision_trace = []
        mission.runtime_model_alias = raw_model_alias
        append_decision(
            mission,
            decision_type="entry_validation",
            result="accepted",
            policy="EntryPointExtractionPolicyV1",
            reason="MissionV1 handoff parsed and validated",
            source_module="codex_oss/managed_bridge.py",
            input_payload={"tier": mission.tier, "mode": mission.mode, "runtime_alias": raw_model_alias},
        )
        effective_deadline = _managed_runtime_deadline(mission, request_deadline)
        _apply_runtime_autonomy_profile(mission, raw_model_alias, effective_deadline)
        log_fn("mission_dispatch", tier=mission.tier, mode=mission.mode, mission_id=mission.mission_id)

        if mission.tier not in ("A2", "A3"):
            if mission.tier not in ("A4", "A5", "A6"):
                return ManagedMissionResult(handled=False)

        ledger = EvidenceLedger(mission_id=mission.mission_id, tool_budget_remaining=mission.tool_budget)

        def call_model(messages, tools, timeout, model_alias_override=None):
            selected_alias = str(model_alias_override or raw_model_alias or "ocg-kimi-k2.6")
            primary = RUNTIME_MODEL_ALIASES.get(selected_alias, selected_alias)
            candidates = [primary]
            for fallback in RUNTIME_MODEL_FALLBACKS.get(selected_alias, []):
                if fallback not in candidates:
                    candidates.append(fallback)
            last_exc: Exception | None = None
            deadline = time.monotonic() + max(1.0, float(timeout or 1.0))
            for idx, reasoning_model in enumerate(candidates):
                remaining = deadline - time.monotonic()
                if remaining <= 1.0:
                    break
                payload = {
                    "model": map_model_fn(reasoning_model),
                    "messages": messages,
                    "stream": False,
                    "tools": [],
                }
                if idx:
                    log_fn("mission_model_fallback_attempt", mission_id=mission.mission_id, model=payload["model"])
                try:
                    response = call_payload_fn(payload, remaining)
                    mission.last_reasoning_model = payload["model"]
                    history = list(getattr(mission, "reasoning_model_history", []) or [])
                    history.append(payload["model"])
                    mission.reasoning_model_history = history
                    mission.fallback_model_used = bool(getattr(mission, "fallback_model_used", False) or idx)
                    if idx:
                        log_fn("mission_model_fallback_ok", mission_id=mission.mission_id, model=payload["model"])
                    return response
                except Exception as exc:
                    last_exc = exc
                    if idx:
                        log_fn(
                            "mission_model_fallback_failed",
                            mission_id=mission.mission_id,
                            model=payload["model"],
                            error=str(exc),
                        )
                    else:
                        log_fn(
                            "mission_model_primary_failed",
                            mission_id=mission.mission_id,
                            model=payload["model"],
                            error=str(exc),
                        )
            if last_exc:
                raise last_exc
            raise RuntimeError("no runtime reasoning model candidates")

        if mission.tier in ("A4", "A5", "A6"):
            acquired, slot_reason = acquire_mission_slot(mission)
            if not acquired:
                append_decision(
                    mission,
                    decision_type="scheduler",
                    result="rejected",
                    policy="ConcurrencyPolicyV1",
                    reason=slot_reason,
                    source_module="codex_oss/runtime/policy.py",
                    input_payload={"tier": mission.tier, "apply_mode": getattr(mission, "apply_mode", "")},
                )
                report = build_deterministic_partial_report(mission, ledger, f"capacity_timeout:{slot_reason}")
                report["status"] = "FAILED"
                return ManagedMissionResult(
                    handled=True,
                    status="FAILED",
                    mission_id=mission.mission_id,
                    report_text=render_report(report),
                )
            append_decision(
                mission,
                decision_type="scheduler",
                result="accepted",
                policy="ConcurrencyPolicyV1",
                reason="mission acquired a scheduler slot",
                source_module="codex_oss/runtime/policy.py",
                input_payload={"tier": mission.tier, "apply_mode": getattr(mission, "apply_mode", "")},
            )
            try:
                result = run_implementation_mission(
                    mission=mission,
                    raw_model_alias=raw_model_alias,
                    handoff=handoff,
                    call_model=call_model,
                    timeout=effective_deadline,
                    project_root=os.getcwd(),
                )
            finally:
                release_mission_slot(mission.mission_id)
            return ManagedMissionResult(
                handled=True,
                status=str(result.get("status", "FAILED")),
                mission_id=mission.mission_id,
                report_text=str(result.get("text", "")),
            )

        fast_path_result = run_deterministic_fast_path(mission, ledger)
        if fast_path_result is not None:
            append_decision(
                mission,
                decision_type="fast_path",
                result="used",
                policy="DeterministicFastPathV1",
                reason=f"Objective satisfied by deterministic {fast_path_result.get('fast_path', 'fast_path')}",
                source_module="codex_oss/fast_path.py",
                input_payload={"objective_type": fast_path_result.get("fast_path", "")},
            )
            report = fast_path_result.get("report", {})
            if isinstance(report, dict):
                _write_readonly_mission_artifacts(mission, ledger, report, str(fast_path_result.get("status", "PARTIAL")))
            return ManagedMissionResult(
                handled=True,
                status=str(fast_path_result.get("status", "PARTIAL")),
                mission_id=mission.mission_id,
                report_text=render_report(report),
            )

        result = run_loop(
            mission,
            ledger,
            call_model,
            [],
            None,
            mission.allowed_roots,
            mission.allowed_paths,
            request_deadline=effective_deadline,
        )
        report = result.get("report", {})
        if isinstance(report, dict):
            _write_readonly_mission_artifacts(mission, ledger, report, str(result.get("status", "PARTIAL")))
        report_text = render_report(report) if isinstance(report, dict) else str(report)
        return ManagedMissionResult(
            handled=True,
            status=str(result.get("status", "PARTIAL")),
            mission_id=mission.mission_id,
            report_text=report_text,
        )
    except InvalidHandoffError as exc:
        log_fn("mission_invalid", error=str(exc), mission_id=mission_id)
        return ManagedMissionResult(
            handled=True,
            status="FAILED",
            mission_id=mission_id,
            report_text=render_report(_failure_report(mission_id, f"invalid OSS handoff schema: {exc}")),
        )
    except Exception as exc:
        log_fn("mission_crash", error=str(exc), mission_id=mission_id)
        report = build_deterministic_partial_report(_MissionStub(mission_id), None, f"runtime_crash:{exc}")
        report["status"] = "FAILED"
        report["caveats"].append("Runtime returned a terminal report instead of falling through to legacy continuation.")
        return ManagedMissionResult(
            handled=True,
            status="FAILED",
            mission_id=mission_id,
            report_text=render_report(report),
        )


def _failure_report(mission_id: str, reason: str) -> JSON:
    return {
        "oss_report_version": "1.0",
        "mission_id": mission_id,
        "status": "FAILED",
        "confidence": "LOW",
        "report_source": "runtime_entrypoint",
        "runtime_model_alias": "",
        "explorer_model": "",
        "finalizer_model": None,
        "fallback_model_used": False,
        "files_inspected": [],
        "commands_run": [],
        "findings": [],
        "uncertainties": [],
        "caveats": [reason],
        "escalation_recommendation": "GPT-5.5 review required",
        "missing_fields": [],
    }


class _MissionStub:
    def __init__(self, mission_id: str):
        self.mission_id = mission_id


def _write_readonly_mission_artifacts(mission: Any, ledger: Any, report: dict, status: str) -> None:
    try:
        from codex_oss.runtime.autonomy import grade_trace
    except Exception:
        grade_trace = None

    root = os.getcwd()
    mission_id = str(getattr(mission, "mission_id", "mission_unknown"))
    artifact_dir = os.path.join(root, ".codex-oss", "missions", mission_id)
    os.makedirs(artifact_dir, exist_ok=True)
    write_decision_trace(root, mission_id, mission)

    mission_payload = {
        "mission_id": mission_id,
        "tier": getattr(mission, "tier", ""),
        "mode": getattr(mission, "mode", ""),
        "apply_mode": getattr(mission, "apply_mode", ""),
        "objective": getattr(mission, "objective", ""),
        "objective_style": getattr(mission, "objective_style", ""),
        "risk_tier": getattr(mission, "risk_tier", ""),
        "allowed_roots": list(getattr(mission, "allowed_roots", []) or []),
        "allowed_paths": list(getattr(mission, "allowed_paths", []) or []),
        "phase_policy_enabled": bool(getattr(mission, "phase_policy_enabled", True)),
        "progress_policy_enabled": bool(getattr(mission, "progress_policy_enabled", True)),
        "objective_spec": dict(getattr(mission, "objective_spec", {}) or {}) if isinstance(getattr(mission, "objective_spec", None), dict) else None,
        "sufficiency_policy": dict(getattr(mission, "sufficiency_policy", {}) or {}),
    }
    ledger_payload = {
        "mission_id": mission_id,
        "tool_budget_remaining": int(getattr(ledger, "tool_budget_remaining", 0) or 0),
        "total_bytes_read": int(getattr(ledger, "total_bytes_read", 0) or 0),
        "redactions_applied": bool(getattr(ledger, "redactions_applied", False)),
        "duplicate_actions_blocked": int(getattr(ledger, "duplicate_actions_blocked", 0) or 0),
        "risk_flags": list(getattr(ledger, "risk_flags", []) or []),
        "files_inspected": [
            {
                "path": path,
                "complete": bool(entry.complete),
                "full_content_cached": bool(entry.full_content_cached),
                "chars_total": int(entry.chars_total),
                "chars_returned": int(entry.chars_returned),
                "sha256": entry.sha256,
                "tool": entry.tool,
                "extract_refs": [extract.get("id") for extract in (entry.extracts or []) if isinstance(extract, dict)],
            }
            for path, entry in (getattr(ledger, "files_inspected", {}) or {}).items()
        ],
        "commands_run": [
            {
                "tool": entry.tool,
                "args": dict(entry.args or {}),
                "exit_code": int(entry.exit_code),
                "matches_count": int(entry.matches_count),
                "stdout_sha256": entry.stdout_sha256,
                "extract_refs": [extract.get("id") for extract in (entry.extracts or []) if isinstance(extract, dict)],
            }
            for entry in (getattr(ledger, "commands_run", []) or [])
        ],
        "action_trace_count": len(getattr(ledger, "action_trace", []) or []),
    }
    claim_graph = refresh_claim_graph(mission, ledger, report=report, reason="artifact_write", persist=False)
    answer_graph = refresh_answer_graph(mission, ledger, claim_graph=claim_graph, report=report, reason="artifact_write", persist=False)
    coverage_graph = dict(answer_graph.get("coverage_graph", {}) or getattr(ledger, "coverage_graph", {}) or {})
    evidence_agenda = dict(answer_graph.get("evidence_agenda", {}) or getattr(ledger, "evidence_agenda", {}) or {})
    investigation_plan = build_investigation_plan(mission)
    trace_grading = grade_trace(ledger, report) if grade_trace else {
        "trace_grading_version": "1.0",
        "labels": ["unavailable"],
        "reasons": ["grade_trace import failed"],
        "counts": {},
    }
    summary_lines = [
        f"# Mission {mission_id}",
        "",
        f"- Status: {status}",
        f"- Objective: {getattr(mission, 'objective', '')}",
        f"- Objective Style: {getattr(mission, 'objective_style', '')}",
        f"- Trace Labels: {', '.join(trace_grading.get('labels', []))}",
        f"- Budget Remaining: {getattr(ledger, 'tool_budget_remaining', 0)}",
        f"- Claim Graph Main Claims: {claim_graph.get('sufficiency', {}).get('main_claim_count', 0)}",
        f"- Answer Obligations: {answer_graph.get('sufficiency', {}).get('required_answered', 0)}/{answer_graph.get('sufficiency', {}).get('required_total', 0)}",
        f"- Missing Required Sources: {', '.join(answer_graph.get('sufficiency', {}).get('missing_required_sources', []) or []) or '(none)'}",
        f"- Closure Source: {report.get('closure_source', report.get('report_source', ''))}",
        "",
        "## Findings",
    ]
    findings = report.get("findings", []) or []
    if findings:
        for finding in findings:
            if isinstance(finding, dict):
                summary_lines.append(f"- {finding.get('claim', '(missing claim)')}")
    else:
        summary_lines.append("- (none)")
    summary_lines.extend(["", "## Caveats"])
    caveats = report.get("caveats", []) or []
    if caveats:
        for caveat in caveats:
            summary_lines.append(f"- {caveat}")
    else:
        summary_lines.append("- (none)")
    summary_lines.extend(["", "## Trace Reasons"])
    for reason in trace_grading.get("reasons", []) or []:
        summary_lines.append(f"- {reason}")

    _write_json(os.path.join(artifact_dir, "mission.json"), mission_payload)
    _write_json(os.path.join(artifact_dir, "ledger.json"), ledger_payload)
    _write_json(os.path.join(artifact_dir, "report.json"), report)
    _write_json(os.path.join(artifact_dir, "claim_graph.json"), claim_graph)
    _write_json(os.path.join(artifact_dir, "investigation_plan.json"), investigation_plan)
    _write_json(os.path.join(artifact_dir, "answer_graph.json"), answer_graph)
    _write_json(os.path.join(artifact_dir, "coverage_graph.json"), coverage_graph)
    _write_json(os.path.join(artifact_dir, "evidence_agenda.json"), evidence_agenda)
    _write_json(os.path.join(artifact_dir, "trace_grading.json"), trace_grading)
    _write_text(os.path.join(artifact_dir, "trace.jsonl"), _readonly_trace_jsonl(ledger, report))
    _write_text(os.path.join(artifact_dir, "summary.md"), "\n".join(summary_lines) + "\n")


def _readonly_trace_jsonl(ledger: Any, report: dict) -> str:
    raw_entries = list(getattr(ledger, "action_trace", []) or [])
    entries = [_trace_entry_payload(entry) for entry in raw_entries]
    if not entries:
        entries = []
        for idx, command in enumerate(getattr(ledger, "commands_run", []) or [], start=1):
            entries.append({
                "turn": idx,
                "phase": "VERIFY",
                "action_type": "tool_call",
                "tool_name": getattr(command, "tool", ""),
                "raw_arguments": dict(getattr(command, "args", {}) or {}),
                "normalized_arguments": dict(getattr(command, "args", {}) or {}),
                "runtime_decision": "allowed",
                "decision_reason": "tool executed",
                "tool_result_summary": {
                    "exit_code": int(getattr(command, "exit_code", 0) or 0),
                    "matches_count": int(getattr(command, "matches_count", 0) or 0),
                },
                "why_not_report_yet": "Runtime gathered required evidence before finalizing the report.",
                "report_status": str(report.get("status", "") or ""),
                "report_source": str(report.get("report_source", "") or ""),
            })
    return "".join(json.dumps(entry, sort_keys=True) + "\n" for entry in entries)


def _trace_entry_payload(entry: Any) -> dict[str, Any]:
    if isinstance(entry, dict):
        return dict(entry)
    if hasattr(entry, "__dict__"):
        payload = {}
        for key, value in vars(entry).items():
            if isinstance(value, dict):
                payload[key] = dict(value)
            elif isinstance(value, list):
                payload[key] = list(value)
            else:
                payload[key] = value
        return payload
    return {"value": str(entry)}


def _write_json(path: str, data: Any) -> None:
    _write_text(path, json.dumps(data, indent=2, sort_keys=True))


def _write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _managed_runtime_deadline(mission: Any, request_deadline: float) -> float:
    """Pick the runtime deadline for internally-managed A2/A3 missions.

    Runtime-backed agents do their whole plan-act-observe loop inside one
    provider request. The default bridge deadline is deliberately conservative
    for legacy continuation calls, but A3 missions may explicitly request a
    larger local runtime budget. Cap it so user-provided MissionV1 cannot turn
    into an unbounded request.
    """
    mission_time = float(getattr(mission, "time_budget_seconds", request_deadline) or request_deadline)
    max_deadline = float(os.getenv("RUNTIME_MISSION_MAX_DEADLINE_SECONDS", "180"))
    return min(max(float(request_deadline), mission_time), max_deadline)


def _apply_runtime_autonomy_profile(mission: Any, raw_model_alias: str, effective_deadline: float) -> None:
    """Apply model-specific adaptive autonomy caps after mission validation.

    MissionV1 remains the user/request contract. The profile is the runtime's
    local safety envelope for different reasoning engines: Flash gets short
    support budgets, Kimi gets normal A3 budgets, and DeepSeek gets more room
    for hypothesis-driven exploration when the mission deadline allows it.
    """
    profile = RUNTIME_AUTONOMY_PROFILES.get(raw_model_alias, {})
    if not profile:
        return

    requested_budget = int(getattr(mission, "tool_budget_requested", getattr(mission, "tool_budget", 0)) or 0)
    profile_budget = int(profile.get("max_tool_budget", getattr(mission, "tool_budget", 0)) or 0)
    reserve = float(os.getenv("DETERMINISTIC_PARTIAL_RESERVE_SECONDS", "5"))
    estimated_turn = float(os.getenv("ESTIMATED_A3_TURN_SECONDS", "8"))
    deadline_budget = max(1, int((max(0.0, effective_deadline - reserve)) // max(1.0, estimated_turn)))
    effective_budget = min(requested_budget or getattr(mission, "tool_budget", 1), profile_budget, deadline_budget)

    requested_time = int(getattr(mission, "time_budget_seconds", effective_deadline) or effective_deadline)
    profile_time = int(profile.get("max_time_seconds", requested_time) or requested_time)
    effective_time = int(min(requested_time, profile_time, effective_deadline))

    reasons = []
    if effective_budget < requested_budget:
        if profile_budget < requested_budget:
            reasons.append("model_profile")
        if deadline_budget < min(requested_budget, profile_budget):
            reasons.append("request_deadline")
    existing_reason = str(getattr(mission, "budget_reduction_reason", "") or "")
    if existing_reason and effective_budget < requested_budget:
        reasons.insert(0, existing_reason)

    mission.tool_budget = effective_budget
    mission.tool_budget_effective = effective_budget
    mission.time_budget_seconds = effective_time
    mission.budget_reduction_reason = "+".join(dict.fromkeys(r for r in reasons if r))
