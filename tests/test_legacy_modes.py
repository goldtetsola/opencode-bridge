#!/usr/bin/env python3
"""Contract tests for legacy continuation mode helpers."""

from __future__ import annotations

import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from codex_oss.legacy_modes import (
    LegacyModeDecision,
    handle_bounded_patch_continuation,
    handle_bounded_write_exact,
    handle_context_pack_report,
    handle_read_finalizer,
)


def patch_report(envelope, changed_paths, status, reason, verification_seen=False, verification_output=""):
    return f"{status}\nChanged owned paths: {', '.join(changed_paths)}\nReason: {reason}\nVerification status: {'observed' if verification_seen else 'not_observed'}\n{verification_output}"


def assert_legacy_mode_decision_defaults_are_initialized():
    decision = LegacyModeDecision(True)
    assert decision.log_fields == {}, decision
    assert decision.response_obj == {}, decision
    assert decision.degraded_report == {}, decision


def assert_exact_write_creates_and_reads_back():
    with tempfile.TemporaryDirectory(dir=ROOT) as d:
        rel = os.path.relpath(os.path.join(d, "exact.txt"), ROOT)
        decision = handle_bounded_write_exact(
            {"owned_paths": [rel], "exact_content": "hello runtime"},
            "bounded_write_exact",
            "read",
            ROOT,
            lambda *args, **kwargs: None,
        )
        assert decision.handled, decision
        assert decision.text.startswith("PASS\n"), decision.text
        assert open(os.path.join(ROOT, rel)).read().strip() == "hello runtime"


def assert_patch_shell_verification_returns_observed_report():
    decision = handle_bounded_patch_continuation(
        envelope={"owned_paths": ["docs/example.md"]},
        tool_kind="shell",
        tool_output_text="Process exited with code 0",
        compacted_output="rtk git diff --check\nProcess exited with code 0",
        exit_code=0,
        target_path="",
        turn=1,
        max_exchanges=2,
        project_root=ROOT,
        collect_owned_path_changes=lambda owned, root: ["docs/example.md"],
        path_is_within_owned_paths=lambda path, owned, root: True,
        build_patch_contract_report=patch_report,
        tool_output_indicates_failure=lambda text: "code 1" in text,
    )
    assert decision.handled, decision
    assert decision.log_event == "bounded_patch_verification_complete", decision
    assert "Verification status: observed" in decision.text, decision.text


def assert_patch_write_can_continue_for_verification():
    decision = handle_bounded_patch_continuation(
        envelope={
            "owned_paths": ["docs/example.md"],
            "deliverable_fields": ["changed_sections", "verification"],
            "verification_steps": ["rtk git diff --check -- docs/example.md"],
        },
        tool_kind="write",
        tool_output_text="ok",
        compacted_output="ok",
        exit_code=0,
        target_path="docs/example.md",
        turn=1,
        max_exchanges=2,
        project_root=ROOT,
        collect_owned_path_changes=lambda owned, root: ["docs/example.md"],
        path_is_within_owned_paths=lambda path, owned, root: True,
        build_patch_contract_report=patch_report,
        tool_output_indicates_failure=lambda text: False,
    )
    assert decision.continue_for_verification, decision
    assert "PATCH CONTRACT LEDGER" in decision.ledger_text, decision.ledger_text


