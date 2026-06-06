"""SpawnLifecycleProofV1 helpers for Desktop MissionV1 child runs."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codex_oss.desktop_transcript_ingestor import ingest_desktop_transcript
from codex_oss.desktop_observation import build_raw_desktop_transcript, capture_thread_observation
from codex_oss.mission_event_bridge import ensure_mission_admitted_from_artifacts
from codex_oss.native_certification import certify_oss_native_parity_project
from codex_oss.route_authority import build_route_authority

JSON = dict[str, Any]


ROLE_MODEL_ALIASES = {
    "oss_deepseek_implementer": "mission-a5-deepseek",
    "oss_deepseek_investigator": "mission-a3-deepseek",
    "oss_kimi_investigator": "mission-a3-kimi",
    "oss_flash_context": "mission-a2-flash",
}


def build_spawn_receipt(
    *,
    mission_id: str,
    agent_id: str,
    agent_role: str = "",
    parent_thread_id: str = "",
    model_alias: str = "",
    model_provider: str = "",
    spawned_at: str = "",
) -> JSON:
    """Build the canonical child identity receipt from spawn_agent output."""
    resolved_model = model_alias or ROLE_MODEL_ALIASES.get(agent_role, "")
    return {
        "schema_version": "spawn_receipt.v1",
        "mission_id": str(mission_id or ""),
        "agent_id": str(agent_id or ""),
        "agent_role": str(agent_role or ""),
        "parent_thread_id": str(parent_thread_id or ""),
        "model_alias": str(resolved_model or ""),
        "model_provider": str(model_provider or ""),
        "spawned_at": spawned_at or datetime.now(timezone.utc).isoformat(),
        "canonical_transcript_identity": "agent_id",
        "read_method": "codex_app.read_thread(threadId=agent_id)",
        "list_threads_used": False,
    }


def finalize_spawn_lifecycle(
    *,
    project_root: str,
    mission_id: str,
    agent_id: str = "",
    agent_role: str = "",
    parent_thread_id: str = "",
    model_alias: str = "",
    model_provider: str = "",
    thread_export_path: str = "",
    read_thread_error: str = "",
) -> JSON:
    """Persist spawn lifecycle artifacts and run targeted certification.

    The actual ``codex_app.read_thread`` call is an app/tool concern. This
    function owns the repo-side contract once the app harness has either
    supplied a raw read-thread export or reported why that read failed.
    """
    root = Path(project_root).resolve()
    mission_dir = root / ".codex-oss" / "missions" / mission_id
    mission_dir.mkdir(parents=True, exist_ok=True)
    ensure_mission_admitted_from_artifacts(root, mission_id)

    existing_receipt = _read_json(mission_dir / "spawn_receipt.json")
    receipt = _merge_receipt(
        existing_receipt,
        build_spawn_receipt(
            mission_id=mission_id,
            agent_id=agent_id or str(existing_receipt.get("agent_id", "") or ""),
            agent_role=agent_role or str(existing_receipt.get("agent_role", "") or ""),
            parent_thread_id=parent_thread_id or str(existing_receipt.get("parent_thread_id", "") or ""),
            model_alias=model_alias or str(existing_receipt.get("model_alias", "") or ""),
            model_provider=model_provider or str(existing_receipt.get("model_provider", "") or ""),
        ),
    )
    if not receipt.get("agent_id"):
        return _write_lifecycle_failure(
            mission_dir=mission_dir,
            mission_id=mission_id,
            reason="spawn_receipt_agent_id_missing",
            receipt=receipt,
        )

    _write_json(mission_dir / "spawn_receipt.json", receipt)
    route_authority = _ensure_route_authority(mission_dir, receipt)

    capture_report: JSON = {}
    transcript_path = mission_dir / "desktop_thread_transcript.json"
    if read_thread_error:
        capture_report = _write_capture_failure(
            mission_dir=mission_dir,
            receipt=receipt,
            reason="read_thread_failed",
            detail=read_thread_error,
        )
    elif thread_export_path:
        transcript = normalize_read_thread_export(
            payload=_read_json(Path(thread_export_path)),
            mission_id=mission_id,
            receipt=receipt,
        )
        _write_json(transcript_path, transcript)
        capture_report = capture_thread_observation(
            project_root=str(root),
            mission_id=mission_id,
            thread_id=str(transcript.get("thread_id", "") or ""),
            spawned_agent_id=str(receipt.get("agent_id", "") or ""),
            transcript_path=str(transcript_path),
        )
        ingest_desktop_transcript(
            project_root=root,
            run_id=mission_id,
            mission_id=mission_id,
            transcript_path=transcript_path,
            expected_agent_id=str(receipt.get("agent_id", "") or ""),
        )
        _remove_if_exists(mission_dir / "capture_failure.json")
    elif not transcript_path.exists():
        capture_report = _write_capture_failure(
            mission_dir=mission_dir,
            receipt=receipt,
            reason="read_thread_export_missing",
            detail="No read_thread export was supplied to finalize_spawn_lifecycle.",
        )

    certification = certify_oss_native_parity_project(str(root), mission_id=mission_id)
    _write_json(mission_dir / "certification_result.json", certification)

    lifecycle = {
        "schema_version": "spawn_lifecycle_proof.v1",
        "ok": bool((capture_report or {}).get("ok")) and bool(route_authority.get("desktop_native_claim_allowed")),
        "mission_id": mission_id,
        "spawn_receipt_path": str(mission_dir / "spawn_receipt.json"),
        "agent_id": str(receipt.get("agent_id", "") or ""),
        "canonical_transcript_identity": "agent_id",
        "attempted_read_method": "codex_app.read_thread(threadId=agent_id)",
        "list_threads_used": False,
        "desktop_thread_transcript_path": str(transcript_path) if transcript_path.exists() else "",
        "capture_failure_path": str(mission_dir / "capture_failure.json") if (mission_dir / "capture_failure.json").exists() else "",
        "route_authority_path": str(mission_dir / "route_authority.json"),
        "spawned_transcript_authority_path": str(mission_dir / "spawned_transcript_authority.json") if (mission_dir / "spawned_transcript_authority.json").exists() else "",
        "certification_result_path": str(mission_dir / "certification_result.json"),
        "desktop_observation": certification.get("desktop_observation", ""),
        "desktop_gold": bool(certification.get("desktop_gold")),
        "certification_status": certification.get("status", ""),
        "reasons": list((capture_report or {}).get("reasons", []) or []),
    }
    _write_json(mission_dir / "spawn_lifecycle_proof.json", lifecycle)
    return lifecycle


def normalize_read_thread_export(*, payload: JSON, mission_id: str, receipt: JSON) -> JSON:
    """Convert codex_app.read_thread output or an existing transcript to the canonical transcript."""
    if payload.get("schema_version") == "desktop_observation_transcript.v1":
        transcript = dict(payload)
        transcript.setdefault("mission_id", mission_id)
        transcript.setdefault("consumer_kind", "codex_desktop_spawned")
        transcript.setdefault("transcript_kind", "codex_desktop_raw_export")
        transcript.setdefault("agent_id", receipt.get("agent_id", ""))
        transcript.setdefault("spawned_agent_id", receipt.get("agent_id", ""))
        transcript.setdefault("thread_id", payload.get("thread_id") or payload.get("desktop_thread_id") or receipt.get("agent_id", ""))
        return transcript

    thread = payload.get("thread") if isinstance(payload.get("thread"), dict) else {}
    thread_id = str(thread.get("id") or payload.get("thread_id") or receipt.get("agent_id") or "")
    messages: list[JSON] = []
    turns = payload.get("turns") if isinstance(payload.get("turns"), list) else []
    for turn in reversed(turns):
        if not isinstance(turn, dict):
            continue
        for item in turn.get("items", []) or []:
            if not isinstance(item, dict) or item.get("type") != "agentMessage":
                continue
            text = str(item.get("text") or "")
            message = {
                "id": str(item.get("id") or ""),
                "role": "assistant",
                "phase": str(item.get("phase") or ""),
                "text": text,
            }
            if isinstance(item.get("metadata"), dict):
                message["metadata"] = dict(item["metadata"])
            event_id = str(item.get("event_id") or _extract_event_id(text) or "")
            if event_id:
                message["event_id"] = event_id
            messages.append(message)
    transcript = build_raw_desktop_transcript(
        mission_id=mission_id,
        messages=messages,
        agent_id=str(receipt.get("agent_id") or ""),
        agent_name=str(receipt.get("agent_role") or ""),
        transcript_kind="codex_desktop_raw_export",
        captured_at=time.time(),
    )
    transcript["thread_id"] = thread_id
    transcript["desktop_thread_id"] = thread_id
    transcript["spawned_agent_id"] = str(receipt.get("agent_id") or "")
    transcript["source"] = "codex_app.read_thread"
    return transcript


def _ensure_route_authority(mission_dir: Path, receipt: JSON) -> JSON:
    existing = _read_json(mission_dir / "route_authority.json")
    if existing:
        return existing
    mission = _first_json([mission_dir / "mission_canonical.json", mission_dir / "mission.json"])
    report = _read_json(mission_dir / "report.json")
    model_alias = str(receipt.get("model_alias") or report.get("runtime_model_alias") or "")
    if not model_alias:
        model_alias = ROLE_MODEL_ALIASES.get(str(receipt.get("agent_role") or ""), "")
    route = build_route_authority(
        agent_name=str(receipt.get("agent_role") or ""),
        model_alias=model_alias,
        handoff_obj=mission if mission else None,
        consumer_kind="codex_desktop_spawned",
    )
    route["source"] = "spawn_receipt + mission_canonical + runtime_report"
    route["agent_id"] = str(receipt.get("agent_id") or "")
    _write_json(mission_dir / "route_authority.json", route)
    return route


def _write_capture_failure(*, mission_dir: Path, receipt: JSON, reason: str, detail: str) -> JSON:
    failure = {
        "schema_version": "desktop_capture_failure.v1",
        "desktop_observation": "FAIL",
        "reason": reason,
        "detail": detail,
        "agent_id": str(receipt.get("agent_id") or ""),
        "attempted_read_method": "codex_app.read_thread(threadId=agent_id)",
        "list_threads_used": False,
        "next_action": "check app surface / verify agent_id / rerun capture",
    }
    _write_json(mission_dir / "capture_failure.json", failure)
    _append_capture_failure_event(mission_dir, receipt, reason)
    return {
        "schema_version": "desktop_observation_capture.v1",
        "ok": False,
        "mission_id": str(receipt.get("mission_id") or ""),
        "reasons": [reason],
        "capture_failure": failure,
    }


def _append_capture_failure_event(mission_dir: Path, receipt: JSON, reason: str) -> None:
    try:
        from codex_oss.mission_event_log import MissionEventLog

        project_root = mission_dir.parents[2]
        mission_id = str(receipt.get("mission_id") or mission_dir.name)
        log = MissionEventLog.for_project(project_root, mission_id, mission_id=mission_id)
        if any(event.get("event_type") == "DesktopTranscriptCaptured" for event in log.read_events(verify=True)):
            return
        log.append(
            "DesktopTranscriptCaptured",
            {
                "agent_id": str(receipt.get("agent_id") or ""),
                "thread_id": str(receipt.get("agent_id") or ""),
                "transcript_kind": "missing",
                "consumer_kind": "codex_desktop_spawned",
                "capture_method": f"codex_app.read_thread:{reason}",
            },
            source_kind="desktop_app",
            authority="diagnostic_only",
        )
    except Exception:
        return


def _write_lifecycle_failure(*, mission_dir: Path, mission_id: str, reason: str, receipt: JSON) -> JSON:
    _write_json(mission_dir / "spawn_receipt.json", receipt)
    lifecycle = {
        "schema_version": "spawn_lifecycle_proof.v1",
        "ok": False,
        "mission_id": mission_id,
        "agent_id": "",
        "reasons": [reason],
        "next_action": "persist spawn_agent.agent_id before wait_agent completes",
    }
    _write_json(mission_dir / "spawn_lifecycle_proof.json", lifecycle)
    return lifecycle


def _merge_receipt(existing: JSON, new: JSON) -> JSON:
    merged = dict(existing or {})
    for key, value in new.items():
        if value not in ("", [], {}, None):
            merged[key] = value
        else:
            merged.setdefault(key, value)
    return merged


def _extract_event_id(text: str) -> str:
    match = re.search(r"\bevent=([A-Za-z0-9_.:-]+)", text or "")
    return match.group(1) if match else ""


def _first_json(paths: list[Path]) -> JSON:
    for path in paths:
        payload = _read_json(path)
        if payload:
            return payload
    return {}


def _read_json(path: Path) -> JSON:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: JSON) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _remove_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
