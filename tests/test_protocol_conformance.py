#!/usr/bin/env python3
"""Protocol conformance gauntlet for OSS bridge task contracts."""

import os
import json
import sys
import tempfile
import time
from pathlib import Path

os.environ["ALLOW_MISSING_OPENCODE_KEY"] = "1"

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from bridge import _path_is_within_owned_paths
from bridge import _extract_read_paths_from_history
from bridge import _extract_completed_read_paths_from_history
from bridge import _count_tool_result_messages
from bridge import _extract_handoff_text
from bridge import build_direct_loop_terminal_report
from bridge import build_pending_child_not_fulfilled_report
from bridge import build_response_from_pending_child
from bridge import complete_pending_reads_from_bridge
from bridge import execute_pending_owned_write_from_bridge
from bridge import execute_pending_owned_shell_from_bridge
from bridge import execute_pending_owned_verification_from_bridge
from bridge import complete_declared_reads_from_bridge
from bridge import build_context_pack
from bridge import build_context_pack_deterministic_report
from bridge import build_patch_contract_report
from bridge import build_task_session
from bridge import collect_owned_path_changes
from bridge import evaluate_evidence_coverage
from bridge import effective_tool_kind
from bridge import normalize_tool_args
from bridge import parse_task_envelope
from bridge import pretool_block_repair_instruction
from bridge import should_retry_pretool_block
from bridge import required_paths_from_envelope
from bridge import explicit_outside_owned_write_target
from bridge import Handler
from bridge import ProxyApp
from bridge import select_mode
from bridge import StateStore
from bridge import StoredResponse
from bridge import should_use_direct_agent_loop
from bridge import direct_loop_terminal_decision
from bridge import direct_loop_required_sources_satisfied
from bridge import declared_read_floor_only
from bridge import _append_redirection_from_shell_command
from bridge import DEFAULT_MODEL_MAP
from bridge import map_model
from bridge import tool_output_indicates_failure
from bridge import validate_report_output
from bridge import validate_model_read_narrative
from bridge import sanitize_model_read_narrative
from bridge import verification_contract_requested
from bridge import _default_local_read_executor
from bridge import _visible_event_stream_identity
from bridge import _visible_event_stream_text
from codex_oss.transport.chat_stream import ChatStreamAssembler
from codex_oss.transport.response_builder import build_response_object_from_chat
from codex_oss.doctor import DoctorReport, _check_agreements
from codex_oss.desktop_tool_loop_probe import (
    build_desktop_tool_loop_probe_response,
    persist_desktop_tool_loop_adoption,
    select_desktop_probe_tool,
)
from codex_oss.mission import InvalidHandoffError, _build_mission


def assert_malformed_handoff_fails_closed():
    handoff = """OSS_HANDOFF_JSON:
{"schema_version":1,"role":"Broken","goal":"Missing required fields"}
"""
    envelope = parse_task_envelope(handoff)
    assert envelope["schema_error"], envelope
    assert select_mode(envelope) == "invalid_handoff", envelope


def assert_visible_commentary_projection_preserves_identity():
    event = {
        "schema_version": "visible_commentary_event.v1",
        "mission_id": "mission_123",
        "event_id": "evt_abc",
        "seq": 3,
        "event_type": "coverage_update",
        "phase": "NARROW",
        "source": "coverage",
        "safe_for_user": True,
        "title": "Coverage updated",
        "message": "Required obligations answered: 2/3.",
    }
    metadata = _visible_event_stream_identity(event)
    identity = metadata["oss_visible_event"]
    assert identity["mission_id"] == "mission_123", identity
    assert identity["event_id"] == "evt_abc", identity
    assert identity["seq"] == 3, identity
    assert identity["event_type"] == "coverage_update", identity
    assert identity["safe_for_user"] is True, identity

    text = _visible_event_stream_text(event)
    assert not text.startswith("[OSS progress"), text
    assert "mission_123" not in text, text
    assert "evt_abc" not in text, text
    assert "evidence is now in hand" in text, text
    assert "Required obligations answered" not in text, text


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


def assert_bounded_write_handoffs_receive_tools_by_default():
    old = os.environ.get("OSS_LEGACY_DIRECT_WRITES")
    os.environ.pop("OSS_LEGACY_DIRECT_WRITES", None)
    try:
        app = ProxyApp()
        body = {
            "model": "ocg-deepseek-v4-flash",
            "input": [
                {
                    "role": "user",
                    "content": (
                        "OSS_HANDOFF_JSON:\n"
                        "{\"schema_version\":1,\"role\":\"Patch docs\",\"goal\":\"Patch one fixture\","
                        "\"task_type\":\"docs_support\",\"owned_paths\":[\"tests/fixtures/protocol-doc.md\"],"
                        "\"read_only_paths\":[],\"forbidden_actions\":[\"Do not edit source\"],"
                        "\"verification_steps\":[\"Run rtk git diff --check\"],"
                        "\"deliverable_fields\":[\"changed_sections\",\"verification\",\"caveats\"],"
                        "\"completion_rule\":\"stop after verification\",\"escalation_rule\":\"stop on scope drift\"}"
                    ),
                }
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "exec_command",
                    "description": "Run a shell command",
                    "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
                }
            ],
        }
        payload, base_messages, _, _, reverse = app.prepare_chat_payload(body)
        assert "tools" in payload, payload
        assert reverse != {}, reverse
        assert "raw OSS write handoff is deprecated" not in base_messages[0]["content"], base_messages
    finally:
        if old is None:
            os.environ.pop("OSS_LEGACY_DIRECT_WRITES", None)
        else:
            os.environ["OSS_LEGACY_DIRECT_WRITES"] = old


def assert_installed_oss_agent_names_map_to_provider_models():
    assert map_model("oss_flash_support", DEFAULT_MODEL_MAP) == "deepseek-v4-flash"
    assert map_model("oss_deepseek_pro", DEFAULT_MODEL_MAP) == "deepseek-v4-pro"
    assert map_model("oss_kimi_rapid", DEFAULT_MODEL_MAP) == "kimi-k2.6"


def assert_bounded_write_handoffs_can_be_disabled_explicitly():
    old = os.environ.get("OSS_LEGACY_DIRECT_WRITES")
    os.environ["OSS_LEGACY_DIRECT_WRITES"] = "0"
    try:
        app = ProxyApp()
        body = {
            "model": "ocg-deepseek-v4-flash",
            "input": [
                {
                    "role": "user",
                    "content": (
                        "OSS_HANDOFF_JSON:\n"
                        "{\"schema_version\":1,\"role\":\"Patch docs\",\"goal\":\"Patch one fixture\","
                        "\"task_type\":\"docs_support\",\"owned_paths\":[\"tests/fixtures/protocol-doc.md\"],"
                        "\"read_only_paths\":[],\"forbidden_actions\":[\"Do not edit source\"],"
                        "\"verification_steps\":[\"Run rtk git diff --check\"],"
                        "\"deliverable_fields\":[\"changed_sections\",\"verification\",\"caveats\"],"
                        "\"completion_rule\":\"stop after verification\",\"escalation_rule\":\"stop on scope drift\"}"
                    ),
                }
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "exec_command",
                    "description": "Run a shell command",
                    "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
                }
            ],
        }
        payload, base_messages, _, _, reverse = app.prepare_chat_payload(body)
        assert "tools" not in payload, payload
        assert reverse == {}, reverse
        assert "raw OSS write handoff is deprecated" in base_messages[0]["content"], base_messages
    finally:
        if old is None:
            os.environ.pop("OSS_LEGACY_DIRECT_WRITES", None)
        else:
            os.environ["OSS_LEGACY_DIRECT_WRITES"] = old


def assert_fresh_raw_write_handoff_demotes_when_disabled():
    old = os.environ.get("OSS_LEGACY_DIRECT_WRITES")
    os.environ["OSS_LEGACY_DIRECT_WRITES"] = "0"
    try:
        class FakeHandler:
            _handle_fresh_turn = Handler._handle_fresh_turn
            _emit_raw_write_demotion_if_needed = Handler._emit_raw_write_demotion_if_needed

            def __init__(self):
                self.wfile = __import__("io").BytesIO()
                self.statuses = []
                self.headers = []
                self.close_connection = False
                self.upstream_called = False

            def send_response(self, status):
                self.statuses.append(status)

            def send_header(self, key, value):
                self.headers.append((key, value))

            def end_headers(self):
                pass

            def _call_upstream_with_fallback(self, payload, model_alias, model_upstream):
                self.upstream_called = True
                raise AssertionError("fresh raw write demotion must not call upstream")

        body = {
            "model": "ocg-deepseek-v4-pro",
            "stream": True,
            "input": [{
                "role": "user",
                "content": (
                    "OSS_HANDOFF_JSON:\n"
                    "{\"schema_version\":1,\"role\":\"Bounded writer\",\"goal\":\"Append a scratch marker\","
                    "\"task_type\":\"bounded_write\",\"owned_paths\":[\"tmp/protocol-write.txt\"],"
                    "\"read_only_paths\":[\"tmp/protocol-write.txt\"],\"forbidden_actions\":[\"Do not edit other files\"],"
                    "\"verification_steps\":[\"Read back tmp/protocol-write.txt\"],"
                    "\"deliverable_fields\":[\"file changed\",\"verification\",\"confidence\",\"caveats\"],"
                    "\"completion_rule\":\"stop after verification\",\"escalation_rule\":\"stop on scope drift\"}"
                ),
            }],
            "tools": [{
                "type": "function",
                "name": "exec_command",
                "description": "Run a shell command",
                "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
            }],
        }
        fake = FakeHandler()
        fake._handle_fresh_turn(body)
        text = fake.wfile.getvalue().decode("utf-8")
        assert fake.statuses == [200], fake.statuses
        assert not fake.upstream_called
        assert "DETERMINISTIC_LEGACY_WRITE_DEMOTED" in text, text
        assert "response.completed" in text, text
        assert "MissionV1 A4/A5/A6" in text, text
        assert "I'll read" not in text and "I’ll read" not in text, text
    finally:
        if old is None:
            os.environ.pop("OSS_LEGACY_DIRECT_WRITES", None)
        else:
            os.environ["OSS_LEGACY_DIRECT_WRITES"] = old


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


def assert_declared_absolute_read_paths_are_supported():
    with tempfile.TemporaryDirectory() as tmp, tempfile.NamedTemporaryFile("w", delete=False, suffix=".md") as outside:
        outside.write("# Global Skill\n\nNative-style subagents can inspect explicitly declared absolute read-only context.\n")
        outside.flush()
        outside_path = outside.name
        try:
            handoff = (
                "OSS_HANDOFF_JSON:\n"
                + json.dumps({
                    "schema_version": 1,
                    "role": "Read scout",
                    "goal": "Inspect one project source and one explicit global source",
                    "task_type": "scout",
                    "owned_paths": [],
                    "read_only_paths": ["README.md", outside_path],
                    "forbidden_actions": ["Do not edit files"],
                    "verification_steps": ["Read both declared sources"],
                    "deliverable_fields": ["files inspected", "confidence", "caveats"],
                    "completion_rule": "stop after report",
                    "escalation_rule": "stop if source pack is partial",
                })
            )
            Path(tmp, "README.md").write_text("# Project Readme\n", encoding="utf-8")
            session = build_task_session({"input": []}, handoff, "resp_absolute_read")
            pack = build_context_pack(session, tmp)
            assert "=== README.md" in pack, pack
            assert f"=== {outside_path}" in pack, pack
            assert "path escapes project root" not in pack, pack
            exit_code, output = _default_local_read_executor(outside_path, tmp)
            assert exit_code == 0, output
            assert "Global Skill" in output, output
            envelope = parse_task_envelope(handoff)
            coverage_ok, missing, covered_paths, _ = evaluate_evidence_coverage(envelope, pack)
            assert coverage_ok, missing
            assert outside_path in covered_paths, covered_paths
        finally:
            try:
                os.unlink(outside_path)
            except FileNotFoundError:
                pass