def assert_context_pack_falls_back_on_intent_text():
    class Session:
        required_outputs = ["summary", "confidence"]
        read_paths = {}
        required_paths = ["README.md"]
        verification_steps = []
        required_commands = []
        handoff_text = "handoff"

    calls = []

    def call(payload, timeout):
        calls.append(payload)
        return {"choices": [{"message": {"content": "I will inspect the files now."}}]}

    decision = handle_context_pack_report(
        body={"input": []},
        envelope={"read_only_paths": ["README.md"], "verification_steps": [], "deliverable_fields": ["summary"]},
        handoff_text="handoff",
        prev_id="resp",
        prev_state_messages=[],
        mode="context_pack_report",
        tool_output_text="",
        request_deadline=90,
        request_start=time.time(),
        project_root=ROOT,
        continuation_model="kimi",
        model_map={},
        continuation_deadline=5,
        continuation_fallbacks=[],
        max_tool_output_chars=1000,
        log_fn=lambda *args, **kwargs: None,
        map_model=lambda model, model_map: model,
        call_continuation_with_deadline=call,
        build_task_session=lambda body, text, response_id: Session(),
        extract_read_paths_from_history=lambda messages: set(),
        build_context_pack=lambda session, root: "=== README.md (10 chars) ===\nhello",
        evaluate_evidence_coverage=lambda envelope, pack: (True, [], ["README.md"], []),
        is_intent_or_status=lambda text: text.startswith("I will"),
        validate_report_output=lambda *args, **kwargs: (True, []),
        build_context_pack_deterministic_report=lambda session, pack, output: "PARTIAL\nfallback context report",
    )
    assert decision.handled, decision
    assert decision.text == "PARTIAL\nfallback context report", decision.text
    assert len(calls) == 2, calls


def assert_context_pack_repairs_model_report_missing_required_field():
    class Session:
        required_outputs = ["summary", "evidence", "confidence"]
        read_paths = {}
        required_paths = ["README.md"]
        verification_steps = []
        required_commands = []
        handoff_text = "handoff"

    calls = []

    def call(payload, timeout):
        calls.append(payload)
        if len(calls) == 1:
            return {"choices": [{"message": {"content": "PARTIAL\nSummary: read the file.\nConfidence: MEDIUM\nCaveats: none"}}]}
        return {"choices": [{"message": {"content": "PARTIAL\nSummary: read the file.\nEvidence: README.md\nConfidence: MEDIUM\nCaveats: none"}}]}

    events = []

    decision = handle_context_pack_report(
        body={"input": []},
        envelope={"read_only_paths": ["README.md"], "verification_steps": [], "deliverable_fields": ["summary", "evidence"]},
        handoff_text="handoff",
        prev_id="resp",
        prev_state_messages=[],
        mode="context_pack_report",
        tool_output_text="",
        request_deadline=90,
        request_start=time.time(),
        project_root=ROOT,
        continuation_model="kimi",
        model_map={},
        continuation_deadline=5,
        continuation_fallbacks=[],
        max_tool_output_chars=1000,
        log_fn=lambda event, **kwargs: events.append((event, kwargs)),
        map_model=lambda model, model_map: model,
        call_continuation_with_deadline=call,
        build_task_session=lambda body, text, response_id: Session(),
        extract_read_paths_from_history=lambda messages: set(),
        build_context_pack=lambda session, root: "=== README.md (10 chars) ===\nhello",
        evaluate_evidence_coverage=lambda envelope, pack: (True, [], ["README.md"], []),
        is_intent_or_status=lambda text: text.startswith("I will"),
        validate_report_output=lambda text, *args, **kwargs: (("evidence" in text.lower()), [] if "evidence" in text.lower() else ["evidence"]),
        build_context_pack_deterministic_report=lambda session, pack, output: "PARTIAL\nfallback context report",
    )
    assert decision.handled, decision
    assert "Synthesis status: FALLBACK" not in decision.text, decision.text
    assert "Evidence: README.md" in decision.text, decision.text
    assert len(calls) == 2, calls
    assert any(event == "context_pack_report_repair_ok" for event, _ in events), events


