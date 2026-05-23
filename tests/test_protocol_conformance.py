#!/usr/bin/env python3
"""Protocol conformance gauntlet for OSS bridge task contracts."""

import os
import sys
import tempfile
from pathlib import Path

os.environ["ALLOW_MISSING_OPENCODE_KEY"] = "1"

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from bridge import _path_is_within_owned_paths
from bridge import _extract_read_paths_from_history
from bridge import _count_tool_result_messages
from bridge import build_context_pack
from bridge import build_context_pack_deterministic_report
from bridge import build_patch_contract_report
from bridge import build_task_session
from bridge import collect_owned_path_changes
from bridge import evaluate_evidence_coverage
from bridge import parse_task_envelope
from bridge import ProxyApp
from bridge import select_mode
from bridge import should_use_direct_agent_loop
from bridge import direct_loop_required_sources_satisfied
from bridge import tool_output_indicates_failure
from bridge import validate_report_output
from bridge import verification_contract_requested
from codex_oss.transport.chat_stream import ChatStreamAssembler
from codex_oss.transport.response_builder import build_response_object_from_chat
from codex_oss.doctor import DoctorReport, _check_agreements


def assert_malformed_handoff_fails_closed():
    handoff = """OSS_HANDOFF_JSON:
{"schema_version":1,"role":"Broken","goal":"Missing required fields"}
"""
    envelope = parse_task_envelope(handoff)
    assert envelope["schema_error"], envelope
    assert select_mode(envelope) == "invalid_handoff", envelope


def assert_exact_write_requires_exact_content():
    patch_handoff = """OSS_HANDOFF_JSON:
{"schema_version":1,"role":"Patch docs","goal":"Patch one doc","task_type":"docs_support","owned_paths":["tests/fixtures/protocol-doc.md"],"read_only_paths":[],"forbidden_actions":["Do not edit source"],"verification_steps":["Run rtk git diff --check -- tests/fixtures/protocol-doc.md"],"deliverable_fields":["changed_sections","verification","caveats"],"completion_rule":"stop after verification","escalation_rule":"stop on scope drift"}
"""
    exact_handoff = """OSS_HANDOFF_JSON:
{"schema_version":1,"role":"Exact writer","goal":"Write exact file","task_type":"bounded_write","owned_paths":["tests/fixtures/protocol-exact.txt"],"read_only_paths":[],"forbidden_actions":["Do not edit other files"],"verification_steps":["Read back the file"],"deliverable_fields":["file","confidence"],"completion_rule":"stop after write","escalation_rule":"stop on scope drift","write_allowed":true,"exact_content":"protocol-ok"}
"""
    patch_envelope = parse_task_envelope(patch_handoff)
    exact_envelope = parse_task_envelope(exact_handoff)
    assert select_mode(patch_envelope) == "bounded_write_patch", patch_envelope
    assert select_mode(exact_envelope) == "bounded_write_exact", exact_envelope


def assert_no_match_search_is_still_covered():
    handoff = """OSS_HANDOFF_JSON:
{"schema_version":1,"role":"Read scout","goal":"Search for present and absent terms","task_type":"scout","owned_paths":[],"read_only_paths":["README.md","tests/fixtures"],"forbidden_actions":["Do not edit files"],"verification_steps":["Search read-only paths for terminal_blocker_state, PROTOCOL_TERM_THAT_SHOULD_NOT_EXIST_9f2c"],"deliverable_fields":["summary","evidence_table","confidence","caveats"],"completion_rule":"stop after report","escalation_rule":"stop if source pack is partial"}
"""
    envelope = parse_task_envelope(handoff)
    session = build_task_session({"input": []}, handoff, "resp_protocol_read")
    pack = build_context_pack(session, ROOT)
    assert "rtk grep PROTOCOL_TERM_THAT_SHOULD_NOT_EXIST_9f2c in README.md" in pack, pack[:2000]
    assert "exit_code: 1" in pack, pack[:2000]
    coverage_ok, missing, covered_paths, covered_terms = evaluate_evidence_coverage(envelope, pack)
    assert coverage_ok, missing
    assert "README.md" in covered_paths, covered_paths
    assert "PROTOCOL_TERM_THAT_SHOULD_NOT_EXIST_9f2c" in covered_terms, covered_terms
    report = build_context_pack_deterministic_report(session, pack, "")
    assert "Synthesis status: SOURCE_PACK_RECOVERY" in report, report
    assert "Synthesis status: FALLBACK" not in report, report
    assert "evidence_coverage:" in report, report
    assert "- status: PASS" in report, report


