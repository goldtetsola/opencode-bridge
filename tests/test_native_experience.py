"""Tests for NativeExperienceContractV1, CommentaryDeliveryV1, and transcript harness."""

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

JSON = dict[str, Any]

PASSED = 0
FAILED = 0

def _pass(name, detail=""):
    global PASSED; PASSED += 1
    print(f"  PASS{' — ' + detail if detail else '':60s} {name}")

def _fail(name, detail=""):
    global FAILED; FAILED += 1
    print(f"  FAIL{' — ' + detail if detail else '':60s} {name}")


# ═══════════════════════════════════════════════════════════════════════════
# NativeExperienceContractV1 tests
# ═══════════════════════════════════════════════════════════════════════════


def test_contract_build_and_evaluate_bronze():
    """Bronze: runtime-safe passes, gold fails without commentary."""
    name = "contract_bronze"
    from codex_oss.native_experience import (
        build_native_experience_contract,
        evaluate_native_experience,
    )

    contract = build_native_experience_contract(
        mission_id="test_bronze",
        task_class="read_floor",
    )

    result = evaluate_native_experience(
        contract,
        runtime_owns_status=True,
        writes_outside_scope=False,
        secrets_leaked=False,
        raw_cot_exposed=False,
        model_narrative_valid=True,
    )

    assert result["level"] == "silver", f"expected silver, got {result['level']}"
    assert result["bronze_pass"] is True
    assert result["silver_pass"] is True
    assert result["gold_pass"] is False  # no commentary observed
    assert result["platinum_pass"] is False
    _pass(name, f"level={result['level']}")


def test_contract_gold_requires_commentary():
    """Gold: requires observed commentary before final."""
    name = "contract_gold_requires_commentary"
    from codex_oss.native_experience import (
        build_native_experience_contract,
        evaluate_native_experience,
    )

    contract = build_native_experience_contract(
        mission_id="test_gold",
        task_class="read_floor",
    )

    # All conditions met EXCEPT commentary
    result = evaluate_native_experience(
        contract,
        runtime_owns_status=True,
        model_narrative_valid=True,
        commentary_events_count=3,
        commentary_event_classes={"mission_or_action_start", "tool_or_evidence_progress", "closure_or_verification_progress"},
        commentary_observed=False,  # NOT observed
        commentary_rendered_before_final=False,
    )

    assert result["gold_pass"] is False
    assert "commentary_not_observed_or_rendered_before_final" in result["missing_evidence"]

    # Now with commentary observed
    result2 = evaluate_native_experience(
        contract,
        artifacts_exist={
            "visible_commentary_jsonl": True,
            "summary_md": True,
            "canonical_evidence": True,
            "adoption_probes": True,
        },
        runtime_owns_status=True,
        model_narrative_valid=True,
        commentary_events_count=3,
        commentary_event_classes={"mission_or_action_start", "tool_or_evidence_progress", "closure_or_verification_progress"},
        commentary_observed=True,
        commentary_rendered_before_final=True,
    )

    assert result2["gold_pass"] is True
    assert result2["level"] == "gold"
    _pass(name, "gold correctly requires observed commentary")


def test_contract_platinum_requires_adoption():
    """Platinum: requires adoption recorded + no replay loops."""
    name = "contract_platinum"
    from codex_oss.native_experience import (
        build_native_experience_contract,
        evaluate_native_experience,
    )

    contract = build_native_experience_contract(
        mission_id="test_platinum",
        task_class="read_floor",
    )

    result = evaluate_native_experience(
        contract,
        artifacts_exist={
            "visible_commentary_jsonl": True,
            "summary_md": True,
            "canonical_evidence": True,
            "adoption_probes": True,
        },
        runtime_owns_status=True,
        model_narrative_valid=True,
        commentary_events_count=3,
        commentary_event_classes={"mission_or_action_start", "tool_or_evidence_progress", "closure_or_verification_progress"},
        commentary_observed=True,
        commentary_rendered_before_final=True,
        replay_loops=0,
        pending_after_final=False,
        adoption_recorded=True,
    )

    assert result["platinum_pass"] is True
    assert result["level"] == "platinum"
    _pass(name, f"level={result['level']}")