def assert_context_pack_synthesis_uses_compacted_pack():
    class Session:
        required_outputs = ["summary", "evidence", "confidence"]
        read_paths = {}
        required_paths = ["README.md"]
        verification_steps = []
        required_commands = []
        handoff_text = "handoff"

    calls = []
    events = []
    old_limit = os.environ.get("CONTEXT_PACK_SYNTHESIS_MAX_CHARS")
    os.environ["CONTEXT_PACK_SYNTHESIS_MAX_CHARS"] = "1200"
    try:
        def call(payload, timeout):
            calls.append(payload)
            return {"choices": [{"message": {"content": "PARTIAL\nSummary: read compact source.\nEvidence: README.md\nConfidence: MEDIUM\nCaveats: source pack was compacted for synthesis"}}]}

        large_pack = "=== README.md (30000 chars) ===\n" + ("alpha\n" * 6000)
        decision = handle_context_pack_report(
            body={"input": []},
            envelope={"read_only_paths": ["README.md"], "verification_steps": [], "deliverable_fields": ["summary", "evidence"]},
            handoff_text="handoff",
            prev_id="resp",
            prev_state_messages=[],
            mode="context_pack_report",
            tool_output_text="",
            request_deadline=90,
            request_start=time.time(),
            project_root=ROOT,
            continuation_model="kimi",
            model_map={},
            continuation_deadline=5,
            continuation_fallbacks=[],
            max_tool_output_chars=1000,
            log_fn=lambda event, **kwargs: events.append((event, kwargs)),
            map_model=lambda model, model_map: model,
            call_continuation_with_deadline=call,
            build_task_session=lambda body, text, response_id: Session(),
            extract_read_paths_from_history=lambda messages: set(),
            build_context_pack=lambda session, root: large_pack,
            evaluate_evidence_coverage=lambda envelope, pack: (True, [], [("README.md", len(pack))], []),
            is_intent_or_status=lambda text: text.startswith("I will"),
            validate_report_output=lambda text, *args, **kwargs: ((("evidence" in text.lower()) and ("confidence" in text.lower())), []),
            build_context_pack_deterministic_report=lambda session, pack, output: "PARTIAL\nfallback context report",
        )
    finally:
        if old_limit is None:
            os.environ.pop("CONTEXT_PACK_SYNTHESIS_MAX_CHARS", None)
        else:
            os.environ["CONTEXT_PACK_SYNTHESIS_MAX_CHARS"] = old_limit

    assert decision.handled, decision
    assert "fallback context report" not in decision.text, decision.text
    source_message = calls[0]["messages"][0]["content"]
    assert len(source_message) < 1800, len(source_message)
    assert "Source pack truncated for synthesis" in source_message, source_message
    assert any(event == "context_pack_synthesis_compacted" for event, _ in events), events


