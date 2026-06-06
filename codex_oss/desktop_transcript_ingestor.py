"""Ingest raw Codex Desktop transcripts into MissionEventLog events."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from codex_oss.mission_event_log import MissionEventLog, hash_json

JSON = dict[str, Any]


def ingest_desktop_transcript(
    *,
    project_root: str | Path,
    run_id: str,
    transcript_path: str | Path,
    expected_agent_id: str = "",
    mission_id: str = "",
) -> JSON:
    path = Path(transcript_path)
    payload = _read_json(path)
    log = MissionEventLog.for_project(project_root, run_id, mission_id=mission_id or run_id)
    agent_id = str(payload.get("spawned_agent_id") or payload.get("agent_id") or "")
    thread_id = str(payload.get("thread_id") or payload.get("desktop_thread_id") or agent_id)
    consumer_kind = str(payload.get("consumer_kind") or "")
    transcript_kind = str(payload.get("transcript_kind") or "")
    trusted = (
        consumer_kind == "codex_desktop_spawned"
        and transcript_kind == "codex_desktop_raw_export"
        and (not expected_agent_id or expected_agent_id == agent_id or expected_agent_id == thread_id)
    )
    authority = "desktop_observed" if trusted else "diagnostic_only"
    captured = log.append(
        "DesktopTranscriptCaptured",
        {
            "agent_id": agent_id,
            "thread_id": thread_id,
            "transcript_kind": transcript_kind,
            "consumer_kind": consumer_kind,
            "capture_method": str(payload.get("source") or "codex_app.read_thread"),
            "transcript_hash": _hash_file(path),
        },
        source_kind="desktop_app",
        authority=authority,
    )
    message_events: list[str] = []
    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    for idx, message in enumerate(messages, 1):
        if not isinstance(message, dict):
            continue
        phase = str(message.get("phase") or "")
        text = str(message.get("text") or "")
        final = phase == "final_answer" or bool(message.get("final"))
        pre_final = not final and bool(text.strip())
        event = log.append(
            "DesktopMessageObserved",
            {
                "agent_id": agent_id,
                "message_id": str(message.get("id") or message.get("message_id") or f"message_{idx}"),
                "text": text,
                "pre_final": pre_final,
                "final": final,
                "runtime_event_id": str(message.get("event_id") or ""),
                "desktop_event_id": str(message.get("desktop_event_id") or ""),
            },
            source_kind="desktop_app",
            authority=authority,
        )
        message_events.append(str(event["event_id"]))
    return {
        "schema_version": "desktop_transcript_ingest.v1",
        "ok": trusted,
        "run_id": run_id,
        "mission_id": mission_id or run_id,
        "authority": authority,
        "transcript_event_id": captured["event_id"],
        "message_event_ids": message_events,
        "reasons": [] if trusted else ["desktop_transcript_not_trusted_for_gold"],
    }


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _read_json(path: Path) -> JSON:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}