def test_desktop_gold_requires_consumer_observation_witness_not_hash_only():
    """Desktop Gold provenance needs observed Desktop witness, not just a transcript hash."""
    name = "desktop_gold_requires_consumer_witness"
    from codex_oss.native_experience import build_native_experience_contract, evaluate_native_experience
    from codex_oss.route_authority import build_route_authority

    route = build_route_authority(
        model_alias="mission-a3-deepseek",
        handoff_obj={"schema_version": "oss_agent_mission.v1"},
        consumer_kind="codex_desktop_spawned",
    )
    contract = build_native_experience_contract(
        mission_id="desktop_hash_only",
        task_class="read_floor",
        route_authority=route,
    )
    contract["desktop_transcript_hash"] = "sha256:hash-is-not-witness"
    common = dict(
        artifacts_exist={
            "visible_commentary_jsonl": True,
            "summary_md": True,
            "canonical_evidence": True,
            "adoption_probes": True,
        },
        runtime_owns_status=True,
        model_narrative_valid=True,
        commentary_events_count=3,
        commentary_event_classes={"mission_or_action_start", "tool_or_evidence_progress", "closure_or_verification_progress"},
        commentary_observed=True,
        commentary_rendered_before_final=True,
    )
    result = evaluate_native_experience(contract, **common)
    assert result["gold_pass"] is True, result
    assert result["desktop_gold_pass"] is False, result
    assert "desktop_consumer_observation_witness_missing_or_failed" in result["missing_evidence"], result

    contract["consumer_observation_witness"] = {"schema_version": "consumer_observation_witness.v1", "ok": True}
    blocked_by_probe = evaluate_native_experience(contract, **common)
    assert blocked_by_probe["desktop_gold_pass"] is False, blocked_by_probe
    assert "desktop_pre_final_text_probe_not_passed:unknown" in blocked_by_probe["missing_evidence"], blocked_by_probe

    contract["desktop_render_surface"] = {"probe_required": True, "probe_status": "pass"}
    witnessed = evaluate_native_experience(contract, **common)
    assert witnessed["desktop_gold_pass"] is True, witnessed
    _pass(name, "hash-only desktop provenance rejected")


# ═══════════════════════════════════════════════════════════════════════════
# CommentaryDeliveryV1 tests
# ═══════════════════════════════════════════════════════════════════════════


def test_commentary_delivery_lifecycle():
    """Full delivery lifecycle: create → sanitize → emit → observe → render."""
    name = "commentary_delivery_lifecycle"
    from codex_oss.native_experience import CommentaryDeliveryTracker

    tracker = CommentaryDeliveryTracker("test_mission")
    eid = tracker.create_event("server_side_read_started", "Completing reads server-side.", phase="READ_FLOOR")

    assert tracker.events[eid]["states"]["created"] is True
    assert tracker.events[eid]["states"]["sse_emitted"] is False

    tracker.mark_sanitized(eid)
    tracker.mark_stream_enqueued(eid)
    tracker.mark_sse_emitted(eid)
    tracker.mark_observed(eid)
    tracker.mark_rendered(eid)
    tracker.mark_reconciled(eid)

    assert tracker.all_emitted is True
    assert tracker.all_rendered is True

    summary = tracker.delivery_summary
    assert summary["total"] == 1
    assert summary["emitted"] == 1
    assert summary["rendered"] == 1
    assert summary["delivery_rate"] == 1.0

    _pass(name, "full delivery lifecycle tracked")


def test_commentary_delivery_failure():
    """Failed delivery is tracked."""
    name = "commentary_delivery_failure"
    from codex_oss.native_experience import CommentaryDeliveryTracker

    tracker = CommentaryDeliveryTracker("test_fail")
    eid = tracker.create_event("mission_started", "Starting...")
    tracker.mark_sse_emitted(eid)
    tracker.mark_failed(eid, "consumer not reachable")

    assert tracker.events[eid]["states"]["failed"] is True
    assert tracker.events[eid]["failure_reason"] == "consumer not reachable"
    assert tracker.all_rendered is False

    _pass(name, "failure tracked correctly")


