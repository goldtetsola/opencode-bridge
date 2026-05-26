#!/usr/bin/env python3
"""VisibleCommentaryV1 focused safety tests."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from codex_oss.visible_commentary import (  # noqa: E402
    VisibleCommentarySink,
    model_action_public_message,
    safe_tool_target_text,
    sanitize_visible_payload,
)


def assert_visible_commentary_recursively_sanitizes_public_event_payloads():
    root = tempfile.mkdtemp(dir=ROOT)
    try:
        streamed = []
        sink = VisibleCommentarySink("mission_visible_safety", root, stream_callback=streamed.append)
        event = sink.emit(
            "tool_action_planned",
            "Planning sk-ocg-SUPERSECRET123",
            "The model wants to inspect OPENCODE_GO_API_KEY=sk-live-secret-token123456.",
            phase="NARROW",
            source="model_action",
            metadata={
                "arguments": {
                    "path": os.path.join(ROOT, ".codex-oss", "env", "opencode-go.env"),
                    "pattern": "Bearer sk-live-secret-token123456",
                    "notes": ["DATABASE_URL=postgres://user:pass@example/db", "ordinary"],
                },
                "raw_prompt": "-----BEGIN PRIVATE KEY-----bad-----END PRIVATE KEY-----",
            },
            evidence_refs=["Bearer sk-live-secret-token123456"],
            artifact_refs=[os.path.join(ROOT, ".codex-oss", "env", "opencode-go.env")],
        )

        assert event["category"] == "model_action", event
        assert event["safe_for_user"] is True, event
        assert event["redactions_applied"] is True, event
        assert event["metadata"]["arguments"]["path"] == "[restricted path]", event
        assert event["metadata"]["arguments"]["pattern"] == "[pattern elided]", event
        assert streamed and streamed[0]["event_type"] == "tool_action_planned", streamed

        serialized = json.dumps(event, sort_keys=True)
        assert "SUPERSECRET" not in serialized, serialized
        assert "sk-live-secret" not in serialized, serialized
        assert "postgres://" not in serialized, serialized
        assert "PRIVATE KEY" not in serialized, serialized
        assert ".codex-oss/env/opencode-go.env" not in serialized, serialized
    finally:
        shutil.rmtree(root)


def assert_model_action_public_message_is_categorical_not_raw_rationale():
    message = model_action_public_message(
        "rtk_grep",
        {"path": "codex_oss/visible_commentary.py", "pattern": "OPENAI_API_KEY=sk-live-secret-token123456"},
        reason="Find OPENAI_API_KEY=sk-live-secret-token123456 in the file.",
        hypothesis="The raw secret appears in the implementation.",
        expected_information_gain="It will reveal the exact secret.",
        why_not_report_yet="Need more evidence first.",
    )
    assert "rtk_grep in codex_oss/visible_commentary.py with a targeted pattern" in message, message
    assert "declared a reason, a hypothesis, expected information gain, and why a report is not ready yet" in message, message
    assert "OPENAI_API_KEY" not in message, message
    assert "sk-live-secret" not in message, message
    assert "exact secret" not in message, message


def assert_safe_tool_target_text_hides_patterns_and_sensitive_paths():
    safe = safe_tool_target_text("rtk_read", {"path": ".codex-oss/env/opencode-go.env"})
    assert safe == "rtk_read on [restricted path]", safe

    grep = safe_tool_target_text("rtk_grep", {"path": "README.md", "pattern": "sk-live-secret-token123456"})
    assert grep == "rtk_grep in README.md with a targeted pattern", grep
    assert "sk-live-secret" not in grep, grep


def assert_sanitize_visible_payload_caps_and_limits_nested_metadata():
    payload, redacted = sanitize_visible_payload({
        "path": os.path.join(ROOT, "codex_oss", "visible_commentary.py"),
        "long": "x" * 300,
        "items": list(range(20)),
    })
    assert payload["path"] == "codex_oss/visible_commentary.py", payload
    assert payload["long"].endswith("..."), payload
    assert payload["items"][-1] == "[list truncated]", payload
    assert redacted is True, payload


def main():
    assert_visible_commentary_recursively_sanitizes_public_event_payloads()
    assert_model_action_public_message_is_categorical_not_raw_rationale()
    assert_safe_tool_target_text_hides_patterns_and_sensitive_paths()
    assert_sanitize_visible_payload_caps_and_limits_nested_metadata()
    print("PASS: visible commentary safety suite")


if __name__ == "__main__":
    main()