def assert_incomplete_evidence_blocks_confident_pass():
    handoff = """OSS_HANDOFF_JSON:
{"schema_version":1,"role":"Read scout","goal":"Search expected terms","task_type":"scout","owned_paths":[],"read_only_paths":["README.md","tests/fixtures"],"forbidden_actions":["Do not edit files"],"verification_steps":["Search read-only paths for terminal_blocker_state"],"deliverable_fields":["summary","evidence_table","confidence","caveats"],"completion_rule":"stop after report","escalation_rule":"stop if source pack is partial"}
"""
    envelope = parse_task_envelope(handoff)
    coverage_ok, missing, _, _ = evaluate_evidence_coverage(envelope, "=== README.md (10 chars) ===\nhello\n")
    assert not coverage_ok, missing
    confident_report = (
        "PASS\n"
        "summary: The delegated scout completed the requested inspection and found the relevant evidence.\n"
        "evidence_table: The report contains rows for every requested path and search term.\n"
        "confidence: high\n"
        "caveats: none beyond normal review requirements for a delegated scout.\n"
    )
    valid, missing_fields = validate_report_output(
        confident_report, "context_pack_report", envelope,
        evidence_coverage_complete=coverage_ok)
    assert not valid, missing_fields
    assert "evidence_coverage" in missing_fields, missing_fields


def assert_patch_acceptance_requires_scope_change_and_verification():
    handoff = """OSS_HANDOFF_JSON:
{"schema_version":1,"role":"Patch docs","goal":"Patch one fixture","task_type":"docs_support","owned_paths":["tests/fixtures/protocol-patch.txt"],"read_only_paths":[],"forbidden_actions":["Do not edit other files"],"verification_steps":["Run rtk git diff --check -- tests/fixtures/protocol-patch.txt"],"deliverable_fields":["changed_sections","verification","caveats"],"completion_rule":"stop after verification","escalation_rule":"stop on scope drift"}
"""
    envelope = parse_task_envelope(handoff)
    assert verification_contract_requested(envelope), envelope
    target = os.path.join(ROOT, "tests", "fixtures", "protocol-patch.txt")
    try:
        with open(target, "w", encoding="utf-8") as f:
            f.write("protocol patch\n")
        changed = collect_owned_path_changes(["tests/fixtures/protocol-patch.txt"], ROOT)
        assert "tests/fixtures/protocol-patch.txt" in changed, changed
        partial = build_patch_contract_report(
            envelope, changed, "PARTIAL",
            "owned path changes were observed, but verification was not observed before the tool budget ended")
        assert "Verification status: not_observed" in partial, partial
        passed = build_patch_contract_report(
            envelope, changed, "PASS",
            "owned path changes and verification tool output were both observed",
            verification_seen=True,
            verification_output="rtk git diff --check\nProcess exited with code 0")
        assert passed.startswith("PASS\n"), passed
        assert "Verification status: observed" in passed, passed
        assert "Verification evidence:" in passed, passed
    finally:
        try:
            os.remove(target)
        except FileNotFoundError:
            pass


def assert_verification_claims_need_observed_results():
    handoff = """OSS_HANDOFF_JSON:
{"schema_version":1,"role":"Patch docs","goal":"Patch one fixture","task_type":"docs_support","owned_paths":["tests/fixtures/protocol-patch.txt"],"read_only_paths":[],"forbidden_actions":["Do not edit other files"],"verification_steps":["Run rtk git diff --check -- tests/fixtures/protocol-patch.txt"],"deliverable_fields":["changed_sections","verification","caveats"],"completion_rule":"stop after verification","escalation_rule":"stop on scope drift"}
"""
    envelope = parse_task_envelope(handoff)
    claimed = (
        "PASS\n"
        "changed_sections: fixture\n"
        "verification: passed\n"
        "caveats: none\n"
        "file: tests/fixtures/protocol-patch.txt\n"
        "confidence: high\n"
    )
    valid, missing = validate_report_output(claimed, "bounded_write_patch", envelope)
    assert not valid, missing
    assert "verification_observed" in missing, missing
    valid, missing = validate_report_output(
        claimed, "bounded_write_patch", envelope,
        verification_observed=True)
    assert valid, missing
    assert tool_output_indicates_failure("Process exited with code 1\nAssertionError: failed")
    assert not tool_output_indicates_failure("Process exited with code 0\nAll protocol checks passed")


