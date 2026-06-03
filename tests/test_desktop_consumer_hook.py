#!/usr/bin/env python3
"""Tests for the Desktop consumer hook integration boundary."""

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
                "mission_id": "desktop_hook",
                "event_id": event_id,
                "seq": seq,
                "event_type": "tool_action_started",
            }
        }
    return {
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


def _identity_sse(mission_id: str = "desktop_hook") -> str:
    return (
        _sse_block(
            "response.output_item.done",
            _message_item("msg_1", f"[OSS progress mission={mission_id} event=evt_0001 seq=1 type=tool_action_started] starting", "commentary", "evt_0001", 1),
        )
        + _sse_block(
            "response.output_item.done",
            _message_item("msg_2", f"[OSS progress mission={mission_id} event=evt_0002 seq=2 type=tool_action_started] reading", "commentary", "evt_0002", 2),
        )
        + _sse_block(
            "response.output_item.done",
            _message_item("msg_3", f"[OSS progress mission={mission_id} event=evt_0003 seq=3 type=tool_action_started] verifying", "commentary", "evt_0003", 3),
        )
        + _sse_block("response.output_item.done", _message_item("msg_4", "OSS_REPORT_BEGIN\nStatus: COMPLETE\nOSS_REPORT_END", "final_answer"))
        + "data: [DONE]\n\n"
    )


def _generic_sse() -> str:
    return (
        _sse_block("response.output_item.done", _message_item("msg_1", "[OSS progress] starting", "commentary"))
        + _sse_block("response.output_item.done", _message_item("msg_2", "[OSS progress] reading", "commentary"))
        + _sse_block("response.output_item.done", _message_item("msg_3", "[OSS progress] verifying", "commentary"))
        + _sse_block("response.output_item.done", _message_item("msg_4", "final", "final_answer"))
    )


def _write_required_artifacts(root: Path, mission_id: str) -> Path:
    mission_dir = root / ".codex-oss" / "missions" / mission_id
    mission_dir.mkdir(parents=True, exist_ok=True)
    (root / ".codex-oss" / "desktop_pre_final_text_probe_result.json").write_text(
        json.dumps({
            "schema_version": "desktop_pre_final_text_probe_result.v1",
            "probe_status": "pass",
            "desktop_render_surface": {"probe_required": True, "probe_status": "pass"},
        }),
        encoding="utf-8",
    )
    (mission_dir / "visible_commentary.jsonl").write_text(
        "\n".join(
            json.dumps({"mission_id": mission_id, "event_id": f"evt_{idx:04d}", "event_type": "tool_action_started"})
            for idx in range(1, 4)
        )
        + "\n",
        encoding="utf-8",
    )
    events = {}
    for idx in range(1, 4):
        event_id = f"evt_{idx:04d}"
        events[event_id] = {
            "schema_version": "commentary_delivery.v1",
            "mission_id": mission_id,
            "event_id": event_id,
            "event_type": "tool_action_started",
            "states": {
                "created": True,
                "sanitized": True,
                "stream_enqueued": True,
                "sse_emitted": True,
                "consumer_observed": False,
                "rendered_before_final": False,
                "failed": False,
            },
        }
    (mission_dir / "commentary_delivery.json").write_text(
        json.dumps({"schema_version": "commentary_delivery_tracker.v1", "mission_id": mission_id, "events": events}),
        encoding="utf-8",
    )
    (mission_dir / "summary.md").write_text("summary", encoding="utf-8")
    (mission_dir / "canonical_read_evidence.json").write_text(
        json.dumps({"mission_id": mission_id, "status_entitlement": {"can_complete": True}}),
        encoding="utf-8",
    )
    (mission_dir / "report.json").write_text(json.dumps({"mission_id": mission_id, "report_source": "runtime"}), encoding="utf-8")
    (mission_dir / "mission.json").write_text(json.dumps({"mission_id": mission_id}), encoding="utf-8")
    (mission_dir / "tool_call_adoption_probes.json").write_text(
        json.dumps(
            {
                "schema_version": "tool_call_adoption_probes.v1",
                "adoption_stats": {"total": 0, "adopted": 0, "not_adopted": 0, "recovery_used": 0},
                "probes": [],
            }
        ),
        encoding="utf-8",
    )
    return mission_dir


def _desktop_route():
    from codex_oss.route_authority import build_route_authority

    return build_route_authority(
        agent_name="oss_deepseek_investigator",
        model_alias="mission-a3-deepseek",
        handoff_obj={"schema_version": "oss_agent_mission.v1"},
        consumer_kind="codex_desktop_spawned",
    )


def assert_consume_sse_marks_delivery_and_verifies_desktop_gold():
    name = "consume_sse_marks_delivery_and_verifies_desktop_gold"
    from codex_oss.desktop_consumer_hook import consume_desktop_sse

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mission_id = "desktop_hook"
        mission_dir = _write_required_artifacts(root, mission_id)
        report = consume_desktop_sse(
            mission_id=mission_id,
            mission_dir=mission_dir,
            project_root=root,
            sse_text=_identity_sse(mission_id),
            route_authority=_desktop_route(),
            agent_id="agent_1",
            agent_name="Curie",
        )
        assert report["ok"] is True, report
        assert report["observation_gate"]["ok"] is True, report
        assert report["verification"]["desktop_gold_pass"] is True, report
        assert Path(report["transcript_path"]).exists(), report
        assert Path(report["result_path"]).exists(), report
        delivery = json.loads((mission_dir / "commentary_delivery.json").read_text(encoding="utf-8"))
        assert delivery["delivery_summary"]["consumer_observed"] == 3, delivery
        assert delivery["delivery_summary"]["rendered_before_final"] == 3, delivery
    _pass(name)


def assert_consume_sse_rejects_non_raw_desktop_provenance():
    name = "consume_sse_rejects_non_raw_desktop_provenance"
    from codex_oss.desktop_consumer_hook import consume_desktop_sse

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mission_id = "desktop_hook"
        mission_dir = _write_required_artifacts(root, mission_id)
        report = consume_desktop_sse(
            mission_id=mission_id,
            mission_dir=mission_dir,
            project_root=root,
            sse_text=_identity_sse(mission_id),
            route_authority=_desktop_route(),
            transcript_kind="bridge_harness_transcript",
        )
        assert report["ok"] is False, report
        assert "transcript_is_not_raw_desktop_export" in report["missing_evidence"], report
        delivery = json.loads((mission_dir / "commentary_delivery.json").read_text(encoding="utf-8"))
        assert not any(event["states"].get("consumer_observed") for event in delivery["events"].values()), delivery
    _pass(name)


def assert_consume_sse_rejects_generic_progress_without_event_identity():
    name = "consume_sse_rejects_generic_progress_without_event_identity"
    from codex_oss.desktop_consumer_hook import consume_desktop_sse

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mission_id = "desktop_hook"
        mission_dir = _write_required_artifacts(root, mission_id)
        report = consume_desktop_sse(
            mission_id=mission_id,
            mission_dir=mission_dir,
            project_root=root,
            sse_text=_generic_sse(),
            route_authority=_desktop_route(),
        )
        assert report["ok"] is False, report
        assert "desktop_consumer_observed_progress_insufficient" in report["missing_evidence"], report
        assert "desktop_progress_event_identity_missing" in report["missing_evidence"], report
    _pass(name)


def main() -> bool:
    for test in [
        assert_consume_sse_marks_delivery_and_verifies_desktop_gold,
        assert_consume_sse_rejects_non_raw_desktop_provenance,
        assert_consume_sse_rejects_generic_progress_without_event_identity,
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
