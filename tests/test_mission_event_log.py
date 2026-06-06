#!/usr/bin/env python3
"""MissionEventLog integrity tests."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codex_oss.mission_event_log import MissionEventLog
from codex_oss.mission_events import MissionEventError

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


def _admit(log: MissionEventLog, *, event_id: str = "admit") -> dict:
    return log.append(
        "MissionAdmitted",
        {
            "task_spec": {"mission_id": log.mission_id, "api_key": "sk-this-would-be-secret"},
            "risk_tier": "low",
            "allowed_tool_classes": ["read"],
            "write_allowed": False,
            "model_alias": "mission-a3-deepseek",
            "route_class": "managed_investigation",
        },
        source_kind="runtime",
        authority="runtime_authoritative",
        event_id=event_id,
        created_at="2026-06-05T00:00:00+00:00",
    )


def assert_event_log_appends_mission_admitted_with_hash_chain() -> None:
    name = "event_log_appends_mission_admitted_with_hash_chain"
    with tempfile.TemporaryDirectory() as tmp:
        log = MissionEventLog.for_project(tmp, "run_1")
        first = _admit(log)
        second = log.append(
            "RuntimeProgressEmitted",
            {"progress_type": "status", "text": "working", "visible": True},
            source_kind="runtime",
            authority="runtime_authoritative",
            created_at="2026-06-05T00:00:01+00:00",
        )
        events = log.read_events(verify=True)
    assert first["seq"] == 1, first
    assert second["prev_hash"] == first["event_hash"], second
    assert len(events) == 2, events
    assert events[0]["payload"]["task_spec"]["api_key"] == "[REDACTED]", events
    _pass(name)


def assert_event_log_rejects_wrong_prev_hash() -> None:
    name = "event_log_rejects_wrong_prev_hash"
    with tempfile.TemporaryDirectory() as tmp:
        log = MissionEventLog.for_project(tmp, "run_2")
        _admit(log)
        try:
            log.append(
                "RuntimeProgressEmitted",
                {"progress_type": "status", "text": "working", "visible": True},
                source_kind="runtime",
                authority="runtime_authoritative",
                expected_prev_hash="sha256:not-the-prev-hash",
            )
        except MissionEventError as exc:
            assert str(exc) == "prev_hash_mismatch", exc
        else:
            raise AssertionError("append unexpectedly accepted wrong prev_hash")
        assert len(log.read_events(verify=True)) == 1
    _pass(name)


def assert_event_log_verifier_rejects_tampered_payload() -> None:
    name = "event_log_verifier_rejects_tampered_payload"
    with tempfile.TemporaryDirectory() as tmp:
        log = MissionEventLog.for_project(tmp, "run_3")
        _admit(log)
        path = Path(tmp) / ".codex-oss" / "runs" / "run_3" / "events.jsonl"
        event = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        event["payload"]["risk_tier"] = "critical"
        path.write_text(json.dumps(event, sort_keys=True) + "\n", encoding="utf-8")
        try:
            log.read_events(verify=True)
        except MissionEventError as exc:
            assert str(exc).startswith("event_hash_mismatch"), exc
        else:
            raise AssertionError("tampered event log verified")
    _pass(name)


def main() -> None:
    for test in [
        assert_event_log_appends_mission_admitted_with_hash_chain,
        assert_event_log_rejects_wrong_prev_hash,
        assert_event_log_verifier_rejects_tampered_payload,
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
