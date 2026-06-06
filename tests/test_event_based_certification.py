#!/usr/bin/env python3
"""Event-backed certification tests."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin" / "codex-oss"
sys.path.insert(0, str(ROOT))

from codex_oss.desktop_transcript_ingestor import ingest_desktop_transcript
from codex_oss.mission_event_log import MissionEventLog

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


def _run_cmd(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(BIN)] + args, text=True, capture_output=True)


def _admit(root: Path, run_id: str, *, mode: str = "managed_investigation", tier: str = "A3") -> MissionEventLog:
    log = MissionEventLog.for_project(root, run_id)
    log.append(
        "MissionAdmitted",
        {
            "task_spec": {"schema_version": "oss_agent_mission.v1", "mission_id": run_id, "mode": mode, "tier": tier},
            "risk_tier": "low",
            "allowed_tool_classes": ["read"],
            "write_allowed": tier in {"A4", "A5", "A6"},
            "model_alias": "mission-a3-deepseek",
            "route_class": mode,
        },
        source_kind="runtime",
        authority="runtime_authoritative",
    )
    log.append("MissionFinalized", {"status": "COMPLETE", "confidence": "MEDIUM", "summary_hash": "sha256:s", "final_text": "done"}, source_kind="runtime", authority="runtime_authoritative")
    return log


def _trusted_transcript(root: Path, run_id: str) -> Path:
    path = root / f"{run_id}_thread.json"
    messages = [{"id": f"m{i}", "phase": "commentary", "text": f"progress {i}", "event_id": f"evt_runtime_{i}"} for i in range(1, 4)]
    messages.append({"id": "m4", "phase": "final_answer", "text": "final"})
    path.write_text(json.dumps({
        "mission_id": run_id,
        "consumer_kind": "codex_desktop_spawned",
        "transcript_kind": "codex_desktop_raw_export",
        "thread_id": "agent_event",
        "spawned_agent_id": "agent_event",
        "messages": messages,
    }), encoding="utf-8")
    return path


def assert_event_claim_gate_rejects_runtime_only_desktop_gold() -> None:
    name = "event_claim_gate_rejects_runtime_only_desktop_gold"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _admit(root, "runtime_only")
        result = _run_cmd(["certify", "--project", tmp, "--target", "desktop-gold", "--run-id", "runtime_only", "--json"])
    assert result.returncode == 1, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["verdict"]["status"] == "UNCONFIRMED", payload
    assert "desktop_progress_before_final_missing" in payload["gates"]["event_claim_gate"]["missing_evidence"], payload
    _pass(name)


def assert_event_claim_gate_certifies_desktop_gold_from_events() -> None:
    name = "event_claim_gate_certifies_desktop_gold_from_events"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _admit(root, "desktop_event_ok")
        ingest_desktop_transcript(project_root=root, run_id="desktop_event_ok", mission_id="desktop_event_ok", transcript_path=_trusted_transcript(root, "desktop_event_ok"), expected_agent_id="agent_event")
        result = _run_cmd(["certify", "--project", tmp, "--target", "desktop-gold", "--run-id", "desktop_event_ok", "--json"])
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["verdict"]["status"] == "CERTIFIED", payload
    assert payload["run_record"]["desktop_observation"]["pre_final_progress_count"] == 3, payload
    _pass(name)


def assert_certify_requires_explicit_run_selector_for_claims() -> None:
    name = "certify_requires_explicit_run_selector_for_claims"
    with tempfile.TemporaryDirectory() as tmp:
        result = _run_cmd(["certify", "--project", tmp, "--target", "desktop-gold", "--json"])
    assert result.returncode == 1, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["gates"]["claim_selector"]["basis"] == "explicit_run_selector_required", payload
    _pass(name)


def assert_aggregate_parity_requires_manifest_and_reports_missing_lanes() -> None:
    name = "aggregate_parity_requires_manifest_and_reports_missing_lanes"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _admit(root, "manifest_desktop")
        ingest_desktop_transcript(project_root=root, run_id="manifest_desktop", mission_id="manifest_desktop", transcript_path=_trusted_transcript(root, "manifest_desktop"), expected_agent_id="agent_event")
        manifest = root / "run_manifest.json"
        manifest.write_text(json.dumps({
            "schema_version": "run_manifest.v1",
            "runs": [{"run_id": "manifest_desktop", "lanes": ["desktop_progress"]}],
        }), encoding="utf-8")
        result = _run_cmd(["certify", "--project", tmp, "--target", "oss-native-parity", "--run-manifest", str(manifest), "--json"])
    assert result.returncode == 1, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["verdict"]["status"] == "PARTIAL", payload
    assert payload["gates"]["desktop_progress"]["ok"] is True, payload
    assert payload["gates"]["tool_loop"]["ok"] is False, payload
    _pass(name)


def main() -> None:
    for test in [
        assert_event_claim_gate_rejects_runtime_only_desktop_gold,
        assert_event_claim_gate_certifies_desktop_gold_from_events,
        assert_certify_requires_explicit_run_selector_for_claims,
        assert_aggregate_parity_requires_manifest_and_reports_missing_lanes,
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