def test_commentary_delivery_persist():
    """Delivery tracker persists to JSON."""
    name = "commentary_delivery_persist"
    from codex_oss.native_experience import CommentaryDeliveryTracker

    with tempfile.TemporaryDirectory() as tmp:
        tracker = CommentaryDeliveryTracker("test_persist")
        eid = tracker.create_event("mission_started", "Starting mission.")
        tracker.mark_sse_emitted(eid)
        tracker.mark_rendered(eid)
        tracker.persist(tmp)

        path = os.path.join(tmp, "commentary_delivery.json")
        assert os.path.exists(path)

        with open(path) as f:
            data = json.load(f)
        assert data["schema_version"] == "commentary_delivery_tracker.v1"
        assert len(data["events"]) == 1

    _pass(name, "persisted and reloaded")


# ═══════════════════════════════════════════════════════════════════════════
# Event classification tests
# ═══════════════════════════════════════════════════════════════════════════


def test_event_classification():
    """All known event types map to required event classes."""
    name = "event_classification"
    from codex_oss.native_experience import classify_commentary_event, extract_event_classes

    # Each required class must have at least one event type
    events = [
        {"event_type": "mission_started"},
        {"event_type": "server_side_read_started"},
        {"event_type": "server_side_read_completed"},
        {"event_type": "mission_completed"},
    ]
    classes = extract_event_classes(events)

    assert "mission_or_action_start" in classes
    assert "tool_or_evidence_progress" in classes
    assert "closure_or_verification_progress" in classes

    # Verify specific mappings
    assert classify_commentary_event("mission_started") == "mission_or_action_start"
    assert classify_commentary_event("server_side_read_started") == "tool_or_evidence_progress"
    assert classify_commentary_event("mission_completed") == "closure_or_verification_progress"
    assert classify_commentary_event("patch_validation_passed") == "tool_or_evidence_progress"
    assert classify_commentary_event("verification_passed") == "closure_or_verification_progress"

    _pass(name, "all event types mapped to required classes")


# ═══════════════════════════════════════════════════════════════════════════
# Spawned transcript unit tests (no live bridge needed)
# ═══════════════════════════════════════════════════════════════════════════


def test_transcript_looks_like_progress():
    """Progress detection heuristic identifies commentary text."""
    name = "transcript_looks_like_progress"
    from codex_oss.spawned_transcript import SpawnedTranscript

    assert SpawnedTranscript._looks_like_progress("I'm completing the read floor server-side.")
    assert SpawnedTranscript._looks_like_progress("I'm checking the file for evidence.")
    assert SpawnedTranscript._looks_like_progress("Patch validation passed; applying in isolation.")
    assert SpawnedTranscript._looks_like_progress("[OSS progress] Verifying the change...")
    assert not SpawnedTranscript._looks_like_progress("The file contains 42 lines of Python code.")
    assert not SpawnedTranscript._looks_like_progress("Hello world")

    _pass(name, "progress detection accurate")


def test_transcript_artifact_loading():
    """Transcript loads artifacts from filesystem."""
    name = "transcript_artifact_loading"
    from codex_oss.spawned_transcript import SpawnedTranscript
    import time

    with tempfile.TemporaryDirectory() as tmp:
        mission_dir = Path(tmp) / ".codex-oss" / "missions" / "test_artifact_load"
        mission_dir.mkdir(parents=True)

        # Write some artifacts
        (mission_dir / "visible_commentary.jsonl").write_text(
            '{"schema_version":"visible_commentary_event.v1","event_type":"mission_started","message":"test"}\n'
        )
        (mission_dir / "summary.md").write_text("# Test Summary\n")
        (mission_dir / "report.json").write_text('{"status":"COMPLETE"}')

        # Monkey-patch cwd for artifact loading
        old_cwd = os.getcwd()
        try:
            os.chdir(tmp)
            transcript = SpawnedTranscript("test_artifact_load", "test_model", "read_floor", time.time())
            transcript.load_artifacts()

            assert transcript.artifacts.get("visible_commentary.jsonl") is True
            assert transcript.artifacts.get("summary.md") is True
            assert transcript.artifacts.get("report.json") is True
            assert isinstance(transcript.artifact_data.get("visible_commentary.jsonl"), list)
            assert len(transcript.artifact_data["visible_commentary.jsonl"]) == 1
        finally:
            os.chdir(old_cwd)

    _pass(name, "artifacts loaded correctly")


