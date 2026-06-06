"""Append-only hash-chained mission event log."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codex_oss.mission_events import (
    EVENT_SCHEMA_VERSION,
    MissionEventError,
    redact_payload,
    validate_event_parts,
    validate_payload,
)

JSON = dict[str, Any]


class MissionEventLog:
    """One immutable event stream for one run_id."""

    def __init__(self, path: str | Path, *, run_id: str, mission_id: str = "") -> None:
        self.path = Path(path)
        self.run_id = str(run_id or "")
        self.mission_id = str(mission_id or run_id or "")
        if not self.run_id:
            raise MissionEventError("run_id_required")

    @classmethod
    def for_project(cls, project_root: str | Path, run_id: str, *, mission_id: str = "") -> "MissionEventLog":
        path = Path(project_root) / ".codex-oss" / "runs" / run_id / "events.jsonl"
        return cls(path, run_id=run_id, mission_id=mission_id or run_id)

    def append(
        self,
        event_type: str,
        payload: JSON,
        *,
        source_kind: str,
        authority: str,
        event_id: str = "",
        created_at: str = "",
        expected_prev_hash: str | None = None,
    ) -> JSON:
        events = self.read_events(verify=True)
        prev_hash = str(events[-1]["event_hash"]) if events else ""
        if expected_prev_hash is not None and expected_prev_hash != prev_hash:
            raise MissionEventError("prev_hash_mismatch")
        seq = len(events) + 1
        resolved_event_id = event_id or f"evt_{seq:06d}"
        if any(event.get("event_id") == resolved_event_id for event in events):
            raise MissionEventError("duplicate_event_id")
        timestamp = created_at or datetime.now(timezone.utc).isoformat()
        clean_payload = redact_payload(payload)
        validate_event_parts(
            event_type=event_type,
            source_kind=source_kind,
            authority=authority,
            created_at=timestamp,
            payload=clean_payload,
        )
        validate_payload(event_type, clean_payload)
        event = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "run_id": self.run_id,
            "mission_id": self.mission_id,
            "event_id": resolved_event_id,
            "seq": seq,
            "prev_hash": prev_hash,
            "event_type": event_type,
            "source_kind": source_kind,
            "authority": authority,
            "created_at": timestamp,
            "payload": clean_payload,
        }
        event["event_hash"] = event_hash(event)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(_canonical_json(event) + "\n")
        return event

    def read_events(self, *, verify: bool = True) -> list[JSON]:
        if not self.path.exists():
            return []
        events: list[JSON] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise MissionEventError(f"event_json_invalid:{line_no}") from exc
                events.append(event)
        if verify:
            verify_event_chain(events, expected_run_id=self.run_id)
        return events


def event_log_path(project_root: str | Path, run_id: str) -> Path:
    return Path(project_root) / ".codex-oss" / "runs" / run_id / "events.jsonl"


def event_log_exists(project_root: str | Path, run_id: str) -> bool:
    return event_log_path(project_root, run_id).exists()


def append_event(project_root: str | Path, run_id: str, event_type: str, payload: JSON, *, source_kind: str, authority: str, mission_id: str = "") -> JSON:
    return MissionEventLog.for_project(project_root, run_id, mission_id=mission_id or run_id).append(
        event_type,
        payload,
        source_kind=source_kind,
        authority=authority,
    )


def read_event_log(project_root: str | Path, run_id: str) -> list[JSON]:
    return MissionEventLog.for_project(project_root, run_id).read_events(verify=True)


def verify_event_chain(events: list[JSON], *, expected_run_id: str = "") -> JSON:
    seen_ids: set[str] = set()
    prev_hash = ""
    for index, event in enumerate(events, 1):
        if event.get("schema_version") != EVENT_SCHEMA_VERSION:
            raise MissionEventError(f"schema_version_invalid:{index}")
        if expected_run_id and event.get("run_id") != expected_run_id:
            raise MissionEventError(f"run_id_mismatch:{index}")
        if int(event.get("seq", 0) or 0) != index:
            raise MissionEventError(f"seq_mismatch:{index}")
        event_id = str(event.get("event_id") or "")
        if not event_id:
            raise MissionEventError(f"event_id_missing:{index}")
        if event_id in seen_ids:
            raise MissionEventError(f"duplicate_event_id:{event_id}")
        seen_ids.add(event_id)
        if str(event.get("prev_hash") or "") != prev_hash:
            raise MissionEventError(f"prev_hash_mismatch:{index}")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        validate_event_parts(
            event_type=str(event.get("event_type") or ""),
            source_kind=str(event.get("source_kind") or ""),
            authority=str(event.get("authority") or ""),
            created_at=str(event.get("created_at") or ""),
            payload=payload,
        )
        validate_payload(str(event.get("event_type") or ""), payload)
        expected_hash = event_hash(event)
        if event.get("event_hash") != expected_hash:
            raise MissionEventError(f"event_hash_mismatch:{index}")
        prev_hash = expected_hash
    return {
        "schema_version": "mission_event_log_verification.v1",
        "ok": True,
        "event_count": len(events),
        "last_event_hash": prev_hash,
    }


def event_hash(event: JSON) -> str:
    without_hash = {key: value for key, value in event.items() if key != "event_hash"}
    return "sha256:" + hashlib.sha256(_canonical_json(without_hash).encode("utf-8")).hexdigest()


def hash_json(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
