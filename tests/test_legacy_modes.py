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


def main():
    assert_legacy_mode_decision_defaults_are_initialized()
    assert_exact_write_creates_and_reads_back()
    assert_patch_shell_verification_returns_observed_report()
    assert_patch_write_can_continue_for_verification()
    assert_context_pack_falls_back_on_intent_text()
    assert_read_finalizer_returns_response_object()
    print("PASS: legacy mode helper suite")


if __name__ == "__main__":
    main()