def assert_bounded_write_handoff_is_not_read_floor_only():
    handoff = (
        "OSS_HANDOFF_JSON:\n"
        + json.dumps({
            "schema_version": 1,
            "role": "Bounded implementation smoke worker",
            "goal": "Append one line to tmp/oss_impl_parity_smoke.txt.",
            "task_type": "bounded_write",
            "owned_paths": ["tmp/oss_impl_parity_smoke.txt"],
            "read_only_paths": ["tmp/oss_impl_parity_smoke.txt"],
            "forbidden_actions": ["Do not edit any other file"],
            "verification_steps": [
                "Read tmp/oss_impl_parity_smoke.txt after the edit and confirm marker is present"
            ],
            "deliverable_fields": ["files inspected", "files changed", "verification", "confidence", "caveats"],
            "completion_rule": "stop after verification",
            "escalation_rule": "stop on scope drift",
        })
    )
    envelope = parse_task_envelope(handoff)
    assert select_mode(envelope) == "bounded_write_patch", envelope
    assert not declared_read_floor_only(envelope), envelope
    child = StoredResponse(
        response_id="resp_write_child",
        model_alias="ocg-deepseek-v4-pro",
        model_upstream="deepseek-v4-pro",
        messages=[
            {"role": "user", "content": handoff},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_read_before_write",
                "type": "function",
                "function": {
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "rtk read tmp/oss_impl_parity_smoke.txt"}),
                },
            }]},
        ],
        pending_call_ids=["call_read_before_write"],
        created_at=1,
        tool_exchange_count=1,
        task_max_exchanges=6,
        previous_response_id="resp_parent",
        pending_replay_count=3,
    )
    report = complete_pending_reads_from_bridge(
        parent_response_id="resp_parent",
        child_state=child,
        handoff_text=handoff,
        project_root=ROOT,
        executor=lambda path, workdir: (0, "initial\n"),
        finalizer_call=lambda prompt, timeout: (
            "Findings: the file was read.\nConfidence: HIGH\nCaveats: write was not attempted."
        ),
    )
    assert report == "", report


def assert_pretool_blocks_get_repair_turn_before_bounded_patch_terminalization():
    output = (
        "Command blocked by PreToolUse hook: use `rtk read ...` instead of raw `tail`. "
        "Command: echo marker >> tmp/oss_impl_parity_smoke.txt && tail -1 tmp/oss_impl_parity_smoke.txt"
    )
    repair = pretool_block_repair_instruction(output)
    assert repair, repair
    assert should_retry_pretool_block(output, exit_code=1, turn=1, max_exchanges=6)
    assert not should_retry_pretool_block(output, exit_code=1, turn=6, max_exchanges=6)
    assert not should_retry_pretool_block("normal test failure", exit_code=1, turn=1, max_exchanges=6)


def assert_pending_owned_append_can_complete_server_side():
    with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
        rel = os.path.relpath(os.path.join(tmp, "owned.txt"), ROOT)
        Path(ROOT, rel).write_text("initial\n", encoding="utf-8")
        handoff = (
            "OSS_HANDOFF_JSON:\n"
            + json.dumps({
                "schema_version": 1,
                "role": "Bounded implementation smoke worker",
                "goal": f"Append marker to {rel}.",
                "task_type": "bounded_write",
                "owned_paths": [rel],
                "read_only_paths": [rel],
                "forbidden_actions": ["Do not edit any other file"],
                "verification_steps": [f"Append OWNED_APPEND_OK to {rel}", f"Read {rel}"],
                "deliverable_fields": ["files changed", "verification", "confidence", "caveats"],
                "completion_rule": "stop after verification",
                "escalation_rule": "stop on scope drift",
            })
        )
        child = StoredResponse(
            response_id="resp_owned_write_child",
            model_alias="ocg-deepseek-v4-pro",
            model_upstream="deepseek-v4-pro",
            messages=[
                {"role": "user", "content": handoff},
                {"role": "assistant", "content": "", "tool_calls": [{
                    "id": "call_append",
                    "type": "function",
                    "function": {
                        "name": "exec_command",
                        "arguments": json.dumps({"cmd": f'echo "OWNED_APPEND_OK" >> {rel}'}),
                    },
                }]},
            ],
            pending_call_ids=["call_append"],
            created_at=1,
            tool_exchange_count=1,
            task_max_exchanges=6,
            previous_response_id="resp_parent",
            pending_replay_count=3,
        )
        report = execute_pending_owned_write_from_bridge(
            parent_response_id="resp_parent",
            child_state=child,
            handoff_text=handoff,
            project_root=ROOT,
        )
        assert report.startswith("PASS"), report
        assert "owned path write completed server-side" in report, report
        assert "OWNED_APPEND_OK" in Path(ROOT, rel).read_text(encoding="utf-8")


def assert_pending_owned_append_recovery_is_idempotent_for_ignored_paths():
    rel = "tmp/protocol-owned-write-recovery.txt"
    target = Path(ROOT, rel)
    target.parent.mkdir(exist_ok=True)
    try:
        target.write_text("initial\n", encoding="utf-8")
        handoff = (
            "OSS_HANDOFF_JSON:\n"
            + json.dumps({
                "schema_version": 1,
                "role": "Bounded implementation smoke worker",
                "goal": f"Append marker to {rel}.",
                "task_type": "bounded_write",
                "owned_paths": [rel],
                "read_only_paths": [rel],
                "forbidden_actions": ["Do not edit any other file"],
                "verification_steps": [f"Append OWNED_IGNORED_OK to {rel}", f"Read {rel}"],
                "deliverable_fields": ["files changed", "verification", "confidence", "caveats"],
                "completion_rule": "stop after verification",
                "escalation_rule": "stop on scope drift",
            })
        )
        child = StoredResponse(
            response_id="resp_owned_write_ignored_child",
            model_alias="ocg-deepseek-v4-pro",
            model_upstream="deepseek-v4-pro",
            messages=[
                {"role": "user", "content": handoff},
                {"role": "assistant", "content": "", "tool_calls": [{
                    "id": "call_append_ignored",
                    "type": "function",
                    "function": {
                        "name": "exec_command",
                        "arguments": json.dumps({"cmd": f'echo "OWNED_IGNORED_OK" >> {rel}'}),
                    },
                }]},
            ],
            pending_call_ids=["call_append_ignored"],
            created_at=1,
            tool_exchange_count=1,
            task_max_exchanges=6,
            previous_response_id="resp_parent",
            pending_replay_count=3,
        )
        first = execute_pending_owned_write_from_bridge(
            parent_response_id="resp_parent",
            child_state=child,
            handoff_text=handoff,
            project_root=ROOT,
        )
        second = execute_pending_owned_write_from_bridge(
            parent_response_id="resp_parent",
            child_state=child,
            handoff_text=handoff,
            project_root=ROOT,
        )
        text = target.read_text(encoding="utf-8")
        assert first.startswith("PASS"), first
        assert second.startswith("PASS"), second
        assert "Changed owned paths: tmp/protocol-owned-write-recovery.txt" in first, first
        assert text.count("OWNED_IGNORED_OK") == 1, text
    finally:
        try:
            target.unlink()
        except FileNotFoundError:
            pass


def assert_shell_append_parser_supports_common_native_forms():
    assert _append_redirection_from_shell_command('echo "APPEND_OK" >> tmp/file.txt') == (
        "tmp/file.txt", "APPEND_OK\n"
    )
    assert _append_redirection_from_shell_command(
        "printf 'APPEND_OK\\n' >> tmp/file.txt && sed -n '/APPEND_OK/p' tmp/file.txt"
    ) == ("tmp/file.txt", "APPEND_OK\n")


def assert_pending_owned_verification_without_mutation_cannot_pass():
    rel = "tmp/protocol-owned-verify-recovery.txt"
    target = Path(ROOT, rel)
    target.parent.mkdir(exist_ok=True)
    try:
        target.write_text("initial\nOWNED_VERIFY_OK\n", encoding="utf-8")
        handoff = (
            "OSS_HANDOFF_JSON:\n"
            + json.dumps({
                "schema_version": 1,
                "role": "Bounded implementation smoke worker",
                "goal": f"Verify OWNED_VERIFY_OK in {rel}.",
                "task_type": "bounded_write",
                "owned_paths": [rel],
                "read_only_paths": [rel],
                "forbidden_actions": ["Do not edit any other file"],
                "verification_steps": [f"Search {rel} for OWNED_VERIFY_OK"],
                "deliverable_fields": ["files changed", "verification", "confidence", "caveats"],
                "completion_rule": "stop after verification",
                "escalation_rule": "stop on scope drift",
            })
        )
        child = StoredResponse(
            response_id="resp_owned_verify_child",
            model_alias="ocg-deepseek-v4-pro",
            model_upstream="deepseek-v4-pro",
            messages=[
                {"role": "user", "content": handoff},
                {"role": "assistant", "content": "", "tool_calls": [{
                    "id": "call_verify",
                    "type": "function",
                    "function": {
                        "name": "exec_command",
                        "arguments": json.dumps({"cmd": f'rg "OWNED_VERIFY_OK" "{rel}"'}),
                    },
                }]},
            ],
            pending_call_ids=["call_verify"],
            created_at=1,
            tool_exchange_count=1,
            task_max_exchanges=6,
            previous_response_id="resp_parent",
            pending_replay_count=3,
        )
        report = execute_pending_owned_verification_from_bridge(
            parent_response_id="resp_parent",
            child_state=child,
            handoff_text=handoff,
            project_root=ROOT,
        )
        assert not report.startswith("PASS"), report
        assert "Verification status: observed" in report, report
        assert "OWNED_VERIFY_OK" in report, report
        assert "Changed owned paths: none" in report, report
        assert "read-only verification cannot certify owned-path mutation" in report, report
    finally:
        try:
            target.unlink()
        except FileNotFoundError:
            pass


def assert_pending_owned_verification_can_complete_declared_marker_write():
    rel = "tmp/protocol-owned-declared-marker-recovery.txt"
    target = Path(ROOT, rel)
    target.parent.mkdir(exist_ok=True)
    try:
        target.write_text("initial\n", encoding="utf-8")
        handoff = (
            "OSS_HANDOFF_JSON:\n"
            + json.dumps({
                "schema_version": 1,
                "role": "Bounded implementation smoke worker",
                "goal": f"Add DECLARED_MARKER_OK to {rel}.",
                "task_type": "bounded_write",
                "owned_paths": [rel],
                "read_only_paths": [rel],
                "forbidden_actions": ["Do not edit any other file"],
                "verification_steps": [
                    f"Write DECLARED_MARKER_OK to {rel} if absent",
                    f"Read {rel} and confirm DECLARED_MARKER_OK",
                ],
                "deliverable_fields": ["files changed", "verification", "confidence", "caveats"],
                "completion_rule": "stop after verification",
                "escalation_rule": "stop on scope drift",
                "write_allowed": True,
            })
        )
        child = StoredResponse(
            response_id="resp_owned_marker_child",
            model_alias="ocg-deepseek-v4-pro",
            model_upstream="deepseek-v4-pro",
            messages=[
                {"role": "user", "content": handoff},
                {"role": "assistant", "content": "", "tool_calls": [{
                    "id": "call_verify_absent_marker",
                    "type": "function",
                    "function": {
                        "name": "exec_command",
                        "arguments": json.dumps({"cmd": f'rtk read "{rel}"'}),
                    },
                }]},
            ],
            pending_call_ids=["call_verify_absent_marker"],
            created_at=1,
            tool_exchange_count=1,
            task_max_exchanges=6,
            previous_response_id="resp_parent",
            pending_replay_count=3,
        )
        report = execute_pending_owned_verification_from_bridge(
            parent_response_id="resp_parent",
            child_state=child,
            handoff_text=handoff,
            project_root=ROOT,
        )
        assert report.startswith("PASS"), report
        assert "declared owned marker write completed server-side" in report, report
        assert f"Changed owned paths: {rel}" in report, report
        assert target.read_text(encoding="utf-8").count("DECLARED_MARKER_OK") == 1
    finally:
        try:
            target.unlink()
        except FileNotFoundError:
            pass


