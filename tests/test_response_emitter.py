#!/usr/bin/env python3
"""Focused transport tests for Responses emitter projections."""

from __future__ import annotations

import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from codex_oss.transport.emitter import ResponseEmitter


class FakeHandler:
    def __init__(self):
        self.wfile = io.BytesIO()
        self.statuses = []
        self.headers = []

    def send_response(self, status):
        self.statuses.append(status)

    def send_header(self, key, value):
        self.headers.append((key, value))

    def end_headers(self):
        pass


def _sse_payloads(raw: bytes):
    payloads = []
    for block in raw.decode("utf-8").split("\n\n"):
        if not block.strip():
            continue
        event = None
        data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[len("event: ") :]
            if line.startswith("data: "):
                raw = line[len("data: ") :]
                if raw == "[DONE]":
                    data = {"done": True}
                else:
                    data = json.loads(raw)
        if event and data is not None:
            payloads.append((event, data))
    return payloads


def test_stream_commentary_preserves_visible_event_metadata():
    handler = FakeHandler()
    emitter = ResponseEmitter(handler, "resp_test", "oss_flash_support", True)
    metadata = {
        "oss_visible_event": {
            "mission_id": "mission_123",
            "event_id": "evt_1",
            "seq": 1,
            "event_type": "mission_started",
            "safe_for_user": True,
        }
    }

    emitter.emit_commentary_message(
        "[OSS progress mission=mission_123 event=evt_1 seq=1 type=mission_started] Mission accepted",
        metadata=metadata,
    )
    emitter.complete()

    payloads = _sse_payloads(handler.wfile.getvalue())
    assert any(event == "response.output_item.added" for event, _ in payloads)
    assert any(event == "response.output_item.done" for event, _ in payloads)

    commentary_payloads = [
        data for event, data in payloads
        if event.startswith("response.output_") or event.startswith("response.content_")
    ]
    assert commentary_payloads
    for data in commentary_payloads:
        assert data.get("phase") == "commentary"
        assert data.get("metadata", {}).get("oss_visible_event", {}).get("event_id") == "evt_1"

    completed = [data for event, data in payloads if event == "response.completed"][0]
    output = completed["response"]["output"]
    assert output[0]["phase"] == "commentary"
    assert output[0]["metadata"]["oss_visible_event"]["mission_id"] == "mission_123"


def test_json_commentary_preserves_visible_event_metadata():
    handler = FakeHandler()
    emitter = ResponseEmitter(handler, "resp_test", "oss_flash_support", False)
    metadata = {
        "oss_visible_event": {
            "mission_id": "mission_123",
            "event_id": "evt_2",
            "seq": 2,
            "event_type": "coverage_update",
            "safe_for_user": True,
        }
    }

    emitter.emit_commentary_message(
        "[OSS progress mission=mission_123 event=evt_2 seq=2 type=coverage_update] Coverage updated",
        metadata=metadata,
    )
    emitter.complete()

    response = json.loads(handler.wfile.getvalue().decode("utf-8"))
    item = response["output"][0]
    assert item["phase"] == "commentary"
    assert item["metadata"]["oss_visible_event"]["event_id"] == "evt_2"
    assert item["content"][0]["text"].startswith("[OSS progress mission=mission_123 event=evt_2 seq=2 type=coverage_update]")


if __name__ == "__main__":
    test_stream_commentary_preserves_visible_event_metadata()
    test_json_commentary_preserves_visible_event_metadata()
    print("test_response_emitter.py: ok")
