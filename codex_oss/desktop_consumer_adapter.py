"""Desktop consumer adapter for spawned-agent commentary streams.

This module is the integration seam the Codex Desktop / multi-agent consumer
must call when it receives child Responses SSE. It does not turn bridge-only
artifacts into Desktop proof; it only reconciles events from a raw
Codex-Desktop-spawned transcript.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from codex_oss.desktop_observation import (
    build_raw_desktop_transcript,
    classify_transcript_provenance,
)

JSON = dict[str, Any]

CANONICAL_PROGRESS_MARKER_RE = re.compile(
    r"\[OSS progress mission=(?P<mission>[^\s\]]+) event=(?P<event_id>[^\s\]]+) seq=(?P<seq>[^\s\]]+) type=(?P<event_type>[^\]]+)\]"
)
LEGACY_PROGRESS_MARKER_RE = re.compile(r"\[OSS progress (?:(?P<mission>[^#\]\s]+)#)?(?P<seq>[^ ]+) (?P<event_type>[^\]]+)\]")


def messages_from_responses_sse(sse_text: str) -> list[JSON]:
    """Extract assistant messages from Responses SSE blocks.

    Prefer `response.output_item.done` because it carries the complete message
    item. Fall back to `response.output_text.done` if a consumer only exposes
    text-level events.
    """
    messages: list[JSON] = []
    text_done_fallbacks: list[JSON] = []
    seen_item_ids: set[str] = set()

    for event_name, payload in _iter_sse_payloads(sse_text):
        if event_name == "response.output_item.done":
            item = payload.get("item") if isinstance(payload, dict) else None
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            item_id = str(item.get("id") or "")
            if item_id and item_id in seen_item_ids:
                continue
            if item_id:
                seen_item_ids.add(item_id)
            text = _message_text(item)
            if not text:
                continue
            messages.append(_message_from_payload(text=text, phase=str(item.get("phase") or payload.get("phase") or ""), metadata=_metadata_from_payload(payload, item)))
        elif event_name == "response.output_text.done":
            text = str(payload.get("text") or "") if isinstance(payload, dict) else ""
            if not text:
                continue
            text_done_fallbacks.append(_message_from_payload(text=text, phase=str(payload.get("phase") or ""), metadata=_metadata_from_payload(payload, None)))

    return messages or text_done_fallbacks


def transcript_from_responses_sse(
    *,
    mission_id: str,
    sse_text: str,
    agent_id: str = "",
    agent_name: str = "",
    captured_at: float | None = None,
    transcript_kind: str = "codex_desktop_raw_export",
) -> JSON:
    """Build DesktopObservationV1 transcript from consumer-observed SSE.

    The default `transcript_kind` is valid only when this function is called by
    the real Desktop spawned-agent consumer. Tests and bridge harnesses should
    pass a reconstructed kind and will fail Desktop Gold gates.
    """
    transcript = build_raw_desktop_transcript(
        mission_id=mission_id,
        messages=messages_from_responses_sse(sse_text),
        agent_id=agent_id,
        agent_name=agent_name,
        captured_at=captured_at if captured_at is not None else time.time(),
        transcript_kind=transcript_kind,
    )
    transcript["adapter"] = {
        "schema_version": "desktop_consumer_adapter.v1",
        "input": "responses_sse",
        "message_count": len(transcript.get("messages", []) or []),
    }
    return transcript


def reconcile_delivery_with_transcript(
    *,
    mission_dir: str | Path,
    transcript: JSON,
    persist: bool = True,
) -> JSON:
    """Mark delivery states from a raw Desktop-observed transcript.

    Reconciliation is deliberately provenance-gated. Artifact-reconstructed
    transcripts can be analyzed, but they cannot mark events as observed.
    """
    mission_path = Path(mission_dir)
    delivery_path = mission_path / "commentary_delivery.json"
    if not delivery_path.exists():
        return _reconcile_report(False, [], ["commentary_delivery_missing"])

    try:
        delivery = json.loads(delivery_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _reconcile_report(False, [], [f"commentary_delivery_unreadable:{exc}"])

    provenance = classify_transcript_provenance(transcript if isinstance(transcript, dict) else {})
    messages = [message for message in transcript.get("messages", []) if isinstance(message, dict)] if isinstance(transcript, dict) else []
    if not provenance.get("ok"):
        return _reconcile_report(False, [], list(provenance.get("reasons", []) or []), provenance=provenance)

    observed_before_final = _observed_event_ids_before_final(messages)
    observed_all = _observed_event_ids(messages)
    events = delivery.get("events", {}) if isinstance(delivery, dict) else {}
    marked: list[str] = []
    rendered_before_final: list[str] = []

    for event_id, event in events.items():
        if not isinstance(event, dict):
            continue
        states = event.setdefault("states", {})
        if event_id in observed_all:
            states["consumer_observed"] = True
            states["consumer_observed_at"] = int(time.time())
            marked.append(event_id)
        if event_id in observed_before_final:
            states["rendered_before_final"] = True
            states["rendered_before_final_at"] = int(time.time())
            rendered_before_final.append(event_id)

    if persist:
        _refresh_delivery_summary(delivery)
        delivery_path.write_text(json.dumps(delivery, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return _reconcile_report(
        True,
        sorted(set(marked)),
        [],
        provenance=provenance,
        rendered_before_final_event_ids=sorted(set(rendered_before_final)),
    )


def _iter_sse_payloads(sse_text: str):
    for block in str(sse_text or "").split("\n\n"):
        if not block.strip():
            continue
        event_name = ""
        data_chunks: list[str] = []
        for line in block.splitlines():
            if line.startswith("event: "):
                event_name = line[len("event: ") :].strip()
            elif line.startswith("data: "):
                data_chunks.append(line[len("data: ") :])
        raw_data = "\n".join(data_chunks).strip()
        if not raw_data or raw_data == "[DONE]":
            continue
        try:
            payload = json.loads(raw_data)
        except json.JSONDecodeError:
            continue
        yield event_name, payload


def _message_from_payload(*, text: str, phase: str, metadata: JSON) -> JSON:
    visible = metadata.get("oss_visible_event") if isinstance(metadata, dict) else None
    message: JSON = {
        "phase": phase,
        "text": text,
        "metadata": metadata if isinstance(metadata, dict) else {},
    }
    if isinstance(visible, dict) and str(visible.get("event_id") or ""):
        message["mission_id"] = visible.get("mission_id", "")
        message["event_id"] = visible.get("event_id", "")
        message["event_type"] = visible.get("event_type", "")
        message["seq"] = visible.get("seq", "")
        message["identity_authority"] = "metadata_oss_visible_event"
    else:
        marker = _marker_from_text(text)
        if marker:
            message.update(marker)
    return message


def _metadata_from_payload(payload: JSON, item: JSON | None) -> JSON:
    if isinstance(item, dict) and isinstance(item.get("metadata"), dict):
        return item["metadata"]
    if isinstance(payload, dict) and isinstance(payload.get("metadata"), dict):
        return payload["metadata"]
    return {}


def _message_text(item: JSON) -> str:
    parts: list[str] = []
    for content in item.get("content", []) or []:
        if isinstance(content, dict) and content.get("type") == "output_text":
            parts.append(str(content.get("text") or ""))
    return "\n".join(part for part in parts if part).strip()


def _marker_from_text(text: str) -> JSON:
    match = CANONICAL_PROGRESS_MARKER_RE.search(text or "")
    if match:
        return {
            "mission_id": match.group("mission"),
            "event_id": match.group("event_id"),
            "event_type": match.group("event_type"),
            "seq": match.group("seq"),
            "identity_authority": "canonical_progress_marker",
        }

    match = LEGACY_PROGRESS_MARKER_RE.search(text or "")
    if not match:
        return {}
    out: JSON = {
        "event_type": match.group("event_type"),
        "seq": match.group("seq"),
        "identity_authority": "render_only_legacy_marker",
    }
    if match.group("mission"):
        out["mission_id"] = match.group("mission")
    return out


def _is_final_message(message: JSON) -> bool:
    phase = str(message.get("phase", "") or "").lower()
    text = str(message.get("text", "") or "").lower()
    return phase in {"final", "final_answer", "answer"} or "oss_report_begin" in text or "oss_implementation_report_begin" in text


def _observed_event_ids(messages: list[JSON]) -> set[str]:
    event_ids: set[str] = set()
    for message in messages:
        event_id = _event_id_from_message(message)
        if event_id:
            event_ids.add(event_id)
    return event_ids


def _observed_event_ids_before_final(messages: list[JSON]) -> set[str]:
    event_ids: set[str] = set()
    for message in messages:
        if _is_final_message(message):
            break
        event_id = _event_id_from_message(message)
        if event_id:
            event_ids.add(event_id)
    return event_ids


def _event_id_from_message(message: JSON) -> str:
    event_id = str(message.get("event_id") or "")
    if event_id:
        return event_id
    metadata = message.get("metadata") if isinstance(message, dict) else {}
    visible = metadata.get("oss_visible_event") if isinstance(metadata, dict) else None
    if isinstance(visible, dict):
        return str(visible.get("event_id") or "")
    return ""


def _refresh_delivery_summary(delivery: JSON) -> None:
    events = delivery.get("events", {}) if isinstance(delivery, dict) else {}
    total = len(events)
    stream_enqueued = 0
    sse_emitted = 0
    consumer_observed = 0
    rendered_before_final = 0
    failed = 0
    for event in events.values():
        states = event.get("states", {}) if isinstance(event, dict) else {}
        if states.get("stream_enqueued"):
            stream_enqueued += 1
        if states.get("sse_emitted"):
            sse_emitted += 1
        if states.get("consumer_observed"):
            consumer_observed += 1
        if states.get("rendered_before_final"):
            rendered_before_final += 1
        if states.get("failed"):
            failed += 1
    delivery["delivery_summary"] = {
        "total": total,
        "stream_enqueued": stream_enqueued,
        "sse_emitted": sse_emitted,
        "emitted": sse_emitted,
        "consumer_observed": consumer_observed,
        "rendered_before_final": rendered_before_final,
        "rendered": rendered_before_final,
        "failed": failed,
        "delivery_rate": (rendered_before_final / total) if total else 0.0,
    }


def _reconcile_report(
    ok: bool,
    marked_event_ids: list[str],
    reasons: list[str],
    *,
    provenance: JSON | None = None,
    rendered_before_final_event_ids: list[str] | None = None,
) -> JSON:
    rendered_event_ids = rendered_before_final_event_ids or []
    return {
        "schema_version": "desktop_consumer_reconciliation.v1",
        "ok": ok,
        "marked_event_ids": marked_event_ids,
        "marked_count": len(marked_event_ids),
        "rendered_before_final_event_ids": rendered_event_ids,
        "rendered_before_final_count": len(rendered_event_ids),
        "reasons": reasons,
        "transcript_provenance": provenance or {},
    }