def assert_read_only_verification_cannot_certify_owned_mutation():
    rel_owned = "tmp/protocol-owned-missing-mutation.txt"
    rel_read_only = "tmp/protocol-readonly-marker.txt"
    owned = Path(ROOT, rel_owned)
    readonly = Path(ROOT, rel_read_only)
    owned.parent.mkdir(exist_ok=True)
    try:
        owned.write_text("owned unchanged\n", encoding="utf-8")
        readonly.write_text("READONLY_MARKER_OK\n", encoding="utf-8")
        handoff = (
            "OSS_HANDOFF_JSON:\n"
            + json.dumps({
                "schema_version": 1,
                "role": "Bounded implementation smoke worker",
                "goal": f"Change {rel_owned}, then verify READONLY_MARKER_OK in {rel_read_only}.",
                "task_type": "bounded_write",
                "owned_paths": [rel_owned],
                "read_only_paths": [rel_read_only],
                "forbidden_actions": ["Do not edit any other file"],
                "verification_steps": [f"Search {rel_read_only} for READONLY_MARKER_OK"],
                "deliverable_fields": ["files changed", "verification", "confidence", "caveats"],
                "completion_rule": "stop after verification",
                "escalation_rule": "stop on scope drift",
                "write_allowed": True,
            })
        )
        child = StoredResponse(
            response_id="resp_readonly_verify_child",
            model_alias="ocg-deepseek-v4-pro",
            model_upstream="deepseek-v4-pro",
            messages=[
                {"role": "user", "content": handoff},
                {"role": "assistant", "content": "", "tool_calls": [{
                    "id": "call_readonly_verify",
                    "type": "function",
                    "function": {
                        "name": "exec_command",
                        "arguments": json.dumps({"cmd": f'rg "READONLY_MARKER_OK" "{rel_read_only}"'}),
                    },
                }]},
            ],
            pending_call_ids=["call_readonly_verify"],
            created_at=1,
            tool_exchange_count=1,
            task_max_exchanges=6,
            previous_response_id="resp_parent",
            pending_replay_count=3,
        )
        report = execute_pending_owned_verification_from_bridge(
            parent_response_id="resp_parent",
            child_state=child,
            handoff_text=handoff,
            project_root=ROOT,
        )
        assert not report.startswith("PASS"), report
        assert "Changed owned paths: none" in report, report
        assert "read-only verification cannot certify owned-path mutation" in report, report
    finally:
        for path in (owned, readonly):
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def assert_pending_owned_generic_shell_can_complete_multi_file_write_server_side():
    with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
        rel_a = os.path.relpath(os.path.join(tmp, "owned-a.txt"), ROOT)
        rel_b = os.path.relpath(os.path.join(tmp, "owned-b.txt"), ROOT)
        command = (
            "python3 -c "
            + json.dumps(
                "from pathlib import Path; "
                f"Path({rel_a!r}).write_text('GENERIC_MULTI_OK a\\n', encoding='utf-8'); "
                f"Path({rel_b!r}).write_text('GENERIC_MULTI_OK b\\n', encoding='utf-8')"
            )
        )
        handoff = (
            "OSS_HANDOFF_JSON:\n"
            + json.dumps({
                "schema_version": 1,
                "role": "Bounded implementation worker",
                "goal": "Write the requested marker to two owned files.",
                "task_type": "bounded_write",
                "owned_paths": [rel_a, rel_b],
                "read_only_paths": [rel_a, rel_b],
                "forbidden_actions": ["Do not edit any other file"],
                "verification_steps": ["Confirm GENERIC_MULTI_OK appears in both owned files"],
                "deliverable_fields": ["files changed", "verification", "confidence", "caveats"],
                "completion_rule": "stop after verification",
                "escalation_rule": "stop on scope drift",
            })
        )
        child = StoredResponse(
            response_id="resp_owned_generic_shell_child",
            model_alias="ocg-deepseek-v4-pro",
            model_upstream="deepseek-v4-pro",
            messages=[
                {"role": "user", "content": handoff},
                {"role": "assistant", "content": "", "tool_calls": [{
                    "id": "call_generic_shell",
                    "type": "function",
                    "function": {
                        "name": "exec_command",
                        "arguments": json.dumps({"cmd": command}),
                    },
                }]},
            ],
            pending_call_ids=["call_generic_shell"],
            created_at=1,
            tool_exchange_count=1,
            task_max_exchanges=6,
            previous_response_id="resp_parent",
            pending_replay_count=3,
        )
        report = execute_pending_owned_shell_from_bridge(
            parent_response_id="resp_parent",
            child_state=child,
            handoff_text=handoff,
            project_root=ROOT,
        )
        assert report.startswith("PASS"), report
        assert "owned path changes completed server-side" in report, report
        assert f"Changed owned paths: {rel_a}, {rel_b}" in report, report
        assert "GENERIC_MULTI_OK a" in Path(ROOT, rel_a).read_text(encoding="utf-8")
        assert "GENERIC_MULTI_OK b" in Path(ROOT, rel_b).read_text(encoding="utf-8")


def assert_streaming_tool_calls_initialize_adoption_state():
    stored = []
    events = []

    def new_id(prefix):
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}{counters[prefix]}"

    counters = {}
    assembler = ChatStreamAssembler(
        body={"previous_response_id": "resp_parent"},
        base_messages=[{"role": "user", "content": "run tool"}],
        model_alias="oss_kimi_rapid",
        model_upstream="kimi",
        reverse_name_map={},
        response_id="resp_stream_adoption",
        created_at=1,
        write_sse=lambda event, payload: events.append((event, payload)),
        write_progress=lambda note: None,
        state_put=lambda state: stored.append(state),
        stored_response_factory=StoredResponse,
        build_response_shell=lambda body, model_alias, response_id, created_at, status, output: {
            "id": response_id,
            "created_at": created_at,
            "status": status,
            "model": model_alias,
            "output": output,
        },
        repair_chat_history=lambda messages, _: messages,
        extract_budget=lambda messages: 4,
        restore_tool_name=lambda name, reverse: name,
        new_id=new_id,
        json_dumps=json.dumps,
        as_text=str,
    )
    assembler.on_tool_call_delta({
        "index": 0,
        "id": "call_stream_adopt",
        "function": {"name": "exec_command", "arguments": json.dumps({"cmd": "echo ok"})},
    })
    assembler.finalize()
    assert stored, "stream finalization must persist response state"
    payload = json.loads(stored[-1].adoption_probes_json)
    assert "call_stream_adopt" in payload["calls"], payload
    assert payload["calls"]["call_stream_adopt"]["completed"] is False, payload
    assert payload["calls"]["call_stream_adopt"]["adopted"] is False, payload


def assert_pending_owned_generic_shell_rejects_outside_workdir():
    with tempfile.TemporaryDirectory(dir=ROOT) as tmp, tempfile.TemporaryDirectory() as outside:
        rel = os.path.relpath(os.path.join(tmp, "owned.txt"), ROOT)
        handoff = (
            "OSS_HANDOFF_JSON:\n"
            + json.dumps({
                "schema_version": 1,
                "role": "Bounded implementation worker",
                "goal": "Write only inside owned file.",
                "task_type": "bounded_write",
                "owned_paths": [rel],
                "read_only_paths": [rel],
                "forbidden_actions": ["Do not edit any other file"],
                "verification_steps": ["Confirm OUTSIDE_WORKDIR_BLOCKED does not bypass scope"],
                "deliverable_fields": ["files changed", "verification", "confidence", "caveats"],
                "completion_rule": "stop after verification",
                "escalation_rule": "stop on scope drift",
            })
        )
        child = StoredResponse(
            response_id="resp_owned_outside_workdir_child",
            model_alias="ocg-deepseek-v4-pro",
            model_upstream="deepseek-v4-pro",
            messages=[
                {"role": "user", "content": handoff},
                {"role": "assistant", "content": "", "tool_calls": [{
                    "id": "call_outside_workdir",
                    "type": "function",
                    "function": {
                        "name": "exec_command",
                        "arguments": json.dumps({
                            "cmd": "python3 -c \"print('outside')\"",
                            "workdir": outside,
                        }),
                    },
                }]},
            ],
            pending_call_ids=["call_outside_workdir"],
            created_at=1,
            tool_exchange_count=1,
            task_max_exchanges=6,
            previous_response_id="resp_parent",
            pending_replay_count=3,
        )
        report = execute_pending_owned_shell_from_bridge(
            parent_response_id="resp_parent",
            child_state=child,
            handoff_text=handoff,
            project_root=ROOT,
        )
        assert report.startswith("FAIL"), report
        assert "outside the project root" in report, report


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
    assert tool_output_indicates_failure("Command blocked by PreToolUse hook: use rtk read instead of raw cat.")
    assert not tool_output_indicates_failure("Process exited with code 0\nAll protocol checks passed")
    repair = pretool_block_repair_instruction("Command blocked by PreToolUse hook: use `rtk read ...` instead of raw `cat`.")
    assert "rtk read" in repair and "final answer" in repair, repair


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


def assert_tool_call_turns_do_not_project_progress_as_terminal_messages():
    captured = []

    def stored_response_factory(**kwargs):
        return kwargs

    body = {"input": []}
    chat_resp = {
        "choices": [{
            "message": {
                "content": "Step 1 complete. Running step 2.",
                "tool_calls": [{
                    "id": "call_next",
                    "type": "function",
                    "function": {
                        "name": "exec_command",
                        "arguments": '{"cmd":"rtk read docs/CONTINUITY.md"}',
                    },
                }],
            },
        }],
    }
    resp = build_response_object_from_chat(
        body=body,
        chat_resp=chat_resp,
        base_messages=[{"role": "user", "content": "task"}],
        model_alias="ocg-test",
        model_upstream="test",
        reverse_name_map={},
        state_put=captured.append,
        stored_response_factory=stored_response_factory,
        repair_chat_history=lambda messages, outputs: list(messages),
        extract_budget=lambda messages: 6,
        restore_tool_name=lambda name, reverse: name,
        new_id=lambda prefix: f"{prefix}_1",
        now=lambda: 1,
        json_dumps=json.dumps,
        as_text=str,
        response_id="resp_tool_projection",
        created_at=1,
    )
    assert [item["type"] for item in resp["output"]] == ["function_call"], resp["output"]
    assert resp["output"][0]["call_id"] == "call_next", resp
    assert captured[-1]["messages"][-1]["content"] == "Step 1 complete. Running step 2.", captured[-1]

    captured.clear()
    sse_events = []
    assembler = ChatStreamAssembler(
        body=body,
        base_messages=[{"role": "user", "content": "task"}],
        model_alias="ocg-test",
        model_upstream="test",
        reverse_name_map={},
        response_id="resp_stream_tool_projection",
        created_at=1,
        write_sse=lambda event, data: sse_events.append((event, data)),
        write_progress=lambda note: None,
        state_put=captured.append,
        stored_response_factory=stored_response_factory,
        build_response_shell=lambda *args, **kwargs: {"output": kwargs.get("output", [])},
        repair_chat_history=lambda messages, outputs: list(messages),
        extract_budget=lambda messages: 6,
        restore_tool_name=lambda name, reverse: name,
        new_id=lambda prefix: f"{prefix}_1",
        json_dumps=json.dumps,
        as_text=str,
    )
    assembler.on_chunk({"choices": [{"delta": {"content": "Step 1 complete. Running step 2."}}]})
    assembler.on_chunk({"choices": [{"delta": {"tool_calls": [{
        "index": 0,
        "id": "call_stream_next",
        "function": {
            "name": "exec_command",
            "arguments": '{"cmd":"rtk read docs/CONTINUITY.md"}',
        },
    }]}}]})
    stream_resp = assembler.finalize()
    assert [item["type"] for item in stream_resp["output"]] == ["function_call"], stream_resp["output"]
    assert stream_resp["output"][0]["call_id"] == "call_stream_next", stream_resp
    done_events = [
        data
        for event, data in sse_events
        if event == "response.function_call_arguments.done"
    ]
    assert done_events, sse_events
    assert done_events[-1]["name"] == "exec_command", done_events[-1]
    assert "call_id" not in done_events[-1], done_events[-1]
    item_done_events = [
        data
        for event, data in sse_events
        if event == "response.output_item.done"
    ]
    assert item_done_events[-1]["item"]["call_id"] == "call_stream_next", item_done_events[-1]
    assert not any(
        event == "response.output_item.added"
        and isinstance(data, dict)
        and (data.get("item") or {}).get("type") == "message"
        for event, data in sse_events
    ), sse_events
    assert captured[-1]["messages"][-1]["content"] == "Step 1 complete. Running step 2.", captured[-1]


