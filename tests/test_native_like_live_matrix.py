"""Test suite for native-like OSS subagent product readiness.

Runs burn-in across bronze (deterministic), silver (model-narrated), and gold
(native consumer adoption) success levels. Inspects artifact output without
requiring live bridge in unit tests.

Set LIVE=1 to run against a live bridge at OSS_BRIDGE_URL.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from typing import Any

JSON = dict[str, Any]

LIVE_MODE = os.getenv("LIVE", "0") == "1"
BRIDGE_URL = os.getenv("OSS_BRIDGE_URL", "http://127.0.0.1:4000/v1")
AUTH = os.getenv("PROXY_API_KEY", "sk-local-codex-bridge")

TESTS_PASSED = 0
TESTS_FAILED = 0
TESTS_SKIPPED = 0


def _pass(name: str, detail: str = "") -> None:
    global TESTS_PASSED
    TESTS_PASSED += 1
    status = "✓" if not detail else f"✓ {detail}"
    print(f"  {status:60s} PASS: {name}")


def _fail(name: str, detail: str = "") -> None:
    global TESTS_FAILED
    TESTS_FAILED += 1
    print(f"  {'FAILED' if not detail else 'FAILED: ' + detail:60s} FAIL: {name}")


def _skip(name: str, reason: str = "") -> None:
    global TESTS_SKIPPED
    TESTS_SKIPPED += 1
    print(f"  {'SKIP' if not reason else 'SKIP (' + reason + ')':60s} SKIP: {name}")


# ═══════════════════════════════════════════════════════════════════════════
# Bronze: Truth-safe deterministic/runtime completion
# ═══════════════════════════════════════════════════════════════════════════


def _build_read_floor_handoff(*, paths: list[str], mission_id: str = "") -> str:
    mid = mission_id or f"mission_read_floor_{len(paths)}_files"
    return (
        "OSS_HANDOFF_JSON:\n"
        + json.dumps({
            "schema_version": 1,
            "role": "Read-only scout",
            "goal": "Inspect the declared read-only sources",
            "task_type": "scout",
            "read_only_paths": paths,
            "forbidden_actions": ["Do not edit files"],
            "verification_steps": ["Verify all required files were inspected"],
            "deliverable_fields": ["files inspected", "confidence", "caveats"],
            "completion_rule": "stop after inspection",
            "escalation_rule": "stop on scope drift",
            "mission_id": mid,
        })
        + "\n</OSS_HANDOFF_JSON>"
    )


def bronze_read_floor_3_files():
    """Bronze: Read floor with 3 files completes deterministically."""
    name = "bronze_read_floor_3_files"
    if not LIVE_MODE:
        _skip(name, "set LIVE=1")
        return

    paths = ["bridge.py", "codex_oss/read_evidence.py", "codex_oss/visible_commentary.py"]
    handoff = _build_read_floor_handoff(paths=paths)

    body = {
        "model": "oss_flash_support",
        "stream": False,
        "input": [{"role": "user", "content": handoff}],
    }
    try:
        payload = post_response(body)
        text = _extract_text(payload)
    except Exception as exc:
        _fail(name, str(exc)[:100])
        return

    checks = [
        ("COMPLETE" in text or "PARTIAL" not in text, "status is COMPLETE"),
        ("DETERMINISTIC_SERVER_SIDE_READ_COMPLETION" in text
         or "MODEL_AUTHORED_SERVER_SIDE_READ_COMPLETION" in text,
         "synthesis status is valid"),
        ("No writes performed: true" in text, "no writes performed"),
        ("Files inspected:" in text, "files inspected listed"),
        ("Evidence:" in text, "evidence section present"),
        (not _has_replay_loops(text), "no replay loops"),
    ]
    all_ok = True
    for ok, desc in checks:
        if not ok:
            _fail(name, desc)
            all_ok = False
    if all_ok:
        _pass(name)


def bronze_grep_zero_match():
    """Bronze: grep with zero matches is valid evidence, not a failure."""
    name = "bronze_grep_zero_match"

    # This test verifies the RuntimeContractCompleter behavior:
    # grep that returns no matches should count as valid evidence
    from codex_oss.read_evidence import execute_runtime_contract_action

    exit_code, output = execute_runtime_contract_action(
        action={
            "kind": "required_grep_zero_match",
            "tool_name": "rtk_grep",
            "arguments": {"path": "bridge.py", "pattern": "definitely_not_in_codebase"},
        },
        project_root=os.getcwd(),
    )
    if exit_code == 0 and "matches: 0" in output:
        _pass(name, "grep zero-match counted as evidence")
    else:
        _fail(name, f"exit_code={exit_code}")


def bronze_no_writes_outside_scope():
    """Bronze: Writing outside owned paths is blocked."""
    name = "bronze_no_writes_outside_scope"
    if not LIVE_MODE:
        _skip(name, "set LIVE=1")
        return

    body = {
        "model": "oss_deepseek_pro",
        "stream": False,
        "input": [{"role": "user", "content": (
            "OSS_HANDOFF_JSON:\n"
            + json.dumps({
                "schema_version": 1,
                "role": "Bounded writer",
                "goal": "Write to a forbidden file",
                "task_type": "bounded_write",
                "owned_paths": ["tests/scratch_allowed.txt"],
                "read_only_paths": [],
                "forbidden_actions": ["Do not edit other files"],
                "verification_steps": [],
                "deliverable_fields": [],
                "completion_rule": "stop after verification",
                "escalation_rule": "stop on scope drift",
            })
            + "\n</OSS_HANDOFF_JSON>"
        )}],
        "tools": [{
            "type": "function",
            "name": "exec_command",
            "description": "Run a shell command",
            "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
        }],
    }
    try:
        payload = post_response(body)
        text = _extract_text(payload)
    except Exception as exc:
        _fail(name, str(exc)[:100])
        return

    # Should demote, not execute direct writes
    ok = "DETERMINISTIC_LEGACY_WRITE_DEMOTED" in text or "MissionV1" in text
    if ok:
        _pass(name)
    else:
        _fail(name, "direct write was not demoted")


def bronze_runtime_contract_completer():
    """Bronze: RuntimeContractCompleter executes reads/greps/ls deterministically."""
    name = "bronze_runtime_contract_completer"
    from codex_oss.read_evidence import RuntimeContractCompleter, execute_runtime_contract_action

    completer = RuntimeContractCompleter(os.getcwd())

    # Test read
    exit_code, output = execute_runtime_contract_action(
        action={"kind": "required_read", "tool_name": "rtk_read", "arguments": {"path": "bridge.py"}},
        project_root=os.getcwd(),
    )
    if exit_code != 0 or "import" not in output.lower():
        _fail(name, f"read failed: exit={exit_code}")
        return

    # Test grep
    exit_code, output = execute_runtime_contract_action(
        action={"kind": "required_grep", "tool_name": "rtk_grep", "arguments": {"path": "bridge.py", "pattern": "def "}},
        project_root=os.getcwd(),
    )
    if exit_code != 0 or "def " not in output:
        _fail(name, f"grep failed: exit={exit_code}")
        return

    # Test ls
    exit_code, output = execute_runtime_contract_action(
        action={"kind": "required_ls", "tool_name": "rtk_ls", "arguments": {"path": "."}},
        project_root=os.getcwd(),
    )
    if exit_code != 0:
        _fail(name, f"ls failed: exit={exit_code}")
        return

    _pass(name)


# ═══════════════════════════════════════════════════════════════════════════
# Silver: Model-authored narration over runtime-owned evidence
# ═══════════════════════════════════════════════════════════════════════════


def silver_canonical_read_evidence_artifacts():
    """Silver: CanonicalReadEvidenceV1, ReadReportSkeletonV1 are written."""
    name = "silver_canonical_read_evidence_artifacts"
    from codex_oss.read_evidence import (
        build_canonical_read_evidence,
        build_read_report_skeleton,
        build_read_narrative_draft,
        persist_read_artifacts,
    )
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        mission_id = "silver_test_mission"
        required = ["bridge.py", "README.md"]
        evidence = {}
        for path in required:
            full = os.path.join(os.getcwd(), path)
            if os.path.exists(full):
                with open(full, "r", encoding="utf-8") as f:
                    content = f.read()
                evidence[path] = {
                    "path": path,
                    "source": "test_executor",
                    "exit_code": 0,
                    "output_chars": len(content),
                    "output_sha256": "test_hash",
                    "output_excerpt": content[:200],
                    "redactions_applied": True,
                }

        # Build narrative draft
        narrative = build_read_narrative_draft(
            findings=["The bridge module handles SSE streaming.", "README.md describes the project."],
            confidence_rationale="High confidence based on two inspected files.",
            caveat_wording=["Only two files were inspected."],
        )

        result = persist_read_artifacts(
            mission_dir=tmp,
            mission_id=mission_id,
            required_paths=required,
            evidence=evidence,
            failures=[],
            status="COMPLETE",
            synthesis_status="MODEL_AUTHORED_SERVER_SIDE_READ_COMPLETION",
            narrative_draft=narrative,
        )

        # Check files exist
        checks = [
            os.path.exists(os.path.join(tmp, "canonical_read_evidence.json")),
            os.path.exists(os.path.join(tmp, "read_report_skeleton.json")),
            os.path.exists(os.path.join(tmp, "read_narrative_draft.json")),
            os.path.exists(os.path.join(tmp, "merged_read_report.json")),
        ]
        if not all(checks):
            _fail(name, f"missing artifacts: {checks}")
            return

        # Validate schema
        canonical = json.loads(open(os.path.join(tmp, "canonical_read_evidence.json")).read())
        if canonical.get("schema_version") != "canonical_read_evidence.v1":
            _fail(name, "wrong canonical evidence schema")
            return
        if canonical.get("status_entitlement", {}).get("can_complete") is not True:
            _fail(name, "can_complete should be true")
            return

        skeleton = json.loads(open(os.path.join(tmp, "read_report_skeleton.json")).read())
        if skeleton.get("schema_version") != "read_report_skeleton.v1":
            _fail(name, "wrong skeleton schema")
            return
        if "model_fillable_fields" not in skeleton:
            _fail(name, "missing model_fillable_fields")
            return

        _pass(name, f"{len(result)} artifacts written")


def silver_finalizer_attempts_logged():
    """Silver: server_side_read_finalizer_attempts.jsonl is written."""
    name = "silver_finalizer_attempts_logged"
    from codex_oss.read_evidence import log_finalizer_attempt
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        log_finalizer_attempt(
            mission_dir=tmp,
            attempt_id="test_001",
            model_alias="test_model",
            prompt_chars=5000,
            files_count=3,
            excerpt_chars_total=1500,
            timeout_seconds=20,
            elapsed_seconds=8.4,
            result="success",
            validation_errors=[],
        )
        log_finalizer_attempt(
            mission_dir=tmp,
            attempt_id="test_002",
            model_alias="test_model",
            prompt_chars=4000,
            files_count=2,
            excerpt_chars_total=800,
            timeout_seconds=20,
            elapsed_seconds=19.5,
            result="timeout",
            validation_errors=["finalizer timed out"],
        )

        attempts_path = os.path.join(tmp, "server_side_read_finalizer_attempts.jsonl")
        if not os.path.exists(attempts_path):
            _fail(name, "attempts file not created")
            return

        with open(attempts_path) as f:
            lines = [json.loads(line) for line in f if line.strip()]
        if len(lines) != 2:
            _fail(name, f"expected 2 attempts, got {len(lines)}")
            return
        if lines[0]["result"] != "success":
            _fail(name, "first attempt should be success")
            return
        if lines[1]["result"] != "timeout":
            _fail(name, "second attempt should be timeout")
            return
        _pass(name, f"{len(lines)} attempts logged")


def silver_reject_action_as_narrative():
    """Silver: Action/intent text is rejected as narrative."""
    name = "silver_reject_action_as_narrative"
    from codex_oss.read_evidence import parse_read_narrative_draft

    # These should be rejected
    rejected = [
        "Running the first verification step now.",
        "Let me read the file first.",
        "I will check the source.",
    ]
    for text in rejected:
        draft, error = parse_read_narrative_draft(text)
        if draft is not None:
            _fail(name, f"should reject: '{text}'")
            return

    # This should be accepted
    narrative, error = parse_read_narrative_draft(
        "Findings:\n- The bridge module handles SSE streaming.\n"
        "- The read_evidence module provides structured evidence schemas.\n"
        "Confidence: High confidence based on thorough inspection.\n"
        "Caveats:\n- Only two files were inspected."
    )
    if narrative is None:
        _fail(name, f"should accept valid narrative: {error}")
        return
    if not narrative.get("findings"):
        _fail(name, "should have findings")
        return
    _pass(name, "action texts rejected, valid narrative accepted")


# ═══════════════════════════════════════════════════════════════════════════
# Gold: Native consumer adoption (live-mode only)
# ═══════════════════════════════════════════════════════════════════════════


def gold_adoption_probe_schema():
    """Gold: ToolCallAdoptionProbeV1 schema is valid."""
    name = "gold_adoption_probe_schema"
    from codex_oss.tool_call_adoption import build_adoption_probe, build_adopted_probe, build_not_adopted_probe

    probe = build_adoption_probe(
        response_id="resp_001",
        item_id="fc_read",
        call_id="call_read",
        tool_name="rtk_read",
        sse_sequence=1,
        consumer_adopted=True,
        adoption_signal="function_call_output_received",
        adoption_latency_ms=842,
    )
    if probe.get("schema_version") != "tool_call_adoption_probe.v1":
        _fail(name, "wrong schema")
        return

    not_adopted = build_not_adopted_probe(
        response_id="resp_002",
        item_id="fc_failed",
        call_id="call_failed",
        tool_name="rtk_read",
        sse_sequence=2,
        replay_count=1,
        recovery_reason="declared_read_floor_completed_by_bridge",
    )
    if not_adopted.get("consumer_adopted") is not False:
        _fail(name, "should not be adopted")
        return
    if not_adopted.get("recovery_used") is not True:
        _fail(name, "recovery should be used")
        return
    _pass(name)


def gold_state_machine_ledger():
    """Gold: ResponsesToolStateMachine tracks all states."""
    name = "gold_state_machine_ledger"
    from codex_oss.tool_call_adoption import ResponsesToolStateMachine

    sm = ResponsesToolStateMachine("resp_001")
    sm.register_tool_call("call_1", "rtk_read", {"path": "bridge.py"})
    sm.register_tool_call("call_2", "rtk_grep", {"path": ".", "pattern": "def"})
    sm.mark_adopted("call_1")
    sm.mark_completed("call_1")
    sm.mark_recovered("call_2", "grep_not_adopted")

    stats = sm.adoption_stats()
    if stats["total"] != 2:
        _fail(name, f"expected 2 calls, got {stats['total']}")
        return
    if stats["adopted"] != 1:
        _fail(name, "expected 1 adopted")
        return
    if stats["recovered"] != 1:
        _fail(name, "expected 1 recovered")
        return

    probes = sm.to_probes()
    if len(probes) != 2:
        _fail(name, f"expected 2 probes, got {len(probes)}")
        return

    ledger = sm.to_ledger()
    if ledger.get("schema_version") != "responses_tool_state_machine_ledger.v1":
        _fail(name, "wrong ledger schema")
        return
    _pass(name, f"adoption_rate={stats['adoption_rate']}")


def gold_promotion_gate():
    """Gold: Promotion gate checks adoption thresholds."""
    name = "gold_promotion_gate"
    from codex_oss.tool_call_adoption import (
        build_adoption_probe,
        check_adoption_promotion_gate,
    )

    # All adopted - should be eligible
    probes = [
        build_adoption_probe(
            response_id=f"resp_{i}",
            item_id=f"fc_{i}",
            call_id=f"call_{i}",
            tool_name="rtk_read",
            sse_sequence=i,
            consumer_adopted=True,
            adoption_signal="function_call_output_received",
        )
        for i in range(20)
    ]
    result = check_adoption_promotion_gate(probes, task_class="read_floor")
    if result.get("promotion_eligible") is not True:
        _fail(name, "should be eligible with 100% adoption")
        return

    # 50% adopted - should not be eligible
    probes_mixed = probes[:10] + [
        build_adoption_probe(
            response_id=f"resp_fail_{i}",
            item_id=f"fc_fail_{i}",
            call_id=f"call_fail_{i}",
            tool_name="rtk_read",
            sse_sequence=i,
            consumer_adopted=False,
            replay_count=1,
            recovery_used=True,
            recovery_reason="declared_read_floor_completed_by_bridge",
        )
        for i in range(10)
    ]
    result_mixed = check_adoption_promotion_gate(probes_mixed, task_class="read_floor")
    if result_mixed.get("promotion_eligible") is not False:
        _fail(name, "should not be eligible with 50% adoption")
        return
    _pass(name)


# ═══════════════════════════════════════════════════════════════════════════
# Capability matrix assertions
# ═══════════════════════════════════════════════════════════════════════════


def capability_read_evidence_artifact_integrity():
    """Artifacts are valid JSON and schema-compliant."""
    name = "capability_read_evidence_artifact_integrity"
    from codex_oss.read_evidence import (
        build_canonical_read_evidence,
        build_read_report_skeleton,
        build_read_narrative_draft,
    )
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        canonical = build_canonical_read_evidence(
            mission_id="test",
            required_paths=["bridge.py"],
            evidence={"bridge.py": {"output_excerpt": "...", "output_chars": 100, "output_sha256": "abc", "source": "test"}},
            failures=[],
        )
        # Verify it's valid JSON
        dumped = json.dumps(canonical)
        assert isinstance(json.loads(dumped), dict)

        skeleton = build_read_report_skeleton(
            mission_id="test",
            status="COMPLETE",
            synthesis_status="DETERMINISTIC_SERVER_SIDE_READ_COMPLETION",
            evidence={},
            required_paths=[],
            failures=[],
        )
        json.dumps(skeleton)  # should not raise

        narrative = build_read_narrative_draft(
            findings=["Test finding"],
            confidence_rationale="Test confidence",
        )
        json.dumps(narrative)  # should not raise

    _pass(name, "all schemas produce valid JSON")


def capability_visible_commentary_stream():
    """VisibleCommentarySink produces valid JSONL and summary.md."""
    name = "capability_visible_commentary_stream"
    from codex_oss.visible_commentary import VisibleCommentarySink
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        sink = VisibleCommentarySink(
            mission_id="test_capability",
            mission_dir=tmp,
            mode="summary",
        )
        sink.emit(
            "mission_started",
            "Mission started",
            "Test mission.",
            phase="LIFECYCLE",
            source="runtime",
        )
        sink.emit(
            "server_side_read_completed",
            "Read complete",
            "All reads done.",
            phase="READ_FLOOR",
            source="runtime",
            evidence_refs=["test.py"],
        )
        sink.close({"status": "COMPLETE", "mission_id": "test_capability", "confidence": "HIGH"})

        # Verify artifacts exist
        commentary_path = os.path.join(tmp, "visible_commentary.jsonl")
        summary_path = os.path.join(tmp, "summary.md")
        if not os.path.exists(commentary_path):
            _fail(name, "visible_commentary.jsonl missing")
            return
        if not os.path.exists(summary_path):
            _fail(name, "summary.md missing")
            return

        # Verify JSONL is valid
        with open(commentary_path) as f:
            events = [json.loads(line) for line in f if line.strip()]
        if len(events) < 2:
            _fail(name, f"expected >=2 events, got {len(events)}")
            return
        for event in events:
            if event.get("schema_version") != "visible_commentary_event.v1":
                _fail(name, f"wrong event schema: {event.get('schema_version')}")
                return
            if not event.get("safe_for_user"):
                _fail(name, "event not marked safe_for_user")
                return

        _pass(name, f"{len(events)} events, summary written")


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def post_response(body: JSON, timeout: float = 120) -> JSON:
    req = urllib.request.Request(
        f"{BRIDGE_URL}/responses",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {AUTH}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _extract_text(payload: JSON) -> str:
    parts = []
    for item in payload.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []) or []:
            if isinstance(content, dict) and content.get("text"):
                parts.append(str(content["text"]))
    return "\n".join(parts)


def _has_replay_loops(text: str) -> bool:
    return "replay" in text.lower() and "count: 0" not in text.lower()


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════


def main():
    print()
    print("=" * 70)
    print(" Native-Like OSS Subagent Burn-In Matrix")
    print("=" * 70)
    print(f" Live mode: {LIVE_MODE}")
    if LIVE_MODE:
        print(f" Bridge URL: {BRIDGE_URL}")
    print()

    # ── Bronze ──
    print("── Bronze: deterministic/runtime completion ──")
    bronze_runtime_contract_completer()
    bronze_grep_zero_match()
    if LIVE_MODE:
        bronze_read_floor_3_files()
        bronze_no_writes_outside_scope()

    # ── Silver ──
    print("\n── Silver: model-authored narration ──")
    silver_canonical_read_evidence_artifacts()
    silver_finalizer_attempts_logged()
    silver_reject_action_as_narrative()

    # ── Gold ──
    print("\n── Gold: native adoption probes ──")
    gold_adoption_probe_schema()
    gold_state_machine_ledger()
    gold_promotion_gate()

    # ── Capability ──
    print("\n── Capability: artifact integrity ──")
    capability_read_evidence_artifact_integrity()
    capability_visible_commentary_stream()

    print()
    print("=" * 70)
    print(f" Results: {TESTS_PASSED} passed, {TESTS_FAILED} failed, {TESTS_SKIPPED} skipped")
    print("=" * 70)
    print()

    return TESTS_FAILED == 0


if __name__ == "__main__":
    if not main():
        sys.exit(1)
