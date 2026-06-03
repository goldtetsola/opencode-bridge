#!/usr/bin/env python3
"""Tests for captured Codex Desktop native UX verifier."""

import json
import os
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


def _write_required_artifacts(root: Path, mission_id: str) -> None:
    mission_dir = root / ".codex-oss" / "missions" / mission_id
    mission_dir.mkdir(parents=True, exist_ok=True)
    (mission_dir / "visible_commentary.jsonl").write_text('{"event_type":"mission_started","message":"[OSS progress] start"}\n', encoding="utf-8")
    events = {}
    for idx in range(1, 4):
        event_id = f"evt_{idx:04d}"
        events[event_id] = {
            "schema_version": "commentary_delivery.v1",
            "mission_id": mission_id,
            "event_id": event_id,
            "event_type": "tool_action_started",
            "states": {"created": True, "stream_enqueued": True, "sse_emitted": True},
        }
    (mission_dir / "commentary_delivery.json").write_text(json.dumps({
        "schema_version": "commentary_delivery_tracker.v1",
        "mission_id": mission_id,
        "events": events,
    }), encoding="utf-8")
    (mission_dir / "summary.md").write_text("summary", encoding="utf-8")
    (mission_dir / "canonical_read_evidence.json").write_text(json.dumps({"mission_id": mission_id, "status_entitlement": {"can_complete": True}}), encoding="utf-8")
    (mission_dir / "report.json").write_text(json.dumps({"mission_id": mission_id, "report_source": "runtime"}), encoding="utf-8")
    (mission_dir / "mission.json").write_text(json.dumps({"mission_id": mission_id}), encoding="utf-8")
    (root / ".codex-oss" / "desktop_pre_final_text_probe_result.json").write_text(json.dumps({
        "schema_version": "desktop_pre_final_text_probe_result.v1",
        "probe_status": "pass",
        "desktop_render_surface": {"probe_required": True, "probe_status": "pass"},
    }), encoding="utf-8")
    (mission_dir / "tool_call_adoption_probes.json").write_text(json.dumps({
        "schema_version": "tool_call_adoption_probes.v1",
        "probes": [{"call_id": "call_1", "consumer_adopted": False, "recovery_used": True}],
    }), encoding="utf-8")


def _identity_progress_messages(mission_id: str, verbs: list[str] | None = None) -> list[dict]:
    verbs = verbs or ["start", "read", "verify"]
    messages = []
    for idx, verb in enumerate(verbs, start=1):
        event_id = f"evt_{idx:04d}"
        messages.append({
            "text": f"[OSS progress mission={mission_id} event={event_id} seq={idx} type=tool_action_started] {verb}",
            "phase": "commentary",
            "event_id": event_id,
            "event_type": "tool_action_started",
            "seq": idx,
        })
    return messages


def _generic_progress_messages() -> list[dict]:
    return [
        {"text": "[OSS progress] start", "phase": "commentary"},
        {"text": "[OSS progress] read", "phase": "commentary"},
        {"text": "[OSS progress] verify", "phase": "commentary"},
    ]


def assert_verifier_fails_without_transcript():
    name = "verifier_fails_without_transcript"
    from codex_oss.desktop_native_verifier import verify_desktop_native_ux

    with tempfile.TemporaryDirectory() as tmp:
        report = verify_desktop_native_ux(
            transcript_path=os.path.join(tmp, "missing.json"),
            mission_id="desktop_missing",
            project_root=tmp,
            route_authority=None,
        )
    assert report["ok"] is False, report
    assert "desktop_native_unproven" in report["missing_evidence"], report
    _pass(name)