def assert_pending_child_sse_replay_is_adoptable_shape():
    class FakeHandler:
        _emit_sse_items_and_completed = Handler._emit_sse_items_and_completed
        _compact_completed_response = Handler._compact_completed_response

        def __init__(self):
            self.events = []
            self.close_connection = False

        def _send_sse_headers(self):
            pass

        def _write_sse(self, event, data):
            self.events.append((event, data))

        def _safe_write(self, data):
            self.events.append(("[DONE]", data.decode("utf-8")))

    resp = {
        "id": "resp_child",
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": "ocg-test",
        "previous_response_id": "resp_parent",
        "output": [{
            "type": "function_call",
            "id": "fc_second",
            "call_id": "call_second",
            "name": "exec_command",
            "arguments": '{"cmd":"rtk read docs/CONTINUITY.md"}',
            "status": "completed",
        }],
    }
    fake = FakeHandler()
    Handler._send_sse(fake, resp)
    semantic = [(event, data) for event, data in fake.events if event != "[DONE]"]
    names = [event for event, _ in semantic]
    assert names == [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.output_item.done",
        "response.completed",
    ], names
    sequence_numbers = [data["sequence_number"] for _, data in semantic]
    assert sequence_numbers == list(range(1, len(semantic) + 1)), sequence_numbers
    assert semantic[0][1]["response"]["status"] == "in_progress", semantic[0]
    assert semantic[0][1]["response"]["output"] == [], semantic[0]
    assert semantic[1][1]["response"]["previous_response_id"] == "resp_parent", semantic[1]
    done_args = semantic[4][1]
    assert done_args["arguments"] == '{"cmd":"rtk read docs/CONTINUITY.md"}', done_args
    assert "call_id" not in done_args, done_args
    assert semantic[5][1]["item"]["call_id"] == "call_second", semantic[5]
    assert semantic[6][1]["response"]["status"] == "completed", semantic[6]
    assert semantic[6][1]["response"]["previous_response_id"] == "resp_parent", semantic[6]
    assert fake.close_connection is True


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


def assert_direct_loop_detects_repeated_completed_source():
    messages = [
        {
            "role": "user",
            "content": (
                "OSS_HANDOFF_JSON:\n"
                '{"schema_version":1,"role":"Scout","goal":"Read required files",'
                '"task_type":"scout","read_only_paths":["/Users/goldtetsola/.codex/skills/napkin/SKILL.md",'
                '".codex/napkin.md","docs/CONTINUITY.md"],"deliverable_fields":["confidence","caveats"]}'
            ),
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_a",
                "type": "function",
                "function": {
                    "name": "rtk_read",
                    "arguments": '{"path":"/Users/goldtetsola/.codex/skills/napkin/SKILL.md"}',
                },
            }],
        },
        {"role": "tool", "tool_call_id": "call_a", "content": "skill"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_b",
                "type": "function",
                "function": {"name": "rtk_read", "arguments": '{"path":".codex/napkin.md"}'},
            }],
        },
        {"role": "tool", "tool_call_id": "call_b", "content": "napkin"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_c",
                "type": "function",
                "function": {"name": "rtk_read", "arguments": '{"path":".codex/napkin.md"}'},
            }],
        },
    ]
    completed = _extract_completed_read_paths_from_history(messages)
    envelope = parse_task_envelope(messages[0]["content"])
    required = required_paths_from_envelope(envelope, messages[0]["content"])
    assert ".codex/napkin.md" in completed, completed
    assert required == [
        "/Users/goldtetsola/.codex/skills/napkin/SKILL.md",
        ".codex/napkin.md",
        "docs/CONTINUITY.md",
    ], required
    decision, remaining = direct_loop_terminal_decision(
        required,
        completed,
        ".codex/napkin.md",
        turn=3,
        max_exchanges=6,
    )
    assert decision == "repeated_completed_read", (decision, remaining)
    assert remaining == ["docs/CONTINUITY.md"], remaining
    report = build_direct_loop_terminal_report(
        reason=decision,
        model_alias="ocg-deepseek-v4-pro",
        completed_paths=completed,
        current_path=".codex/napkin.md",
        remaining_paths=remaining,
        turn=3,
        max_exchanges=6,
    )
    assert "DETERMINISTIC_DIRECT_LOOP_TERMINAL" in report, report
    assert "docs/CONTINUITY.md" in report, report


def assert_pending_child_replay_is_idempotent_and_terminalizable():
    handoff_obj = {
        "schema_version": 1,
        "role": "Scout",
        "goal": "Read required files",
        "task_type": "scout",
        "owned_paths": [],
        "read_only_paths": [".codex/napkin.md", "docs/CONTINUITY.md"],
        "forbidden_actions": ["do not write files"],
        "verification_steps": ["read listed files"],
        "deliverable_fields": ["files inspected", "confidence", "caveats"],
        "completion_rule": "stop after deliverable",
        "escalation_rule": "stop if blocked",
    }
    handoff = "OSS_HANDOFF_JSON:\n" + __import__("json").dumps(handoff_obj)
    child_messages = [
        {"role": "user", "content": handoff},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_first",
                "type": "function",
                "function": {"name": "rtk_read", "arguments": '{"path":".codex/napkin.md"}'},
            }],
        },
        {"role": "tool", "tool_call_id": "call_first", "content": "napkin"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_second",
                "type": "function",
                "function": {"name": "rtk_read", "arguments": '{"path":"docs/CONTINUITY.md"}'},
            }],
        },
    ]

    with tempfile.TemporaryDirectory(dir=ROOT) as d:
        stable_output = [{
            "type": "function_call",
            "id": "fc_stable_second",
            "call_id": "call_second",
            "name": "rtk_read",
            "arguments": '{"path":"docs/CONTINUITY.md"}',
            "status": "completed",
        }]
        store = StateStore(str(Path(d) / "state.sqlite3"))
        store.put(StoredResponse(
            response_id="resp_child",
            model_alias="ocg-deepseek-v4-pro",
            model_upstream="deepseek-v4-pro",
            messages=child_messages,
            pending_call_ids=["call_second"],
            created_at=123,
            tool_exchange_count=1,
            task_max_exchanges=6,
            previous_response_id="resp_parent",
            output_items_json=json.dumps(stable_output),
        ))
        child = store.find_pending_child("resp_parent")
        assert child and child.response_id == "resp_child", child
        replay = build_response_from_pending_child({"previous_response_id": "resp_parent"}, child)
        replay_again = build_response_from_pending_child({"previous_response_id": "resp_parent"}, child)
        assert replay["id"] == "resp_child", replay
        assert replay["output"][0] == stable_output[0], replay
        assert replay_again["output"][0] == stable_output[0], replay_again
        assert replay["output"][0]["call_id"] == "call_second", replay
        assert replay["output"][0]["arguments"] == '{"path":"docs/CONTINUITY.md"}', replay
        assert store.increment_pending_replay_count("resp_child") == 1
        child = store.get("resp_child")
        report = build_pending_child_not_fulfilled_report(
            parent_response_id="resp_parent",
            child_state=child,
            handoff_text=handoff,
        )
        assert "DETERMINISTIC_PENDING_TOOL_ADOPTION_FAILURE" in report, report
        assert "Pending command: docs/CONTINUITY.md" in report, report


def assert_semantic_pending_replays_survive_fresh_call_ids():
    output = lambda call_id: json.dumps([{
        "type": "function_call",
        "id": "fc_" + call_id,
        "call_id": call_id,
        "name": "exec_command",
        "arguments": '{"cmd":"rtk read docs/visible-commentary.md"}',
        "status": "completed",
    }])
    with tempfile.TemporaryDirectory(dir=ROOT) as d:
        store = StateStore(str(Path(d) / "state.sqlite3"))
        for idx in range(3):
            call_id = f"call_{idx}"
            store.put(StoredResponse(
                response_id=f"resp_child_{idx}",
                model_alias="ocg-deepseek-v4-flash",
                model_upstream="deepseek-v4-flash",
                messages=[{"role": "assistant", "content": "", "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "exec_command", "arguments": '{"cmd":"rtk read docs/visible-commentary.md"}'},
                }]}],
                pending_call_ids=[call_id],
                created_at=123 + idx,
                tool_exchange_count=1,
                task_max_exchanges=6,
                previous_response_id="resp_parent",
                output_items_json=output(call_id),
            ))
        latest = store.find_pending_child("resp_parent")
        assert latest and latest.response_id == "resp_child_2", latest
        assert latest.pending_replay_count == 2, latest
        assert store.increment_pending_replay_count(latest.response_id) == 3


def assert_terminal_pending_child_uses_semantic_replay_not_latest_child():
    def output(call_id: str, cmd: str) -> str:
        return json.dumps([{
            "type": "function_call",
            "id": "fc_" + call_id,
            "call_id": call_id,
            "name": "exec_command",
            "arguments": json.dumps({"cmd": cmd}),
            "status": "completed",
        }])

    with tempfile.TemporaryDirectory(dir=ROOT) as d:
        store = StateStore(str(Path(d) / "state.sqlite3"))
        store.put(StoredResponse(
            response_id="resp_replayed_read",
            model_alias="ocg-deepseek-v4-flash",
            model_upstream="deepseek-v4-flash",
            messages=[],
            pending_call_ids=["call_read"],
            created_at=100,
            previous_response_id="resp_parent",
            pending_replay_count=5,
            output_items_json=output("call_read", "rtk read docs/visible-commentary.md"),
        ))
        store.put(StoredResponse(
            response_id="resp_latest_probe",
            model_alias="ocg-deepseek-v4-flash",
            model_upstream="deepseek-v4-flash",
            messages=[],
            pending_call_ids=["call_probe"],
            created_at=101,
            previous_response_id="resp_parent",
            pending_replay_count=0,
            output_items_json=output("call_probe", "which rtk"),
        ))
        latest = store.find_pending_child("resp_parent")
        assert latest and latest.response_id == "resp_latest_probe", latest
        terminal = store.find_terminal_pending_child("resp_parent", 2)
        assert terminal and terminal.response_id == "resp_replayed_read", terminal


def assert_orphan_tool_output_can_bind_pending_child_lineage():
    with tempfile.TemporaryDirectory(dir=ROOT) as d:
        store = StateStore(str(Path(d) / "state.sqlite3"))
        store.put(StoredResponse(
            response_id="resp_parent",
            model_alias="ocg-deepseek-v4-pro",
            model_upstream="deepseek-v4-pro",
            messages=[{"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_parent",
                "type": "function",
                "function": {"name": "rtk_read", "arguments": '{"path":"README.md"}'},
            }]}],
            pending_call_ids=["call_parent"],
            created_at=1,
            tool_exchange_count=0,
            task_max_exchanges=6,
        ))
        recovered = store.find_by_call_ids(["call_parent"])
        body = {"input": [{"type": "function_call_output", "call_id": "call_parent", "output": "README"}]}
        if recovered and not body.get("previous_response_id"):
            body["previous_response_id"] = recovered.response_id
        store.put(StoredResponse(
            response_id="resp_child",
            model_alias="ocg-deepseek-v4-pro",
            model_upstream="deepseek-v4-pro",
            messages=[],
            pending_call_ids=["call_child"],
            created_at=2,
            tool_exchange_count=1,
            task_max_exchanges=6,
            previous_response_id=str(body.get("previous_response_id") or ""),
        ))
        child = store.find_pending_child("resp_parent")
        assert child and child.response_id == "resp_child", child