def assert_scope_violation_is_detectable():
    assert _path_is_within_owned_paths(
        "tests/fixtures/protocol-patch.txt", ["tests/fixtures"], ROOT)
    assert not _path_is_within_owned_paths(
        "/tmp/outside-opencode-bridge-protocol.txt", ["tests/fixtures"], ROOT)


def assert_continuation_synthesis_uses_streaming_transport():
    old_setting = os.environ.get("CONTINUATION_STREAM")
    os.environ["CONTINUATION_STREAM"] = "1"
    try:
        app = ProxyApp()
        app.upstream_key = "test-key"
        calls = []

        def fake_stream(payload, timeout=None):
            calls.append((payload, timeout))
            yield ("chunk", {"choices": [{"delta": {"content": "PARTIAL\nSummary: read README.\n"}}]})
            yield ("chunk", {"choices": [{"delta": {"content": "Evidence: README.md\nConfidence: MEDIUM\nCaveats: none\n"}}]})
            yield ("done", None)

        app.iter_upstream_chat_stream = fake_stream
        resp = app.call_continuation_with_deadline(
            {"model": "deepseek-v4-flash", "messages": [], "stream": False, "tools": []},
            5,
        )
    finally:
        if old_setting is None:
            os.environ.pop("CONTINUATION_STREAM", None)
        else:
            os.environ["CONTINUATION_STREAM"] = old_setting

    text = resp["choices"][0]["message"]["content"]
    assert text.startswith("PARTIAL\nSummary"), text
    assert "Evidence: README.md" in text, text
    assert calls and calls[0][1] > 0, calls


def assert_direct_read_only_utility_prefers_live_agent_loop():
    assert should_use_direct_agent_loop("context_pack_report", False)
    assert should_use_direct_agent_loop("context_pack", False)
    assert not should_use_direct_agent_loop("context_pack_report", True)
    assert not should_use_direct_agent_loop("bounded_write_patch", False)
    assert not should_use_direct_agent_loop("context_pack_report", False, enabled=False)


def assert_direct_loop_turn_count_survives_response_projection():
    captured = []

    def stored_response_factory(**kwargs):
        return kwargs

    body = {
        "input": [],
        "_codex_oss_tool_exchange_count": 3,
        "_codex_oss_task_max_exchanges": 6,
    }
    build_response_object_from_chat(
        body=body,
        chat_resp={"choices": [{"message": {"content": "done"}}]},
        base_messages=[{"role": "user", "content": "task"}],
        model_alias="ocg-test",
        model_upstream="test",
        reverse_name_map={},
        state_put=captured.append,
        stored_response_factory=stored_response_factory,
        repair_chat_history=lambda messages, outputs: list(messages),
        extract_budget=lambda messages: 1,
        restore_tool_name=lambda name, reverse: name,
        new_id=lambda prefix: f"{prefix}_1",
        now=lambda: 1,
        json_dumps=lambda obj: "{}",
        as_text=str,
        response_id="resp_test",
        created_at=1,
    )
    assert captured[-1]["tool_exchange_count"] == 3, captured[-1]
    assert captured[-1]["task_max_exchanges"] == 6, captured[-1]

    captured.clear()
    sse_events = []
    assembler = ChatStreamAssembler(
        body=body,
        base_messages=[{"role": "user", "content": "task"}],
        model_alias="ocg-test",
        model_upstream="test",
        reverse_name_map={},
        response_id="resp_stream",
        created_at=1,
        write_sse=lambda event, data: sse_events.append((event, data)),
        write_progress=lambda note: None,
        state_put=captured.append,
        stored_response_factory=stored_response_factory,
        build_response_shell=lambda *args, **kwargs: {},
        repair_chat_history=lambda messages, outputs: list(messages),
        extract_budget=lambda messages: 1,
        restore_tool_name=lambda name, reverse: name,
        new_id=lambda prefix: f"{prefix}_1",
        json_dumps=lambda obj: "{}",
        as_text=str,
    )
    assembler.on_chunk({"choices": [{"delta": {"content": "done"}}]})
    assembler.finalize()
    assert captured[-1]["tool_exchange_count"] == 3, captured[-1]
    assert captured[-1]["task_max_exchanges"] == 6, captured[-1]


