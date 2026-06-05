#!/usr/bin/env python3
"""Tests for NativeLikeCertificationV1 fail-closed claim gates."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin" / "codex-oss"

sys.path.insert(0, str(ROOT))


PASSED = 0
FAILED = 0


def _pass(name: str) -> None:
    global PASSED
    PASSED += 1
    print(f"  PASS {name}")


def _fail(name: str, detail: str = "") -> None:
    global FAILED
    FAILED += 1
    print(f"  FAIL {name}: {detail}")


def _mission_json(mission_id: str) -> dict:
    return {
        "schema_version": "oss_agent_mission.v1",
        "mission_id": mission_id,
        "tier": "A3",
        "mode": "managed_investigation",
        "objective": "Inspect fixture evidence.",
        "write_allowed": False,
        "allowed_paths": ["README.md"],
        "allowed_roots": [],
        "allowed_tool_classes": ["read", "search"],
        "required_outputs": ["confidence", "caveats"],
    }


def _write_runtime_mission(root: Path, mission_id: str) -> Path:
    mission_dir = root / ".codex-oss" / "missions" / mission_id
    mission_dir.mkdir(parents=True, exist_ok=True)
    (mission_dir / "mission.json").write_text(json.dumps({"mission_id": mission_id}, indent=2), encoding="utf-8")
    (mission_dir / "mission_canonical.json").write_text(json.dumps(_mission_json(mission_id), indent=2), encoding="utf-8")
    (mission_dir / "report.json").write_text(json.dumps({
        "status": "COMPLETE",
        "report_source": "runtime_answer_graph",
        "closure_source": "runtime_answer_graph",
        "runtime_entitlement_reconciliation": {"decision": "accepted_complete"},
        "sufficiency": {"enough_evidence_to_report": True},
    }, indent=2), encoding="utf-8")
    (mission_dir / "canonical_read_evidence.json").write_text(json.dumps({
        "mission_id": mission_id,
        "status_entitlement": {"can_complete": True},
        "coverage_status": {"blocked_obligations": []},
        "missing_required_sources": [],
    }, indent=2), encoding="utf-8")
    events = {}
    for idx in range(1, 4):
        event_id = f"evt_{idx:04d}"
        events[event_id] = {
            "schema_version": "commentary_delivery.v1",
            "mission_id": mission_id,
            "event_id": event_id,
            "event_type": "tool_action_started",
            "states": {"stream_enqueued": True, "sse_emitted": True},
        }
    (mission_dir / "commentary_delivery.json").write_text(json.dumps({
        "schema_version": "commentary_delivery_tracker.v1",
        "mission_id": mission_id,
        "events": events,
    }, indent=2), encoding="utf-8")
    return mission_dir


def _add_desktop_artifacts(root: Path, mission_id: str) -> tuple[Path, Path]:
    from codex_oss.route_authority import build_route_authority

    mission_dir = root / ".codex-oss" / "missions" / mission_id
    (mission_dir / "visible_commentary.jsonl").write_text('{"event_type":"mission_started","message":"start"}\n', encoding="utf-8")
    (mission_dir / "summary.md").write_text("summary", encoding="utf-8")
    (mission_dir / "tool_call_adoption_probes.json").write_text(json.dumps({
        "schema_version": "tool_call_adoption_probes.v1",
        "probes": [{"call_id": "call_1", "consumer_adopted": False, "recovery_used": True}],
    }, indent=2), encoding="utf-8")
    (root / ".codex-oss" / "desktop_pre_final_text_probe_result.json").write_text(json.dumps({
        "schema_version": "desktop_pre_final_text_probe_result.v1",
        "probe_status": "pass",
        "desktop_render_surface": {"probe_required": True, "probe_status": "pass"},
    }, indent=2), encoding="utf-8")
    messages = []
    for idx, verb in enumerate(["mission started", "evidence collected", "verification complete"], start=1):
        event_id = f"evt_{idx:04d}"
        messages.append({
            "text": f"[OSS progress mission={mission_id} event={event_id} seq={idx} type=tool_action_started] {verb}",
            "phase": "commentary",
            "event_id": event_id,
            "event_type": "tool_action_started",
            "seq": idx,
        })
    messages.append({"text": "Final answer from runtime evidence.", "phase": "final_answer"})
    transcript = root / "desktop_transcript.json"
    transcript.write_text(json.dumps({
        "mission_id": mission_id,
        "consumer_kind": "codex_desktop_spawned",
        "transcript_kind": "codex_desktop_raw_export",
        "thread_id": f"thread_{mission_id}",
        "spawned_agent_id": f"agent_{mission_id}",
        "messages": messages,
    }, indent=2), encoding="utf-8")
    route = root / "route_authority.json"
    route.write_text(json.dumps(build_route_authority(
        agent_name="oss_deepseek_investigator",
        model_alias="mission-a3-deepseek",
        handoff_obj={"schema_version": "oss_agent_mission.v1"},
        consumer_kind="codex_desktop_spawned",
    ), indent=2), encoding="utf-8")
    return transcript, route


def _add_desktop_artifacts_in_mission(root: Path, mission_id: str, *, model_alias: str = "mission-a3-deepseek", agent_name: str = "oss_deepseek_investigator") -> None:
    from codex_oss.route_authority import build_route_authority

    transcript, _ = _add_desktop_artifacts(root, mission_id)
    mission_dir = root / ".codex-oss" / "missions" / mission_id
    (mission_dir / "desktop_thread_transcript.json").write_text(transcript.read_text(encoding="utf-8"), encoding="utf-8")
    (mission_dir / "route_authority.json").write_text(json.dumps(build_route_authority(
        agent_name=agent_name,
        model_alias=model_alias,
        handoff_obj={"schema_version": "oss_agent_mission.v1"},
        consumer_kind="codex_desktop_spawned",
    ), indent=2), encoding="utf-8")


def _write_tool_loop_mission(root: Path, mission_id: str, *, recovered: bool = False) -> None:
    _write_runtime_mission(root, mission_id)
    _add_desktop_artifacts_in_mission(root, mission_id, model_alias="mission-a3-kimi", agent_name="oss_kimi_investigator")
    mission_dir = root / ".codex-oss" / "missions" / mission_id
    status = "RECOVERED" if recovered else "PASS"
    (mission_dir / "adoption_or_recovery.json").write_text(json.dumps({
        "schema_version": "adoption_or_recovery.v1",
        "mission_id": mission_id,
        "status": status,
        "reason": "recovered_by_runtime" if recovered else "adoption_observed",
        "pending_tool_calls_emitted": 1,
        "runtime_recovery_used": recovered,
        "artifacts": ["tool_call_adoption_probes.json"],
    }, indent=2), encoding="utf-8")
    (mission_dir / "tool_call_adoption_probes.json").write_text(json.dumps({
        "schema_version": "tool_call_adoption_probes.v1",
        "adoption_stats": {"total": 1, "adopted": 0 if recovered else 1, "not_adopted": 1 if recovered else 0, "recovery_used": 1 if recovered else 0},
        "probes": [{
            "call_id": "call_1",
            "consumer_adopted": not recovered,
            "recovery_used": recovered,
            "tool_name": "read_file",
        }],
    }, indent=2), encoding="utf-8")
    if recovered:
        (mission_dir / "recovery_proof.json").write_text(json.dumps({
            "schema_version": "recovery_proof.v1",
            "mission_id": mission_id,
            "status": "RECOVERED",
            "injected_failure": True,
            "reason": "tool_call_recovered_by_runtime",
        }, indent=2), encoding="utf-8")


def _write_implementation_mission(root: Path, mission_id: str) -> None:
    mission_dir = _write_runtime_mission(root, mission_id)
    canonical = _mission_json(mission_id)
    canonical.update({
        "tier": "A5",
        "mode": "bounded_implementation",
        "write_allowed": True,
        "owned_paths": ["tests/owned.txt"],
        "allowed_paths": ["tests/owned.txt"],
    })
    (mission_dir / "mission_canonical.json").write_text(json.dumps(canonical, indent=2), encoding="utf-8")
    (mission_dir / "report.json").write_text(json.dumps({
        "mission_id": mission_id,
        "implementation_report_version": "1.0",
        "status": "VERIFIED",
        "verification": [{"command": ["true"], "exit_code": 0}],
        "rollback": {"available": True, "artifact": "rollback.diff"},
        "implementation_authority": {"patch_authority": "bridge_runtime"},
        "runtime_built_diff": True,
        "patch_artifact": "patch.diff",
        "main_workspace_mutated": False,
        "runtime_entitlement_reconciliation": {"decision": "accepted_complete"},
    }, indent=2), encoding="utf-8")
    _add_desktop_artifacts_in_mission(root, mission_id, model_alias="mission-a5-kimi", agent_name="oss_deepseek_implementer")


def _write_read_thread_export(path: Path, mission_id: str, agent_id: str) -> None:
    items = [{"type": "userMessage", "id": "item-1", "content": [{"type": "text", "text": "spawned mission"}]}]
    for idx, label in enumerate(["started", "validated", "verified"], start=1):
        event_id = f"evt_{idx:04d}"
        items.append({
            "type": "agentMessage",
            "id": f"item-{idx + 1}",
            "phase": "commentary",
            "text": f"[OSS progress mission={mission_id} event={event_id} seq={idx} type=tool_action_started] {label}",
        })
    items.append({
        "type": "agentMessage",
        "id": "item-5",
        "phase": "final_answer",
        "text": "OSS_IMPLEMENTATION_REPORT_BEGIN\nStatus: VERIFIED\nOSS_IMPLEMENTATION_REPORT_END",
    })
    path.write_text(json.dumps({
        "schemaVersion": 1,
        "thread": {"id": agent_id, "title": "Spawned child"},
        "turns": [{"id": "turn-1", "items": items}],
    }, indent=2), encoding="utf-8")


def _run_cmd(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(BIN)] + args,
        cwd=str(cwd or ROOT),
        text=True,
        capture_output=True,
    )


def assert_validate_mission_handoff_accepts_canonical_wrapper() -> None:
    name = "validate_mission_handoff_accepts_canonical_wrapper"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "handoff.md"
        path.write_text(
            "<OSS_HANDOFF_JSON>\n"
            + json.dumps(_mission_json("native_handoff_ok"))
            + "\n</OSS_HANDOFF_JSON>\n",
            encoding="utf-8",
        )
        result = _run_cmd(["validate-mission-handoff", str(path)])
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["valid"] is True, payload
    assert payload["active_blocks"] == 1, payload
    assert payload["mission_id"] == "native_handoff_ok", payload
    _pass(name)


def assert_validate_mission_handoff_rejects_legacy_raw() -> None:
    name = "validate_mission_handoff_rejects_legacy_raw"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "legacy.md"
        path.write_text('OSS_HANDOFF_JSON:\n{"schema_version":1,"role":"scout","goal":"legacy raw"}\n', encoding="utf-8")
        result = _run_cmd(["validate-mission-handoff", str(path)])
    assert result.returncode == 1, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["valid"] is False, payload
    assert payload["active_blocks"] == 0, payload
    _pass(name)


def assert_validate_mission_handoff_ignores_placeholder_examples() -> None:
    name = "validate_mission_handoff_ignores_placeholder_examples"
    template = _mission_json("<stable mission id>")
    template["objective"] = "<concrete sub-task>"
    template["allowed_paths"] = ["<files or dirs>"]
    active = _mission_json("native_handoff_with_example")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "mixed.md"
        path.write_text(
            "<OSS_HANDOFF_JSON>\n"
            + json.dumps(template)
            + "\n</OSS_HANDOFF_JSON>\n\n"
            "<OSS_HANDOFF_JSON>\n"
            + json.dumps(active)
            + "\n</OSS_HANDOFF_JSON>\n",
            encoding="utf-8",
        )
        result = _run_cmd(["validate-mission-handoff", str(path)])
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["valid"] is True, payload
    assert payload["active_blocks"] == 1, payload
    assert payload["ignored_examples"] == 1, payload
    assert payload["mission_id"] == "native_handoff_with_example", payload
    _pass(name)


def assert_certify_native_like_runtime_only_blocks_desktop_claim() -> None:
    name = "certify_native_like_runtime_only_blocks_desktop_claim"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mission_id = "native_runtime_only"
        _write_runtime_mission(root, mission_id)
        result = _run_cmd([
            "certify",
            "--project", tmp,
            "--target", "native-like",
            "--mission-id", mission_id,
            "--json",
        ])
    assert result.returncode == 1, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["verdict"]["status"] == "RUNTIME_ONLY", payload
    assert "runtime-governed OSS subagent completed safely" in payload["allowed_claims"], payload
    assert "Desktop-native live-progress OSS subagent" in payload["disallowed_claims"], payload
    assert payload["gates"]["desktop_observation"]["ok"] is False, payload
    _pass(name)


def assert_certify_native_like_accepts_consumer_observation_witness() -> None:
    name = "certify_native_like_accepts_consumer_observation_witness"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mission_id = "native_desktop_gold"
        _write_runtime_mission(root, mission_id)
        transcript, route = _add_desktop_artifacts(root, mission_id)
        result = _run_cmd([
            "certify",
            "--project", tmp,
            "--target", "native-like",
            "--mission-id", mission_id,
            "--transcript", str(transcript),
            "--route-authority", str(route),
            "--json",
        ])
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["verdict"]["status"] == "CERTIFIED", payload
    assert "Desktop-native live-progress OSS subagent" in payload["allowed_claims"], payload
    assert payload["gates"]["desktop_observation"]["ok"] is True, payload
    _pass(name)


def assert_certify_desktop_gold_accepts_mission_bound_transcript_without_manual_handoff() -> None:
    name = "certify_desktop_gold_accepts_mission_bound_transcript_without_manual_handoff"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mission_id = "desktop_gold_ok"
        _write_runtime_mission(root, mission_id)
        transcript, route = _add_desktop_artifacts(root, mission_id)
        (root / ".codex-oss" / "desktop_pre_final_text_probe_result.json").write_text(json.dumps({
            "schema_version": "desktop_pre_final_text_probe_result.v1",
            "probe_status": "setup_failed",
            "desktop_render_surface": {"probe_required": True, "probe_status": "unknown"},
        }, indent=2), encoding="utf-8")
        result = _run_cmd([
            "certify",
            "--project", tmp,
            "--target", "desktop-gold",
            "--mission-id", mission_id,
            "--transcript", str(transcript),
            "--route-authority", str(route),
            "--json",
        ])
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "PASS", payload
    assert payload["gates"]["handoff_authority"]["ok"] is True, payload
    assert payload["gates"]["runtime_evidence"]["ok"] is True, payload
    assert payload["gates"]["adoption_or_recovery"]["status"] == "NOT_APPLICABLE", payload
    assert payload["gates"]["render_surface_proof"]["render_surface_proof"]["source"] == "mission_bound_transcript_witness", payload
    _pass(name)


def assert_certify_desktop_gold_fails_without_transcript_when_probe_unknown() -> None:
    name = "certify_desktop_gold_fails_without_transcript_when_probe_unknown"
    from codex_oss.route_authority import build_route_authority

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mission_id = "desktop_gold_no_witness"
        _write_runtime_mission(root, mission_id)
        mission_dir = root / ".codex-oss" / "missions" / mission_id
        (mission_dir / "route_authority.json").write_text(json.dumps(build_route_authority(
            agent_name="oss_deepseek_investigator",
            model_alias="mission-a3-deepseek",
            handoff_obj={"schema_version": "oss_agent_mission.v1"},
            consumer_kind="codex_desktop_spawned",
        ), indent=2), encoding="utf-8")
        (root / ".codex-oss" / "desktop_pre_final_text_probe_result.json").write_text(json.dumps({
            "schema_version": "desktop_pre_final_text_probe_result.v1",
            "probe_status": "setup_failed",
            "desktop_render_surface": {"probe_required": True, "probe_status": "unknown"},
        }, indent=2), encoding="utf-8")
        result = _run_cmd([
            "certify",
            "--project", tmp,
            "--target", "desktop-gold",
            "--mission-id", mission_id,
            "--json",
        ])
    assert result.returncode == 1, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "FAIL", payload
    assert payload["gates"]["consumer_observation"]["ok"] is False, payload
    assert payload["gates"]["render_surface_proof"]["ok"] is False, payload
    _pass(name)


def assert_patch_authority_classifies_a5_statuses() -> None:
    name = "patch_authority_classifies_a5_statuses"
    from codex_oss.native_certification import classify_implementation_status, evaluate_patch_authority

    verified = {
        "implementation_report_version": "1.0",
        "status": "VERIFIED",
        "verification": [{"command": ["true"], "exit_code": 0}],
        "rollback": {"available": True, "artifact": "rollback.diff"},
        "implementation_authority": {"patch_authority": "bridge_runtime"},
        "runtime_built_diff": True,
        "patch_artifact": "patch.diff",
    }
    assert classify_implementation_status(verified)["status"] == "verified", verified
    no_rollback = dict(verified)
    no_rollback["rollback"] = {"available": False, "artifact": ""}
    gate = evaluate_patch_authority(report=no_rollback, mission_json={"tier": "A5"})
    assert gate["ok"] is False, gate
    assert "verified_without_rollback_artifact" in gate["reasons"], gate
    _pass(name)


def assert_certify_oss_native_parity_partial_for_desktop_only() -> None:
    name = "certify_oss_native_parity_partial_for_desktop_only"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mission_id = "parity_desktop_only"
        _write_runtime_mission(root, mission_id)
        _add_desktop_artifacts_in_mission(root, mission_id)
        result = _run_cmd([
            "certify",
            "--project", tmp,
            "--target", "oss-native-parity",
            "--json",
        ])
    assert result.returncode == 1, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["verdict"]["status"] == "PARTIAL", payload
    assert payload["gates"]["desktop_progress_parity"]["ok"] is True, payload
    assert payload["gates"]["tool_loop_parity"]["ok"] is False, payload
    assert "Arbitrary OSS native tool-loop parity." in payload["disallowed_claims"], payload
    _pass(name)


def assert_a5_verified_without_transcript_stays_runtime_only() -> None:
    name = "a5_verified_without_transcript_stays_runtime_only"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mission_id = "parity_a5_no_transcript"
        _write_implementation_mission(root, mission_id)
        mission_dir = root / ".codex-oss" / "missions" / mission_id
        (mission_dir / "desktop_thread_transcript.json").unlink()
        result = _run_cmd([
            "certify",
            "--project", tmp,
            "--target", "oss-native-parity",
            "--mission-id", mission_id,
            "--json",
        ])
    assert result.returncode == 1, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "FAIL", payload
    assert payload["runtime_execution"] == "PASS", payload
    assert payload["patch_authority"] == "PASS", payload
    assert payload["implementation_status"] == "VERIFIED", payload
    assert payload["desktop_observation"] == "FAIL", payload
    assert payload["desktop_gold"] is False, payload
    assert payload["basis"] == "desktop_transcript_missing", payload
    assert payload["next_required_artifact"] == "spawned_transcript_authority.json", payload
    assert "A5 bounded implementation verified under runtime authority in isolated temporary project copy" in payload["allowed_claims"], payload
    assert payload["mission_reports_sample"][0]["implementation"]["patch_authority_ok"] is True, payload
    assert payload["mission_reports_sample"][0]["implementation"]["ok"] is False, payload
    _pass(name)


def assert_a5_verified_with_raw_child_transcript_passes_implementation_lane() -> None:
    name = "a5_verified_with_raw_child_transcript_passes_implementation_lane"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mission_id = "parity_a5_with_transcript"
        _write_implementation_mission(root, mission_id)
        result = _run_cmd([
            "certify",
            "--project", tmp,
            "--target", "oss-native-parity",
            "--mission-id", mission_id,
            "--json",
        ])
    assert result.returncode == 1, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["desktop_observation"] == "PASS", payload
    assert payload["desktop_gold"] is True, payload
    assert payload["basis"] == "raw_desktop_child_transcript", payload
    report = payload["mission_reports_sample"][0]
    assert report["spawned_transcript_authority"]["ok"] is True, payload
    assert report["implementation"]["ok"] is True, payload
    assert payload["gates"]["implementation_parity"]["ok"] is True, payload
    assert payload["gates"]["tool_loop_parity"]["ok"] is False, payload
    assert payload["gates"]["recovery_parity"]["ok"] is False, payload
    _pass(name)


def assert_spawn_lifecycle_finalizes_from_agent_id_thread_export() -> None:
    name = "spawn_lifecycle_finalizes_from_agent_id_thread_export"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mission_id = "parity_a5_lifecycle"
        agent_id = "agent_lifecycle_123"
        _write_implementation_mission(root, mission_id)
        mission_dir = root / ".codex-oss" / "missions" / mission_id
        for artifact in [
            "desktop_thread_transcript.json",
            "route_authority.json",
            "spawned_transcript_authority.json",
            "consumer_observation_witness.json",
            "commentary_delivery_reconciliation.json",
        ]:
            path = mission_dir / artifact
            if path.exists():
                path.unlink()
        read_export = root / "read_thread_export.json"
        _write_read_thread_export(read_export, mission_id, agent_id)
        result = _run_cmd([
            "spawn-lifecycle",
            "finalize",
            "--project", tmp,
            "--mission-id", mission_id,
            "--agent-id", agent_id,
            "--agent-role", "oss_deepseek_implementer",
            "--thread-export", str(read_export),
            "--json",
        ])
        assert result.returncode == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload["ok"] is True, payload
        assert payload["canonical_transcript_identity"] == "agent_id", payload
        assert payload["list_threads_used"] is False, payload
        assert payload["desktop_observation"] == "PASS", payload
        assert payload["desktop_gold"] is True, payload
        receipt = json.loads((mission_dir / "spawn_receipt.json").read_text(encoding="utf-8"))
        transcript = json.loads((mission_dir / "desktop_thread_transcript.json").read_text(encoding="utf-8"))
        route = json.loads((mission_dir / "route_authority.json").read_text(encoding="utf-8"))
        cert = json.loads((mission_dir / "certification_result.json").read_text(encoding="utf-8"))
        assert receipt["agent_id"] == agent_id, receipt
        assert receipt["read_method"] == "codex_app.read_thread(threadId=agent_id)", receipt
        assert transcript["thread_id"] == agent_id, transcript
        assert transcript["spawned_agent_id"] == agent_id, transcript
        assert route["desktop_native_claim_allowed"] is True, route
        assert cert["desktop_observation"] == "PASS", cert
        assert cert["gates"]["implementation_parity"]["ok"] is True, cert
    _pass(name)


def assert_spawn_lifecycle_missing_read_thread_export_is_actionable() -> None:
    name = "spawn_lifecycle_missing_read_thread_export_is_actionable"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mission_id = "parity_a5_lifecycle_missing_export"
        agent_id = "agent_missing_export"
        _write_implementation_mission(root, mission_id)
        mission_dir = root / ".codex-oss" / "missions" / mission_id
        for artifact in ["desktop_thread_transcript.json", "route_authority.json", "spawned_transcript_authority.json"]:
            path = mission_dir / artifact
            if path.exists():
                path.unlink()
        result = _run_cmd([
            "spawn-lifecycle",
            "finalize",
            "--project", tmp,
            "--mission-id", mission_id,
            "--agent-id", agent_id,
            "--agent-role", "oss_deepseek_implementer",
            "--json",
        ])
        assert result.returncode == 1, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload["ok"] is False, payload
        assert payload["capture_failure_path"], payload
        failure = json.loads((mission_dir / "capture_failure.json").read_text(encoding="utf-8"))
        cert = json.loads((mission_dir / "certification_result.json").read_text(encoding="utf-8"))
        assert failure["reason"] == "read_thread_export_missing", failure
        assert failure["agent_id"] == agent_id, failure
        assert failure["attempted_read_method"] == "codex_app.read_thread(threadId=agent_id)", failure
        assert failure["list_threads_used"] is False, failure
        assert "read_thread_export_missing" in cert["mission_reports_sample"][0]["desktop_gold"]["basis"], cert
    _pass(name)


def assert_spawn_lifecycle_requires_spawn_agent_id() -> None:
    name = "spawn_lifecycle_requires_spawn_agent_id"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mission_id = "parity_lifecycle_no_agent"
        _write_runtime_mission(root, mission_id)
        result = _run_cmd([
            "spawn-lifecycle",
            "finalize",
            "--project", tmp,
            "--mission-id", mission_id,
            "--agent-role", "oss_deepseek_investigator",
            "--json",
        ])
    assert result.returncode == 1, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is False, payload
    assert payload["reasons"] == ["spawn_receipt_agent_id_missing"], payload
    _pass(name)


def assert_tool_loop_proof_record_recovered_turns_gates_green() -> None:
    name = "tool_loop_proof_record_recovered_turns_gates_green"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mission_id = "parity_tool_loop_recorded"
        _write_runtime_mission(root, mission_id)
        _add_desktop_artifacts_in_mission(root, mission_id, model_alias="mission-a3-deepseek", agent_name="oss_deepseek_investigator")
        result = _run_cmd([
            "tool-loop-proof",
            "record",
            "--project", tmp,
            "--mission-id", mission_id,
            "--call-id", "call_read_1",
            "--tool-name", "read_file",
            "--status", "recovered",
            "--evidence-source", "desktop_thread_transcript.json",
            "--json",
        ])
        assert result.returncode == 0, result.stdout + result.stderr
        proof = json.loads(result.stdout)
        assert proof["ok"] is True, proof
        cert_result = _run_cmd([
            "certify",
            "--project", tmp,
            "--target", "oss-native-parity",
            "--mission-id", mission_id,
            "--json",
        ])
        assert cert_result.returncode == 1, cert_result.stdout + cert_result.stderr
        payload = json.loads(cert_result.stdout)
        report = payload["mission_reports_sample"][0]
        assert report["desktop_gold"]["ok"] is True, payload
        assert report["tool_loop"]["ok"] is True, payload
        assert report["tool_loop"]["status"] == "RECOVERED", payload
        assert report["recovery"]["ok"] is True, payload
        assert report["recovery"]["recovery_status"] == "RECOVERED", payload
        assert payload["gates"]["tool_loop_parity"]["ok"] is True, payload
    assert payload["gates"]["recovery_parity"]["ok"] is True, payload
    _pass(name)


def assert_raw_direct_recovered_tool_loop_stays_research_only() -> None:
    name = "raw_direct_recovered_tool_loop_stays_research_only"
    from codex_oss.route_authority import build_route_authority

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mission_id = "raw_pending_read_recovered"
        _write_runtime_mission(root, mission_id)
        _add_desktop_artifacts_in_mission(root, mission_id, model_alias="mission-a3-deepseek", agent_name="oss_deepseek_investigator")
        mission_dir = root / ".codex-oss" / "missions" / mission_id
        (mission_dir / "mission_canonical.json").write_text(json.dumps({
            **_mission_json(mission_id),
            "tier": "RAW",
            "mode": "raw_direct_pending_read_recovery",
        }, indent=2), encoding="utf-8")
        (mission_dir / "route_authority.json").write_text(json.dumps(build_route_authority(
            agent_name="oss_kimi_rapid",
            model_alias="ocg-kimi-k2.6",
            handoff_obj={"schema_version": "oss_agent_mission.v1"},
            consumer_kind="codex_desktop_spawned",
        ), indent=2), encoding="utf-8")
        result = _run_cmd([
            "tool-loop-proof",
            "record",
            "--project", tmp,
            "--mission-id", mission_id,
            "--call-id", "pending_read_raw",
            "--tool-name", "bridge_server_side_read",
            "--status", "recovered",
            "--evidence-source", "codex_app.read_thread:raw-child",
            "--json",
        ])
        assert result.returncode == 0, result.stdout + result.stderr
        cert_result = _run_cmd([
            "certify",
            "--project", tmp,
            "--target", "oss-native-parity",
            "--mission-id", mission_id,
            "--json",
        ])
        assert cert_result.returncode == 1, cert_result.stdout + cert_result.stderr
        payload = json.loads(cert_result.stdout)
        report = payload["mission_reports_sample"][0]
        assert report["spawned_transcript_authority"]["ok"] is True, payload
        assert report["tool_loop"]["status"] == "RECOVERED", payload
        assert report["tool_loop"]["pending_tool_calls_emitted"] == 1, payload
        assert report["desktop_gold"]["ok"] is False, payload
        assert "desktop_route_authority_missing" in report["desktop_gold"]["basis"], payload
        assert report["tool_loop"]["ok"] is False, payload
        assert payload["gates"]["tool_loop_parity"]["ok"] is False, payload
    _pass(name)


def assert_tool_loop_and_recovery_require_desktop_witness() -> None:
    name = "tool_loop_and_recovery_require_desktop_witness"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mission_id = "parity_recovery_no_transcript"
        _write_tool_loop_mission(root, mission_id, recovered=True)
        mission_dir = root / ".codex-oss" / "missions" / mission_id
        (mission_dir / "desktop_thread_transcript.json").unlink()
        result = _run_cmd([
            "certify",
            "--project", tmp,
            "--target", "oss-native-parity",
            "--mission-id", mission_id,
            "--json",
        ])
    assert result.returncode == 1, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    report = payload["mission_reports_sample"][0]
    assert report["tool_loop"]["ok"] is False, payload
    assert report["tool_loop"]["status"] == "RECOVERED", payload
    assert report["recovery"]["recovery_artifact_ok"] is True, payload
    assert report["recovery"]["ok"] is False, payload
    assert report["desktop_gold"]["basis"] == "desktop_transcript_missing", payload
    _pass(name)


def assert_certify_oss_native_parity_full_matrix_passes() -> None:
    name = "certify_oss_native_parity_full_matrix_passes"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_runtime_mission(root, "parity_read")
        _add_desktop_artifacts_in_mission(root, "parity_read", model_alias="mission-a3-deepseek")
        _write_tool_loop_mission(root, "parity_tool_recovered", recovered=True)
        _write_implementation_mission(root, "parity_impl")
        result = _run_cmd([
            "certify",
            "--project", tmp,
            "--target", "oss-native-parity",
            "--json",
        ])
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["verdict"]["status"] == "CERTIFIED", payload
    for gate in payload["gates"].values():
        assert gate["ok"] is True, payload
    assert "Supported runtime-controlled MissionV1 OSS subagents behave native-like across the covered parity matrix." in payload["allowed_claims"], payload
    _pass(name)


def main() -> None:
    for test in [
        assert_validate_mission_handoff_accepts_canonical_wrapper,
        assert_validate_mission_handoff_rejects_legacy_raw,
        assert_validate_mission_handoff_ignores_placeholder_examples,
        assert_certify_native_like_runtime_only_blocks_desktop_claim,
        assert_certify_native_like_accepts_consumer_observation_witness,
        assert_certify_desktop_gold_accepts_mission_bound_transcript_without_manual_handoff,
        assert_certify_desktop_gold_fails_without_transcript_when_probe_unknown,
        assert_patch_authority_classifies_a5_statuses,
        assert_certify_oss_native_parity_partial_for_desktop_only,
        assert_a5_verified_without_transcript_stays_runtime_only,
        assert_a5_verified_with_raw_child_transcript_passes_implementation_lane,
        assert_spawn_lifecycle_finalizes_from_agent_id_thread_export,
        assert_spawn_lifecycle_missing_read_thread_export_is_actionable,
        assert_spawn_lifecycle_requires_spawn_agent_id,
        assert_tool_loop_proof_record_recovered_turns_gates_green,
        assert_raw_direct_recovered_tool_loop_stays_research_only,
        assert_tool_loop_and_recovery_require_desktop_witness,
        assert_certify_oss_native_parity_full_matrix_passes,
    ]:
        try:
            test()
        except Exception as exc:
            _fail(test.__name__, str(exc))
    print(f"\nPassed: {PASSED}, Failed: {FAILED}")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    main()