def test_transcript_commentary_extraction():
    """Transcript extracts commentary from raw messages."""
    name = "transcript_commentary_extraction"
    from codex_oss.spawned_transcript import SpawnedTranscript
    import time

    transcript = SpawnedTranscript("test_extract", "test_model", "read_floor", time.time())

    # Simulate raw messages from SSE
    transcript.raw_messages = [
        {"timestamp": 1, "event_type": "response.output_text.delta", "text": "I'm completing the declared read-only evidence floor server-side.", "phase": "READ_FLOOR"},
        {"timestamp": 2, "event_type": "response.output_text.delta", "text": "I inspected bridge.py and README.md.", "phase": "READ_FLOOR"},
        {"timestamp": 3, "event_type": "response.output_text.delta", "text": "The read floor is complete.", "phase": "REPORT"},
    ]

    transcript.extract_commentary()

    assert len(transcript.commentary_messages) == 3
    assert len(transcript.commentary_before_final) >= 2
    assert "mission_or_action_start" in transcript.commentary_event_classes or "tool_or_evidence_progress" in transcript.commentary_event_classes

    _pass(name, f"{len(transcript.commentary_messages)} commentary messages extracted")


def test_transcript_gold_rejects_unobserved_artifact_commentary():
    """Gold must fail if commentary exists only in artifacts and not in transcript."""
    name = "transcript_gold_rejects_unobserved_artifacts"
    from codex_oss.spawned_transcript import SpawnedTranscript
    import time

    with tempfile.TemporaryDirectory() as tmp:
        mission_id = "test_unobserved_artifacts"
        mission_dir = Path(tmp) / ".codex-oss" / "missions" / mission_id
        mission_dir.mkdir(parents=True)

        artifact_events = [
            {"schema_version": "visible_commentary_event.v1", "event_type": "mission_started", "message": "I found a declared read-only evidence floor."},
            {"schema_version": "visible_commentary_event.v1", "event_type": "server_side_read_started", "message": "I inspected bridge.py, README.md, and codex_oss/read_evidence.py."},
            {"schema_version": "visible_commentary_event.v1", "event_type": "mission_completed", "message": "The read floor is complete; finalizing from canonical evidence."},
        ]
        (mission_dir / "visible_commentary.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in artifact_events),
            encoding="utf-8",
        )
        (mission_dir / "summary.md").write_text("# Summary\n", encoding="utf-8")
        (mission_dir / "canonical_read_evidence.json").write_text(
            json.dumps({"status_entitlement": {"can_complete": True}}),
            encoding="utf-8",
        )
        (mission_dir / "read_report_skeleton.json").write_text("{}", encoding="utf-8")
        (mission_dir / "report.json").write_text(
            json.dumps({"report_source": "runtime", "implementation_narrative_valid": True}),
            encoding="utf-8",
        )

        old_cwd = os.getcwd()
        try:
            os.chdir(tmp)
            transcript = SpawnedTranscript(mission_id, "test_model", "read_floor", time.time())
            transcript.raw_messages = [
                {"timestamp": 1, "event_type": "response.output_text.delta", "text": "Final answer only.", "phase": "final_answer"},
            ]
            transcript.load_artifacts()
            transcript.extract_commentary()
            result = transcript.evaluate_gold_ux()
        finally:
            os.chdir(old_cwd)

    assert result["gold_pass"] is False, result
    assert result["transcript_metadata"]["commentary_before_final"] == 0, result
    _pass(name, "artifact-only commentary is not accepted as rendered UX")


