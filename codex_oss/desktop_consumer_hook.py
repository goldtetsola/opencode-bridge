"""Desktop consumer integration hook for observed OSS child streams.

This module is the repo-owned integration seam for Codex Desktop. It does
not let bridge/runtime artifacts certify Desktop-native UX by themselves.
Only raw Desktop-consumer-observed SSE/messages can be converted into a
Desktop transcript, reconciled against commentary delivery, and verified.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from codex_oss.desktop_consumer_adapter import (
    reconcile_delivery_with_transcript,
    transcript_from_responses_sse,
)
from codex_oss.desktop_native_verifier import verify_desktop_native_ux
from codex_oss.desktop_observation import build_raw_desktop_transcript

JSON = dict[str, Any]
MIN_DESKTOP_PROGRESS_EVENTS = 3


def consume_desktop_sse(
    *,
    mission_id: str,
    mission_dir: str | Path,
    project_root: str | Path,
    sse_text: str,
    route_authority: JSON | None = None,
    agent_id: str = "",
    agent_name: str = "",
    transcript_kind: str = "codex_desktop_raw_export",
    output_path: str | Path | None = None,
    persist: bool = True,
    verify: bool = True,
) -> JSON:
    """Observe child Responses SSE from the Desktop consumer boundary.

    The default transcript kind is valid only for the real Desktop consumer.
    Harnesses/tests must pass a non-raw transcript kind and will fail Desktop
    Gold verification.
    """
    transcript = transcript_from_responses_sse(
        mission_id=mission_id,
        sse_text=sse_text,
        agent_id=agent_id,
        agent_name=agent_name,
        transcript_kind=transcript_kind,
    )
    return consume_desktop_transcript(
        mission_id=mission_id,
        mission_dir=mission_dir,
        project_root=project_root,
        transcript=transcript,
        route_authority=route_authority,
        output_path=output_path,
        persist=persist,
        verify=verify,
    )


def consume_desktop_messages(
    *,
    mission_id: str,
    mission_dir: str | Path,
    project_root: str | Path,
    messages: list[JSON],
    route_authority: JSON | None = None,
    agent_id: str = "",
    agent_name: str = "",
    transcript_kind: str = "codex_desktop_raw_export",
    output_path: str | Path | None = None,
    persist: bool = True,
    verify: bool = True,
) -> JSON:
    """Observe already-rendered Desktop child messages."""
    transcript = build_raw_desktop_transcript(
        mission_id=mission_id,
        messages=messages,
        agent_id=agent_id,
        agent_name=agent_name,
        transcript_kind=transcript_kind,
    )
    transcript["adapter"] = {
        "schema_version": "desktop_consumer_hook.v1",
        "input": "desktop_messages",
        "message_count": len(messages),
    }
    return consume_desktop_transcript(
        mission_id=mission_id,
        mission_dir=mission_dir,
        project_root=project_root,
        transcript=transcript,
        route_authority=route_authority,
        output_path=output_path,
        persist=persist,
        verify=verify,
    )


def consume_desktop_transcript(
    *,
    mission_id: str,
    mission_dir: str | Path,
    project_root: str | Path,
    transcript: JSON,
    route_authority: JSON | None = None,
    output_path: str | Path | None = None,
    persist: bool = True,
    verify: bool = True,
) -> JSON:
    """Persist, reconcile, and optionally verify a raw Desktop transcript."""
    mission_path = Path(mission_dir)
    mission_path.mkdir(parents=True, exist_ok=True)
    transcript_path = Path(output_path) if output_path else mission_path / "desktop_observed_transcript.json"
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text(json.dumps(transcript, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    reconciliation = reconcile_delivery_with_transcript(
        mission_dir=mission_path,
        transcript=transcript,
        persist=persist,
    )
    observation_gate = _observation_gate(reconciliation)
    verification_report: JSON | None = None
    if verify:
        verification_report = verify_desktop_native_ux(
            transcript_path=str(transcript_path),
            mission_id=mission_id,
            project_root=str(project_root),
            route_authority=route_authority,
        )

    missing = list(observation_gate.get("reasons", []))
    if not reconciliation.get("ok"):
        missing.extend(str(reason) for reason in reconciliation.get("reasons", []) if str(reason))
    if verify and not (verification_report or {}).get("ok"):
        missing.extend(str(reason) for reason in (verification_report or {}).get("missing_evidence", []) if str(reason))

    result = {
        "schema_version": "desktop_consumer_hook_result.v1",
        "mission_id": mission_id,
        "ok": not missing,
        "transcript_path": str(transcript_path),
        "reconciliation": reconciliation,
        "observation_gate": observation_gate,
        "verification": verification_report,
        "missing_evidence": _dedupe(missing),
        "recorded_at": time.time(),
    }
    result_path = mission_path / "desktop_consumer_observation.json"
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["result_path"] = str(result_path)
    return result


def _observation_gate(reconciliation: JSON) -> JSON:
    marked = int(reconciliation.get("marked_count", 0) or 0)
    rendered = int(reconciliation.get("rendered_before_final_count", 0) or 0)
    reasons: list[str] = []
    if marked < MIN_DESKTOP_PROGRESS_EVENTS:
        reasons.append("desktop_consumer_observed_progress_insufficient")
    if rendered < MIN_DESKTOP_PROGRESS_EVENTS:
        reasons.append("desktop_consumer_rendered_before_final_insufficient")
    return {
        "schema_version": "desktop_consumer_observation_gate.v1",
        "ok": not reasons,
        "marked_count": marked,
        "rendered_before_final_count": rendered,
        "min_progress_events": MIN_DESKTOP_PROGRESS_EVENTS,
        "reasons": reasons,
    }


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out
