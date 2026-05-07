"""Bridge adapter for A2/A3 managed investigation missions.

This module keeps the HTTP handler out of the mission runtime. It owns only the
translation from a Responses request body to a validated runtime report.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict

from codex_oss.ledger import EvidenceLedger
from codex_oss.mission import InvalidHandoffError, _build_mission
from codex_oss.runtime.loop import run_loop
from codex_oss.runtime.policy import build_deterministic_partial_report, extract_single_handoff_block
from codex_oss.validation import render_report

JSON = Dict[str, Any]

RUNTIME_MODEL_ALIASES = {
    "mission-a2-kimi": "ocg-kimi-k2.6",
    "mission-a3-kimi": "ocg-kimi-k2.6",
    "mission-a2-deepseek": "ocg-deepseek-v4-pro",
    "mission-a3-deepseek": "ocg-deepseek-v4-pro",
    "mission-a2-flash": "ocg-deepseek-v4-flash",
    "mission-a3-flash": "ocg-deepseek-v4-flash",
}

RUNTIME_MODEL_FALLBACKS = {
    "mission-a2-kimi": ["ocg-deepseek-v4-flash"],
    "mission-a3-kimi": ["ocg-deepseek-v4-pro", "ocg-deepseek-v4-flash"],
    "mission-a2-deepseek": ["ocg-deepseek-v4-flash", "ocg-kimi-k2.6"],
    "mission-a3-deepseek": ["ocg-kimi-k2.6", "ocg-deepseek-v4-flash"],
    "mission-a2-flash": ["ocg-kimi-k2.6"],
    "mission-a3-flash": ["ocg-kimi-k2.6"],
}

RUNTIME_AUTONOMY_PROFILES = {
    "mission-a2-flash": {"max_tool_budget": 8, "max_time_seconds": 90},
    "mission-a3-flash": {"max_tool_budget": 8, "max_time_seconds": 90},
    "mission-a2-kimi": {"max_tool_budget": 10, "max_time_seconds": 90},
    "mission-a3-kimi": {"max_tool_budget": 14, "max_time_seconds": 150},
    "mission-a2-deepseek": {"max_tool_budget": 12, "max_time_seconds": 120},
    "mission-a3-deepseek": {"max_tool_budget": 20, "max_time_seconds": 180},
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
        mission.runtime_model_alias = raw_model_alias
        effective_deadline = _managed_runtime_deadline(mission, request_deadline)
        _apply_runtime_autonomy_profile(mission, raw_model_alias, effective_deadline)
        log_fn("mission_dispatch", tier=mission.tier, mode=mission.mode, mission_id=mission.mission_id)

        if mission.tier not in ("A2", "A3"):
            return ManagedMissionResult(handled=False)

        ledger = EvidenceLedger(mission_id=mission.mission_id, tool_budget_remaining=mission.tool_budget)

        def call_model(messages, tools, timeout):
            primary = RUNTIME_MODEL_ALIASES.get(raw_model_alias, raw_model_alias or "ocg-kimi-k2.6")
            candidates = [primary]
            for fallback in RUNTIME_MODEL_FALLBACKS.get(raw_model_alias, []):
                if fallback not in candidates:
                    candidates.append(fallback)
            last_exc: Exception | None = None
            for idx, reasoning_model in enumerate(candidates):
                payload = {
                    "model": map_model_fn(reasoning_model),
                    "messages": messages,
                    "stream": False,
                    "tools": [],
                }
                if idx:
                    log_fn("mission_model_fallback_attempt", mission_id=mission.mission_id, model=payload["model"])
                try:
                    response = call_payload_fn(payload, timeout)
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
