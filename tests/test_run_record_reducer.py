#!/usr/bin/env python3
"""RunRecord reducer and projection tests."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codex_oss.desktop_transcript_ingestor import ingest_desktop_transcript
from codex_oss.mission_event_log import MissionEventLog
from codex_oss.projections import check_projection_drift, write_run_record_projections
from codex_oss.run_record import build_run_record

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


def _write_base_run(root: Path, run_id: str = "run_record_ok") -> MissionEventLog:
    log = MissionEventLog.for_project(root, run_id)
    log.append(
        "MissionAdmitted",
        {
            "task_spec": {"schema_version": "oss_agent_mission.v1", "mission_id": run_id, "mode": "bounded_implementation", "tier": "A5"},
            "risk_tier": "low",
            "allowed_tool_classes": ["read"],
            "write_allowed": True,
            "model_alias": "mission-a5-deepseek",
            "route_class": "bounded_implementation",
        },
        source_kind="runtime",
        authority="runtime_authoritative",
    )
    return log


def _write_transcript(path: Path, mission_id: str) -> None:
    messages = [{"id": f"m{i}", "phase": "commentary", "text": f"progress {i}", "event_id": f"runtime_{i}"} for i in range(1, 4)]
    messages.append({"id": "m4", "phase": "final_answer", "text": "done"})
    path.write_text(json.dumps({
        "mission_id": mission_id,
        "consumer_kind": "codex_desktop_spawned",
        "transcript_kind": "codex_desktop_raw_export",
        "thread_id": "agent_1",
        "spawned_agent_id": "agent_1",
        "messages": messages,
    }, indent=2), encoding="utf-8")


def assert_run_record_reduces_summary_without_projection_files() -> None:
    name = "run_record_reduces_summary_without_projection_files"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        log = _write_base_run(root)
        transcript = root / "thread.json"
        _write_transcript(transcript, "run_record_ok")
        ingest_desktop_transcript(project_root=root, run_id="run_record_ok", mission_id="run_record_ok", transcript_path=transcript, expected_agent_id="agent_1")
        log.append("ToolCallEmitted", {"call_id": "call_1", "tool_name": "read_file", "arguments_hash": "sha256:x", "tool_class": "read", "response_id": "resp_1"}, source_kind="runtime", authority="runtime_authoritative")
        log.append("DesktopToolCallResolved", {"call_id": "call_1", "output_hash": "sha256:y", "consumer_kind": "codex_desktop_spawned", "adopted": True}, source_kind="desktop_app", authority="desktop_observed")
        log.append("PatchIntentProposed", {"owned_paths": ["src/a.py"], "rationale": "fix", "model_alias": "mission-a5-deepseek", "risk_tier": "low"}, source_kind="model", authority="model_narrative")
        log.append("PatchAppliedByRuntime", {"owned_paths": ["src/a.py"], "patch_hash": "sha256:p", "apply_status": "success"}, source_kind="runtime", authority="runtime_authoritative")
        log.append("VerificationRan", {"command": ["python3", "-m", "py_compile", "src/a.py"], "exit_code": 0, "result": "pass", "duration_ms": 1}, source_kind="runtime", authority="runtime_authoritative")
        record = build_run_record(log.read_events(verify=True)).to_dict()
    assert record["desktop_observation"]["ok"] is True, record
    assert record["tool_calls"]["all_resolved"] is True, record
    assert record["implementation"]["ok"] is True, record
    _pass(name)


def assert_projection_writer_marks_source_run_record_hash_and_drift() -> None:
    name = "projection_writer_marks_source_run_record_hash_and_drift"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_base_run(root, "projection_run")
        result = write_run_record_projections(root, "projection_run")
        summary_path = Path(result["paths"]["mission_summary"])
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        assert summary["compatibility_projection"] is True, summary
        assert summary["source_run_record_hash"] == result["source_run_record_hash"], result
        summary["source_run_record_hash"] = "sha256:edited"
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        drift = check_projection_drift(root, "projection_run")
    assert drift["ok"] is False, drift
    assert drift["drifted_paths"], drift
    _pass(name)


def main() -> None:
    for test in [
        assert_run_record_reduces_summary_without_projection_files,
        assert_projection_writer_marks_source_run_record_hash_and_drift,
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