def assert_pending_child_adoption_failure_can_complete_reads_server_side():
    handoff_obj = {
        "schema_version": 1,
        "role": "Scout",
        "goal": "Read required files",
        "task_type": "scout",
        "owned_paths": [],
        "read_only_paths": ["README.md", "bridge.py", "tests/test_protocol_conformance.py"],
        "forbidden_actions": ["do not write files"],
        "verification_steps": ["read listed files"],
        "deliverable_fields": ["files inspected", "confidence", "caveats"],
        "completion_rule": "stop after deliverable",
        "escalation_rule": "stop if blocked",
    }
    handoff = "OSS_HANDOFF_JSON:\n" + __import__("json").dumps(handoff_obj)
    root_json = __import__("json").dumps(ROOT)
    child = StoredResponse(
        response_id="resp_child",
        model_alias="ocg-deepseek-v4-pro",
        model_upstream="deepseek-v4-pro",
        messages=[
            {"role": "user", "content": handoff},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_readme",
                "type": "function",
                "function": {"name": "exec_command", "arguments": '{"cmd":"rtk read README.md","workdir":' + root_json + '}'},
            }]},
            {"role": "tool", "tool_call_id": "call_readme", "content": "README contents"},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_bridge",
                "type": "function",
                "function": {"name": "exec_command", "arguments": '{"cmd":"rtk read bridge.py","workdir":' + root_json + '}'},
            }]},
        ],
        pending_call_ids=["call_bridge"],
        created_at=1,
        tool_exchange_count=1,
        task_max_exchanges=6,
        previous_response_id="resp_parent",
        pending_replay_count=3,
    )
    executed = []

    def fake_executor(path, workdir):
        executed.append((path, workdir))
        return 0, f"contents for {path}"

    report = complete_pending_reads_from_bridge(
        parent_response_id="resp_parent",
        child_state=child,
        handoff_text=handoff,
        project_root=ROOT,
        executor=fake_executor,
    )
    assert report.startswith("COMPLETE"), report
    assert "DETERMINISTIC_SERVER_SIDE_READ_COMPLETION" in report, report
    assert "No writes performed: true" in report, report
    assert "README.md" in report and "bridge.py" in report, report
    assert "tests/test_protocol_conformance.py" in report, report
    assert executed == [
        ("bridge.py", ROOT),
        ("tests/test_protocol_conformance.py", ROOT),
    ], executed


def assert_server_side_read_completion_prefers_model_authored_report():
    handoff_obj = {
        "schema_version": 1,
        "role": "Scout",
        "goal": "Read visible commentary docs",
        "task_type": "scout",
        "owned_paths": [],
        "read_only_paths": ["docs/visible-commentary.md"],
        "forbidden_actions": ["do not write files"],
        "verification_steps": ["read listed files"],
        "deliverable_fields": ["files inspected", "confidence", "caveats"],
        "completion_rule": "stop after deliverable",
        "escalation_rule": "stop if blocked",
    }
    handoff = "OSS_HANDOFF_JSON:\n" + json.dumps(handoff_obj)
    child = StoredResponse(
        response_id="resp_child_model_final",
        model_alias="ocg-deepseek-v4-flash",
        model_upstream="deepseek-v4-flash",
        messages=[{"role": "user", "content": handoff}],
        pending_call_ids=[],
        created_at=1,
        tool_exchange_count=1,
        task_max_exchanges=6,
        previous_response_id="resp_parent",
        pending_replay_count=3,
    )
    prompts = []

    def fake_executor(path, workdir):
        return 0, "# Visible Commentary\nRuntime-backed agents show progress."

    def fake_finalizer(prompt, timeout):
        prompts.append(prompt)
        return (
            "Findings: The doc describes visible runtime progress for OSS agents.\n"
            "Confidence: HIGH\n"
            "Caveats: The narrative is limited to the provided excerpt."
        )

    report = complete_pending_reads_from_bridge(
        parent_response_id="resp_parent",
        child_state=child,
        handoff_text=handoff,
        project_root=ROOT,
        executor=fake_executor,
        finalizer_call=fake_finalizer,
        finalizer_timeout_seconds=3,
    )
    assert report.startswith("COMPLETE"), report
    assert "MODEL_AUTHORED_SERVER_SIDE_READ_COMPLETION" in report, report
    assert "DETERMINISTIC_SERVER_SIDE_READ_COMPLETION" not in report, report
    assert "Evidence authority: bridge_runtime" in report, report
    assert "Narrative authority: model_finalizer" in report, report
    assert "Files inspected: docs/visible-commentary.md" in report, report
    assert "Model-authored narrative:" in report, report
    assert prompts and "Do not call tools" in prompts[0], prompts
    assert "runtime owns status" in prompts[0].lower(), prompts[0]
    assert "# Visible Commentary" in prompts[0], prompts[0]


def assert_model_authored_pending_recovery_persists_tool_loop_artifacts():
    mission_id = "pending_model_recovery_artifacts"
    with tempfile.TemporaryDirectory() as tmp:
        handoff_obj = {
            "schema_version": 1,
            "role": "Scout",
            "goal": "Read required files",
            "task_type": "scout",
            "owned_paths": [],
            "read_only_paths": ["README.md"],
            "forbidden_actions": ["do not write files"],
            "verification_steps": ["read listed files"],
            "deliverable_fields": ["files inspected", "confidence", "caveats"],
            "completion_rule": "stop after deliverable",
            "escalation_rule": "stop if blocked",
            "mission_id": mission_id,
        }
        handoff = "OSS_HANDOFF_JSON:\n" + json.dumps(handoff_obj)
        child = StoredResponse(
            response_id="resp_child_model_pending",
            model_alias="ocg-kimi-k2.6",
            model_upstream="kimi-k2.6",
            messages=[
                {"role": "user", "content": handoff},
                {"role": "assistant", "content": "", "tool_calls": [{
                    "id": "call_readme_pending",
                    "type": "function",
                    "function": {
                        "name": "exec_command",
                        "arguments": json.dumps({"cmd": "rtk read README.md", "workdir": tmp}),
                    },
                }]},
            ],
            pending_call_ids=["call_readme_pending"],
            created_at=1,
            tool_exchange_count=1,
            task_max_exchanges=6,
            previous_response_id="resp_parent",
            pending_replay_count=3,
        )

        report = complete_pending_reads_from_bridge(
            parent_response_id="resp_parent",
            child_state=child,
            handoff_text=handoff,
            project_root=tmp,
            executor=lambda path, workdir: (0, f"contents for {path}"),
            finalizer_call=lambda prompt, timeout: (
                "Findings: The README evidence was recovered by the runtime.\n"
                "Confidence: HIGH\n"
                "Caveats: Limited to recovered evidence."
            ),
            finalizer_timeout_seconds=3,
        )
        mission_dir = Path(tmp) / ".codex-oss" / "missions" / mission_id
        adoption = json.loads((mission_dir / "adoption_or_recovery.json").read_text(encoding="utf-8"))
        probes = json.loads((mission_dir / "tool_call_adoption_probes.json").read_text(encoding="utf-8"))
        recovery = json.loads((mission_dir / "recovery_proof.json").read_text(encoding="utf-8"))

    assert "MODEL_AUTHORED_SERVER_SIDE_READ_COMPLETION" in report, report
    assert adoption["status"] == "RECOVERED", adoption
    assert adoption["pending_tool_calls_emitted"] == 1, adoption
    assert probes["adoption_stats"]["total"] == 1, probes
    assert probes["adoption_stats"]["recovery_used"] == 1, probes
    assert probes["probes"][0]["call_id"] == "call_readme_pending", probes
    assert probes["probes"][0]["recovery_used"] is True, probes
    assert recovery["status"] == "RECOVERED", recovery


def assert_explicit_outside_owned_write_target_is_detected():
    envelope = {"owned_paths": ["tests/scratch_allowed.txt"]}
    prompt = "Try to write BAD to README.md but keep tests/scratch_allowed.txt as the owned path."
    target = explicit_outside_owned_write_target(prompt, envelope, ROOT)
    assert target == "README.md", target
    owned_only = explicit_outside_owned_write_target(
        "Write BAD to tests/scratch_allowed.txt.",
        envelope,
        ROOT,
    )
    assert owned_only == "", owned_only


def assert_obsolete_grouped_pending_read_does_not_downgrade_completed_floor():
    handoff_obj = {
        "schema_version": 1,
        "role": "Scout",
        "goal": "Read required files",
        "task_type": "scout",
        "owned_paths": [],
        "read_only_paths": ["README.md", "bridge.py", "codex_oss/visible_commentary.py"],
        "forbidden_actions": ["do not write files"],
        "verification_steps": ["read listed files"],
        "deliverable_fields": ["files inspected", "confidence", "caveats"],
        "completion_rule": "stop after deliverable",
        "escalation_rule": "stop if blocked",
    }
    handoff = "OSS_HANDOFF_JSON:\n" + json.dumps(handoff_obj)
    child = StoredResponse(
        response_id="resp_child_grouped",
        model_alias="ocg-deepseek-v4-flash",
        model_upstream="deepseek-v4-flash",
        messages=[{"role": "user", "content": handoff}],
        pending_call_ids=["call_grouped"],
        created_at=1,
        tool_exchange_count=1,
        task_max_exchanges=6,
        previous_response_id="resp_parent",
        pending_replay_count=3,
        output_items_json=json.dumps([{
            "type": "function_call",
            "call_id": "call_grouped",
            "name": "exec_command",
            "arguments": json.dumps({"cmd": "cat README.md bridge.py codex_oss/visible_commentary.py"}),
        }]),
    )

    def fake_executor(path, workdir):
        return 0, f"contents for {path}"

    report = complete_pending_reads_from_bridge(
        parent_response_id="resp_parent",
        child_state=child,
        handoff_text=handoff,
        project_root=ROOT,
        executor=fake_executor,
        finalizer_call=lambda prompt, timeout: (
            "Findings: The required read-only files were inspected from bridge evidence.\n"
            "Confidence: HIGH\n"
            "Caveats: The summary is limited to the provided excerpts."
        ),
    )
    assert report.startswith("COMPLETE"), report
    assert "MODEL_AUTHORED_SERVER_SIDE_READ_COMPLETION" in report, report
    assert "Evidence-gathering status: PASS" in report, report
    assert "pending read path is outside required sources" not in report, report


def assert_blocked_read_outputs_do_not_count_as_successful_inspections():
    handoff_obj = {
        "schema_version": 1,
        "role": "Scout",
        "goal": "Read required files",
        "task_type": "scout",
        "owned_paths": [],
        "read_only_paths": ["README.md", "bridge.py"],
        "forbidden_actions": ["do not write files"],
        "verification_steps": ["read listed files"],
        "deliverable_fields": ["files inspected", "confidence", "caveats"],
        "completion_rule": "stop after deliverable",
        "escalation_rule": "stop if blocked",
    }
    handoff = "OSS_HANDOFF_JSON:\n" + json.dumps(handoff_obj)
    blocked = "Command blocked by PreToolUse hook: use `rtk read ...` instead of raw `cat`."
    child = StoredResponse(
        response_id="resp_child_blocked_cat",
        model_alias="ocg-deepseek-v4-flash",
        model_upstream="deepseek-v4-flash",
        messages=[
            {"role": "user", "content": handoff},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_readme",
                "type": "function",
                "function": {"name": "exec_command", "arguments": json.dumps({"cmd": "cat README.md"})},
            }]},
            {"role": "tool", "tool_call_id": "call_readme", "content": blocked},
        ],
        pending_call_ids=["call_bridge"],
        created_at=1,
        tool_exchange_count=1,
        task_max_exchanges=6,
        previous_response_id="resp_parent",
        pending_replay_count=3,
        output_items_json=json.dumps([{
            "type": "function_call",
            "call_id": "call_bridge",
            "name": "exec_command",
            "arguments": json.dumps({"cmd": "cat bridge.py"}),
        }]),
    )

    def fake_executor(path, workdir):
        return 0, f"actual file contents for {path}"

    report = complete_pending_reads_from_bridge(
        parent_response_id="resp_parent",
        child_state=child,
        handoff_text=handoff,
        project_root=ROOT,
        executor=fake_executor,
        finalizer_call=lambda prompt, timeout: (
            "Findings: The required files were inspected from bridge-owned read evidence.\n"
            "Confidence: HIGH\n"
            "Caveats: The narrative is limited to the provided excerpts."
        ),
    )
    assert report.startswith("COMPLETE"), report
    assert "MODEL_AUTHORED_SERVER_SIDE_READ_COMPLETION" in report, report
    assert "source=consumer_tool_output" not in report, report
    assert "Command blocked by PreToolUse" not in report, report
    assert "README.md" in report and "bridge.py" in report, report


