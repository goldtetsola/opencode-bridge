#!/usr/bin/env python3
"""Tests for OSSFinalClaimGateV1."""

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


def assert_success_fails_with_unresolved_pending_calls():
    name = "success_fails_with_unresolved_pending_calls"
    from codex_oss.final_claim_gate import evaluate_final_claim_gate

    gate = evaluate_final_claim_gate(
        claim_type="bridge_gold",
        requested_status="GOLD",
        pending_call_ids=["call_a"],
    )
    assert gate["ok"] is False, gate
    assert gate["effective_status"] == "FAIL", gate
    assert gate["publication_decision"] == "success_claim_denied", gate
    assert "pending_tool_calls_unresolved:call_a" in gate["reasons"], gate
    _pass(name)


def assert_recovered_pending_calls_can_continue():
    name = "recovered_pending_calls_can_continue"
    from codex_oss.final_claim_gate import evaluate_final_claim_gate

    gate = evaluate_final_claim_gate(
        claim_type="bridge_gold",
        requested_status="GOLD",
        pending_call_ids=["call_a"],
        recovered_call_ids=["call_a"],
        recovery_used=True,
    )
    assert gate["ok"] is True, gate
    _pass(name)


def assert_desktop_gold_requires_route_and_transcript():
    name = "desktop_gold_requires_route_and_transcript"
    from codex_oss.final_claim_gate import evaluate_final_claim_gate
    from codex_oss.route_authority import build_route_authority

    raw_route = build_route_authority(
        model_alias="ocg-kimi-k2.6",
        handoff_obj={"schema_version": 1},
        consumer_kind="codex_desktop_spawned",
    )
    gate = evaluate_final_claim_gate(
        claim_type="desktop_gold",
        requested_status="DESKTOP_GOLD",
        route_authority=raw_route,
    )
    assert gate["ok"] is False, gate
    assert gate["claim_tuple"]["consumer_kind"] == "codex_desktop_spawned", gate
    assert "desktop_route_authority_missing" in gate["reasons"], gate
    assert "desktop_consumer_observation_witness_missing_or_failed" in gate["reasons"], gate
    assert "desktop_transcript_hash_missing" in gate["reasons"], gate
    assert "desktop_pre_final_text_probe_not_passed:unknown" in gate["reasons"], gate
    _pass(name)


def assert_implementation_success_requires_witness_and_changes():
    name = "implementation_success_requires_witness_and_changes"
    from codex_oss.final_claim_gate import evaluate_final_claim_gate

    gate = evaluate_final_claim_gate(
        claim_type="implementation",
        requested_status="VERIFIED",
        implementation_witness={"ok": False},
        changed_owned_paths=[],
    )
    assert gate["ok"] is False, gate
    assert gate["implementation_decision"] == "implementation_success_denied", gate
    assert "implementation_witness_missing_or_failed" in gate["reasons"], gate
    assert "changed_owned_paths_empty_for_success" in gate["reasons"], gate
    _pass(name)


def assert_desktop_claim_requires_consumer_witness_not_just_transcript_path():
    name = "desktop_claim_requires_consumer_witness_not_just_transcript_path"
    from codex_oss.final_claim_gate import evaluate_final_claim_gate
    from codex_oss.route_authority import build_route_authority

    with tempfile.TemporaryDirectory() as tmp:
        transcript = Path(tmp) / "transcript.txt"
        artifact = Path(tmp) / "report.json"
        transcript.write_text("[OSS progress] one\n[OSS progress] two\n[OSS progress] three\nFinal\n", encoding="utf-8")
        artifact.write_text(json.dumps({"ok": True}), encoding="utf-8")
        route = build_route_authority(
            model_alias="mission-a3-kimi",
            handoff_obj={"schema_version": "oss_agent_mission.v1"},
            consumer_kind="codex_desktop_spawned",
        )
        gate = evaluate_final_claim_gate(
            claim_type="desktop_gold",
            requested_status="DESKTOP_GOLD",
            route_authority=route,
            transcript_path=str(transcript),
            artifact_paths=[str(artifact)],
        )
    assert gate["ok"] is False, gate
    assert "desktop_consumer_observation_witness_missing_or_failed" in gate["reasons"], gate
    assert "desktop_pre_final_text_probe_not_passed:unknown" in gate["reasons"], gate
    assert gate["transcript_hash"].startswith("sha256:"), gate
    _pass(name)


def assert_hashes_are_recorded_for_witnessed_desktop_claim():
    name = "hashes_are_recorded_for_witnessed_desktop_claim"
    from codex_oss.final_claim_gate import evaluate_final_claim_gate
    from codex_oss.route_authority import build_route_authority

    with tempfile.TemporaryDirectory() as tmp:
        transcript = Path(tmp) / "transcript.txt"
        artifact = Path(tmp) / "report.json"
        transcript.write_text("[OSS progress] one\n[OSS progress] two\n[OSS progress] three\nFinal\n", encoding="utf-8")
        artifact.write_text(json.dumps({"ok": True}), encoding="utf-8")
        route = build_route_authority(
            model_alias="mission-a3-kimi",
            handoff_obj={"schema_version": "oss_agent_mission.v1"},
            consumer_kind="codex_desktop_spawned",
        )
        gate = evaluate_final_claim_gate(
            claim_type="desktop_gold",
            requested_status="DESKTOP_GOLD",
            route_authority=route,
            transcript_path=str(transcript),
            artifact_paths=[str(artifact)],
            consumer_observation_witness={"schema_version": "consumer_observation_witness.v1", "ok": True},
            desktop_render_surface={"probe_required": True, "probe_status": "pass"},
        )
    assert gate["ok"] is True, gate
    assert gate["transcript_hash"].startswith("sha256:"), gate
    assert next(iter(gate["artifact_hashes"].values())).startswith("sha256:"), gate
    _pass(name)


def main():
    for test in [
        assert_success_fails_with_unresolved_pending_calls,
        assert_recovered_pending_calls_can_continue,
        assert_desktop_gold_requires_route_and_transcript,
        assert_implementation_success_requires_witness_and_changes,
        assert_desktop_claim_requires_consumer_witness_not_just_transcript_path,
        assert_hashes_are_recorded_for_witnessed_desktop_claim,
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