def assert_shell_rtk_read_counts_as_read_evidence():
    messages = [{
        "role": "assistant",
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "exec_command",
                "arguments": "{\"command\":\"rtk read README.md\"}",
            },
        }],
    }]
    assert "README.md" in _extract_read_paths_from_history(messages)

    sed_messages = [{
        "role": "assistant",
        "tool_calls": [{
            "id": "call_2",
            "type": "function",
            "function": {
                "name": "exec_command",
                "arguments": "{\"cmd\":\"sed -n '1,$p' README.md\"}",
            },
        }],
    }]
    assert "README.md" in _extract_read_paths_from_history(sed_messages)


def assert_turn_count_can_be_recovered_from_history():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": []},
        {"role": "tool", "tool_call_id": "a", "content": "one"},
        {"role": "tool", "tool_call_id": "b", "content": "two"},
        {"type": "function_call_output", "call_id": "c", "output": "three"},
    ]
    assert _count_tool_result_messages(messages) == 3


def assert_direct_loop_stops_after_required_source_is_read():
    root = os.getcwd()
    required_abs = os.path.join(root, "README.md")
    assert direct_loop_required_sources_satisfied([required_abs], set(), "README.md")
    assert direct_loop_required_sources_satisfied(["README.md"], {required_abs}, "")
    assert not direct_loop_required_sources_satisfied(["README.md", "missing.md"], {"README.md"}, "")


def assert_doctor_rejects_embedded_mission_examples_in_agreements():
    with tempfile.TemporaryDirectory(dir=ROOT) as d:
        root = Path(d)
        (root / "AGENTS.md").write_text(
            "Use OSS_HANDOFF_JSON.\n"
            "<OSS_HANDOFF_JSON>\n{}\n</OSS_HANDOFF_JSON>\n"
            "Never use recursive codex exec.\n",
            encoding="utf-8",
        )
        report = DoctorReport()
        _check_agreements(root, report)
        checks = {check.name: check for check in report.checks}
        assert checks["agreements.no_embedded_mission_examples"].status == "FAIL", checks

    with tempfile.TemporaryDirectory(dir=ROOT) as d:
        root = Path(d)
        (root / "AGENTS.md").write_text(
            "Use exactly one OSS_HANDOFF_JSON MissionV1 block in the spawned worker prompt.\n"
            "Use fork_turns: \"none\".\n"
            "Never use recursive codex exec.\n",
            encoding="utf-8",
        )
        report = DoctorReport()
        _check_agreements(root, report)
        checks = {check.name: check for check in report.checks}
        assert checks["agreements.no_embedded_mission_examples"].status == "PASS", checks


def main():
    assert_malformed_handoff_fails_closed()
    assert_exact_write_requires_exact_content()
    assert_no_match_search_is_still_covered()
    assert_incomplete_evidence_blocks_confident_pass()
    assert_patch_acceptance_requires_scope_change_and_verification()
    assert_verification_claims_need_observed_results()
    assert_scope_violation_is_detectable()
    assert_continuation_synthesis_uses_streaming_transport()
    assert_direct_read_only_utility_prefers_live_agent_loop()
    assert_direct_loop_turn_count_survives_response_projection()
    assert_shell_rtk_read_counts_as_read_evidence()
    assert_turn_count_can_be_recovered_from_history()
    assert_direct_loop_stops_after_required_source_is_read()
    assert_doctor_rejects_embedded_mission_examples_in_agreements()
    print("PASS: OSS bridge protocol conformance suite")


if __name__ == "__main__":
    main()