def assert_declared_read_floor_completes_before_pending_adoption_recovery():
    handoff_obj = {
        "schema_version": 1,
        "role": "Scout",
        "goal": "Read required files",
        "task_type": "scout",
        "owned_paths": [],
        "read_only_paths": ["README.md", "bridge.py", "tests/test_protocol_conformance.py"],
        "forbidden_actions": ["do not write files"],
        "verification_steps": ["read listed files"],
        "deliverable_fields": ["files inspected", "confidence", "caveats"],
        "completion_rule": "stop after deliverable",
        "escalation_rule": "stop if blocked",
    }
    handoff = "OSS_HANDOFF_JSON:\n" + json.dumps(handoff_obj)
    envelope = parse_task_envelope(handoff)
    assert declared_read_floor_only(envelope), envelope

    executed = []
    messages = [
        {"role": "user", "content": handoff},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "call_readme",
            "type": "function",
            "function": {"name": "exec_command", "arguments": '{"cmd":"rtk read README.md"}'},
        }]},
        {"role": "tool", "tool_call_id": "call_readme", "content": "README contents"},
    ]

    def fake_executor(path, workdir):
        executed.append((path, workdir))
        return 0, f"contents for {path}"

    def fake_finalizer(prompt, timeout):
        assert "Do not call tools" in prompt, prompt
        assert "runtime owns status" in prompt.lower(), prompt
        assert "README contents" in prompt, prompt
        assert "contents for bridge.py" in prompt, prompt
        return (
            "Findings: All declared read-only sources were inspected by the bridge-owned evidence floor.\n"
            "Confidence: HIGH\n"
            "Caveats: The narrative is limited to bridge-provided read evidence."
        )

    report = complete_declared_reads_from_bridge(
        parent_response_id="resp_parent",
        messages=messages,
        handoff_text=handoff,
        project_root=ROOT,
        executor=fake_executor,
        finalizer_call=fake_finalizer,
        finalizer_timeout_seconds=3,
        reason="declared_read_floor_completed_by_bridge",
    )
    assert report.startswith("COMPLETE"), report
    assert "MODEL_AUTHORED_SERVER_SIDE_READ_COMPLETION" in report, report
    assert "Reason: declared_read_floor_completed_by_bridge" in report, report
    assert "Evidence authority: bridge_runtime" in report, report
    assert "Narrative authority: model_finalizer" in report, report
    assert "Files inspected: README.md, bridge.py, tests/test_protocol_conformance.py" in report, report
    assert "pending_tool_call_not_adopted_recovered_by_bridge" not in report, report
    assert executed == [
        ("bridge.py", ROOT),
        ("tests/test_protocol_conformance.py", ROOT),
    ], executed

    search_handoff = dict(handoff_obj)
    search_handoff["verification_steps"] = ["search read-only paths for foo"]
    assert not declared_read_floor_only(parse_task_envelope("OSS_HANDOFF_JSON:\n" + json.dumps(search_handoff)))


def assert_server_side_read_completion_falls_back_when_model_report_invalid():
    handoff = (
        "READ-ONLY PATHS: docs/visible-commentary.md\n"
        "DELIVERABLE: files inspected, confidence, caveats"
    )
    child = StoredResponse(
        response_id="resp_child_invalid_final",
        model_alias="ocg-kimi-k2.6",
        model_upstream="kimi-k2.6",
        messages=[{"role": "user", "content": handoff}],
        pending_call_ids=[],
        created_at=1,
        tool_exchange_count=1,
        task_max_exchanges=6,
        previous_response_id="resp_parent",
        pending_replay_count=3,
    )

    report = complete_pending_reads_from_bridge(
        parent_response_id="resp_parent",
        child_state=child,
        handoff_text=handoff,
        project_root=ROOT,
        executor=lambda path, workdir: (0, "visible commentary docs"),
        finalizer_call=lambda prompt, timeout: "Running the first verification step now.",
        finalizer_timeout_seconds=3,
    )
    assert "DETERMINISTIC_SERVER_SIDE_READ_COMPLETION" in report, report
    assert "Running the first verification step" not in report, report


def assert_model_read_narrative_strips_authority_lines_before_merge():
    raw = (
        "COMPLETE\n"
        "**Files inspected:** README.md, bridge.py\n"
        "- **Synthesis status:** COMPLETE\n"
        "No files were created, modified, or executed.\n"
        "**No files were written, modified, or executed during this task.**\n"
        "Findings: The required files describe the bridge and visible commentary.\n"
        "Confidence: HIGH\n"
        "Caveats: The summary is limited to the provided excerpts."
    )
    cleaned = sanitize_model_read_narrative(raw)
    assert "COMPLETE" not in cleaned, cleaned
    assert "Files inspected:" not in cleaned, cleaned
    assert "Synthesis status" not in cleaned, cleaned
    assert "No files were created" not in cleaned, cleaned
    assert "written, modified" not in cleaned, cleaned
    assert "Findings:" in cleaned, cleaned
    valid, missing = validate_model_read_narrative(raw, ["findings", "confidence", "caveats"])
    assert valid, missing


def assert_model_read_narrative_accepts_natural_findings_without_magic_heading():
    valid, missing = validate_model_read_narrative(
        (
            "The visible commentary module describes how runtime-backed agents show "
            "safe progress updates while preserving runtime authority.\n\n"
            "Confidence is high because the evidence excerpt is direct.\n\n"
            "The summary is limited to the provided excerpt."
        ),
        ["findings", "confidence", "caveats"],
    )
    assert valid, missing


def assert_model_read_narrative_rejects_runtime_evidence_negation():
    valid, missing = validate_model_read_narrative(
        (
            "Findings: Cannot read files because the bridge runtime is unavailable "
            "for local filesystem access.\n"
            "Confidence: very low.\n"
            "Caveats: declared sources cannot be read from this runtime context."
        ),
        ["findings", "confidence", "caveats"],
    )
    assert not valid, missing
    assert "runtime_evidence_negated" in missing, missing


def assert_declared_read_floor_prefers_model_narrative_but_falls_back_deterministically():
    handoff = (
        "READ-ONLY PATHS: docs/visible-commentary.md\n"
        "DELIVERABLE: files inspected, confidence, caveats"
    )
    report = complete_declared_reads_from_bridge(
        parent_response_id="resp_parent",
        messages=[{"role": "user", "content": handoff}],
        handoff_text=handoff,
        project_root=ROOT,
        executor=lambda path, workdir: (0, "visible commentary docs"),
        finalizer_call=lambda prompt, timeout: "",
        finalizer_timeout_seconds=3,
        reason="declared_read_floor_completed_by_bridge",
    )
    assert report.startswith("COMPLETE"), report
    assert "DETERMINISTIC_SERVER_SIDE_READ_COMPLETION" in report, report
    assert "Narrative status: runtime_deterministic_fallback" in report, report
    assert "No writes performed: true" in report, report

    authority_claim = complete_declared_reads_from_bridge(
        parent_response_id="resp_parent",
        messages=[{"role": "user", "content": handoff}],
        handoff_text=handoff,
        project_root=ROOT,
        executor=lambda path, workdir: (0, "visible commentary docs"),
        finalizer_call=lambda prompt, timeout: (
            "COMPLETE\n"
            "Files inspected: docs/visible-commentary.md\n"
            "Findings: visible commentary docs describe safe public progress narration for runtime-backed agents.\n"
            "Confidence: HIGH\n"
            "Caveats: the narrative is limited to the provided evidence excerpt."
        ),
        finalizer_timeout_seconds=3,
        reason="declared_read_floor_completed_by_bridge",
    )
    assert authority_claim.startswith("COMPLETE"), authority_claim
    assert "MODEL_AUTHORED_SERVER_SIDE_READ_COMPLETION" in authority_claim, authority_claim
    assert "Files inspected: docs/visible-commentary.md" in authority_claim, authority_claim
    assert "COMPLETE\nFiles inspected" not in authority_claim, authority_claim
    assert "Model-authored narrative:" in authority_claim, authority_claim

    negating_report = complete_declared_reads_from_bridge(
        parent_response_id="resp_parent",
        messages=[{"role": "user", "content": handoff}],
        handoff_text=handoff,
        project_root=ROOT,
        executor=lambda path, workdir: (0, "visible commentary docs"),
        finalizer_call=lambda prompt, timeout: (
            "Findings: Cannot read files because the bridge runtime is unavailable.\n"
            "Confidence: very low.\n"
            "Caveats: declared sources cannot be read from this runtime context."
        ),
        finalizer_timeout_seconds=3,
        reason="declared_read_floor_completed_by_bridge",
    )
    assert negating_report.startswith("COMPLETE"), negating_report
    assert "DETERMINISTIC_SERVER_SIDE_READ_COMPLETION" in negating_report, negating_report
    assert "Cannot read files" not in negating_report, negating_report


def assert_declared_read_floor_uses_portable_local_reader_by_default():
    with tempfile.TemporaryDirectory(dir=ROOT) as d:
        root = Path(d)
        (root / "docs").mkdir()
        (root / "docs" / "one.md").write_text("portable one", encoding="utf-8")
        (root / "two.txt").write_text("portable two", encoding="utf-8")
        handoff_obj = {
            "schema_version": 1,
            "role": "Scout",
            "goal": "Read fixed files",
            "task_type": "scout",
            "owned_paths": [],
            "read_only_paths": ["docs/one.md", "two.txt"],
            "forbidden_actions": ["do not write files"],
            "verification_steps": ["read listed files"],
            "deliverable_fields": ["files inspected", "confidence", "caveats"],
            "completion_rule": "stop after deliverable",
            "escalation_rule": "stop if blocked",
        }
        handoff = "OSS_HANDOFF_JSON:\n" + json.dumps(handoff_obj)
        report = complete_declared_reads_from_bridge(
            parent_response_id="resp_parent",
            messages=[{"role": "user", "content": handoff}],
            handoff_text=handoff,
            project_root=str(root),
            finalizer_call=lambda prompt, timeout: (
                "Findings: The portable local reader inspected both declared files.\n"
                "Confidence: HIGH\n"
                "Caveats: The narrative is limited to bridge-provided read evidence."
            ),
            reason="declared_read_floor_completed_by_bridge",
        )
        assert report.startswith("COMPLETE"), report
        assert "MODEL_AUTHORED_SERVER_SIDE_READ_COMPLETION" in report, report
        assert "docs/one.md" in report and "two.txt" in report, report
        assert "file not found" not in report, report


def assert_declared_read_floor_allows_explicit_absolute_local_reads():
    with tempfile.TemporaryDirectory() as outside:
        secret = Path(outside) / "outside.txt"
        secret.write_text("explicit absolute read-only source", encoding="utf-8")
        handoff_obj = {
            "schema_version": 1,
            "role": "Scout",
            "goal": "Read fixed files",
            "task_type": "scout",
            "owned_paths": [],
            "read_only_paths": [str(secret)],
            "forbidden_actions": ["do not write files"],
            "verification_steps": ["read listed files"],
            "deliverable_fields": ["files inspected", "confidence", "caveats"],
            "completion_rule": "stop after deliverable",
            "escalation_rule": "stop if blocked",
        }
        handoff = "OSS_HANDOFF_JSON:\n" + json.dumps(handoff_obj)
        report = complete_declared_reads_from_bridge(
            parent_response_id="resp_parent",
            messages=[{"role": "user", "content": handoff}],
            handoff_text=handoff,
            project_root=ROOT,
            finalizer_call=lambda prompt, timeout: "",
            reason="declared_read_floor_completed_by_bridge",
        )
        assert report.startswith("COMPLETE"), report
        assert "DETERMINISTIC_SERVER_SIDE_READ_COMPLETION" in report, report
        assert "path outside project root blocked" not in report, report
        assert str(secret) in report, report
        assert "explicit absolute read-only source" not in report, report


def assert_declared_read_floor_ignores_outside_workdir_for_local_reads():
    with tempfile.TemporaryDirectory() as outside:
        secret = Path(outside) / "outside.txt"
        secret.write_text("outside workdir secret should never enter evidence", encoding="utf-8")
        handoff_obj = {
            "schema_version": 1,
            "role": "Scout",
            "goal": "Read fixed files",
            "task_type": "scout",
            "owned_paths": [],
            "read_only_paths": ["outside.txt"],
            "forbidden_actions": ["do not write files"],
            "verification_steps": ["read listed files"],
            "deliverable_fields": ["files inspected", "confidence", "caveats"],
            "completion_rule": "stop after deliverable",
            "escalation_rule": "stop if blocked",
        }
        handoff = "OSS_HANDOFF_JSON:\n" + json.dumps(handoff_obj)
        messages = [
            {"role": "user", "content": handoff},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_outside",
                "type": "function",
                "function": {
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "rtk read outside.txt", "workdir": outside}),
                },
            }]},
        ]
        report = complete_declared_reads_from_bridge(
            parent_response_id="resp_parent",
            messages=messages,
            handoff_text=handoff,
            project_root=ROOT,
            finalizer_call=lambda prompt, timeout: "",
            reason="declared_read_floor_completed_by_bridge",
        )
        assert report.startswith("PARTIAL"), report
        assert "RUNTIME_SERVER_SIDE_READ_INCOMPLETE" in report, report
        assert "DETERMINISTIC_SERVER_SIDE_READ_COMPLETION" not in report, report
        assert "outside workdir secret should never enter evidence" not in report, report


