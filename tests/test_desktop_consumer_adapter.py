#!/usr/bin/env python3
"""Tests for Desktop consumer adapter SSE -> transcript reconciliation."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

PASSED = 0
FAILED = 0


def _pass(name):
    global PASSED
    PASSED += 1
    print(f"  PASS {name}")


def _fail(name, detail=""):
    global FAILED
    FAILED += 1
    print(f"  FAIL {name}: {detail}")


def _sse_block(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


def _message_item(item_id: str, text: str, phase: str, event_id: str = "", seq: int = 0) -> dict:
    metadata = {}
    if event_id:
        metadata = {
            "oss_visible_event": {
                "mission_id": "mission_desktop_adapter",
                "event_id": event_id,
                "seq": seq,
                "event_type": "tool_action_started",
                "phase": "EXPLORE",
                "source": "runtime",
                "safe_for_user": True,
            }
        }
    return {
        "type": "response.output_item.done",
        "output_index": seq,
        "item": {
            "type": "message",
            "id": item_id,
            "status": "completed",
            "role": "assistant",
            "phase": phase,
            "metadata": metadata,
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        },
    }


def _sample_sse() -> str:
    return (
        _sse_block("response.output_item.done", _message_item("msg_1", "[OSS progress mission=mission_desktop_adapter event=evt_0001 seq=1 type=tool_action_started] reading one", "commentary", "evt_0001", 1))
        + _sse_block("response.output_item.done", _message_item("msg_2", "[OSS progress mission=mission_desktop_adapter event=evt_0002 seq=2 type=tool_action_started] reading two", "commentary", "evt_0002", 2))
        + _sse_block("response.output_item.done", _message_item("msg_3", "[OSS progress mission=mission_desktop_adapter event=evt_0003 seq=3 type=tool_action_started] reading three", "commentary", "evt_0003", 3))
        + _sse_block("response.output_item.done", _message_item("msg_4", "OSS_REPORT_BEGIN\nStatus: COMPLETE\nOSS_REPORT_END", "final_answer"))
        + "data: [DONE]\n\n"
    )


def _write_delivery(path: Path) -> None:
    events = {}
    for idx in range(1, 4):
        event_id = f"evt_{idx:04d}"
        events[event_id] = {
            "schema_version": "commentary_delivery.v1",
            "mission_id": "mission_desktop_adapter",
            "event_id": event_id,
            "event_type": "tool_action_started",
            "message": f"event {idx}",
            "phase": "EXPLORE",
            "safe_for_user": True,
            "states": {
                "created": True,
                "sanitized": True,
                "stream_enqueued": True,
                "sse_emitted": True,
                "consumer_observed": False,
                "rendered_before_final": False,
                "reconciled_with_summary": True,
                "failed": False,
            },
            "failure_reason": None,
        }
    payload = {
        "schema_version": "commentary_delivery_tracker.v1",
        "mission_id": "mission_desktop_adapter",
        "events": events,
        "delivery_summary": {"total": 3, "emitted": 3, "rendered": 0, "failed": 0, "delivery_rate": 0.0},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def assert_sse_parser_preserves_visible_event_identity():
    name = "sse_parser_preserves_visible_event_identity"
    from codex_oss.desktop_consumer_adapter import messages_from_responses_sse

    messages = messages_from_responses_sse(_sample_sse())
    assert len(messages) == 4, messages
    assert messages[0]["phase"] == "commentary", messages[0]
    assert messages[0]["event_id"] == "evt_0001", messages[0]
    assert messages[0]["metadata"]["oss_visible_event"]["seq"] == 1, messages[0]
    assert messages[-1]["phase"] == "final_answer", messages[-1]
    _pass(name)


def assert_canonical_marker_preserves_identity_without_metadata():
    name = "canonical_marker_preserves_identity_without_metadata"
    from codex_oss.desktop_consumer_adapter import messages_from_responses_sse

    sse = _sse_block("response.output_text.done", {
        "phase": "commentary",
        "text": "[OSS progress mission=mission_desktop_adapter event=evt_0001 seq=1 type=tool_action_started] reading one",
    })
    messages = messages_from_responses_sse(sse)
    assert len(messages) == 1, messages
    assert messages[0]["mission_id"] == "mission_desktop_adapter", messages
    assert messages[0]["event_id"] == "evt_0001", messages
    assert messages[0]["identity_authority"] == "canonical_progress_marker", messages
    _pass(name)


def assert_raw_desktop_sse_reconciles_delivery_states():
    name = "raw_desktop_sse_reconciles_delivery_states"
    from codex_oss.desktop_consumer_adapter import reconcile_delivery_with_transcript, transcript_from_responses_sse

    with tempfile.TemporaryDirectory() as tmp:
        mission_dir = Path(tmp)
        _write_delivery(mission_dir / "commentary_delivery.json")
        transcript = transcript_from_responses_sse(
            mission_id="mission_desktop_adapter",
            sse_text=_sample_sse(),
            agent_id="agent_1",
            agent_name="Curie",
            transcript_kind="codex_desktop_raw_export",
        )
        result = reconcile_delivery_with_transcript(mission_dir=mission_dir, transcript=transcript)
        assert result["ok"] is True, result
        assert result["marked_count"] == 3, result
        updated = json.loads((mission_dir / "commentary_delivery.json").read_text())
        states = [event["states"] for event in updated["events"].values()]
        assert all(state["consumer_observed"] for state in states), states
        assert all(state["rendered_before_final"] for state in states), states
        assert updated["delivery_summary"]["rendered"] == 3, updated
    _pass(name)


def assert_legacy_marker_cannot_mark_delivery_observed():
    name = "legacy_marker_cannot_mark_delivery_observed"
    from codex_oss.desktop_consumer_adapter import reconcile_delivery_with_transcript, transcript_from_responses_sse

    legacy_sse = (
        _sse_block("response.output_item.done", _message_item("msg_1", "[OSS progress mission_desktop_adapter#1 tool_action_started] reading one", "commentary"))
        + _sse_block("response.output_item.done", _message_item("msg_2", "[OSS progress mission_desktop_adapter#2 tool_action_started] reading two", "commentary"))
        + _sse_block("response.output_item.done", _message_item("msg_3", "[OSS progress mission_desktop_adapter#3 tool_action_started] reading three", "commentary"))
        + _sse_block("response.output_item.done", _message_item("msg_4", "OSS_REPORT_BEGIN\nStatus: COMPLETE\nOSS_REPORT_END", "final_answer"))
    )

    with tempfile.TemporaryDirectory() as tmp:
        mission_dir = Path(tmp)
        _write_delivery(mission_dir / "commentary_delivery.json")
        transcript = transcript_from_responses_sse(
            mission_id="mission_desktop_adapter",
            sse_text=legacy_sse,
            transcript_kind="codex_desktop_raw_export",
        )
        result = reconcile_delivery_with_transcript(mission_dir=mission_dir, transcript=transcript)
        assert result["ok"] is True, result
        assert result["marked_count"] == 0, result
        updated = json.loads((mission_dir / "commentary_delivery.json").read_text())
        states = [event["states"] for event in updated["events"].values()]
        assert not any(state["consumer_observed"] for state in states), states
    _pass(name)


def assert_reconstructed_sse_cannot_mark_desktop_observed():
    name = "reconstructed_sse_cannot_mark_desktop_observed"
    from codex_oss.desktop_consumer_adapter import reconcile_delivery_with_transcript, transcript_from_responses_sse

    with tempfile.TemporaryDirectory() as tmp:
        mission_dir = Path(tmp)
        _write_delivery(mission_dir / "commentary_delivery.json")
        transcript = transcript_from_responses_sse(
            mission_id="mission_desktop_adapter",
            sse_text=_sample_sse(),
            transcript_kind="bridge_harness_transcript",
        )
        result = reconcile_delivery_with_transcript(mission_dir=mission_dir, transcript=transcript)
        assert result["ok"] is False, result
        assert "transcript_is_not_raw_desktop_export" in result["reasons"], result
        updated = json.loads((mission_dir / "commentary_delivery.json").read_text())
        states = [event["states"] for event in updated["events"].values()]
        assert not any(state["consumer_observed"] for state in states), states
    _pass(name)


def main() -> bool:
    for test in [
        assert_sse_parser_preserves_visible_event_identity,
        assert_canonical_marker_preserves_identity_without_metadata,
        assert_raw_desktop_sse_reconciles_delivery_states,
        assert_legacy_marker_cannot_mark_delivery_observed,
        assert_reconstructed_sse_cannot_mark_desktop_observed,
    ]:
        try:
            test()
        except Exception as exc:
            _fail(test.__name__, repr(exc))
    print(f"Results: {PASSED} passed, {FAILED} failed")
    return FAILED == 0


if __name__ == "__main__":
    if not main():
        sys.exit(1)