def assert_read_finalizer_returns_response_object():
    def build_response_object(body, chat_resp, messages, model_alias, model_used, reverse_name_map):
        text = chat_resp["choices"][0]["message"]["content"]
        return {"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}

    decision = handle_read_finalizer(
        body={"input": []},
        prev_state_messages=[
            {"role": "user", "content": "Read README"},
            {"role": "assistant", "content": "ok"},
        ],
        tool_outputs=[{"role": "tool", "tool_call_id": "x", "content": "README"}],
        tool_kind="read",
        compacted_output="README contents",
        model_alias="ocg-kimi-k2.6",
        reverse_name_map={},
        continuation_model="kimi",
        model_map={},
        continuation_tools="none",
        continuation_deadline=5,
        continuation_fallbacks=[],
        max_tool_output_chars=1000,
        degraded_completion_on_timeout=True,
        log_fn=lambda *args, **kwargs: None,
        map_model=lambda model, model_map: model,
        repair_chat_history=lambda messages, outputs: list(messages) + list(outputs),
        merge_new_user_messages=lambda messages, new: messages,
        extract_handoff_text=lambda messages: "DELIVERABLE: summary",
        extract_required_deliverables=lambda text: ["summary"],
        validate_report=lambda text, required: (True, []),
        call_continuation_with_deadline=lambda payload, timeout: {"choices": [{"message": {"content": "summary: done"}}]},
        build_response_object=build_response_object,
        build_degraded_completion=lambda model, reason, kind: {"content": [{"text": "degraded"}]},
    )
    assert decision.handled, decision
    assert decision.text == "summary: done", decision.text
    assert decision.response_obj["output"][0]["content"][0]["text"] == "summary: done"


def assert_read_finalizer_repairs_invalid_report_before_accepting():
    def build_response_object(body, chat_resp, messages, model_alias, model_used, reverse_name_map):
        text = chat_resp["choices"][0]["message"]["content"]
        return {"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}

    calls = []
    events = []

    def call(payload, timeout):
        calls.append(payload)
        if len(calls) == 1:
            return {"choices": [{"message": {"content": "I will inspect README now."}}]}
        return {"choices": [{"message": {"content": "summary: inspected README\nconfidence: medium"}}]}

    decision = handle_read_finalizer(
        body={"input": []},
        prev_state_messages=[
            {"role": "user", "content": "Read README"},
            {"role": "assistant", "content": "ok"},
        ],
        tool_outputs=[{"role": "tool", "tool_call_id": "x", "content": "README"}],
        tool_kind="read",
        compacted_output="README contents",
        model_alias="ocg-kimi-k2.6",
        reverse_name_map={},
        continuation_model="kimi",
        model_map={},
        continuation_tools="none",
        continuation_deadline=5,
        continuation_fallbacks=[],
        max_tool_output_chars=1000,
        degraded_completion_on_timeout=True,
        log_fn=lambda event, **kwargs: events.append((event, kwargs)),
        map_model=lambda model, model_map: model,
        repair_chat_history=lambda messages, outputs: list(messages) + list(outputs),
        merge_new_user_messages=lambda messages, new: messages,
        extract_handoff_text=lambda messages: "DELIVERABLE: summary, confidence",
        extract_required_deliverables=lambda text: ["summary", "confidence"],
        validate_report=lambda text, required: (
            all(field in text.lower() for field in required),
            [field for field in required if field not in text.lower()],
        ),
        call_continuation_with_deadline=call,
        build_response_object=build_response_object,
        build_degraded_completion=lambda model, reason, kind: {"content": [{"text": "degraded"}]},
    )
    assert decision.handled, decision
    assert decision.log_event == "continuation_finalizer_repair_ok", decision
    assert decision.text == "summary: inspected README\nconfidence: medium", decision.text
    assert len(calls) == 2, calls
    assert any(event == "report_invalid" for event, _ in events), events


def assert_read_finalizer_degrades_to_deterministic_recovery():
    events = []

    def failing_call(payload, timeout):
        raise TimeoutError("finalizer timeout")

    decision = handle_read_finalizer(
        body={"input": []},
        prev_state_messages=[
            {"role": "user", "content": "Read docs/CONTINUITY.md and report execution cursor"},
            {"role": "assistant", "content": "ok"},
        ],
        tool_outputs=[{"role": "tool", "tool_call_id": "x", "content": "## Execution Cursor\nactive intent_artifact_payload_missing"}],
        tool_kind="read",
        compacted_output="## Execution Cursor\nactive intent_artifact_payload_missing\nNo writes observed.",
        model_alias="ocg-kimi-k2.6",
        reverse_name_map={},
        continuation_model="kimi",
        model_map={},
        continuation_tools="none",
        continuation_deadline=1,
        continuation_fallbacks=[],
        max_tool_output_chars=1000,
        degraded_completion_on_timeout=True,
        log_fn=lambda event, **kwargs: events.append((event, kwargs)),
        map_model=lambda model, model_map: model,
        repair_chat_history=lambda messages, outputs: list(messages) + list(outputs),
        merge_new_user_messages=lambda messages, new: messages,
        extract_handoff_text=lambda messages: "DELIVERABLE: execution cursor, confidence, caveats",
        extract_required_deliverables=lambda text: ["execution cursor", "confidence", "caveats"],
        validate_report=lambda text, required: (False, required),
        call_continuation_with_deadline=failing_call,
        build_response_object=lambda *args, **kwargs: {},
        build_degraded_completion=lambda model, reason, kind: {"content": [{"text": "all finalizers failed"}]},
        evidence_metadata={
            "schema_version": "raw_read_evidence.v1",
            "model_alias": "ocg-kimi-k2.6",
            "tool_name": "rtk_read",
            "tool_kind": "read",
            "command": "docs/CONTINUITY.md",
            "normalized_path": "docs/CONTINUITY.md",
            "exit_code": 0,
            "output_chars": 77,
            "output_lines": 3,
            "output_sha256": "sha-test",
        },
    )
    assert decision.handled, decision
    assert decision.log_event == "continuation_deterministic_read_recovery", decision
    assert "Synthesis status: DETERMINISTIC_READ_RECOVERY" in decision.text, decision.text
    assert "Canonical evidence:" in decision.text, decision.text
    assert "raw_read_evidence.v1" in decision.text, decision.text
    assert "output_sha256: sha-test" in decision.text, decision.text
    assert "all finalizers failed" not in decision.text, decision.text
    assert "intent_artifact_payload_missing" in decision.text, decision.text


def assert_read_finalizer_rejects_progress_text_as_terminal():
    def build_response_object(body, chat_resp, messages, model_alias, model_used, reverse_name_map):
        text = chat_resp["choices"][0]["message"]["content"]
        return {"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}

    calls = []
    events = []

    def call(payload, timeout):
        calls.append(payload)
        return {"choices": [{"message": {"content": "Running the first verification step against the requested files now."}}]}

    decision = handle_read_finalizer(
        body={"input": []},
        prev_state_messages=[
            {"role": "user", "content": "Read docs/CONTINUITY.md and report execution cursor"},
            {"role": "assistant", "content": "ok"},
        ],
        tool_outputs=[{"role": "tool", "tool_call_id": "x", "content": "Command blocked by PreToolUse hook"}],
        tool_kind="read",
        compacted_output="Command blocked by PreToolUse hook: use rtk read instead of raw cat.",
        model_alias="ocg-kimi-k2.6",
        reverse_name_map={},
        continuation_model="kimi",
        model_map={},
        continuation_tools="none",
        continuation_deadline=1,
        continuation_fallbacks=[],
        max_tool_output_chars=1000,
        degraded_completion_on_timeout=True,
        log_fn=lambda event, **kwargs: events.append((event, kwargs)),
        map_model=lambda model, model_map: model,
        repair_chat_history=lambda messages, outputs: list(messages) + list(outputs),
        merge_new_user_messages=lambda messages, new: messages,
        extract_handoff_text=lambda messages: "DELIVERABLE: execution cursor, confidence, caveats",
        extract_required_deliverables=lambda text: ["execution cursor", "confidence", "caveats"],
        validate_report=lambda text, required: (
            False if text.lower().startswith("running ") else all(field in text.lower() for field in required),
            ["intent_or_status_detected"] if text.lower().startswith("running ") else [field for field in required if field not in text.lower()],
        ),
        call_continuation_with_deadline=call,
        build_response_object=build_response_object,
        build_degraded_completion=lambda model, reason, kind: {"content": [{"text": "all finalizers failed"}]},
        evidence_metadata={
            "schema_version": "raw_read_evidence.v1",
            "tool_name": "exec_command",
            "tool_kind": "read",
            "command": "cat /Users/goldtetsola/.codex/skills/napkin/SKILL.md",
            "exit_code": 1,
            "output_sha256": "blocked-sha",
        },
    )
    assert decision.handled, decision
    assert "Running the first verification step" not in decision.text, decision.text
    assert "Synthesis status: DETERMINISTIC_READ_RECOVERY" in decision.text, decision.text
    assert "blocked-sha" in decision.text, decision.text
    assert any(event == "report_invalid" for event, _ in events), events


def main():
    assert_legacy_mode_decision_defaults_are_initialized()
    assert_exact_write_creates_and_reads_back()
    assert_patch_shell_verification_returns_observed_report()
    assert_patch_write_can_continue_for_verification()
    assert_context_pack_falls_back_on_intent_text()
    assert_context_pack_repairs_model_report_missing_required_field()
    assert_context_pack_synthesis_uses_compacted_pack()
    assert_read_finalizer_returns_response_object()
    assert_read_finalizer_repairs_invalid_report_before_accepting()
    assert_read_finalizer_degrades_to_deterministic_recovery()
    assert_read_finalizer_rejects_progress_text_as_terminal()
    print("PASS: legacy mode helper suite")


if __name__ == "__main__":
    main()
