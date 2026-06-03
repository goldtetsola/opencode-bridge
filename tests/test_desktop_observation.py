#!/usr/bin/env python3
"""Tests for DesktopObservationV1 transcript provenance."""

import sys

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


def assert_raw_desktop_transcript_classifies_as_observed():
    name = "raw_desktop_transcript_classifies_as_observed"
    from codex_oss.desktop_observation import classify_transcript_provenance

    provenance = classify_transcript_provenance({
        "consumer_kind": "codex_desktop_spawned",
        "transcript_kind": "codex_desktop_raw_export",
    })

    assert provenance["ok"] is True, provenance
    assert provenance["claim_scope"] == "desktop_observed", provenance
    assert provenance["reasons"] == [], provenance
    _pass(name)


def assert_artifact_reconciled_candidate_is_not_observed():
    name = "artifact_reconciled_candidate_is_not_observed"
    from codex_oss.desktop_observation import classify_transcript_provenance

    provenance = classify_transcript_provenance({
        "consumer_kind": "codex_desktop_spawned",
        "transcript_kind": "artifact_reconciled_candidate_not_raw_desktop_export",
    })

    assert provenance["ok"] is False, provenance
    assert provenance["claim_scope"] == "desktop_unproven", provenance
    assert "transcript_is_not_raw_desktop_export" in provenance["reasons"], provenance
    _pass(name)


def assert_terminal_notification_only_is_not_observed():
    name = "terminal_notification_only_is_not_observed"
    from codex_oss.desktop_observation import classify_transcript_provenance

    provenance = classify_transcript_provenance({
        "consumer_kind": "codex_desktop_spawned",
        "transcript_kind": "desktop_terminal_notification_only",
    })

    assert provenance["ok"] is False, provenance
    assert "transcript_is_not_raw_desktop_export" in provenance["reasons"], provenance
    _pass(name)


def assert_consumer_observation_witness_requires_raw_progress_before_final():
    name = "consumer_observation_witness_requires_raw_progress_before_final"
    from codex_oss.desktop_observation import build_consumer_observation_witness

    terminal_only = build_consumer_observation_witness(
        mission_id="desktop_observed",
        transcript_meta={
            "mission_id": "desktop_observed",
            "consumer_kind": "codex_desktop_spawned",
            "transcript_kind": "desktop_terminal_notification_only",
        },
        messages=[{"phase": "final_answer", "text": "Done."}],
    )
    assert terminal_only["ok"] is False, terminal_only
    assert "transcript_is_not_raw_desktop_export" in terminal_only["reasons"], terminal_only
    assert "desktop_progress_before_final_missing" in terminal_only["reasons"], terminal_only

    raw_progress = build_consumer_observation_witness(
        mission_id="desktop_observed",
        transcript_meta={
            "mission_id": "desktop_observed",
            "consumer_kind": "codex_desktop_spawned",
            "transcript_kind": "codex_desktop_raw_export",
        },
        messages=[
            {"phase": "commentary", "text": "[OSS progress] start"},
            {"phase": "commentary", "text": "[OSS progress] read"},
            {"phase": "commentary", "text": "[OSS progress] verify"},
            {"phase": "final_answer", "text": "Done."},
        ],
    )
    assert raw_progress["ok"] is False, raw_progress
    assert "desktop_progress_event_identity_missing" in raw_progress["reasons"], raw_progress

    identity_progress = build_consumer_observation_witness(
        mission_id="desktop_observed",
        transcript_meta={
            "mission_id": "desktop_observed",
            "consumer_kind": "codex_desktop_spawned",
            "transcript_kind": "codex_desktop_raw_export",
        },
        messages=[
            {"phase": "commentary", "text": "[OSS progress] start", "event_id": "evt_1"},
            {"phase": "commentary", "text": "[OSS progress] read", "event_id": "evt_2"},
            {"phase": "commentary", "text": "[OSS progress] verify", "event_id": "evt_3"},
            {"phase": "final_answer", "text": "Done."},
        ],
        expected_event_ids=["evt_1", "evt_2", "evt_3"],
    )
    assert identity_progress["ok"] is True, identity_progress
    _pass(name)


def assert_builder_preserves_only_observed_messages_contract():
    name = "builder_preserves_only_observed_messages_contract"
    from codex_oss.desktop_observation import build_raw_desktop_transcript, classify_transcript_provenance

    transcript = build_raw_desktop_transcript(
        mission_id="desktop_observed",
        agent_id="agent_123",
        agent_name="Noether",
        messages=[
            {"phase": "commentary", "text": "I inspected the required source."},
            {"phase": "final_answer", "text": "Final answer."},
            "not a message",
        ],
        captured_at=1.0,
    )

    assert transcript["schema_version"] == "desktop_observation_transcript.v1", transcript
    assert transcript["consumer_kind"] == "codex_desktop_spawned", transcript
    assert transcript["transcript_kind"] == "codex_desktop_raw_export", transcript
    assert len(transcript["messages"]) == 2, transcript
    assert classify_transcript_provenance(transcript)["ok"] is True, transcript
    _pass(name)


def main():
    for test in [
        assert_raw_desktop_transcript_classifies_as_observed,
        assert_artifact_reconciled_candidate_is_not_observed,
        assert_terminal_notification_only_is_not_observed,
        assert_consumer_observation_witness_requires_raw_progress_before_final,
        assert_builder_preserves_only_observed_messages_contract,
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