def test_transcript_reconciles_observed_artifact_event_classes():
    """Observed transcript text should inherit its artifact event class for Gold."""
    name = "transcript_observed_artifact_event_classes"
    from codex_oss.spawned_transcript import SpawnedTranscript
    import time

    with tempfile.TemporaryDirectory() as tmp:
        mission_id = "test_observed_artifacts"
        mission_dir = Path(tmp) / ".codex-oss" / "missions" / mission_id
        mission_dir.mkdir(parents=True)
        artifact_events = [
            {"schema_version": "visible_commentary_event.v1", "event_type": "mission_started", "message": "I found a declared read-only evidence floor."},
            {"schema_version": "visible_commentary_event.v1", "event_type": "server_side_read_started", "message": "I inspected bridge.py, README.md, and codex_oss/read_evidence.py."},
            {"schema_version": "visible_commentary_event.v1", "event_type": "mission_completed", "message": "The read floor is complete; finalizing from canonical evidence."},
        ]
        (mission_dir / "visible_commentary.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in artifact_events),
            encoding="utf-8",
        )
        (mission_dir / "summary.md").write_text("# Summary\n", encoding="utf-8")
        (mission_dir / "canonical_read_evidence.json").write_text(
            json.dumps({"status_entitlement": {"can_complete": True}}),
            encoding="utf-8",
        )
        (mission_dir / "read_report_skeleton.json").write_text("{}", encoding="utf-8")
        (mission_dir / "report.json").write_text(
            json.dumps({"report_source": "runtime", "implementation_narrative_valid": True}),
            encoding="utf-8",
        )
        (mission_dir / "tool_call_adoption_probes.json").write_text(
            json.dumps({"probes": []}),
            encoding="utf-8",
        )

        old_cwd = os.getcwd()
        try:
            os.chdir(tmp)
            transcript = SpawnedTranscript(mission_id, "test_model", "read_floor", time.time())
            transcript.raw_messages = [
                {"timestamp": 1, "event_type": "response.output_text.delta", "text": "[OSS progress] I found a declared read-only evidence floor.", "phase": "commentary"},
                {"timestamp": 2, "event_type": "response.output_text.delta", "text": "[OSS progress] I inspected bridge.py, README.md, and codex_oss/read_evidence.py.", "phase": "commentary"},
                {"timestamp": 3, "event_type": "response.output_text.delta", "text": "[OSS progress] The read floor is complete; finalizing from canonical evidence.", "phase": "commentary"},
                {"timestamp": 4, "event_type": "response.output_text.delta", "text": "Final answer.", "phase": "final_answer"},
            ]
            transcript.load_artifacts()
            transcript.extract_commentary()
            result = transcript.evaluate_gold_ux()
        finally:
            os.chdir(old_cwd)

    assert result["gold_pass"] is True, result
    assert result["level"] == "gold", result
    assert result["transcript_metadata"]["commentary_before_final"] == 3, result
    _pass(name, "observed commentary reconciles to artifact event classes")


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════


def main():
    print()
    print("=" * 70)
    print(" NativeExperienceContractV1 + CommentaryDeliveryV1 + Transcript")
    print("=" * 70)
    print()

    print("── NativeExperienceContractV1 ──")
    test_contract_build_and_evaluate_bronze()
    test_contract_gold_requires_commentary()
    test_contract_platinum_requires_adoption()
    test_desktop_gold_requires_consumer_observation_witness_not_hash_only()

    print("\n── CommentaryDeliveryV1 ──")
    test_commentary_delivery_lifecycle()
    test_commentary_delivery_failure()
    test_commentary_delivery_persist()

    print("\n── Event classification ──")
    test_event_classification()

    print("\n── Spawned transcript ──")
    test_transcript_looks_like_progress()
    test_transcript_artifact_loading()
    test_transcript_commentary_extraction()
    test_transcript_gold_rejects_unobserved_artifact_commentary()
    test_transcript_reconciles_observed_artifact_event_classes()

    print()
    print("=" * 70)
    print(f" Results: {PASSED} passed, {FAILED} failed")
    print("=" * 70)
    print()

    return FAILED == 0


if __name__ == "__main__":
    if not main():
        sys.exit(1)