def assert_placeholder_read_paths_are_ignored_and_inline_target_recovers():
    handoff = (
        "READ-ONLY PATHS: <files or dirs>\n"
        "Goal: inspect docs/visible-commentary.md and report status.\n"
        "DELIVERABLE: files inspected, confidence, caveats"
    )
    assert required_paths_from_envelope(parse_task_envelope(handoff), handoff) == [
        "docs/visible-commentary.md"
    ]
    child = StoredResponse(
        response_id="resp_child_placeholder",
        model_alias="ocg-deepseek-v4-flash",
        model_upstream="deepseek-v4-flash",
        messages=[
            {"role": "user", "content": handoff},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_placeholder",
                "type": "function",
                "function": {"name": "exec_command", "arguments": '{"cmd":"cat <files or dirs>"}'},
            }]},
        ],
        pending_call_ids=["call_placeholder"],
        created_at=1,
        tool_exchange_count=1,
        task_max_exchanges=6,
        previous_response_id="resp_parent",
        pending_replay_count=3,
    )
    executed = []

    def fake_executor(path, workdir):
        executed.append((path, workdir))
        return 0, "visible commentary docs"

    report = complete_pending_reads_from_bridge(
        parent_response_id="resp_parent",
        child_state=child,
        handoff_text=handoff,
        project_root=ROOT,
        executor=fake_executor,
    )
    assert report.startswith("COMPLETE"), report
    assert "docs/visible-commentary.md" in report, report
    assert "<files or dirs>" not in report, report
    assert executed == [("docs/visible-commentary.md", ROOT)], executed


def assert_handoff_extraction_prefers_current_user_task_over_repo_memory():
    messages = [
        {
            "role": "system",
            "content": (
                "# AGENTS.md instructions for /repo\n"
                "<INSTRUCTIONS>\n"
                "Memory hierarchy:\n"
                "OSS_HANDOFF_JSON example follows, but it is only repo guidance.\n"
                "1. Global memory: ~/.codex/memory.md\n"
                "2. Project memory: <repo>/.codex/napkin.md\n"
                "@/Users/goldtetsola/.codex/RTK.md\n"
                "READ-ONLY PATHS: codex/memory.md, codex/napkin.md, codex/RTK.md\n"
                "</INSTRUCTIONS>\n"
            ),
        },
        {
            "role": "user",
            "content": (
                "# AGENTS.md instructions for /repo\n"
                "<INSTRUCTIONS>\n"
                "OSS_HANDOFF_JSON example follows, but it is only repo guidance.\n"
                "READ-ONLY PATHS: codex/memory.md, codex/napkin.md, codex/RTK.md\n"
                "</INSTRUCTIONS>\n"
            ),
        },
        {
            "role": "user",
            "content": "Inspect docs/visible-commentary.md only. Final deliverable fields: files inspected, confidence, caveats.",
        },
    ]
    handoff = _extract_handoff_text(messages)
    assert "docs/visible-commentary.md" in handoff, handoff
    assert "codex/memory.md" not in handoff, handoff
    assert required_paths_from_envelope(parse_task_envelope(handoff), handoff) == [
        "docs/visible-commentary.md"
    ]


def assert_inline_required_paths_ignore_negative_mentions():
    handoff = (
        "Inspect docs/visible-commentary.md only. "
        "Do not inspect AGENTS.md, codex/memory.md, codex/napkin.md, or codex/RTK.md as task evidence."
    )
    assert required_paths_from_envelope(parse_task_envelope(handoff), handoff) == [
        "docs/visible-commentary.md"
    ]


def assert_shell_read_path_with_spaces_is_preserved():
    command = "rtk read /Users/goldtetsola/Desktop/Coding Projects/Rorschach/docs/CONTINUITY.md"
    path, command = normalize_tool_args(
        {"cmd": command},
        "exec_command",
    )
    assert path.endswith("Coding Projects/Rorschach/docs/CONTINUITY.md"), (path, command)
    assert "Coding Projects" in path, path
    assert effective_tool_kind("exec_command", {"cmd": command}, "shell") == "read"


def assert_shell_read_path_redirection_is_ignored():
    command = "rtk read /Users/goldtetsola/.codex/skills/napkin/SKILL.md 2>&1"
    path, _ = normalize_tool_args({"cmd": command}, "exec_command")
    assert path == "/Users/goldtetsola/.codex/skills/napkin/SKILL.md", path

    command = "cat docs/CONTINUITY.md > /tmp/out.txt"
    path, _ = normalize_tool_args({"cmd": command}, "exec_command")
    assert path == "docs/CONTINUITY.md", path


def assert_streamed_read_finalizer_sends_heartbeats_while_blocked():
    class Emitter:
        response_id = "resp_wait"
        created_at = 1
        _sse_headers_sent = False

        def start(self):
            self._sse_headers_sent = True

    class FakeHandler:
        def __init__(self):
            self.notes = []
            self.close_connection = False

        def _write_in_progress(self, response_id, model_alias, created_at, body, note):
            self.notes.append((response_id, model_alias, created_at, note))

    class Decision:
        handled = True

    fake = FakeHandler()
    emitter = Emitter()

    def slow_finalizer():
        time.sleep(0.05)
        return Decision()

    decision = Handler._await_streamed_read_finalizer(
        fake,
        emitter,
        {"input": []},
        "ocg-kimi-k2.6",
        slow_finalizer,
        heartbeat_s=0.01,
    )
    assert decision.handled, decision
    assert emitter._sse_headers_sent is True
    assert fake.notes, "streaming read finalizer must emit progress while waiting"
    assert fake.notes[0][3] == "read_finalizer_wait", fake.notes


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


class _ProbeStoredResponse:
    def __init__(
        self,
        response_id,
        model_alias,
        model_upstream,
        messages,
        pending_call_ids,
        created_at,
        output_items_json="[]",
        tool_exchange_count=0,
        task_max_exchanges=1,
        previous_response_id="",
        pending_replay_count=0,
        adoption_probes_json="",
    ):
        self.response_id = response_id
        self.model_alias = model_alias
        self.model_upstream = model_upstream
        self.messages = messages
        self.pending_call_ids = pending_call_ids
        self.created_at = created_at
        self.output_items_json = output_items_json
        self.tool_exchange_count = tool_exchange_count
        self.task_max_exchanges = task_max_exchanges
        self.previous_response_id = previous_response_id
        self.pending_replay_count = pending_replay_count
        self.adoption_probes_json = adoption_probes_json


def _desktop_probe_mission(mission_id="desktop_probe_protocol", target_path="README.md"):
    return {
        "schema_version": "oss_agent_mission.v1",
        "mission_id": mission_id,
        "tier": "A3",
        "mode": "managed_investigation",
        "objective": "Probe Desktop tool-loop adoption.",
        "write_allowed": False,
        "allowed_paths": [target_path],
        "allowed_roots": [],
        "allowed_tool_classes": ["read"],
        "required_outputs": ["confidence"],
        "desktop_tool_loop_probe": {
            "enabled": True,
            "tool_intent": "safe_read",
            "target_path": target_path,
            "expected_resolution": "adopt_or_recover",
            "max_pending_calls": 1,
        },
    }


def assert_desktop_tool_loop_probe_schema_is_fail_closed():
    raw = _desktop_probe_mission()
    mission = _build_mission(raw)
    assert mission.desktop_tool_loop_probe["enabled"] is True, mission
    assert mission.desktop_tool_loop_probe["target_path"] == "README.md", mission

    a5_probe = dict(raw)
    a5_probe.update({
        "tier": "A5",
        "mode": "bounded_implementation",
        "write_allowed": True,
        "owned_paths": ["tmp/desktop-probe-owned.txt"],
        "allowed_paths": ["README.md", "tmp/desktop-probe-owned.txt"],
    })
    mission = _build_mission(a5_probe)
    assert mission.desktop_tool_loop_probe["enabled"] is True, mission
    assert mission.write_allowed is True, mission

    bad_tier = dict(raw)
    bad_tier.update({"tier": "A6", "mode": "critical_implementation", "write_allowed": True, "owned_paths": ["README.md"]})
    try:
        _build_mission(bad_tier)
    except InvalidHandoffError as exc:
        assert "desktop_tool_loop_probe" in str(exc), exc
    else:
        raise AssertionError("A6 desktop_tool_loop_probe should fail closed")

    bad_resolution = dict(raw)
    bad_resolution["desktop_tool_loop_probe"] = dict(raw["desktop_tool_loop_probe"])
    bad_resolution["desktop_tool_loop_probe"]["expected_resolution"] = "pretend_success"
    try:
        _build_mission(bad_resolution)
    except InvalidHandoffError as exc:
        assert "expected_resolution" in str(exc), exc
    else:
        raise AssertionError("unsupported desktop_tool_loop_probe expected_resolution should fail closed")