def assert_verifier_requires_desktop_route_authority():
    name = "verifier_requires_desktop_route_authority"
    from codex_oss.desktop_native_verifier import verify_desktop_native_ux
    from codex_oss.route_authority import build_route_authority

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mission_id = "desktop_raw"
        _write_required_artifacts(root, mission_id)
        transcript = root / "transcript.json"
        transcript.write_text(json.dumps({"messages": [
            *_identity_progress_messages(mission_id),
            {"text": "final", "phase": "final_answer"},
        ], "mission_id": mission_id, "consumer_kind": "codex_desktop_spawned", "transcript_kind": "codex_desktop_raw_export"}), encoding="utf-8")
        raw_route = build_route_authority(
            agent_name="oss_kimi_rapid",
            model_alias="ocg-kimi-k2.6",
            handoff_obj={"schema_version": 1},
            consumer_kind="codex_desktop_spawned",
        )
        report = verify_desktop_native_ux(
            transcript_path=str(transcript),
            mission_id=mission_id,
            project_root=tmp,
            route_authority=raw_route,
        )
    assert report["ok"] is False, report
    assert "desktop_route_authority_missing" in report["missing_evidence"], report
    _pass(name)


def assert_verifier_passes_with_desktop_mission_evidence():
    name = "verifier_passes_with_desktop_mission_evidence"
    from codex_oss.desktop_native_verifier import verify_desktop_native_ux
    from codex_oss.route_authority import build_route_authority

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mission_id = "desktop_mission"
        _write_required_artifacts(root, mission_id)
        transcript = root / "transcript.txt"
        transcript.write_text(json.dumps({"mission_id": mission_id, "consumer_kind": "codex_desktop_spawned", "transcript_kind": "codex_desktop_raw_export", "messages": [
            *_identity_progress_messages(mission_id, ["mission started", "evidence collected", "verification complete"]),
            {"text": "Final answer from runtime evidence.", "phase": "final_answer"},
        ]}), encoding="utf-8")
        route = build_route_authority(
            agent_name="oss_deepseek_investigator",
            model_alias="mission-a3-deepseek",
            handoff_obj={"schema_version": "oss_agent_mission.v1"},
            consumer_kind="codex_desktop_spawned",
        )
        report = verify_desktop_native_ux(
            transcript_path=str(transcript),
            mission_id=mission_id,
            project_root=tmp,
            route_authority=route,
        )
    assert report["ok"] is True, report
    assert report["desktop_gold_pass"] is True, report
    _pass(name)


def assert_verifier_requires_adoption_or_recovery_probes():
    name = "verifier_requires_adoption_or_recovery_probes"
    from codex_oss.desktop_native_verifier import verify_desktop_native_ux
    from codex_oss.route_authority import build_route_authority

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mission_id = "desktop_no_adoption"
        _write_required_artifacts(root, mission_id)
        (root / ".codex-oss" / "missions" / mission_id / "tool_call_adoption_probes.json").write_text(json.dumps({"probes": []}), encoding="utf-8")
        transcript = root / "transcript.json"
        transcript.write_text(json.dumps({"mission_id": mission_id, "consumer_kind": "codex_desktop_spawned", "transcript_kind": "codex_desktop_raw_export", "messages": [
            *_identity_progress_messages(mission_id),
            {"text": "final", "phase": "final_answer"},
        ]}), encoding="utf-8")
        route = build_route_authority(
            agent_name="oss_deepseek_investigator",
            model_alias="mission-a3-deepseek",
            handoff_obj={"schema_version": "oss_agent_mission.v1"},
            consumer_kind="codex_desktop_spawned",
        )
        report = verify_desktop_native_ux(
            transcript_path=str(transcript),
            mission_id=mission_id,
            project_root=tmp,
            route_authority=route,
        )
    assert report["ok"] is False, report
    assert "adoption_or_recovery_missing" in report["missing_evidence"], report
    _pass(name)


