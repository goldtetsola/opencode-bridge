"""Bridge adapter for A2/A3 managed investigation missions.

This module keeps the HTTP handler out of the mission runtime. It owns only the
translation from a Responses request body to a validated runtime report.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict

from codex_oss.ledger import EvidenceLedger
from codex_oss.mission import InvalidHandoffError, _build_mission
from codex_oss.runtime.loop import run_loop
from codex_oss.runtime.policy import build_deterministic_partial_report, extract_single_handoff_block
from codex_oss.validation import render_report

JSON = Dict[str, Any]


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
        return ManagedMissionResult(handled=False)

    mission_id = "unknown"
    try:
        raw = json.loads(mission_block)
        mission = _build_mission(raw)
        mission_id = mission.mission_id
        log_fn("mission_dispatch", tier=mission.tier, mode=mission.mode, mission_id=mission.mission_id)

        if mission.tier not in ("A2", "A3"):
            return ManagedMissionResult(handled=False)

        ledger = EvidenceLedger(mission_id=mission.mission_id, tool_budget_remaining=mission.tool_budget)

        def call_model(messages, tools, timeout):
            payload = {
                "model": map_model_fn(raw_model_alias or "ocg-kimi-k2.6"),
                "messages": messages,
                "stream": False,
                "tools": [],
            }
            return call_payload_fn(payload, timeout)

        result = run_loop(
            mission,
            ledger,
            call_model,
            [],
            None,
            mission.allowed_roots,
            mission.allowed_paths,
            request_deadline=request_deadline,
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
