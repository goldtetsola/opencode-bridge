#!/usr/bin/env python3
"""Tests for the Codex app-server progress probe helper."""

import os
import shutil
import tempfile

from codex_oss.app_server_probe import (
    VISIBLE_COMMENTARY_FINAL_MARKER,
    app_server_config_args,
    build_visible_commentary_probe_payload,
    render_commentary_event_for_app_server,
)


def assert_app_server_config_args_define_probe_provider():
    args = app_server_config_args(43211)
    joined = "\n".join(args)
    assert 'model_provider="desktop_pre_final_text_probe"' in joined
    assert 'model="desktop-pre-final-text-probe"' in joined
    assert 'model_providers.desktop_pre_final_text_probe.base_url="http://127.0.0.1:43211/v1"' in joined
    assert 'model_providers.desktop_pre_final_text_probe.wire_api="responses"' in joined
    assert 'model_providers.desktop_pre_final_text_probe.auth.command="echo"' in joined


def assert_visible_commentary_probe_payload_uses_runtime_events():
    root = tempfile.mkdtemp()
    try:
        payload = build_visible_commentary_probe_payload(mission_dir=root)
        events = payload["events"]
        lines = payload["lines"]
        assert len(events) == 4, events
        assert events[0]["schema_version"] == "visible_commentary_event.v1", events[0]
        assert events[0]["event_type"] == "mission_started", events[0]
        assert events[1]["event_type"] == "tool_action_started", events[1]
        assert events[2]["event_type"] == "coverage_update", events[2]
        assert events[3]["event_type"] == "mission_completed", events[3]
        assert lines[-1] == VISIBLE_COMMENTARY_FINAL_MARKER, lines
        assert all(event["message"] in line for event, line in zip(events, lines)), lines
        assert os.path.exists(payload["visible_commentary_path"]), payload
        assert os.path.exists(payload["summary_path"]), payload
        assert os.path.exists(payload["delivery_path"]), payload
    finally:
        shutil.rmtree(root)


def assert_render_commentary_event_for_app_server_keeps_phase_title_message():
    line = render_commentary_event_for_app_server({
        "phase": "VERIFY",
        "title": "Coverage updated",
        "message": "The required evidence floor is satisfied.",
    })
    assert line == "[VERIFY] Coverage updated: The required evidence floor is satisfied.", line


def main():
    tests = [
        assert_app_server_config_args_define_probe_provider,
        assert_visible_commentary_probe_payload_uses_runtime_events,
        assert_render_commentary_event_for_app_server_keeps_phase_title_message,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"Results: {len(tests) - failed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