def assert_verifier_accepts_explicit_zero_pending_adoption_ledger():
    name = "verifier_accepts_explicit_zero_pending_adoption_ledger"
    from codex_oss.desktop_native_verifier import verify_desktop_native_ux
    from codex_oss.route_authority import build_route_authority

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mission_id = "desktop_zero_pending"
        _write_required_artifacts(root, mission_id)
        (root / ".codex-oss" / "missions" / mission_id / "tool_call_adoption_probes.json").write_text(json.dumps({
            "schema_version": "tool_call_adoption_probes.v1",
            "adoption_stats": {"total": 0, "adopted": 0, "not_adopted": 0, "recovery_used": 0},
            "probes": [],
        }), encoding="utf-8")
        transcript = root / "transcript.json"
        transcript.write_text(json.dumps({"mission_id": mission_id, "consumer_kind": "codex_desktop_spawned", "transcript_kind": "codex_desktop_raw_export", "messages": [
            *_identity_progress_messages(mission_id, ["start", "patch", "verify"]),
            {"text": "final", "phase": "final_answer"},
        ]}), encoding="utf-8")
        route = build_route_authority(
            agent_name="oss_deepseek_implementer",
            model_alias="mission-a5-deepseek",
            handoff_obj={"schema_version": "oss_agent_mission.v1"},
            consumer_kind="codex_desktop_spawned",
        )
        report = verify_desktop_native_ux(
            transcript_path=str(transcript),
            mission_id=mission_id,
            project_root=tmp,
            route_authority=route,
        )
    assert report["ok"] is True, report
    assert report["desktop_gold_pass"] is True, report
    _pass(name)


def assert_verifier_rejects_artifact_reconciled_candidate_as_desktop_gold():
    name = "verifier_rejects_artifact_reconciled_candidate_as_desktop_gold"
    from codex_oss.desktop_native_verifier import verify_desktop_native_ux
    from codex_oss.route_authority import build_route_authority

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mission_id = "desktop_reconstructed"
        _write_required_artifacts(root, mission_id)
        transcript = root / "transcript.json"
        transcript.write_text(json.dumps({
            "mission_id": mission_id,
            "consumer_kind": "codex_desktop_spawned",
            "transcript_kind": "artifact_reconciled_candidate_not_raw_desktop_export",
            "messages": [
                *_identity_progress_messages(mission_id, ["start", "patch", "verify"]),
                {"text": "final", "phase": "final_answer"},
            ],
        }), encoding="utf-8")
        route = build_route_authority(
            agent_name="oss_deepseek_implementer",
            model_alias="mission-a5-deepseek",
            handoff_obj={"schema_version": "oss_agent_mission.v1"},
            consumer_kind="codex_desktop_spawned",
        )
        report = verify_desktop_native_ux(
            transcript_path=str(transcript),
            mission_id=mission_id,
            project_root=tmp,
            route_authority=route,
        )
    assert report["ok"] is False, report
    assert report["desktop_gold_pass"] is False, report
    assert "desktop_transcript_provenance_missing" in report["missing_evidence"], report
    _pass(name)


def assert_verifier_rejects_generic_progress_without_event_identity():
    name = "verifier_rejects_generic_progress_without_event_identity"
    from codex_oss.desktop_native_verifier import verify_desktop_native_ux
    from codex_oss.route_authority import build_route_authority

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mission_id = "desktop_generic_progress"
        _write_required_artifacts(root, mission_id)
        transcript = root / "transcript.json"
        transcript.write_text(json.dumps({
            "mission_id": mission_id,
            "consumer_kind": "codex_desktop_spawned",
            "transcript_kind": "codex_desktop_raw_export",
            "messages": [*_generic_progress_messages(), {"text": "final", "phase": "final_answer"}],
        }), encoding="utf-8")
        route = build_route_authority(
            agent_name="oss_deepseek_investigator",
            model_alias="mission-a3-deepseek",
            handoff_obj={"schema_version": "oss_agent_mission.v1"},
            consumer_kind="codex_desktop_spawned",
        )
        report = verify_desktop_native_ux(
            transcript_path=str(transcript),
            mission_id=mission_id,
            project_root=tmp,
            route_authority=route,
        )
    assert report["ok"] is False, report
    assert "desktop_progress_event_identity_missing" in report["missing_evidence"], report
    _pass(name)


def main():
    for test in [
        assert_verifier_fails_without_transcript,
        assert_verifier_requires_desktop_route_authority,
        assert_verifier_passes_with_desktop_mission_evidence,
        assert_verifier_requires_adoption_or_recovery_probes,
        assert_verifier_accepts_explicit_zero_pending_adoption_ledger,
        assert_verifier_rejects_artifact_reconciled_candidate_as_desktop_gold,
        assert_verifier_rejects_generic_progress_without_event_identity,
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