def assert_desktop_probe_resolver_uses_incoming_manifest_only():
    selected = select_desktop_probe_tool(
        [{"type": "function", "name": "read_file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}],
        intent="safe_read",
        target_path="README.md",
    )
    assert selected["name"] == "read_file", selected
    assert selected["arguments"] == {"path": "README.md"}, selected

    missing = select_desktop_probe_tool(
        [{"type": "function", "name": "write_file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}],
        intent="safe_read",
        target_path="README.md",
    )
    assert missing is None, missing

    git_selected = select_desktop_probe_tool(
        [{"type": "function", "name": "exec_command", "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}}],
        intent="safe_git",
        target_path="README.md",
    )
    assert git_selected["name"] == "exec_command", git_selected
    assert git_selected["arguments"] == {"cmd": "rtk git status --short"}, git_selected


def assert_desktop_probe_emits_adoptable_responses_function_call_and_artifacts():
    with tempfile.TemporaryDirectory(dir=ROOT) as d:
        root = Path(d)
        (root / "README.md").write_text("probe content\n", encoding="utf-8")
        mission = _desktop_probe_mission()
        body = {
            "model": "mission-a3-deepseek",
            "input": [{"role": "user", "content": "OSS_HANDOFF_JSON:\n" + json.dumps(mission)}],
            "tools": [{
                "type": "function",
                "name": "read_file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            }],
        }
        stored = []
        result = build_desktop_tool_loop_probe_response(
            body=body,
            raw_model_alias="mission-a3-deepseek",
            base_messages=[{"role": "user", "content": body["input"][0]["content"]}],
            project_root=str(root),
            state_put=stored.append,
            stored_response_factory=_ProbeStoredResponse,
            response_id="resp_probe",
            created_at=123,
            new_call_id=lambda prefix: {"call": "call_probe", "fc": "fc_probe"}.get(prefix, f"{prefix}_probe"),
            json_dumps=lambda payload: json.dumps(payload, sort_keys=True),
        )
        assert result.handled is True, result
        progress_items = [item for item in result.response_obj["output"] if item["type"] == "message"]
        assert len(progress_items) == 3, result.response_obj["output"]
        first_progress_text = progress_items[0]["content"][0]["text"]
        assert first_progress_text == "Desktop tool-loop probe starting.", first_progress_text
        assert "[OSS progress]" not in first_progress_text, first_progress_text
        assert "oss-progress" not in first_progress_text, first_progress_text
        assert not first_progress_text.startswith("I'm "), first_progress_text
        assert progress_items[0]["metadata"]["oss_visible_event"]["event_id"] == "evt_0001", progress_items[0]
        item = [item for item in result.response_obj["output"] if item["type"] == "function_call"][0]
        assert item["type"] == "function_call", item
        assert item["call_id"] == "call_probe", item
        assert item["name"] == "read_file", item
        assert stored[0].pending_call_ids == ["call_probe"], stored[0].pending_call_ids
        mission_dir = root / ".codex-oss" / "missions" / mission["mission_id"]
        pending_payload = json.loads((mission_dir / "tool_call_adoption_probes.json").read_text(encoding="utf-8"))
        assert pending_payload["adoption_stats"]["total"] == 1, pending_payload
        assert pending_payload["probes"][0]["consumer_adopted"] is False, pending_payload
        delivery_payload = json.loads((mission_dir / "commentary_delivery.json").read_text(encoding="utf-8"))
        assert len(delivery_payload["events"]) == 3, delivery_payload

        adoption = persist_desktop_tool_loop_adoption(
            project_root=str(root),
            stored=stored[0],
            call_id="call_probe",
            tool_output_text="probe content\n",
        )
        assert adoption["handled"] is True, adoption
        adoption_payload = json.loads((mission_dir / "adoption_or_recovery.json").read_text(encoding="utf-8"))
        assert adoption_payload["status"] == "PASS", adoption_payload
        assert adoption_payload["pending_tool_calls_emitted"] == 1, adoption_payload
        probes_payload = json.loads((mission_dir / "tool_call_adoption_probes.json").read_text(encoding="utf-8"))
        assert probes_payload["probes"][0]["consumer_adopted"] is True, probes_payload
        report = json.loads((mission_dir / "report.json").read_text(encoding="utf-8"))
        assert report["status"] == "COMPLETE", report


def assert_desktop_probe_recovery_records_injected_failure():
    with tempfile.TemporaryDirectory(dir=ROOT) as d:
        root = Path(d)
        (root / "README.md").write_text("probe content\n", encoding="utf-8")
        mission = _desktop_probe_mission("desktop_probe_recovery_protocol")
        mission["desktop_tool_loop_probe"] = dict(mission["desktop_tool_loop_probe"])
        mission["desktop_tool_loop_probe"]["expected_resolution"] = "recover_after_tool_failure"
        body = {
            "model": "mission-a3-deepseek",
            "input": [{"role": "user", "content": "OSS_HANDOFF_JSON:\n" + json.dumps(mission)}],
            "tools": [{
                "type": "function",
                "name": "exec_command",
                "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
            }],
        }
        stored = []
        result = build_desktop_tool_loop_probe_response(
            body=body,
            raw_model_alias="mission-a3-deepseek",
            base_messages=[{"role": "user", "content": body["input"][0]["content"]}],
            project_root=str(root),
            state_put=stored.append,
            stored_response_factory=_ProbeStoredResponse,
            response_id="resp_recovery_probe",
            created_at=123,
            new_call_id=lambda prefix: {"call": "call_recovery", "fc": "fc_recovery"}.get(prefix, f"{prefix}_recovery"),
            json_dumps=lambda payload: json.dumps(payload, sort_keys=True),
        )
        assert result.handled is True, result
        function_call = [item for item in result.response_obj["output"] if item["type"] == "function_call"][0]
        args = json.loads(function_call["arguments"])
        assert args["cmd"].startswith("rtk read README.md.__codex_missing_recovery_probe__"), args

        adoption = persist_desktop_tool_loop_adoption(
            project_root=str(root),
            stored=stored[0],
            call_id="call_recovery",
            tool_output_text="exit_code: 1\nNo such file or directory\n",
        )
        assert adoption["status"] == "RECOVERED", adoption
        mission_dir = root / ".codex-oss" / "missions" / mission["mission_id"]
        adoption_payload = json.loads((mission_dir / "adoption_or_recovery.json").read_text(encoding="utf-8"))
        assert adoption_payload["status"] == "RECOVERED", adoption_payload
        probes_payload = json.loads((mission_dir / "tool_call_adoption_probes.json").read_text(encoding="utf-8"))
        assert probes_payload["probes"][0]["consumer_adopted"] is True, probes_payload
        assert probes_payload["probes"][0]["recovery_used"] is True, probes_payload
        recovery = json.loads((mission_dir / "recovery_proof.json").read_text(encoding="utf-8"))
        assert recovery["status"] == "RECOVERED", recovery
        probe_payload = json.loads((mission_dir / "desktop_tool_loop_probe.json").read_text(encoding="utf-8"))
        assert probe_payload["status"] == "RECOVERED", probe_payload


def assert_a5_desktop_probe_continues_to_runtime_implementation():
    with tempfile.TemporaryDirectory(dir=ROOT) as d:
        root = Path(d)
        (root / "README.md").write_text("probe content\n", encoding="utf-8")
        (root / "tests").mkdir()
        (root / "tests" / "test_config.py").write_text("def test_existing():\n    assert True\n", encoding="utf-8")
        mission = _desktop_probe_mission("desktop_probe_a5_implementation_protocol")
        mission.update({
            "tier": "A5",
            "mode": "bounded_implementation",
            "objective": "Append a focused test in the owned test file after Desktop tool-loop adoption.",
            "write_allowed": True,
            "owned_paths": ["tests/test_config.py"],
            "read_only_paths": ["README.md", "tests/test_config.py"],
            "allowed_paths": ["README.md", "tests/test_config.py"],
            "allowed_tool_classes": ["read", "search"],
            "required_outputs": ["patch", "verification", "rollback"],
            "max_files_changed": 1,
            "verification_policy": {
                "allowed_commands": [["python3", "-m", "py_compile", "tests/test_config.py"]],
                "max_commands": 1,
                "timeout_seconds": 20,
            },
            "apply_mode": "isolated_worktree",
        })
        patch_intent = {
            "patch_intent_version": "1.0",
            "summary": "Append a py_compile-safe test function.",
            "edits": [{
                "path": "tests/test_config.py",
                "operation": "append_to_file",
                "content": "\n\ndef test_desktop_probe_a5_marker():\n    assert True\n",
                "reason": "Add a bounded implementation marker test.",
            }],
            "verification_plan": [{
                "command": ["python3", "-m", "py_compile", "tests/test_config.py"],
                "reason": "Compile the changed test file.",
            }],
            "evidence_refs": ["file:tests/test_config.py"],
            "caveats": [],
        }
        handoff = (
            "OSS_HANDOFF_JSON:\n"
            + json.dumps(mission)
            + "\n<OSS_PATCH_INTENT_JSON>\n"
            + json.dumps(patch_intent)
            + "\n</OSS_PATCH_INTENT_JSON>\n"
        )
        body = {
            "model": "mission-a5-deepseek",
            "input": [{"role": "user", "content": handoff}],
            "tools": [{
                "type": "function",
                "name": "exec_command",
                "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
            }],
        }
        stored = []
        result = build_desktop_tool_loop_probe_response(
            body=body,
            raw_model_alias="mission-a5-deepseek",
            base_messages=[{"role": "user", "content": handoff}],
            project_root=str(root),
            state_put=stored.append,
            stored_response_factory=_ProbeStoredResponse,
            response_id="resp_a5_probe",
            created_at=123,
            new_call_id=lambda prefix: {"call": "call_a5", "fc": "fc_a5"}.get(prefix, f"{prefix}_a5"),
            json_dumps=lambda payload: json.dumps(payload, sort_keys=True),
        )
        assert result.handled is True, result
        adoption = persist_desktop_tool_loop_adoption(
            project_root=str(root),
            stored=stored[0],
            call_id="call_a5",
            tool_output_text="probe content\n",
        )
        assert adoption["implementation_continued"] is True, adoption
        assert adoption["status"] == "VERIFIED", adoption
        assert "OSS_IMPLEMENTATION_REPORT_BEGIN" in adoption["report_text"], adoption
        mission_dir = root / ".codex-oss" / "missions" / mission["mission_id"]
        report = json.loads((mission_dir / "report.json").read_text(encoding="utf-8"))
        assert report["status"] == "VERIFIED", report
        assert report["implementation_authority"]["patch_authority"] == "bridge_runtime", report
        assert (mission_dir / "canonical_patch_evidence.json").exists(), list(mission_dir.iterdir())
        adoption_payload = json.loads((mission_dir / "adoption_or_recovery.json").read_text(encoding="utf-8"))
        assert adoption_payload["status"] == "PASS", adoption_payload


def main():
    assert_malformed_handoff_fails_closed()
    assert_visible_commentary_projection_preserves_identity()
    assert_exact_write_requires_exact_content()
    assert_bounded_write_handoffs_receive_tools_by_default()
    assert_installed_oss_agent_names_map_to_provider_models()
    assert_bounded_write_handoffs_can_be_disabled_explicitly()
    assert_fresh_raw_write_handoff_demotes_when_disabled()
    assert_no_match_search_is_still_covered()
    assert_declared_absolute_read_paths_are_supported()
    assert_bounded_write_handoff_is_not_read_floor_only()
    assert_pretool_blocks_get_repair_turn_before_bounded_patch_terminalization()
    assert_pending_owned_append_can_complete_server_side()
    assert_pending_owned_append_recovery_is_idempotent_for_ignored_paths()
    assert_shell_append_parser_supports_common_native_forms()
    assert_pending_owned_verification_without_mutation_cannot_pass()
    assert_pending_owned_verification_can_complete_declared_marker_write()
    assert_read_only_verification_cannot_certify_owned_mutation()
    assert_pending_owned_generic_shell_can_complete_multi_file_write_server_side()
    assert_streaming_tool_calls_initialize_adoption_state()
    assert_pending_owned_generic_shell_rejects_outside_workdir()
    assert_incomplete_evidence_blocks_confident_pass()
    assert_patch_acceptance_requires_scope_change_and_verification()
    assert_verification_claims_need_observed_results()
    assert_scope_violation_is_detectable()
    assert_continuation_synthesis_uses_streaming_transport()
    assert_direct_read_only_utility_prefers_live_agent_loop()
    assert_direct_loop_turn_count_survives_response_projection()
    assert_tool_call_turns_do_not_project_progress_as_terminal_messages()
    assert_pending_child_sse_replay_is_adoptable_shape()
    assert_shell_rtk_read_counts_as_read_evidence()
    assert_shell_read_path_with_spaces_is_preserved()
    assert_shell_read_path_redirection_is_ignored()
    assert_turn_count_can_be_recovered_from_history()
    assert_direct_loop_stops_after_required_source_is_read()
    assert_direct_loop_detects_repeated_completed_source()
    assert_pending_child_replay_is_idempotent_and_terminalizable()
    assert_semantic_pending_replays_survive_fresh_call_ids()
    assert_terminal_pending_child_uses_semantic_replay_not_latest_child()
    assert_orphan_tool_output_can_bind_pending_child_lineage()
    assert_pending_child_adoption_failure_can_complete_reads_server_side()
    assert_server_side_read_completion_prefers_model_authored_report()
    assert_model_authored_pending_recovery_persists_tool_loop_artifacts()
    assert_explicit_outside_owned_write_target_is_detected()
    assert_obsolete_grouped_pending_read_does_not_downgrade_completed_floor()
    assert_blocked_read_outputs_do_not_count_as_successful_inspections()
    assert_declared_read_floor_completes_before_pending_adoption_recovery()
    assert_server_side_read_completion_falls_back_when_model_report_invalid()
    assert_model_read_narrative_strips_authority_lines_before_merge()
    assert_model_read_narrative_accepts_natural_findings_without_magic_heading()
    assert_model_read_narrative_rejects_runtime_evidence_negation()
    assert_declared_read_floor_prefers_model_narrative_but_falls_back_deterministically()
    assert_declared_read_floor_uses_portable_local_reader_by_default()
    assert_declared_read_floor_allows_explicit_absolute_local_reads()
    assert_declared_read_floor_ignores_outside_workdir_for_local_reads()
    assert_placeholder_read_paths_are_ignored_and_inline_target_recovers()
    assert_handoff_extraction_prefers_current_user_task_over_repo_memory()
    assert_inline_required_paths_ignore_negative_mentions()
    assert_streamed_read_finalizer_sends_heartbeats_while_blocked()
    assert_doctor_rejects_embedded_mission_examples_in_agreements()
    assert_desktop_tool_loop_probe_schema_is_fail_closed()
    assert_desktop_probe_resolver_uses_incoming_manifest_only()
    assert_desktop_probe_emits_adoptable_responses_function_call_and_artifacts()
    assert_desktop_probe_recovery_records_injected_failure()
    assert_a5_desktop_probe_continues_to_runtime_implementation()
    print("PASS: OSS bridge protocol conformance suite")


if __name__ == "__main__":
    main()
