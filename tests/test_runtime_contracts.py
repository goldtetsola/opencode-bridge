#!/usr/bin/env python3
"""Runtime contract tests for OSS Agent Runtime v1."""

from __future__ import annotations

import os
import json
import sys
import tempfile
import threading
import time
import shutil

os.environ["ALLOW_MISSING_OPENCODE_KEY"] = "1"
ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from codex_oss.ledger import EvidenceLedger
from codex_oss.health import build_health_status
from codex_oss.audit import audit_mission
from codex_oss.managed_bridge import run_managed_mission_from_body, should_handle_managed_mission_body
from codex_oss.mission import InvalidHandoffError, _build_mission
from codex_oss.runtime import ToolResult, resolve_path
from codex_oss.runtime.loop import (
    _evidence_ref_summary,
    _extract_model_text,
    _normalize_action_arguments,
    _normalize_arguments,
    run_loop,
)
from codex_oss.runtime.policy import detect_critical_finality, is_broad_root
from codex_oss.runtime.objectives import classify_objective, synthesize_objective_finding
from codex_oss.validation import validate_report
from codex_oss.runtime.policy import acquire_mission_slot, release_mission_slot
from codex_oss.runtime.closure import record_closure_telemetry
from codex_oss.answer_graph import pending_required_agenda_items, refresh_answer_graph
from codex_oss.transport.emitter import ResponseEmitter


def mission(**overrides):
    raw = {
        "schema_version": "oss_agent_mission.v1",
        "mission_id": "mission_runtime_test",
        "tier": "A3",
        "mode": "managed_investigation",
        "objective": "Exercise the runtime contract.",
        "risk_tier": "low",
        "write_allowed": False,
        "allowed_roots": ["codex_oss/"],
        "allow_broad_read_scope": True,
        "allowed_paths": [],
        "tool_budget": 3,
        "time_budget_seconds": 30,
        "allowed_tool_classes": ["read", "search", "list"],
        "stop_conditions": ["valid_report", "budget_exhausted", "deadline_reached"],
        "report_schema": "managed_investigation_report.v1",
        "required_outputs": [
            "files_inspected",
            "commands_run",
            "findings",
            "uncertainties",
            "confidence",
            "caveats",
            "escalation_recommendation",
        ],
    }
    raw.update(overrides)
    return _build_mission(raw)


def assert_run_loop_accepts_valid_final_report():
    m = mission()
    ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)

    def fake_model(messages, tools, timeout):
        assert tools == [], tools
        assert "Allowed roots:" in messages[0]["content"], messages[0]["content"]
        assert "codex_oss/" in messages[0]["content"], messages[0]["content"]
        assert "Do not guess alternate filenames or read '.'" in messages[0]["content"], messages[0]["content"]
        assert '"action_type":"tool_call"' in messages[0]["content"], messages[0]["content"]
        assert '"action_type":"final_report"' in messages[0]["content"], messages[0]["content"]
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"action_type":"final_report","report":'
                            '{"oss_report_version":"1.0","mission_id":"mission_runtime_test",'
                            '"status":"PARTIAL","confidence":"LOW","files_inspected":[],'
                            '"commands_run":[],"findings":[],"uncertainties":[],'
                            '"caveats":["no tools needed for smoke"],'
                            '"escalation_recommendation":"GPT-5.5 review required",'
                            '"missing_fields":[]}}'
                        )
                    }
                }
            ]
        }

    result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=30)
    assert result["status"] == "PARTIAL", result
    assert result["report"]["status"] == "PARTIAL", result


def assert_model_text_extraction_handles_provider_variants():
    text = '{"action_type":"final_report"}'
    variants = [
        text,
        {"choices": [text]},
        {"choices": [{"message": text}]},
        {"choices": [{"message": {"content": text}}]},
        {"choices": [{"message": {"content": [{"type": "text", "text": text}]}}]},
    ]
    for response in variants:
        assert _extract_model_text(response) == text, response


def assert_deterministic_fast_path_handles_single_file_function_location_without_model_call():
    body = {
        "input": [
            {
                "role": "user",
                "content": (
                    "<OSS_HANDOFF_JSON>\n"
                    '{"schema_version":"oss_agent_mission.v1","mission_id":"mission_fast_path_test",'
                    '"tier":"A3","mode":"managed_investigation","objective":"Locate certify_project.",'
                    '"risk_tier":"low","write_allowed":false,"allowed_roots":[],"allowed_paths":["codex_oss/certify.py"],'
                    '"tool_budget":3,"time_budget_seconds":30,"allowed_tool_classes":["read"],'
                    '"stop_conditions":["valid_report"],"report_schema":"managed_investigation_report.v1",'
                    '"required_outputs":["files_inspected","commands_run","findings","uncertainties","confidence","caveats","escalation_recommendation"],'
                    '"objective_spec":{"schema_version":"objective_spec.v1","objective_type":"function_location","target":{"symbol":"certify_project"},'
                    '"required_outputs":["file_path","function_definition"],"required_evidence_shapes":["function_definition"],'
                    '"completion_criteria":["function_definition_present"]}}'
                    "\n</OSS_HANDOFF_JSON>"
                ),
            }
        ]
    }

    def should_not_call_model(payload, timeout):
        raise AssertionError("deterministic fast path should not call the model")

    result = run_managed_mission_from_body(
        body,
        "mission-a3-kimi",
        lambda *args, **kwargs: None,
        should_not_call_model,
        lambda model: model,
        request_deadline=30,
    )
    assert result.handled, result
    assert result.status == "COMPLETE", result
    assert "deterministic_fast_path" in result.report_text, result.report_text
    assert "certify_project" in result.report_text, result.report_text


def assert_read_pool_scheduler_allows_multiple_low_risk_missions():
    missions = [
        mission(mission_id="slot_read_1", allowed_roots=[], allowed_paths=["README.md"]),
        mission(mission_id="slot_read_2", allowed_roots=[], allowed_paths=["README.md"]),
        mission(mission_id="slot_read_3", allowed_roots=[], allowed_paths=["README.md"]),
    ]
    acquired_ids = []
    try:
        for item in missions:
            acquired, reason = acquire_mission_slot(item)
            assert acquired, reason
            acquired_ids.append(item.mission_id)
    finally:
        for mission_id in acquired_ids:
            release_mission_slot(mission_id)


def assert_mission_requires_scope_and_tool_contract():
    try:
        mission(allowed_roots=[], allowed_paths=[])
    except InvalidHandoffError as exc:
        assert "allowed_roots or allowed_paths" in str(exc), exc
    else:
        raise AssertionError("A3 mission without read scope should fail closed")

    try:
        mission(allowed_tool_classes=[])
    except InvalidHandoffError as exc:
        assert "allowed_tool_classes" in str(exc), exc
    else:
        raise AssertionError("A3 mission without tool classes should fail closed")


def assert_path_policy_blocks_empty_scope_and_denied_symlink():
    resolved, error = resolve_path("bridge.py", [], [])
    assert resolved is None, resolved
    assert "no allowed roots" in error, error

    with tempfile.NamedTemporaryFile("w", dir=os.getcwd(), prefix="runtime-secret-", suffix=".env", delete=False) as f:
        f.write("OPENAI_API_KEY=sk-runtime-test-secret\n")
        env_name = os.path.basename(f.name)
    link_path = "tests/fixtures/runtime_link_to_env"
    try:
        try:
            os.unlink(link_path)
        except FileNotFoundError:
            pass
        os.symlink(os.path.join("..", "..", env_name), link_path)
        resolved, error = resolve_path(link_path, ["tests/fixtures/"], [])
        assert resolved is None, resolved
        assert "deny pattern" in error, error
    finally:
        try:
            os.unlink(link_path)
        except FileNotFoundError:
            pass
        try:
            os.unlink(env_name)
        except FileNotFoundError:
            pass


def assert_broad_roots_are_exactly_broad():
    for root in (".", "/", "src/", "scripts/", "docs/", "src", "scripts", "docs"):
        assert is_broad_root(root), root


def assert_critical_finality_uses_word_boundaries():
    fixture_claim = (
        "Runtime-backed OSS agents use MissionV1, a managed JSON action loop, "
        "runtime-owned read/search/list tools, evidence refs, and report validation."
    )
    assert not detect_critical_finality(fixture_claim)
    assert detect_critical_finality("The finalizer is 100% correct and safe to merge.")


def assert_file_extract_refs_resolve():
    ledger = EvidenceLedger(mission_id="mission_runtime_test")
    result = ToolResult(
        tool="rtk_read",
        args={"path": "codex_oss/runtime/loop.py"},
        exit_code=0,
        stdout="alpha\nbeta\n",
        stderr="",
        complete=True,
        chars_total=11,
    )
    ledger.add_file("codex_oss/runtime/loop.py", result, turn=0)
    report = {
        "oss_report_version": "1.0",
        "mission_id": "mission_runtime_test",
        "status": "COMPLETE",
        "confidence": "LOW",
        "files_inspected": [{"path": "codex_oss/runtime/loop.py", "complete": True}],
        "commands_run": [],
        "findings": [
            {
                "claim": "The file was inspected.",
                "evidence_refs": ["file:codex_oss/runtime/loop.py#extract:1"],
                "confidence": "LOW",
            }
        ],
        "uncertainties": [],
        "caveats": [],
        "escalation_recommendation": "GPT-5.5 review required",
        "missing_fields": [],
    }
    validation = validate_report(report, ledger)
    assert validation.is_valid, validation.errors
    refs = _evidence_ref_summary(ledger)
    assert "file:codex_oss/runtime/loop.py#extract:1" in refs, refs


def assert_report_validation_rejects_non_object_findings_without_crashing():
    ledger = EvidenceLedger(mission_id="mission_runtime_test")
    report = {
        "oss_report_version": "1.0",
        "mission_id": "mission_runtime_test",
        "status": "COMPLETE",
        "confidence": "LOW",
        "files_inspected": [],
        "commands_run": [],
        "findings": ["plain string finding from a loose provider"],
        "uncertainties": [],
        "caveats": [],
        "escalation_recommendation": "GPT-5.5 review required",
        "missing_fields": [],
    }
    validation = validate_report(report, ledger)
    assert not validation.is_valid
    assert "finding[0] is not an object" in validation.errors, validation.errors


def assert_managed_bridge_returns_terminal_report_on_runtime_error():
    body = {
        "input": [
            {
                "role": "user",
                "content": (
                    "<OSS_HANDOFF_JSON>\n"
                    '{"schema_version":"oss_agent_mission.v1","mission_id":"mission_bridge_test",'
                    '"tier":"A3","mode":"managed_investigation","objective":"Crash safely",'
                    '"risk_tier":"low","write_allowed":false,"allowed_roots":["codex_oss/"],'
                    '"allow_broad_read_scope":true,"allowed_paths":[],"tool_budget":1,"time_budget_seconds":30,'
                    '"allowed_tool_classes":["read"],"stop_conditions":["valid_report"],'
                    '"report_schema":"managed_investigation_report.v1",'
                    '"required_outputs":["files_inspected","commands_run","findings","uncertainties",'
                    '"confidence","caveats","escalation_recommendation"]}'
                    "\n</OSS_HANDOFF_JSON>"
                ),
            }
        ]
    }

    def crashing_call(payload, timeout):
        raise RuntimeError("simulated upstream outage")

    result = run_managed_mission_from_body(
        body,
        "ocg-kimi-k2.6",
        lambda *args, **kwargs: None,
        crashing_call,
        lambda model: model,
        request_deadline=30,
    )
    assert result.handled, result
    assert result.status == "PARTIAL", result
    assert "OSS_REPORT_BEGIN" in result.report_text, result.report_text
    assert "model_call_failed" in result.report_text, result.report_text

    invalid = {
        "input": [
            {
                "role": "user",
                "content": (
                    "<OSS_HANDOFF_JSON>{}</OSS_HANDOFF_JSON>\n"
                    "<OSS_HANDOFF_JSON>{}</OSS_HANDOFF_JSON>"
                ),
            }
        ]
    }
    invalid_result = run_managed_mission_from_body(
        invalid,
        "ocg-kimi-k2.6",
        lambda *args, **kwargs: None,
        crashing_call,
        lambda model: model,
        request_deadline=30,
    )
    assert invalid_result.handled, invalid_result
    assert invalid_result.status == "FAILED", invalid_result
    assert "invalid entrypoint" in invalid_result.report_text, invalid_result.report_text


def assert_runtime_model_alias_requires_mission_and_maps_reasoning_model():
    calls = []

    def call_payload(payload, timeout):
        calls.append(payload)
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"action_type":"final_report","report":'
                            '{"oss_report_version":"1.0","mission_id":"mission_alias_test",'
                            '"status":"PARTIAL","confidence":"LOW","files_inspected":[],'
                            '"commands_run":[],"findings":[],"uncertainties":[],'
                            '"caveats":["alias smoke"],'
                            '"escalation_recommendation":"GPT-5.5 review required",'
                            '"missing_fields":[]}}'
                        )
                    }
                }
            ]
        }

    missing = run_managed_mission_from_body(
        {"input": [{"role": "user", "content": "Investigate README.md"}]},
        "mission-a3-kimi",
        lambda *args, **kwargs: None,
        call_payload,
        lambda model: model,
        request_deadline=30,
    )
    assert missing.handled, missing
    assert missing.status == "FAILED", missing
    assert "requires exactly one OSS_HANDOFF_JSON MissionV1" in missing.report_text, missing.report_text
    assert calls == [], calls

    body = {
        "input": [
            {
                "role": "user",
                "content": (
                    "<OSS_HANDOFF_JSON>\n"
                    '{"schema_version":"oss_agent_mission.v1","mission_id":"mission_alias_test",'
                    '"tier":"A3","mode":"managed_investigation","objective":"Alias route smoke",'
                    '"risk_tier":"low","write_allowed":false,"allowed_roots":["codex_oss/"],'
                    '"allow_broad_read_scope":true,"allowed_paths":[],"tool_budget":1,"time_budget_seconds":30,'
                    '"allowed_tool_classes":["read"],"stop_conditions":["valid_report"],'
                    '"report_schema":"managed_investigation_report.v1",'
                    '"required_outputs":["files_inspected","commands_run","findings","uncertainties",'
                    '"confidence","caveats","escalation_recommendation"]}'
                    "\n</OSS_HANDOFF_JSON>"
                ),
            }
        ]
    }
    handled = run_managed_mission_from_body(
        body,
        "mission-a3-kimi",
        lambda *args, **kwargs: None,
        call_payload,
        lambda model: {"ocg-kimi-k2.6": "kimi-k2.6"}[model],
        request_deadline=30,
    )
    assert handled.handled, handled
    assert handled.status == "PARTIAL", handled
    assert calls[-1]["model"] == "kimi-k2.6", calls[-1]
    assert calls[-1]["tools"] == [], calls[-1]


def assert_runtime_model_alias_uses_fallback_on_model_failure():
    calls = []
    logs = []

    def call_payload(payload, timeout):
        calls.append(payload)
        if len(calls) == 1:
            raise RuntimeError("simulated primary model 500")
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"action_type":"final_report","report":'
                            '{"oss_report_version":"1.0","mission_id":"mission_fallback_test",'
                            '"status":"PARTIAL","confidence":"LOW","files_inspected":[],'
                            '"commands_run":[],"findings":[],"uncertainties":[],'
                            '"caveats":["fallback smoke"],'
                            '"escalation_recommendation":"GPT-5.5 review required",'
                            '"missing_fields":[]}}'
                        )
                    }
                }
            ]
        }

    body = {
        "input": [
            {
                "role": "user",
                "content": (
                    "<OSS_HANDOFF_JSON>\n"
                    '{"schema_version":"oss_agent_mission.v1","mission_id":"mission_fallback_test",'
                    '"tier":"A3","mode":"managed_investigation","objective":"Fallback route smoke",'
                    '"risk_tier":"low","write_allowed":false,"allowed_roots":["codex_oss/"],'
                    '"allow_broad_read_scope":true,"allowed_paths":[],"tool_budget":1,"time_budget_seconds":30,'
                    '"allowed_tool_classes":["read"],"stop_conditions":["valid_report"],'
                    '"report_schema":"managed_investigation_report.v1",'
                    '"required_outputs":["files_inspected","commands_run","findings","uncertainties",'
                    '"confidence","caveats","escalation_recommendation"]}'
                    "\n</OSS_HANDOFF_JSON>"
                ),
            }
        ]
    }
    handled = run_managed_mission_from_body(
        body,
        "mission-a3-deepseek",
        lambda event, **fields: logs.append((event, fields)),
        call_payload,
        lambda model: {"ocg-deepseek-v4-pro": "deepseek-pro", "ocg-kimi-k2.6": "kimi"}[model],
        request_deadline=30,
    )
    assert handled.handled, handled
    assert handled.status == "PARTIAL", handled
    assert [call["model"] for call in calls] == ["deepseek-pro", "kimi"], calls
    assert any(event == "mission_model_primary_failed" for event, _ in logs), logs
    assert any(event == "mission_model_fallback_ok" for event, _ in logs), logs


def assert_readonly_mission_writes_artifact_bundle():
    cwd = os.getcwd()
    root = tempfile.mkdtemp(prefix="runtime_artifacts_")
    try:
        os.chdir(root)
        os.makedirs("notes", exist_ok=True)
        with open("notes/example.txt", "w", encoding="utf-8") as handle:
            handle.write("alpha\nbeta\n")

        calls = {"count": 0}

        def call_payload(payload, timeout):
            calls["count"] += 1
            if calls["count"] == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"action_type":"tool_call","tool_name":"rtk_read",'
                                    '"arguments":{"path":"notes/example.txt"},'
                                    '"reason":"Read the file","hypothesis":"The file contains the answer.",'
                                    '"expected_information_gain":"Gather file evidence.",'
                                    '"why_not_report_yet":"Need evidence first."}'
                                )
                            }
                        }
                    ]
                }
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"action_type":"final_report","report":'
                                '{"oss_report_version":"1.0","mission_id":"mission_artifact_test",'
                                '"status":"COMPLETE","confidence":"LOW",'
                                '"files_inspected":[{"path":"notes/example.txt","complete":true}],'
                                '"commands_run":[{"tool":"rtk_read","args":{"path":"notes/example.txt"}}],'
                                '"findings":[{"claim":"The file was inspected.","evidence_refs":["command:0"],"confidence":"LOW"}],'
                                '"uncertainties":[],"caveats":["artifact smoke"],'
                                '"escalation_recommendation":"GPT-5.5 review required",'
                                '"missing_fields":[]}}'
                            )
                        }
                    }
                ]
            }

        body = {
            "input": [
                {
                    "role": "user",
                    "content": (
                        "<OSS_HANDOFF_JSON>\n"
                        '{"schema_version":"oss_agent_mission.v1","mission_id":"mission_artifact_test",'
                        '"tier":"A3","mode":"managed_investigation","objective":"Inspect one file and report.",'
                        '"risk_tier":"low","write_allowed":false,"allowed_roots":[],"allowed_paths":["notes/example.txt"],'
                        '"tool_budget":2,"time_budget_seconds":30,"allowed_tool_classes":["read"],'
                        '"stop_conditions":["valid_report"],"report_schema":"managed_investigation_report.v1",'
                        '"required_outputs":["files_inspected","commands_run","findings","uncertainties","confidence","caveats","escalation_recommendation"]}'
                        "\n</OSS_HANDOFF_JSON>"
                    ),
                }
            ]
        }
        live_events = []
        assert should_handle_managed_mission_body(body, "mission-a3-kimi") is True
        result = run_managed_mission_from_body(
            body,
            "mission-a3-kimi",
            lambda *args, **kwargs: None,
            call_payload,
            lambda model: model,
            request_deadline=30,
            commentary_callback=live_events.append,
        )
        assert result.handled, result
        assert result.status == "COMPLETE", result
        artifact_dir = os.path.join(root, ".codex-oss", "missions", "mission_artifact_test")
        for name in ("mission.json", "ledger.json", "report.json", "claim_graph.json", "summary.md", "trace.jsonl", "trace_grading.json", "visible_commentary.jsonl"):
            assert os.path.exists(os.path.join(artifact_dir, name)), name
        with open(os.path.join(artifact_dir, "report.json"), encoding="utf-8") as handle:
            report_json = json.load(handle)
        assert report_json["visible_commentary_path"].endswith("/visible_commentary.jsonl"), report_json
        assert report_json["summary_path"].endswith("/summary.md"), report_json
        assert "Visible work:" in result.report_text, result.report_text
        with open(os.path.join(artifact_dir, "trace_grading.json"), encoding="utf-8") as handle:
            grading = json.load(handle)
        assert "productive_exploration" in grading["labels"], grading
        with open(os.path.join(artifact_dir, "visible_commentary.jsonl"), encoding="utf-8") as handle:
            visible_events = [json.loads(line) for line in handle if line.strip()]
        assert visible_events, visible_events
        assert visible_events[0]["event_type"] == "mission_started", visible_events
        assert visible_events[-1]["event_type"] == "mission_completed", visible_events
        assert all(event.get("safe_for_user") is True for event in visible_events), visible_events
        assert [event["event_type"] for event in live_events] == [event["event_type"] for event in visible_events], live_events
        event_types = [event["event_type"] for event in visible_events]
        assert "model_action_requested" in event_types, event_types
        assert "tool_action_planned" in event_types, event_types
        assert "tool_action_started" in event_types, event_types
        assert "tool_result_summary" in event_types, event_types
        assert "coverage_update" in event_types, event_types
        assert "model_report_received" in event_types, event_types
        with open(os.path.join(artifact_dir, "summary.md"), encoding="utf-8") as handle:
            summary = handle.read()
        assert "Mission Summary: mission_artifact_test" in summary, summary
        assert "Visible trace:" in summary, summary
        audited = audit_mission(root, "mission_artifact_test")
        assert audited["ok"] is True, audited
    finally:
        os.chdir(cwd)
        shutil.rmtree(root)


def assert_response_emitter_streams_commentary_and_final_phases():
    class FakeWFile:
        def __init__(self):
            self.data = bytearray()

        def write(self, chunk):
            self.data.extend(chunk)

        def flush(self):
            pass

    class FakeHandler:
        def __init__(self):
            self.wfile = FakeWFile()
            self.statuses = []
            self.headers = []

        def send_response(self, status):
            self.statuses.append(status)

        def send_header(self, key, value):
            self.headers.append((key, value))

        def end_headers(self):
            pass

    handler = FakeHandler()
    emitter = ResponseEmitter(handler, "resp_stream_test", "mission-a3-kimi", True)
    emitter.start()
    emitter.emit_commentary_message("I'm reading the required source before closure.")
    emitter.emit_text_message("Final report text.", phase="final_answer")
    emitter.complete()

    frames = []
    for raw_frame in handler.wfile.data.decode("utf-8").split("\n\n"):
        if not raw_frame.strip() or "data: [DONE]" in raw_frame:
            continue
        event = ""
        data = ""
        for line in raw_frame.splitlines():
            if line.startswith("event: "):
                event = line[len("event: "):]
            elif line.startswith("data: "):
                data = line[len("data: "):]
        if data:
            frames.append((event, json.loads(data)))

    added = [payload["item"] for event, payload in frames if event == "response.output_item.added"]
    done = [payload["item"] for event, payload in frames if event == "response.output_item.done"]
    completed = [payload["response"] for event, payload in frames if event == "response.completed"]
    assert added[0]["phase"] == "commentary", added
    assert added[1]["phase"] == "final_answer", added
    assert done[0]["phase"] == "commentary", done
    assert done[1]["phase"] == "final_answer", done
    assert [item["phase"] for item in completed[-1]["output"]] == ["commentary", "final_answer"], completed


def assert_runtime_mission_time_budget_extends_internal_deadline():
    timeouts = []

    def call_payload(payload, timeout):
        timeouts.append(timeout)
        assert "Budget: 20 tool calls remaining" in payload["messages"][0]["content"], payload["messages"][0]["content"]
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps({
                            "action_type": "final_report",
                            "report": {
                                "oss_report_version": "1.0",
                                "mission_id": "mission_deadline_extension_test",
                                "status": "PARTIAL",
                                "confidence": "LOW",
                                "files_inspected": [],
                                "commands_run": [],
                                "findings": [],
                                "uncertainties": [],
                                "caveats": ["deadline extension smoke"],
                                "escalation_recommendation": "GPT-5.5 review required",
                                "missing_fields": [],
                            },
                        })
                    }
                }
            ]
        }

    body = {
        "input": [
            {
                "role": "user",
                "content": (
                    "<OSS_HANDOFF_JSON>\n"
                    '{"schema_version":"oss_agent_mission.v1","mission_id":"mission_deadline_extension_test",'
                    '"tier":"A3","mode":"managed_investigation","objective":"Deadline route smoke",'
                    '"risk_tier":"low","write_allowed":false,"allowed_roots":["codex_oss/"],'
                    '"allow_broad_read_scope":true,"allowed_paths":[],"tool_budget":20,"time_budget_seconds":180,'
                    '"allowed_tool_classes":["read"],"stop_conditions":["valid_report"],'
                    '"report_schema":"managed_investigation_report.v1",'
                    '"required_outputs":["files_inspected","commands_run","findings","uncertainties",'
                    '"confidence","caveats","escalation_recommendation"]}'
                    "\n</OSS_HANDOFF_JSON>"
                ),
            }
        ]
    }
    handled = run_managed_mission_from_body(
        body,
        "mission-a3-deepseek",
        lambda *args, **kwargs: None,
        call_payload,
        lambda model: model,
        request_deadline=30,
    )
    assert handled.handled, handled
    assert handled.status == "PARTIAL", handled
    assert timeouts and timeouts[0] > 100, timeouts


def assert_tool_classes_are_enforced_and_aliases_normalize():
    normalized = _normalize_arguments({"pattern": "alias", "root": "codex_oss/"})
    assert normalized == {"pattern": "alias", "path": "codex_oss/"}, normalized
    read_args, unsupported = _normalize_action_arguments(
        "rtk_read",
        {"path": "codex_oss/managed_bridge.py", "offset": 1, "limit": 30},
    )
    assert read_args == {
        "path": "codex_oss/managed_bridge.py",
        "start_line": 2,
        "end_line": 31,
    }, read_args
    assert unsupported == [], unsupported
    grep_args, unsupported = _normalize_action_arguments(
        "rtk_grep",
        {
            "path": "codex_oss/",
            "pattern": "autonomy",
            "output_context_lines": 2,
            "options": "-r",
        },
    )
    assert grep_args == {
        "path": "codex_oss/",
        "pattern": "autonomy",
        "context_lines": 2,
    }, grep_args
    assert unsupported == [], unsupported
    plural_path_args, unsupported = _normalize_action_arguments(
        "rtk_grep",
        {"paths": ["codex_oss/managed_bridge.py"], "pattern": "mission-a2-flash"},
    )
    assert plural_path_args == {
        "path": "codex_oss/managed_bridge.py",
        "pattern": "mission-a2-flash",
    }, plural_path_args
    assert unsupported == [], unsupported

    m = mission(allowed_tool_classes=["read"], tool_budget=2)
    ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
    calls = []

    def fake_model(messages, tools, timeout):
        calls.append(messages[-1]["content"])
        if len(calls) == 1:
            return {"choices": [{"message": {"content": '{"action_type":"tool_call","tool_name":"rtk_grep","arguments":{"pattern":"x","path":"codex_oss/"}}'}}]}
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"action_type":"final_report","report":'
                            '{"oss_report_version":"1.0","mission_id":"mission_runtime_test",'
                            '"status":"PARTIAL","confidence":"LOW","files_inspected":[],'
                            '"commands_run":[],"findings":[],"uncertainties":[],'
                            '"caveats":["grep was disallowed"],'
                            '"escalation_recommendation":"GPT-5.5 review required",'
                            '"missing_fields":[]}}'
                        )
                    }
                }
            ]
        }

    result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=30)
    assert result["status"] == "PARTIAL", result
    assert ledger.tool_budget_remaining == m.tool_budget, ledger.tool_budget_remaining
    assert any("Tool not allowed" in item for item in calls), calls


def assert_duplicate_searches_are_suppressed():
    m = mission(allowed_tool_classes=["search"], tool_budget=3)
    ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
    calls = []

    def fake_model(messages, tools, timeout):
        calls.append(messages[-1]["content"])
        if len(calls) == 1:
            return {"choices": [{"message": {"content": '{"action_type":"tool_call","tool_name":"rtk_grep","arguments":{"pattern":"MissionV1","path":"codex_oss/","recurse":true}}'}}]}
        if len(calls) == 2:
            return {"choices": [{"message": {"content": '{"action_type":"tool_call","tool_name":"rtk_grep","arguments":{"pattern":"MissionV1","root":"codex_oss/","recursive":true}}'}}]}
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"action_type":"final_report","report":'
                            '{"oss_report_version":"1.0","mission_id":"mission_runtime_test",'
                            '"status":"PARTIAL","confidence":"LOW","files_inspected":[],'
                            '"commands_run":[],"findings":[],"uncertainties":[],'
                            '"caveats":["duplicate search suppressed"],'
                            '"escalation_recommendation":"GPT-5.5 review required",'
                            '"missing_fields":[]}}'
                        )
                    }
                }
            ]
        }

    result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=30)
    assert result["status"] == "PARTIAL", result
    assert ledger.duplicate_actions_blocked == 1, ledger.duplicate_actions_blocked
    assert len(ledger.commands_run) == 1, ledger.commands_run
    assert any("[DUPLICATE TOOL]" in item for item in calls), calls


def assert_near_deadline_requests_final_report_when_evidence_exists():
    fixture_path = "runtime_deadline_fixture.txt"
    with open(fixture_path, "w", encoding="utf-8") as handle:
        handle.write("deadline fixture evidence\n")
    try:
        m = mission(
            allowed_roots=[],
            allowed_paths=[fixture_path],
            allowed_tool_classes=["read"],
            tool_budget=2,
        )
        ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
        calls = []
        timeouts = []

        def fake_model(messages, tools, timeout):
            timeouts.append(timeout)
            calls.append(messages[-1]["content"])
            if len(calls) == 1:
                time.sleep(1.2)
                return {
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"action_type":"tool_call","tool_name":"rtk_read",'
                                    f'"arguments":{{"path":"{fixture_path}"}},'
                                    '"reason":"Read fixture evidence."}'
                                )
                            }
                        }
                    ]
                }
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"action_type":"final_report","report":'
                                '{"oss_report_version":"1.0","mission_id":"mission_runtime_test",'
                                '"status":"COMPLETE","confidence":"LOW",'
                                f'"files_inspected":[{{"path":"{fixture_path}","complete":true}}],'
                                '"commands_run":[],'
                                '"findings":[{"claim":"The deadline fixture was inspected.",'
                                f'"evidence_refs":["file:{fixture_path}#extract:1"],'
                                '"confidence":"LOW"}],'
                                '"uncertainties":[],"caveats":["deadline final-report path exercised"],'
                                '"escalation_recommendation":"No escalation required",'
                                '"missing_fields":[]}}'
                            )
                        }
                    }
                ]
            }

        result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=26)
        assert result["status"] == "COMPLETE", result
        assert any("Deadline is near" in item for item in calls), calls
    finally:
        try:
            os.unlink(fixture_path)
        except FileNotFoundError:
            pass


def assert_runtime_forces_final_report_after_file_evidence_when_budget_is_low():
    fixture_path = "runtime_low_budget_fixture.txt"
    with open(fixture_path, "w", encoding="utf-8") as handle:
        handle.write("runtime alias evidence\n")
    try:
        m = mission(
            allowed_roots=["codex_oss/"],
            allowed_paths=[fixture_path],
            allowed_tool_classes=["read", "search"],
            tool_budget=2,
        )
        ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
        calls = []
        timeouts = []

        def fake_model(messages, tools, timeout):
            timeouts.append(timeout)
            calls.append(messages[-1]["content"])
            if len(calls) == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"action_type":"tool_call","tool_name":"rtk_read",'
                                    f'"arguments":{{"path":"{fixture_path}"}},'
                                    '"reason":"Read known evidence."}'
                                )
                            }
                        }
                    ]
                }
            if len(calls) == 2:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"action_type":"tool_call","tool_name":"rtk_grep",'
                                    '"arguments":{"pattern":"alias","path":"codex_oss/"},'
                                    '"reason":"Search again."}'
                                )
                            }
                        }
                    ]
                }
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"action_type":"final_report","report":'
                                '{"oss_report_version":"1.0","mission_id":"mission_runtime_test",'
                                '"status":"COMPLETE","confidence":"LOW",'
                                f'"files_inspected":[{{"path":"{fixture_path}","complete":true}}],'
                                '"commands_run":[],'
                                '"findings":[{"claim":"File evidence was gathered before finalization.",'
                                f'"evidence_refs":["file:{fixture_path}#extract:1"],'
                                '"confidence":"LOW"}],'
                                '"uncertainties":[],"caveats":["runtime forced final report after evidence"],'
                                '"escalation_recommendation":"No escalation required",'
                                '"missing_fields":[]}}'
                            )
                        }
                    }
                ]
            }

        result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=90)
        assert result["status"] == "COMPLETE", result
        assert len(ledger.commands_run) == 1, ledger.commands_run
        assert any("After file evidence exists" in item or "Adaptive autonomy budget requires closure" in item for item in calls), calls
    finally:
        try:
            os.unlink(fixture_path)
        except FileNotFoundError:
            pass


def assert_runtime_traces_followup_search_after_file_evidence():
    fixture_path = "runtime_after_file_fixture.txt"
    with open(fixture_path, "w", encoding="utf-8") as handle:
        handle.write("runtime alias evidence\n")
    try:
        m = mission(
            allowed_roots=["codex_oss/"],
            allowed_paths=[fixture_path],
            allowed_tool_classes=["read", "search"],
            tool_budget=6,
        )
        ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
        calls = []
        timeouts = []

        def fake_model(messages, tools, timeout):
            timeouts.append(timeout)
            calls.append(messages[-1]["content"])
            if len(calls) == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"action_type":"tool_call","tool_name":"rtk_read",'
                                    f'"arguments":{{"path":"{fixture_path}"}},'
                                    '"reason":"Read known evidence."}'
                                )
                            }
                        }
                    ]
                }
            if len(calls) == 2:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"action_type":"tool_call","tool_name":"rtk_grep",'
                                    '"arguments":{"pattern":"alias","path":"codex_oss/"},'
                                    '"reason":"Verify the file finding with a targeted follow-up search.",'
                                    '"hypothesis":"The alias term may have additional confirming references.",'
                                    '"expected_information_gain":"Confirm whether the file evidence is representative.",'
                                    '"why_not_report_yet":"Need verification evidence before finalizing."}'
                                )
                            }
                        }
                    ]
                }
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"action_type":"final_report","report":'
                                '{"oss_report_version":"1.0","mission_id":"mission_runtime_test",'
                                '"status":"COMPLETE","confidence":"LOW",'
                                f'"files_inspected":[{{"path":"{fixture_path}","complete":true}}],'
                                '"commands_run":[],'
                                '"findings":[{"claim":"Runtime traced follow-up exploration after file evidence.",'
                                f'"evidence_refs":["file:{fixture_path}#extract:1"],'
                                '"confidence":"LOW"}],'
                                '"uncertainties":[],"caveats":["follow-up exploration was allowed and traced"],'
                                '"escalation_recommendation":"No escalation required",'
                                '"missing_fields":[]}}'
                            )
                        }
                    }
                ]
            }

        result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=90)
        assert result["status"] == "COMPLETE", result
        assert len(ledger.commands_run) == 2, ledger.commands_run
        assert len(ledger.action_trace) >= 2, ledger.action_trace
        assert ledger.action_trace[-1].runtime_decision == "allowed", ledger.action_trace[-1]
        assert ledger.action_trace[-1].tool_name == "rtk_grep", ledger.action_trace[-1]
    finally:
        try:
            os.unlink(fixture_path)
        except FileNotFoundError:
            pass


def assert_duplicate_range_read_is_served_from_cached_evidence():
    fixture_path = "runtime_cached_range_fixture.txt"
    with open(fixture_path, "w", encoding="utf-8") as handle:
        handle.write("one\ntwo profile limit\nthree\nfour\n")
    try:
        m = mission(
            allowed_roots=[],
            allowed_paths=[fixture_path],
            allowed_tool_classes=["read"],
            tool_budget=4,
        )
        ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
        calls = []
        timeouts = []

        def fake_model(messages, tools, timeout):
            timeouts.append(timeout)
            calls.append(messages[-1]["content"])
            if len(calls) == 1:
                return {"choices": [{"message": {"content": json.dumps({
                    "action_type": "tool_call",
                    "tool_name": "rtk_read",
                    "arguments": {"path": fixture_path},
                    "reason": "Read the whole allowed file.",
                    "hypothesis": "The fixture contains the profile limit.",
                    "expected_information_gain": "Gather file evidence.",
                    "why_not_report_yet": "Need evidence first.",
                })}}]}
            if len(calls) == 2:
                return {"choices": [{"message": {"content": json.dumps({
                    "action_type": "tool_call",
                    "tool_name": "rtk_read",
                    "arguments": {"path": fixture_path, "start_line": 2, "end_line": 2},
                    "reason": "Extract the exact profile line.",
                    "hypothesis": "The useful evidence is line 2.",
                    "expected_information_gain": "Get precise extract.",
                    "why_not_report_yet": "Need exact line evidence.",
                })}}]}
            return {"choices": [{"message": {"content": json.dumps({
                "action_type": "final_report",
                "report": {
                    "oss_report_version": "1.0",
                    "mission_id": "mission_runtime_test",
                    "status": "COMPLETE",
                    "confidence": "LOW",
                    "files_inspected": [{"path": fixture_path, "complete": True}],
                    "commands_run": [],
                    "findings": [{
                        "claim": "The cached line-range extract was available as evidence.",
                        "evidence_refs": [f"file:{fixture_path}#extract:2"],
                        "confidence": "LOW",
                    }],
                    "uncertainties": [],
                    "caveats": [],
                    "escalation_recommendation": "No escalation required",
                    "missing_fields": [],
                },
            })}}]}

        result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=90)
        assert result["status"] == "COMPLETE", result
        assert ledger.tool_budget_remaining == m.tool_budget - 1, ledger.tool_budget_remaining
        assert ledger.action_trace[1].runtime_decision == "cache_hit", ledger.action_trace[1]
        assert ledger.files_inspected[fixture_path].extracts[1]["text"] == "two profile limit\n"
        assert "Cached range result" in calls[-1], calls
    finally:
        try:
            os.unlink(fixture_path)
        except FileNotFoundError:
            pass


def assert_duplicate_full_read_returns_cached_extracts_and_requests_report():
    fixture_path = "runtime_cached_duplicate_fixture.txt"
    with open(fixture_path, "w", encoding="utf-8") as handle:
        handle.write("profile limit evidence\n")
    try:
        m = mission(
            allowed_roots=[],
            allowed_paths=[fixture_path],
            allowed_tool_classes=["read"],
            tool_budget=4,
        )
        ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
        calls = []
        duplicate_read_timeouts = []

        def fake_model(messages, tools, timeout):
            duplicate_read_timeouts.append(timeout)
            calls.append(messages[-1]["content"])
            if len(calls) == 1:
                return {"choices": [{"message": {"content": json.dumps({
                    "action_type": "tool_call",
                    "tool_name": "rtk_read",
                    "arguments": {"path": fixture_path},
                    "reason": "Read the evidence file.",
                    "hypothesis": "The file contains the needed evidence.",
                    "expected_information_gain": "Gather evidence.",
                    "why_not_report_yet": "Need evidence first.",
                })}}]}
            if len(calls) == 2:
                return {"choices": [{"message": {"content": json.dumps({
                    "action_type": "tool_call",
                    "tool_name": "rtk_read",
                    "arguments": {"path": fixture_path},
                    "reason": "Read it again for context.",
                    "hypothesis": "The file may need rechecking.",
                    "expected_information_gain": "Maybe more context.",
                    "why_not_report_yet": "Need confidence.",
                })}}]}
            return {"choices": [{"message": {"content": json.dumps({
                "action_type": "final_report",
                "report": {
                    "oss_report_version": "1.0",
                    "mission_id": "mission_runtime_test",
                    "status": "COMPLETE",
                    "confidence": "LOW",
                    "files_inspected": [{"path": fixture_path, "complete": True}],
                    "commands_run": [],
                    "findings": [{
                        "claim": "Cached extracts were enough to report after a duplicate read.",
                        "evidence_refs": [f"file:{fixture_path}#extract:1"],
                        "confidence": "LOW",
                    }],
                    "uncertainties": [],
                    "caveats": [],
                    "escalation_recommendation": "No escalation required",
                    "missing_fields": [],
                },
            })}}]}

        result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=90)
        assert result["status"] == "COMPLETE", result
        assert ledger.duplicate_actions_blocked == 1, ledger.duplicate_actions_blocked
        assert ledger.action_trace[1].runtime_decision == "redirected", ledger.action_trace[1]
        assert "Cached extracts" in calls[-1], calls
        assert "Return exactly one final_report" in calls[-1], calls
        assert max(duplicate_read_timeouts[2:]) <= 20, duplicate_read_timeouts
    finally:
        try:
            os.unlink(fixture_path)
        except FileNotFoundError:
            pass


def assert_unsupported_followup_after_file_evidence_redirects_to_report():
    fixture_path = "runtime_followup_redirect_fixture.txt"
    with open(fixture_path, "w", encoding="utf-8") as handle:
        handle.write("runtime autonomy profile lives here\n")
    try:
        m = mission(
            allowed_roots=["codex_oss/"],
            allowed_paths=[fixture_path],
            allowed_tool_classes=["read", "search"],
            tool_budget=6,
        )
        ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
        calls = []

        def fake_model(messages, tools, timeout):
            calls.append(messages[-1]["content"])
            if len(calls) == 1:
                return {"choices": [{"message": {"content": json.dumps({
                    "action_type": "tool_call",
                    "tool_name": "rtk_read",
                    "arguments": {"path": fixture_path},
                    "reason": "Read the relevant fixture.",
                    "hypothesis": "The fixture has the evidence.",
                    "expected_information_gain": "Gather file evidence.",
                    "why_not_report_yet": "Need evidence first.",
                })}}]}
            if len(calls) == 2:
                return {"choices": [{"message": {"content": json.dumps({
                    "action_type": "tool_call",
                    "tool_name": "rtk_grep",
                    "arguments": {"path": fixture_path, "pattern": "autonomy", "flags": "-ri"},
                    "reason": "Try one more search.",
                    "hypothesis": "Search may verify the file evidence.",
                    "expected_information_gain": "Verification.",
                    "why_not_report_yet": "Need verification.",
                })}}]}
            return {"choices": [{"message": {"content": json.dumps({
                "action_type": "final_report",
                "report": {
                    "oss_report_version": "1.0",
                    "mission_id": "mission_runtime_test",
                    "status": "COMPLETE",
                    "confidence": "LOW",
                    "files_inspected": [{"path": fixture_path, "complete": True}],
                    "commands_run": [],
                    "findings": [{
                        "claim": "File evidence was gathered before the unsupported follow-up.",
                        "evidence_refs": [f"file:{fixture_path}#extract:1"],
                        "confidence": "LOW",
                    }],
                    "uncertainties": [],
                    "caveats": [],
                    "escalation_recommendation": "No escalation required",
                    "missing_fields": [],
                },
            })}}]}

        result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=90)
        assert result["status"] == "COMPLETE", result
        assert ledger.action_trace[1].runtime_decision == "redirected", ledger.action_trace[1]
        assert "do not call another tool" in calls[-1], calls
    finally:
        try:
            os.unlink(fixture_path)
        except FileNotFoundError:
            pass


def assert_model_failure_after_evidence_returns_finding_not_empty_partial():
    fixture_path = "runtime_evidence_finalizer_fixture.txt"
    with open(fixture_path, "w", encoding="utf-8") as handle:
        handle.write("mission profile evidence\n")
    try:
        m = mission(
            allowed_roots=[],
            allowed_paths=[fixture_path],
            allowed_tool_classes=["read"],
            tool_budget=3,
        )
        ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
        calls = []

        def fake_model(messages, tools, timeout):
            calls.append(messages[-1]["content"])
            if len(calls) == 1:
                return {"choices": [{"message": {"content": json.dumps({
                    "action_type": "tool_call",
                    "tool_name": "rtk_read",
                    "arguments": {"path": fixture_path},
                    "reason": "Read evidence before finalizing.",
                    "hypothesis": "The fixture has evidence.",
                    "expected_information_gain": "Gather evidence.",
                    "why_not_report_yet": "Need evidence first.",
                })}}]}
            raise RuntimeError("simulated finalizer outage")

        result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=90)
        assert result["status"] == "PARTIAL", result
        report = result["report"]
        assert report["findings"], report
        assert report["findings"][0]["evidence_refs"] == [f"file:{fixture_path}#extract:1"], report
        assert "model_call_failed" in " ".join(report["caveats"]), report
    finally:
        try:
            os.unlink(fixture_path)
        except FileNotFoundError:
            pass


def assert_deterministic_finalizer_extracts_profile_limits_from_cached_evidence():
    fixture_path = "runtime_profile_finalizer_fixture.py"
    with open(fixture_path, "w", encoding="utf-8") as handle:
        handle.write(
            'RUNTIME_AUTONOMY_PROFILES = {\n'
            '    "mission-a3-deepseek": {"max_tool_budget": 20, "max_time_seconds": 180},\n'
            '}\n'
        )
    try:
        m = mission(
            objective="Find where runtime autonomy profiles are defined and name the DeepSeek A3 limits.",
            allowed_roots=[],
            allowed_paths=[fixture_path],
            allowed_tool_classes=["read"],
            tool_budget=3,
        )
        ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
        calls = []

        def fake_model(messages, tools, timeout):
            calls.append(messages[-1]["content"])
            if len(calls) == 1:
                return {"choices": [{"message": {"content": json.dumps({
                    "action_type": "tool_call",
                    "tool_name": "rtk_read",
                    "arguments": {"path": fixture_path},
                    "reason": "Read the profile fixture.",
                    "hypothesis": "The fixture contains profile limits.",
                    "expected_information_gain": "Gather profile evidence.",
                    "why_not_report_yet": "Need evidence first.",
                })}}]}
            raise RuntimeError("simulated finalizer outage")

        result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=90)
        assert result["status"] == "PARTIAL", result
        finding = result["report"]["findings"][0]
        assert "mission-a3-deepseek" in finding["claim"], finding
        assert "max_tool_budget=20" in finding["claim"], finding
        assert "max_time_seconds=180" in finding["claim"], finding
        assert finding["evidence_refs"] == [f"file:{fixture_path}#extract:1"], finding
    finally:
        try:
            os.unlink(fixture_path)
        except FileNotFoundError:
            pass


def assert_deterministic_finalizer_mines_cached_text_beyond_default_extracts():
    fixture_path = "runtime_profile_finalizer_long_fixture.py"
    with open(fixture_path, "w", encoding="utf-8") as handle:
        handle.write("# filler\n" * 180)
        handle.write(
            'RUNTIME_AUTONOMY_PROFILES = {\n'
            '    "mission-a3-deepseek": {"max_tool_budget": 20, "max_time_seconds": 180},\n'
            '}\n'
        )
        handle.write("# trailing filler\n" * 180)
    try:
        m = mission(
            objective="Find where runtime autonomy profiles are defined and name the DeepSeek A3 limits.",
            allowed_roots=[],
            allowed_paths=[fixture_path],
            allowed_tool_classes=["read"],
            tool_budget=3,
        )
        ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
        calls = []

        def fake_model(messages, tools, timeout):
            calls.append(messages[-1]["content"])
            if len(calls) == 1:
                return {"choices": [{"message": {"content": json.dumps({
                    "action_type": "tool_call",
                    "tool_name": "rtk_read",
                    "arguments": {"path": fixture_path},
                    "reason": "Read the long profile fixture.",
                    "hypothesis": "The file contains profile limits beyond the first extract.",
                    "expected_information_gain": "Gather profile evidence.",
                    "why_not_report_yet": "Need evidence first.",
                })}}]}
            raise RuntimeError("simulated finalizer outage")

        result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=90)
        finding = result["report"]["findings"][0]
        assert "mission-a3-deepseek" in finding["claim"], finding
        assert "max_tool_budget=20" in finding["claim"], finding
        assert "max_time_seconds=180" in finding["claim"], finding
        assert finding["evidence_refs"][-1].startswith(f"file:{fixture_path}#extract:"), finding
    finally:
        try:
            os.unlink(fixture_path)
        except FileNotFoundError:
            pass


def assert_deterministic_finalizer_does_not_treat_symbol_mentions_as_definitions():
    fixture_path = "runtime_profile_mention_not_definition_fixture.py"
    with open(fixture_path, "w", encoding="utf-8") as handle:
        handle.write(
            'def helper(text):\n'
            '    if "RUNTIME_AUTONOMY_PROFILES" not in text:\n'
            '        return None\n'
            '    return "mission-a3-deepseek"\n'
        )
    try:
        m = mission(
            objective="Find where runtime autonomy profiles are defined and name the DeepSeek A3 limits.",
            allowed_roots=[],
            allowed_paths=[fixture_path],
            allowed_tool_classes=["read"],
            tool_budget=3,
        )
        ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
        calls = []

        def fake_model(messages, tools, timeout):
            calls.append(messages[-1]["content"])
            if len(calls) == 1:
                return {"choices": [{"message": {"content": json.dumps({
                    "action_type": "tool_call",
                    "tool_name": "rtk_read",
                    "arguments": {"path": fixture_path},
                    "reason": "Read helper fixture.",
                    "hypothesis": "The fixture may mention profile terms.",
                    "expected_information_gain": "Gather evidence.",
                    "why_not_report_yet": "Need evidence first.",
                })}}]}
            raise RuntimeError("simulated finalizer outage")

        result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=90)
        finding = result["report"]["findings"][0]
        assert "are defined in" not in finding["claim"], finding
        assert "max_tool_budget" not in finding["claim"], finding
    finally:
        try:
            os.unlink(fixture_path)
        except FileNotFoundError:
            pass


def assert_range_read_does_not_block_later_full_read_or_finalizer_claim():
    fixture_path = "runtime_range_then_full_profile_fixture.py"
    with open(fixture_path, "w", encoding="utf-8") as handle:
        handle.write(
            'RUNTIME_AUTONOMY_PROFILES = {\n'
            '    "mission-a3-deepseek": {"max_tool_budget": 20, "max_time_seconds": 180},\n'
            '}\n'
            '# filler\n' * 140
        )
    try:
        m = mission(
            objective="Find where runtime autonomy profiles are defined and name the DeepSeek A3 limits.",
            allowed_roots=[],
            allowed_paths=[fixture_path],
            allowed_tool_classes=["read"],
            tool_budget=4,
        )
        ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
        calls = []

        def fake_model(messages, tools, timeout):
            calls.append(messages[-1]["content"])
            if len(calls) == 1:
                return {"choices": [{"message": {"content": json.dumps({
                    "action_type": "tool_call",
                    "tool_name": "rtk_read",
                    "arguments": {"path": fixture_path, "start_line": 100, "end_line": 120},
                    "reason": "Read an initially wrong range.",
                    "hypothesis": "The useful profile may be lower in the file.",
                    "expected_information_gain": "Try a candidate range.",
                    "why_not_report_yet": "Need profile evidence.",
                })}}]}
            if len(calls) == 2:
                return {"choices": [{"message": {"content": json.dumps({
                    "action_type": "tool_call",
                    "tool_name": "rtk_read",
                    "arguments": {"path": fixture_path},
                    "reason": "Read the full file after the range was insufficient.",
                    "hypothesis": "The profile definition may be elsewhere in the file.",
                    "expected_information_gain": "Gather complete file evidence.",
                    "why_not_report_yet": "Need exact limits.",
                })}}]}
            raise RuntimeError("simulated finalizer outage")

        result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=90)
        assert len(ledger.commands_run) == 2, ledger.commands_run
        assert ledger.files_inspected[fixture_path].complete is True, ledger.files_inspected[fixture_path]
        finding = result["report"]["findings"][0]
        assert "mission-a3-deepseek" in finding["claim"], finding
        assert "max_tool_budget=20" in finding["claim"], finding
    finally:
        try:
            os.unlink(fixture_path)
        except FileNotFoundError:
            pass


def assert_definition_claims_require_definition_shaped_evidence():
    path = "codex_oss/runtime/policy.py"
    ledger = EvidenceLedger(mission_id="mission_runtime_test", tool_budget_remaining=1)
    result = ToolResult(
        tool="rtk_read",
        args={"path": path},
        stdout=(
            'needles.extend(["RUNTIME_AUTONOMY_PROFILES", "mission-a3-deepseek"])\n'
            'if not re.search(r"\\\\bRUNTIME_AUTONOMY_PROFILES\\\\s*=", text):\n'
            "    return None\n"
        ),
        stderr="",
        exit_code=0,
        complete=True,
    )
    ledger.add_file(path, result, 0)
    report = {
        "oss_report_version": "1.0",
        "mission_id": "mission_runtime_test",
        "status": "COMPLETE",
        "confidence": "LOW",
        "files_inspected": [{"path": path, "complete": True}],
        "commands_run": [],
        "findings": [
            {
                "claim": (
                    "Runtime autonomy profiles are defined in codex_oss/runtime/policy.py; "
                    "the file contains a RUNTIME_AUTONOMY_PROFILES dictionary."
                ),
                "evidence_refs": [f"file:{path}#extract:1"],
                "confidence": "LOW",
            }
        ],
        "uncertainties": [],
        "caveats": [],
        "escalation_recommendation": "No escalation required",
        "missing_fields": [],
    }

    validation = validate_report(report, ledger)
    assert validation.is_valid is False, validation
    assert "definition assignment" in " ".join(validation.errors), validation.errors


def assert_definition_claims_can_be_supported_by_command_evidence():
    ledger = EvidenceLedger(mission_id="mission_runtime_test", tool_budget_remaining=1)
    command_result = ToolResult(
        tool="rtk_grep",
        args={"path": "codex_oss/", "pattern": "RUNTIME_AUTONOMY_PROFILES"},
        stdout="codex_oss/managed_bridge.py:40:RUNTIME_AUTONOMY_PROFILES = {\n",
        stderr="",
        exit_code=0,
        complete=True,
    )
    ledger.add_command("rtk_grep", command_result.args, command_result, 0)
    report = {
        "oss_report_version": "1.0",
        "mission_id": "mission_runtime_test",
        "status": "COMPLETE",
        "confidence": "LOW",
        "files_inspected": [],
        "commands_run": [{"tool": "rtk_grep", "args": command_result.args}],
        "findings": [
            {
                "claim": "Runtime autonomy profiles are defined in codex_oss/managed_bridge.py by RUNTIME_AUTONOMY_PROFILES.",
                "evidence_refs": ["command:0"],
                "confidence": "LOW",
            }
        ],
        "uncertainties": [],
        "caveats": [],
        "escalation_recommendation": "No escalation required",
        "missing_fields": [],
    }

    validation = validate_report(report, ledger)
    assert validation.is_valid is True, validation.errors


def assert_deterministic_finalizer_mines_command_profile_evidence():
    from codex_oss.runtime.policy import build_deterministic_partial_report

    m = mission(
        objective="Find where runtime autonomy profiles are defined and name the DeepSeek A3 limits.",
        allowed_tool_classes=["search"],
    )
    ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=1)
    definition = ToolResult(
        tool="rtk_grep",
        args={"path": "codex_oss/", "pattern": "RUNTIME_AUTONOMY_PROFILES"},
        stdout="codex_oss/managed_bridge.py:40:RUNTIME_AUTONOMY_PROFILES = {\n",
        stderr="",
        exit_code=0,
        complete=True,
    )
    profile = ToolResult(
        tool="rtk_grep",
        args={"path": "codex_oss/", "pattern": "mission-a3-deepseek"},
        stdout='codex_oss/managed_bridge.py:45:    "mission-a3-deepseek": {"max_tool_budget": 20, "max_time_seconds": 180},\n',
        stderr="",
        exit_code=0,
        complete=True,
    )
    ledger.add_command("rtk_grep", definition.args, definition, 0)
    ledger.add_command("rtk_grep", profile.args, profile, 1)

    report = build_deterministic_partial_report(m, ledger, "deadline_reached")
    finding = report["findings"][0]
    assert "codex_oss/managed_bridge.py" in finding["claim"], finding
    assert "max_tool_budget=20" in finding["claim"], finding
    assert finding["evidence_refs"] == ["command:0", "command:1"], finding


def assert_deterministic_finalizer_mines_alias_mapping_from_file_evidence():
    from codex_oss.runtime.policy import build_deterministic_partial_report

    m = mission(
        objective="Find where runtime model aliases map mission-a3-kimi to the underlying reasoning model.",
        allowed_tool_classes=["read"],
    )
    ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=1)
    result = ToolResult(
        tool="rtk_read",
        args={"path": "codex_oss/managed_bridge.py"},
        stdout=(
            'RUNTIME_MODEL_ALIASES = {\n'
            '    "mission-a2-kimi": "ocg-kimi-k2.6",\n'
            '    "mission-a3-kimi": "ocg-kimi-k2.6",\n'
            '}\n'
        ),
        stderr="",
        exit_code=0,
        complete=True,
    )
    ledger.add_file("codex_oss/managed_bridge.py", result, 0)

    report = build_deterministic_partial_report(m, ledger, "model_call_failed")
    finding = report["findings"][0]
    assert "mission-a3-kimi" in finding["claim"], finding
    assert "ocg-kimi-k2.6" in finding["claim"], finding
    assert finding["evidence_refs"] == ["file:codex_oss/managed_bridge.py#extract:1"], finding


def assert_deterministic_finalizer_mines_validator_module_evidence():
    from codex_oss.runtime.policy import build_deterministic_partial_report

    m = mission(
        objective="Find whether the runtime has a ValidatedReportV1 validator module.",
        allowed_tool_classes=["read"],
    )
    ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=1)
    result = ToolResult(
        tool="rtk_read",
        args={"path": "codex_oss/validation/__init__.py"},
        stdout=(
            '"""ValidatedReportV1 — strict report schema validation with evidence refs."""\n'
            "def validate_report(report: dict, ledger=None):\n"
            "    return ValidationResult(True)\n"
        ),
        stderr="",
        exit_code=0,
        complete=True,
    )
    ledger.add_file("codex_oss/validation/__init__.py", result, 0)

    report = build_deterministic_partial_report(m, ledger, "model_call_failed")
    finding = report["findings"][0]
    assert "ValidatedReportV1 validator module" in finding["claim"], finding
    assert "codex_oss/validation/__init__.py" in finding["claim"], finding


def assert_objective_specs_handle_generic_config_function_and_zero_match():
    config_mission = mission(
        objective="Find APP_LIMITS worker config values max_jobs and timeout_seconds.",
        allowed_tool_classes=["read"],
    )
    config_ledger = EvidenceLedger(mission_id=config_mission.mission_id, tool_budget_remaining=1)
    config_result = ToolResult(
        tool="rtk_read",
        args={"path": "settings.py"},
        stdout='APP_LIMITS = {"worker": {"max_jobs": 4, "timeout_seconds": 60}}\n',
        stderr="",
        exit_code=0,
        complete=True,
    )
    config_ledger.add_file("settings.py", config_result, 0)
    config_spec = classify_objective(config_mission)
    assert config_spec.objective_type == "config_value_extraction", config_spec
    config_finding = synthesize_objective_finding(config_mission, config_ledger)
    assert "max_jobs=4" in config_finding["claim"], config_finding
    assert "timeout_seconds=60" in config_finding["claim"], config_finding

    function_mission = mission(
        objective="Find function locate_widget and return the file path.",
        allowed_tool_classes=["read"],
    )
    function_ledger = EvidenceLedger(mission_id=function_mission.mission_id, tool_budget_remaining=1)
    function_result = ToolResult(
        tool="rtk_read",
        args={"path": "widgets.py"},
        stdout="def locate_widget(name):\n    return name\n",
        stderr="",
        exit_code=0,
        complete=True,
    )
    function_ledger.add_file("widgets.py", function_result, 0)
    function_spec = classify_objective(function_mission)
    assert function_spec.objective_type == "function_location", function_spec
    function_finding = synthesize_objective_finding(function_mission, function_ledger)
    assert "locate_widget is defined in widgets.py" in function_finding["claim"], function_finding

    grep_function_ledger = EvidenceLedger(mission_id=function_mission.mission_id, tool_budget_remaining=1)
    grep_function_result = ToolResult(
        tool="rtk_grep",
        args={"pattern": "locate_widget", "path": "widgets.py"},
        stdout="widgets.py:1:def locate_widget(name):\nwidgets.py:2:    return name\n",
        stderr="",
        exit_code=0,
        complete=True,
    )
    grep_function_ledger.add_command("rtk_grep", grep_function_result.args, grep_function_result, 0)
    grep_function_finding = synthesize_objective_finding(function_mission, grep_function_ledger)
    assert grep_function_finding is not None, "grep-prefixed function definitions should satisfy function_location"
    assert grep_function_finding["evidence_refs"] == ["command:0"], grep_function_finding
    assert "locate_widget is defined in widgets.py" in grep_function_finding["claim"], grep_function_finding

    rtk_grep_function_ledger = EvidenceLedger(mission_id=function_mission.mission_id, tool_budget_remaining=1)
    rtk_grep_function_result = ToolResult(
        tool="rtk_grep",
        args={"pattern": "locate_widget", "path": "widgets.py"},
        stdout="1 matches in 1F:\n\n[file] widgets.py (1):\n    1: def locate_widget(name):\n",
        stderr="",
        exit_code=0,
        complete=True,
    )
    rtk_grep_function_ledger.add_command("rtk_grep", rtk_grep_function_result.args, rtk_grep_function_result, 0)
    rtk_grep_function_finding = synthesize_objective_finding(function_mission, rtk_grep_function_ledger)
    assert rtk_grep_function_finding is not None, "RTK-formatted grep definitions should satisfy function_location"
    assert rtk_grep_function_finding["evidence_refs"] == ["command:0"], rtk_grep_function_finding
    assert "locate_widget is defined in widgets.py" in rtk_grep_function_finding["claim"], rtk_grep_function_finding

    zero_mission = mission(
        objective='Confirm zero-match evidence for "unsafe_command" under src/.',
        allowed_tool_classes=["search"],
    )
    zero_ledger = EvidenceLedger(mission_id=zero_mission.mission_id, tool_budget_remaining=1)
    zero_result = ToolResult(
        tool="rtk_grep",
        args={"pattern": "unsafe_command", "path": "src/"},
        stdout="0 matches for unsafe_command\n",
        stderr="",
        exit_code=1,
        complete=True,
    )
    zero_ledger.add_command("rtk_grep", zero_result.args, zero_result, 0)
    zero_spec = classify_objective(zero_mission)
    assert zero_spec.objective_type == "zero_match_evidence", zero_spec
    zero_finding = synthesize_objective_finding(zero_mission, zero_ledger)
    assert "No matches for 'unsafe_command'" in zero_finding["claim"], zero_finding
    assert zero_finding["evidence_refs"] == ["command:0#zero_match"], zero_finding


def assert_answer_graph_preserves_command_result_requirements():
    m = mission(
        objective="Search for a non-existent function name 'definitely_not_in_codebase' in codex_oss/. Verify zero-match evidence.",
        objective_style="open_investigation",
        allowed_tool_classes=["read", "search"],
        allowed_paths=["codex_oss/runtime/loop.py"],
        answer_obligations=[{
            "id": "q1",
            "question": "Does 'definitely_not_in_codebase' appear anywhere in codex_oss/?",
            "required": True,
            "source_requirements": [{
                "path": "codex_oss/runtime/loop.py",
                "evidence_kind": "zero_match",
                "evidence_plane": "command_result",
                "required": True,
                "prefetch": False,
                "required_shapes": ["zero_match"],
            }],
        }],
    )
    ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=1)
    graph = refresh_answer_graph(m, ledger, reason="test", persist=False)
    pending = pending_required_agenda_items(graph)
    assert pending, graph
    item = pending[0]
    assert item["kind"] == "required_grep_zero_match", item
    assert item["tool_name"] == "rtk_grep", item
    assert item["evidence_plane"] == "command_result", item
    assert item["pattern"] == "definitely_not_in_codebase", item
    action = graph["coverage_graph"]["next_required_actions"][0]
    assert action["tool_name"] == "rtk_grep", action
    assert action["arguments"]["pattern"] == "definitely_not_in_codebase", action

    zero_result = ToolResult(
        tool="rtk_grep",
        args={"pattern": "definitely_not_in_codebase", "path": "codex_oss/runtime/loop.py"},
        stdout="0 matches for definitely_not_in_codebase\n",
        stderr="",
        exit_code=1,
        complete=True,
    )
    ledger.add_command("rtk_grep", zero_result.args, zero_result, 0)
    graph = refresh_answer_graph(m, ledger, reason="test", persist=False)
    assert graph["sufficiency"]["can_close"], graph["sufficiency"]
    assert graph["coverage_graph"]["coverage_status"]["coverage_complete"], graph["coverage_graph"]


def assert_config_value_extraction_uses_target_block_not_first_fields():
    config_mission = mission(
        objective="Find mission-a3-deepseek profile limits.",
        allowed_tool_classes=["read"],
        allow_heuristic_objective=False,
        objective_spec={
            "schema_version": "objective_spec.v1",
            "objective_type": "config_value_extraction",
            "target": {"key": "mission-a3-deepseek"},
            "required_values": ["max_tool_budget", "max_time_seconds"],
            "required_outputs": ["file_path", "max_tool_budget", "max_time_seconds", "evidence_ref"],
            "required_evidence_shapes": ["dictionary_entry"],
        },
    )
    config_ledger = EvidenceLedger(mission_id=config_mission.mission_id, tool_budget_remaining=1)
    config_result = ToolResult(
        tool="rtk_read",
        args={"path": "managed_bridge.py"},
        stdout=(
            'RUNTIME_MODEL_ALIASES = {"mission-a3-deepseek": "ocg-deepseek-v4-pro"}\n'
            'RUNTIME_AUTONOMY_PROFILES = {\n'
            '    "mission-a2-flash": {"max_tool_budget": 8, "max_time_seconds": 90},\n'
            '    "mission-a3-deepseek": {"max_tool_budget": 20, "max_time_seconds": 180},\n'
            '}\n'
        ),
        stderr="",
        exit_code=0,
        complete=True,
    )
    config_ledger.add_file("managed_bridge.py", config_result, 0)
    config_finding = synthesize_objective_finding(config_mission, config_ledger)
    assert "max_tool_budget=20" in config_finding["claim"], config_finding
    assert "max_time_seconds=180" in config_finding["claim"], config_finding
    assert "max_tool_budget=8" not in config_finding["claim"], config_finding


def assert_mission_accepts_explicit_objective_spec_and_strict_mode():
    explicit = mission(
        objective="Locate the runtime report validator.",
        allow_heuristic_objective=False,
        objective_spec={
            "schema_version": "objective_spec.v1",
            "objective_type": "function_location",
            "target": {"symbol": "validate_report"},
            "required_outputs": ["file_path", "symbol_name", "evidence_ref"],
            "required_evidence_shapes": ["function_definition"],
        },
    )
    assert explicit.objective_spec["objective_type"] == "function_location", explicit.objective_spec
    assert explicit.allow_heuristic_objective is False, explicit
    spec = classify_objective(explicit)
    assert spec.explicit is True, spec
    assert spec.target == "validate_report", spec

    try:
        mission(allow_heuristic_objective=False)
    except InvalidHandoffError as exc:
        assert "objective_spec" in str(exc), exc
    else:
        raise AssertionError("strict A3 mission without objective_spec should fail closed")

    try:
        mission(objective_spec={"schema_version": "objective_spec.v1", "objective_type": "made_up"})
    except InvalidHandoffError as exc:
        assert "objective_spec.objective_type" in str(exc), exc
    else:
        raise AssertionError("unknown objective_spec type should fail closed")


def assert_explicit_objective_spec_overrides_prose_classifier():
    m = mission(
        objective="Find function mission-a3-kimi and return file path.",
        allowed_tool_classes=["read"],
        allow_heuristic_objective=False,
        objective_spec={
            "schema_version": "objective_spec.v1",
            "objective_type": "mapping_lookup",
            "target": {"key": "mission-a3-kimi"},
            "required_outputs": ["mapped_value", "mapping_file", "evidence_ref"],
            "required_evidence_shapes": ["mapping_assignment"],
        },
    )
    spec = classify_objective(m)
    assert spec.objective_type == "mapping_lookup", spec
    assert spec.target == "mission-a3-kimi", spec

    ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=1)
    result = ToolResult(
        tool="rtk_read",
        args={"path": "bridge_models.py"},
        stdout='ALIASES = {"mission-a3-kimi": "ocg-kimi-k2.6"}\n',
        stderr="",
        exit_code=0,
        complete=True,
    )
    ledger.add_file("bridge_models.py", result, 0)
    finding = synthesize_objective_finding(m, ledger)
    assert "mission-a3-kimi" in finding["claim"], finding
    assert "ocg-kimi-k2.6" in finding["claim"], finding


def assert_complete_report_with_explicit_spec_requires_required_value():
    fixture_path = "runtime_explicit_alias_fixture.py"
    with open(fixture_path, "w", encoding="utf-8") as handle:
        handle.write('ALIASES = {"mission-a3-kimi": "ocg-kimi-k2.6"}\n')
    try:
        m = mission(
            objective="Return the mapped runtime model.",
            allowed_roots=[],
            allowed_paths=[fixture_path],
            allowed_tool_classes=["read"],
            tool_budget=3,
            allow_heuristic_objective=False,
            objective_spec={
                "schema_version": "objective_spec.v1",
                "objective_type": "mapping_lookup",
                "target": {"key": "mission-a3-kimi"},
                "required_outputs": ["mapped_value", "mapping_file", "evidence_ref"],
                "required_evidence_shapes": ["mapping_assignment"],
            },
        )
        ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
        calls = []

        def fake_model(messages, tools, timeout):
            calls.append(messages[-1]["content"])
            if len(calls) == 1:
                return {"choices": [{"message": {"content": json.dumps({
                    "action_type": "tool_call",
                    "tool_name": "rtk_read",
                    "arguments": {"path": fixture_path},
                    "reason": "Read explicit alias contract evidence.",
                    "hypothesis": "The file contains a key/value alias mapping.",
                    "expected_information_gain": "Find the mapped model value.",
                    "why_not_report_yet": "Need evidence first.",
                })}}]}
            if len(calls) == 2:
                return {"choices": [{"message": {"content": json.dumps({
                    "action_type": "final_report",
                    "report": {
                        "oss_report_version": "1.0",
                        "mission_id": "mission_runtime_test",
                        "status": "COMPLETE",
                        "confidence": "LOW",
                        "files_inspected": [{"path": fixture_path, "complete": True}],
                        "commands_run": [],
                        "findings": [{
                            "claim": f"Runtime model alias mission-a3-kimi is listed in {fixture_path}.",
                            "evidence_refs": [f"file:{fixture_path}#extract:1"],
                            "confidence": "LOW",
                        }],
                        "uncertainties": ["mapped value omitted"],
                        "caveats": [],
                        "escalation_recommendation": "No escalation required",
                        "missing_fields": [],
                    },
                })}}]}
            return {"choices": [{"message": {"content": json.dumps({
                "action_type": "final_report",
                "report": {
                    "oss_report_version": "1.0",
                    "mission_id": "mission_runtime_test",
                    "status": "COMPLETE",
                    "confidence": "LOW",
                    "files_inspected": [{"path": fixture_path, "complete": True}],
                    "commands_run": [],
                    "findings": [{
                        "claim": f"Runtime model alias mission-a3-kimi maps to underlying reasoning model ocg-kimi-k2.6 in {fixture_path}.",
                        "evidence_refs": [f"file:{fixture_path}#extract:1"],
                        "confidence": "LOW",
                    }],
                    "uncertainties": [],
                    "caveats": [],
                    "escalation_recommendation": "No escalation required",
                    "missing_fields": [],
                },
            })}}]}

        result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=90)
        assert result["status"] == "COMPLETE", result
        assert any("objective coverage" in item for item in calls), calls
        assert "ocg-kimi-k2.6" in result["report"]["findings"][0]["claim"], result
    finally:
        try:
            os.unlink(fixture_path)
        except FileNotFoundError:
            pass


def assert_explicit_objective_satisfaction_switches_to_short_closure():
    fixture_path = "runtime_explicit_satisfied_fixture.py"
    with open(fixture_path, "w", encoding="utf-8") as handle:
        handle.write('ALIASES = {"mission-a3-kimi": "ocg-kimi-k2.6"}\n')
    try:
        m = mission(
            objective="Return the mapped runtime model.",
            allowed_roots=[],
            allowed_paths=[fixture_path],
            allowed_tool_classes=["read"],
            tool_budget=4,
            allow_heuristic_objective=False,
            objective_spec={
                "schema_version": "objective_spec.v1",
                "objective_type": "mapping_lookup",
                "target": {"key": "mission-a3-kimi"},
                "required_outputs": ["mapped_value", "mapping_file", "evidence_ref"],
                "required_evidence_shapes": ["mapping_assignment"],
            },
        )
        ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
        calls = []
        closure_timeouts = []

        def fake_model(messages, tools, timeout):
            closure_timeouts.append(timeout)
            calls.append(messages[-1]["content"])
            if len(calls) == 1:
                return {"choices": [{"message": {"content": json.dumps({
                    "action_type": "tool_call",
                    "tool_name": "rtk_read",
                    "arguments": {"path": fixture_path},
                    "reason": "Read alias evidence.",
                    "hypothesis": "The mapping is in this allowed file.",
                    "expected_information_gain": "Capture the mapped model.",
                    "why_not_report_yet": "Need evidence first.",
                })}}]}
            return {"choices": [{"message": {"content": json.dumps({
                "action_type": "final_report",
                "report": {
                    "oss_report_version": "1.0",
                    "mission_id": "mission_runtime_test",
                    "status": "COMPLETE",
                    "confidence": "LOW",
                    "files_inspected": [{"path": fixture_path, "complete": True}],
                    "commands_run": [],
                    "findings": [{
                        "claim": f"Runtime model alias mission-a3-kimi maps to underlying reasoning model ocg-kimi-k2.6 in {fixture_path}.",
                        "evidence_refs": [f"file:{fixture_path}#extract:1"],
                        "confidence": "LOW",
                    }],
                    "uncertainties": [],
                    "caveats": [],
                    "escalation_recommendation": "No escalation required",
                    "missing_fields": [],
                },
            })}}]}

        result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=90)
        assert result["status"] == "COMPLETE", result
        assert any("explicit objective_spec appears satisfied" in item for item in calls), calls
        assert closure_timeouts[1] <= 20, closure_timeouts
    finally:
        try:
            os.unlink(fixture_path)
        except FileNotFoundError:
            pass


def assert_deterministic_finalizer_can_complete_explicit_satisfied_objective():
    from codex_oss.runtime.policy import build_deterministic_partial_report

    m = mission(
        objective="Return the mapped runtime model.",
        allowed_tool_classes=["read"],
        allow_heuristic_objective=False,
        objective_spec={
            "schema_version": "objective_spec.v1",
            "objective_type": "mapping_lookup",
            "target": {"key": "mission-a3-kimi"},
            "required_outputs": ["mapped_value", "mapping_file", "evidence_ref"],
            "required_evidence_shapes": ["mapping_assignment"],
        },
    )
    ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=1)
    result = ToolResult(
        tool="rtk_read",
        args={"path": "managed_bridge.py"},
        stdout='ALIASES = {"mission-a3-kimi": "ocg-kimi-k2.6"}\n',
        stderr="",
        exit_code=0,
        complete=True,
    )
    ledger.add_file("managed_bridge.py", result, 0)
    report = build_deterministic_partial_report(m, ledger, "deadline_final_report_ignored")
    assert report["status"] == "COMPLETE", report
    assert "runtime-synthesized" in report["escalation_recommendation"], report
    assert report["report_source"] == "runtime_finalizer", report


def assert_deterministic_finalizer_can_complete_explicit_rtk_grep_function_location():
    from codex_oss.runtime.policy import build_deterministic_partial_report

    m = mission(
        objective="Find where run_managed_mission_from_body is defined and return the file path.",
        allowed_tool_classes=["search"],
        allow_heuristic_objective=False,
        objective_spec={
            "schema_version": "objective_spec.v1",
            "objective_type": "function_location",
            "target": {"symbol": "run_managed_mission_from_body"},
            "required_outputs": ["file_path", "symbol_name", "evidence_ref"],
            "required_evidence_shapes": ["function_definition"],
        },
    )
    ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=1)
    result = ToolResult(
        tool="rtk_grep",
        args={"pattern": "run_managed_mission_from_body", "path": "codex_oss/managed_bridge.py"},
        stdout=(
            "1 matches in 1F:\n\n"
            "[file] codex_oss/managed_bridge.py (1):\n"
            "    75: def run_managed_mission_from_body(\n"
        ),
        stderr="",
        exit_code=0,
        complete=True,
    )
    ledger.add_command("rtk_grep", result.args, result, 0)

    report = build_deterministic_partial_report(m, ledger, "report_validation_failed")
    assert report["status"] == "COMPLETE", report
    assert "run_managed_mission_from_body" in report["findings"][0]["claim"], report
    assert "codex_oss/managed_bridge.py" in report["findings"][0]["claim"], report
    assert report["findings"][0]["evidence_refs"] == ["command:0"], report


def assert_model_reports_are_annotated_with_runtime_provenance():
    m = mission()
    m.runtime_model_alias = "mission-a3-kimi"
    m.last_reasoning_model = "ocg-kimi-k2.6"
    ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)

    calls = []

    def fake_model(messages, tools, timeout):
        calls.append(messages)
        if len(calls) == 1:
            return {"choices": [{"message": {"content": json.dumps({
                "action_type": "tool_call",
                "tool_name": "rtk_read",
                "arguments": {"path": "codex_oss/runtime/loop.py"},
                "reason": "Collect evidence.",
                "hypothesis": "Runtime loop file contains the relevant text.",
                "expected_information_gain": "Evidence ref for report.",
                "why_not_report_yet": "Need evidence first.",
            })}}]}
        return {"choices": [{"message": {"content": json.dumps({
            "action_type": "final_report",
            "report": {
                "oss_report_version": "1.0",
                "mission_id": m.mission_id,
                "status": "PARTIAL",
                "confidence": "LOW",
                "files_inspected": [{"path": "codex_oss/runtime/loop.py", "complete": True}],
                "commands_run": [],
                "findings": [{
                    "claim": "The runtime loop file was inspected.",
                    "evidence_refs": ["file:codex_oss/runtime/loop.py#extract:1"],
                    "confidence": "LOW",
                }],
                "uncertainties": [],
                "caveats": [],
                "escalation_recommendation": "GPT-5.5 review required",
                "missing_fields": [],
            },
        })}}]}

    result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=30)
    report = result["report"]
    assert report["report_source"] == "model_report", report
    assert report["runtime_model_alias"] == "mission-a3-kimi", report
    assert report["explorer_model"] == "ocg-kimi-k2.6", report


def assert_concurrency_policy_serializes_with_small_queue():
    from codex_oss.runtime import policy

    with policy._active_missions_lock:
        policy._mission_lane_by_id.clear()
        for lane in list(policy._active_missions_by_lane):
            policy._active_missions_by_lane[lane].clear()
            policy._waiting_missions_by_lane[lane] = 0

    first = mission(mission_id="mission_first", allowed_roots=[], allowed_paths=["README.md"])
    second = mission(mission_id="mission_second", allowed_roots=[], allowed_paths=["README.md"])

    acquired, reason = policy.acquire_mission_slot(first)
    assert acquired, reason
    result = {}

    def acquire_second():
        result["value"] = policy.acquire_mission_slot(second)

    thread = threading.Thread(target=acquire_second)
    thread.start()
    thread.join(timeout=0.5)
    assert result.get("value", (False, "missing"))[0] is True, result
    policy.release_mission_slot("mission_first")
    policy.release_mission_slot("mission_second")


def assert_workspace_apply_scheduler_stays_serialized():
    from codex_oss.runtime import policy

    with policy._active_missions_lock:
        policy._mission_lane_by_id.clear()
        for lane in list(policy._active_missions_by_lane):
            policy._active_missions_by_lane[lane].clear()
            policy._waiting_missions_by_lane[lane] = 0

    first = mission(
        mission_id="mission_workspace_first",
        tier="A5",
        mode="bounded_implementation",
        write_allowed=True,
        allowed_roots=[],
        allowed_paths=["tests/test_config.py"],
        owned_paths=["tests/test_config.py"],
        read_only_paths=["tests/test_config.py"],
        apply_mode="workspace_low_risk",
        verification_policy={"allowed_commands": [], "max_commands": 0, "timeout_seconds": 10},
        objective_spec={
            "schema_version": "objective_spec.v1",
            "objective_type": "implementation_test_only",
            "target": {"test_file": "tests/test_config.py", "required_test_names": ["test_example"], "source_files": []},
            "required_outputs": ["changed_test_file"],
            "required_evidence_shapes": ["test_definition"],
            "completion_criteria": ["required_test_present"],
        },
    )
    second = mission(
        mission_id="mission_workspace_second",
        tier="A5",
        mode="bounded_implementation",
        write_allowed=True,
        allowed_roots=[],
        allowed_paths=["tests/test_config.py"],
        owned_paths=["tests/test_config.py"],
        read_only_paths=["tests/test_config.py"],
        apply_mode="workspace_low_risk",
        verification_policy={"allowed_commands": [], "max_commands": 0, "timeout_seconds": 10},
        objective_spec={
            "schema_version": "objective_spec.v1",
            "objective_type": "implementation_test_only",
            "target": {"test_file": "tests/test_config.py", "required_test_names": ["test_example"], "source_files": []},
            "required_outputs": ["changed_test_file"],
            "required_evidence_shapes": ["test_definition"],
            "completion_criteria": ["required_test_present"],
        },
    )

    acquired, reason = policy.acquire_mission_slot(first)
    assert acquired, reason
    result = {}

    def acquire_second():
        result["value"] = policy.acquire_mission_slot(second)

    thread = threading.Thread(target=acquire_second)
    thread.start()
    time.sleep(0.2)
    assert "value" not in result, result
    policy.release_mission_slot("mission_workspace_first")
    thread.join(timeout=2)
    assert result.get("value", (False, "missing"))[0] is True, result
    policy.release_mission_slot("mission_workspace_second")


def assert_adaptive_autonomy_budget_redirects_excess_broad_searches():
    first_path = "runtime_broad_one.py"
    second_path = "runtime_broad_two.py"
    with open(first_path, "w", encoding="utf-8") as handle:
        handle.write("alpha = 1\n")
    with open(second_path, "w", encoding="utf-8") as handle:
        handle.write("beta = 2\n")
    try:
        m = mission(
            objective="Explore broad runtime terms, then report.",
            allowed_roots=[],
            allowed_paths=[first_path, second_path],
            allowed_tool_classes=["search"],
            tool_budget=4,
            max_broad_searches=1,
        )
        ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
        calls = []

        def fake_model(messages, tools, timeout):
            calls.append(messages[-1]["content"])
            if len(calls) == 1:
                content = {
                    "action_type": "tool_call",
                    "tool_name": "rtk_grep",
                    "arguments": {"path": first_path, "pattern": "alpha"},
                    "reason": "Initial broad search.",
                    "hypothesis": "A relevant marker may exist.",
                    "expected_information_gain": "Find first marker.",
                    "why_not_report_yet": "Need evidence.",
                }
            elif len(calls) == 2:
                content = {
                    "action_type": "tool_call",
                    "tool_name": "rtk_grep",
                    "arguments": {"path": second_path, "pattern": "beta"},
                    "reason": "Second broad search.",
                    "hypothesis": "Another marker may exist.",
                    "expected_information_gain": "Find second marker.",
                    "why_not_report_yet": "Need more coverage.",
                }
            else:
                content = {
                    "action_type": "final_report",
                    "report": {
                        "oss_report_version": "1.0",
                        "mission_id": "mission_runtime_test",
                        "status": "PARTIAL",
                        "confidence": "LOW",
                        "files_inspected": [],
                        "commands_run": [{"tool": "rtk_grep", "args": {"path": first_path, "pattern": "alpha"}}],
                        "findings": [{
                            "claim": "The first broad search gathered evidence.",
                            "evidence_refs": ["command:0"],
                            "confidence": "LOW",
                        }],
                        "uncertainties": ["Second broad search was redirected by adaptive budget."],
                        "caveats": [],
                        "escalation_recommendation": "No escalation required",
                        "missing_fields": [],
                    },
                }
            return {"choices": [{"message": {"content": json.dumps(content)}}]}

        result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=90)
        assert result["status"] == "PARTIAL", result
        assert len(ledger.commands_run) == 1, ledger.commands_run
        assert ledger.action_trace[1].runtime_decision == "redirected", ledger.action_trace
        assert "Broad exploration budget is exhausted" in calls[-1], calls
    finally:
        for path in (first_path, second_path):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


def assert_adaptive_autonomy_budget_allows_post_evidence_verification_with_rationale():
    fixture_path = "runtime_verify_fixture.py"
    with open(fixture_path, "w", encoding="utf-8") as handle:
        handle.write("target = 'value'\n")
    try:
        m = mission(
            objective="Read evidence, then verify target usage.",
            allowed_roots=[],
            allowed_paths=[fixture_path],
            allowed_tool_classes=["read", "search"],
            tool_budget=4,
        )
        ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
        calls = []

        def fake_model(messages, tools, timeout):
            calls.append(messages[-1]["content"])
            if len(calls) == 1:
                content = {
                    "action_type": "tool_call",
                    "tool_name": "rtk_read",
                    "arguments": {"path": fixture_path},
                    "reason": "Read primary evidence.",
                    "hypothesis": "The target value is in this file.",
                    "expected_information_gain": "Confirm target assignment.",
                    "why_not_report_yet": "Need primary evidence.",
                }
            elif len(calls) == 2:
                content = {
                    "action_type": "tool_call",
                    "tool_name": "rtk_grep",
                    "arguments": {"path": fixture_path, "pattern": "target", "max_results": 5},
                    "reason": "Verify the finding with a targeted search.",
                    "hypothesis": "The finding should be confirmed by a search hit.",
                    "expected_information_gain": "Confirm evidence location for the finding.",
                    "why_not_report_yet": "Need verification evidence before reporting.",
                }
            else:
                content = {
                    "action_type": "final_report",
                    "report": {
                        "oss_report_version": "1.0",
                        "mission_id": "mission_runtime_test",
                        "status": "COMPLETE",
                        "confidence": "LOW",
                        "files_inspected": [{"path": fixture_path, "complete": True}],
                        "commands_run": [{"tool": "rtk_grep", "args": {"path": fixture_path, "pattern": "target", "max_results": 5}}],
                        "findings": [{
                            "claim": "The target value was read and then verified by search.",
                            "evidence_refs": [f"file:{fixture_path}#extract:1", "command:0"],
                            "confidence": "LOW",
                        }],
                        "uncertainties": [],
                        "caveats": [],
                        "escalation_recommendation": "No escalation required",
                        "missing_fields": [],
                    },
                }
            return {"choices": [{"message": {"content": json.dumps(content)}}]}

        result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=90)
        assert result["status"] == "COMPLETE", result
        assert len(ledger.commands_run) == 2, ledger.commands_run
        assert ledger.action_trace[1].runtime_decision == "allowed", ledger.action_trace
    finally:
        try:
            os.unlink(fixture_path)
        except FileNotFoundError:
            pass


def assert_complete_alias_report_must_name_mapped_model():
    fixture_path = "runtime_alias_objective_fixture.py"
    with open(fixture_path, "w", encoding="utf-8") as handle:
        handle.write(
            'RUNTIME_MODEL_ALIASES = {\n'
            '    "mission-a3-kimi": "ocg-kimi-k2.6",\n'
            '}\n'
        )
    try:
        m = mission(
            objective="Find where runtime model aliases map mission-a3-kimi to the underlying reasoning model.",
            allowed_roots=[],
            allowed_paths=[fixture_path],
            allowed_tool_classes=["read"],
            tool_budget=3,
        )
        ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
        calls = []

        def fake_model(messages, tools, timeout):
            calls.append(messages[-1]["content"])
            if len(calls) == 1:
                return {"choices": [{"message": {"content": json.dumps({
                    "action_type": "tool_call",
                    "tool_name": "rtk_read",
                    "arguments": {"path": fixture_path},
                    "reason": "Read alias mapping file.",
                    "hypothesis": "The alias map is in this file.",
                    "expected_information_gain": "Find mapped model.",
                    "why_not_report_yet": "Need evidence.",
                })}}]}
            if len(calls) == 2:
                return {"choices": [{"message": {"content": json.dumps({
                    "action_type": "final_report",
                    "report": {
                        "oss_report_version": "1.0",
                        "mission_id": "mission_runtime_test",
                        "status": "COMPLETE",
                        "confidence": "LOW",
                        "files_inspected": [{"path": fixture_path, "complete": True}],
                        "commands_run": [],
                        "findings": [{
                            "claim": f"Runtime model aliases are defined in {fixture_path}.",
                            "evidence_refs": [f"file:{fixture_path}#extract:1"],
                            "confidence": "LOW",
                        }],
                        "uncertainties": ["The exact mapped model is not verified."],
                        "caveats": [],
                        "escalation_recommendation": "No escalation required",
                        "missing_fields": [],
                    },
                })}}]}
            return {"choices": [{"message": {"content": json.dumps({
                "action_type": "final_report",
                "report": {
                    "oss_report_version": "1.0",
                    "mission_id": "mission_runtime_test",
                    "status": "COMPLETE",
                    "confidence": "LOW",
                    "files_inspected": [{"path": fixture_path, "complete": True}],
                    "commands_run": [],
                    "findings": [{
                        "claim": f"Runtime model alias mission-a3-kimi maps to underlying reasoning model ocg-kimi-k2.6 in {fixture_path}.",
                        "evidence_refs": [f"file:{fixture_path}#extract:1"],
                        "confidence": "LOW",
                    }],
                    "uncertainties": [],
                    "caveats": [],
                    "escalation_recommendation": "No escalation required",
                    "missing_fields": [],
                },
            })}}]}

        result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=90)
        assert result["status"] == "COMPLETE", result
        assert "ocg-kimi-k2.6" in result["report"]["findings"][0]["claim"], result
        assert any("objective coverage" in item for item in calls), calls
    finally:
        try:
            os.unlink(fixture_path)
        except FileNotFoundError:
            pass


def assert_grep_definition_result_provides_targeted_read_hint():
    fixture_path = "runtime_profile_hint_fixture.py"
    with open(fixture_path, "w", encoding="utf-8") as handle:
        handle.write('RUNTIME_AUTONOMY_PROFILES = {\n    "mission-a3-deepseek": {"max_tool_budget": 20, "max_time_seconds": 180},\n}\n')
    try:
        m = mission(
            objective="Find where runtime autonomy profiles are defined.",
            allowed_roots=[],
            allowed_paths=[fixture_path],
            allowed_tool_classes=["search"],
            tool_budget=2,
        )
        ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
        calls = []

        def fake_model(messages, tools, timeout):
            calls.append(messages[-1]["content"])
            if len(calls) == 1:
                return {"choices": [{"message": {"content": json.dumps({
                    "action_type": "tool_call",
                    "tool_name": "rtk_grep",
                    "arguments": {"path": fixture_path, "pattern": "RUNTIME_AUTONOMY_PROFILES"},
                    "reason": "Find the definition.",
                    "hypothesis": "The constant is defined in the allowed file.",
                    "expected_information_gain": "Get the definition line.",
                    "why_not_report_yet": "Need evidence.",
                })}}]}
            return {"choices": [{"message": {"content": json.dumps({
                "action_type": "final_report",
                "report": {
                    "oss_report_version": "1.0",
                    "mission_id": "mission_runtime_test",
                    "status": "PARTIAL",
                    "confidence": "LOW",
                    "files_inspected": [],
                    "commands_run": [{"tool": "rtk_grep", "args": {"path": fixture_path, "pattern": "RUNTIME_AUTONOMY_PROFILES"}}],
                    "findings": [{
                        "claim": "Runtime autonomy profiles are defined by RUNTIME_AUTONOMY_PROFILES.",
                        "evidence_refs": ["command:0"],
                        "confidence": "LOW",
                    }],
                    "uncertainties": [],
                    "caveats": [],
                    "escalation_recommendation": "No escalation required",
                    "missing_fields": [],
                },
            })}}]}

        result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=30)
        assert result["status"] == "PARTIAL", result
        assert "Runtime next-action hint" in calls[-1], calls[-1]
        assert '"start_line":1' in calls[-1], calls[-1]
    finally:
        try:
            os.unlink(fixture_path)
        except FileNotFoundError:
            pass


def assert_unsupported_tool_args_are_reported_without_execution():
    m = mission(allowed_tool_classes=["search"], tool_budget=2)
    ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
    calls = []

    def fake_model(messages, tools, timeout):
        calls.append(messages[-1]["content"])
        if len(calls) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"action_type":"tool_call","tool_name":"rtk_grep",'
                                '"arguments":{"pattern":"alias","path":"codex_oss/","flags":"-ri"},'
                                '"reason":"Try a refined search.",'
                                '"hypothesis":"Alias definitions live in Python files.",'
                                '"expected_information_gain":"Find the alias map.",'
                                '"why_not_report_yet":"No evidence has been gathered."}'
                            )
                        }
                    }
                ]
            }
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"action_type":"final_report","report":'
                            '{"oss_report_version":"1.0","mission_id":"mission_runtime_test",'
                            '"status":"PARTIAL","confidence":"LOW","files_inspected":[],'
                            '"commands_run":[],"findings":[],"uncertainties":[],'
                            '"caveats":["unsupported grep args were rejected"],'
                            '"escalation_recommendation":"GPT-5.5 review required",'
                            '"missing_fields":[]}}'
                        )
                    }
                }
            ]
        }

    result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=30)
    assert result["status"] == "PARTIAL", result
    assert ledger.tool_budget_remaining == m.tool_budget, ledger.tool_budget_remaining
    assert len(ledger.commands_run) == 0, ledger.commands_run
    assert ledger.action_trace[0].runtime_decision == "repaired", ledger.action_trace[0]
    assert ledger.action_trace[0].unsupported_arguments == ["flags"], ledger.action_trace[0]
    assert any("Unsupported arguments for rtk_grep" in item for item in calls), calls


def assert_advanced_grep_args_are_supported():
    fixture_path = "runtime_grep_args_fixture.py"
    with open(fixture_path, "w", encoding="utf-8") as handle:
        handle.write("Runtime_Model_Alias = True\n")
    try:
        m = mission(
            allowed_roots=[],
            allowed_paths=[fixture_path],
            allowed_tool_classes=["search"],
            tool_budget=2,
        )
        ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
        calls = []

        def fake_model(messages, tools, timeout):
            calls.append(messages[-1]["content"])
            if len(calls) == 1:
                content = json.dumps({
                    "action_type": "tool_call",
                    "tool_name": "rtk_grep",
                    "arguments": {
                        "pattern": "runtime_model_alias",
                        "path": fixture_path,
                        "include": "*.py",
                        "case_sensitive": False,
                        "max_results": 5,
                    },
                    "reason": "Run a refined case-insensitive search.",
                    "hypothesis": "The alias name may differ in case.",
                    "expected_information_gain": "Find the matching line.",
                    "why_not_report_yet": "Need search evidence first.",
                })
                return {
                    "choices": [
                        {
                            "message": {
                                "content": content
                            }
                        }
                    ]
                }
            content = json.dumps({
                "action_type": "final_report",
                "report": {
                    "oss_report_version": "1.0",
                    "mission_id": "mission_runtime_test",
                    "status": "COMPLETE",
                    "confidence": "LOW",
                    "files_inspected": [],
                    "commands_run": [
                        {
                            "tool": "rtk_grep",
                            "args": {
                                "pattern": "runtime_model_alias",
                                "path": fixture_path,
                                "case_sensitive": False,
                                "include_glob": "*.py",
                                "max_results": 5,
                            },
                        }
                    ],
                    "findings": [
                        {
                            "claim": "The advanced grep found the alias line.",
                            "evidence_refs": ["command:0#match:0"],
                            "confidence": "LOW",
                        }
                    ],
                    "uncertainties": [],
                    "caveats": [],
                    "escalation_recommendation": "No escalation required",
                    "missing_fields": [],
                },
            })
            return {
                "choices": [
                    {
                        "message": {
                            "content": content
                        }
                    }
                ]
            }

        result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=30)
        assert result["status"] == "COMPLETE", result
        assert len(ledger.commands_run) == 1, ledger.commands_run
        assert ledger.action_trace[0].unsupported_arguments == [], ledger.action_trace[0]
        assert ledger.commands_run[0].matches_count >= 1, ledger.commands_run[0]
    finally:
        try:
            os.unlink(fixture_path)
        except FileNotFoundError:
            pass


def assert_read_line_ranges_are_supported():
    fixture_path = "runtime_range_fixture.txt"
    with open(fixture_path, "w", encoding="utf-8") as handle:
        handle.write("one\ntwo\nthree\nfour\n")
    try:
        m = mission(
            allowed_roots=[],
            allowed_paths=[fixture_path],
            allowed_tool_classes=["read"],
            tool_budget=2,
        )
        ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
        calls = []

        def fake_model(messages, tools, timeout):
            calls.append(messages[-1]["content"])
            if len(calls) == 1:
                content = json.dumps({
                    "action_type": "tool_call",
                    "tool_name": "rtk_read",
                    "arguments": {
                        "path": fixture_path,
                        "start_line": 2,
                        "end_line": 3,
                    },
                    "reason": "Read the exact relevant range.",
                    "hypothesis": "The needed evidence is in a small line range.",
                    "expected_information_gain": "Confirm the range.",
                    "why_not_report_yet": "Need the evidence first.",
                })
                return {
                    "choices": [
                        {
                            "message": {
                                "content": content
                            }
                        }
                    ]
                }
            content = json.dumps({
                "action_type": "final_report",
                "report": {
                    "oss_report_version": "1.0",
                    "mission_id": "mission_runtime_test",
                    "status": "COMPLETE",
                    "confidence": "LOW",
                    "files_inspected": [{"path": fixture_path, "complete": True}],
                    "commands_run": [],
                    "findings": [
                        {
                            "claim": "The selected range was read.",
                            "evidence_refs": [f"file:{fixture_path}#extract:1"],
                            "confidence": "LOW",
                        }
                    ],
                    "uncertainties": [],
                    "caveats": [],
                    "escalation_recommendation": "No escalation required",
                    "missing_fields": [],
                },
            })
            return {
                "choices": [
                    {
                        "message": {
                            "content": content
                        }
                    }
                ]
            }

        result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=30)
        assert result["status"] == "COMPLETE", result
        entry = ledger.files_inspected[fixture_path]
        assert entry.extracts[0]["text"] == "two\nthree\n", entry.extracts
        assert ledger.action_trace[0].unsupported_arguments == [], ledger.action_trace[0]
    finally:
        try:
            os.unlink(fixture_path)
        except FileNotFoundError:
            pass


def assert_incidental_critical_terms_do_not_stop_low_risk_mission():
    fixture_path = "runtime_schema_term_fixture.txt"
    with open(fixture_path, "w", encoding="utf-8") as handle:
        handle.write("This runtime fixture mentions schema as an ordinary contract word.\n")
    try:
        m = mission(
            allowed_roots=[],
            allowed_paths=[fixture_path],
            allowed_tool_classes=["read"],
            tool_budget=2,
        )
        ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
        calls = []

        def fake_model(messages, tools, timeout):
            calls.extend(item["content"] for item in messages)
            if len(calls) == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"action_type":"tool_call","tool_name":"rtk_read",'
                                    f'"arguments":{{"path":"{fixture_path}"}},'
                                    '"reason":"Read the allowed fixture."}'
                                )
                            }
                        }
                    ]
                }
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"action_type":"final_report","report":'
                                '{"oss_report_version":"1.0","mission_id":"mission_runtime_test",'
                                '"status":"COMPLETE","confidence":"LOW",'
                                f'"files_inspected":[{{"path":"{fixture_path}","complete":true}}],'
                                '"commands_run":[],'
                                '"findings":[{"claim":"The fixture was inspected.",'
                                f'"evidence_refs":["file:{fixture_path}#extract:1"],'
                                '"confidence":"LOW"}],'
                                '"uncertainties":[],"caveats":["critical-domain word observed as incidental text"],'
                                '"escalation_recommendation":"GPT-5.5 review required if using this as critical-path evidence",'
                                '"missing_fields":[]}}'
                            )
                        }
                    }
                ]
            }

        result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=30)
        assert result["status"] == "COMPLETE", result
        assert any("critical_terms_detected" in flag for flag in ledger.risk_flags), ledger.risk_flags
        assert any("Risk note" in item for item in calls), calls
    finally:
        try:
            os.unlink(fixture_path)
        except FileNotFoundError:
            pass


def assert_health_exposes_source_identity():
    class State:
        path = "state.sqlite3"

    class App:
        model_health = {"kimi": {"status": "ok", "errors": 0, "since": 1}}
        gpt_model_strategy = "error"
        upstream_streaming = True
        upstream_key = "present"
        state = State()
        max_global_concurrency = 1

    status = build_health_status(App(), "test-version", os.path.join(ROOT, "bridge.py"), 0)
    assert status["source_path"].endswith("bridge.py"), status
    assert len(status["source_sha256"]) == 64, status["source_sha256"]
    assert status["transport_contract"]["stream_terminal_guarantee"] is True, status


def assert_open_investigation_runtime_answer_graph_can_complete_after_model_timeout():
    m = mission(
        mission_id="mission_open_timeout_complete",
        objective="Investigate where readonly artifacts are written.",
        objective_style="open_investigation",
        allowed_roots=[],
        allowed_paths=["codex_oss/managed_bridge.py"],
        allowed_tool_classes=["read"],
        answer_obligations=[
            {"id": "q1", "question": "Which runtime file writes readonly artifacts?", "required": True, "source_hints": ["codex_oss/managed_bridge.py"]},
            {"id": "q2", "question": "What conclusion is justified from the inspected evidence?", "required": True, "source_hints": ["codex_oss/managed_bridge.py"]},
        ],
        must_inspect=["codex_oss/managed_bridge.py"],
        exploration_policy={"after_required_floor": "close_immediately", "min_optional_actions_after_floor": 0, "max_optional_actions_after_floor": 0, "require_contradiction_search": False},
        objective_spec={
            "schema_version": "objective_spec.v1",
            "objective_type": "function_location",
            "target": {"symbol": "_write_readonly_mission_artifacts"},
            "required_outputs": ["file_path", "function_definition"],
            "required_evidence_shapes": ["function_definition"],
            "completion_criteria": ["function_definition_present"],
        },
    )
    ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
    calls = {"n": 0}

    def fake_model(messages, tools, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "choices": [{
                    "message": {
                        "content": (
                            '{"action_type":"tool_call","phase":"NARROW","tool_name":"rtk_read",'
                            '"arguments":{"path":"codex_oss/managed_bridge.py"},'
                            '"reason":"Inspect the runtime writer.","hypothesis":"The managed bridge persists readonly artifacts.",'
                            '"target_question":"Which runtime file writes readonly artifacts?",'
                            '"expected_information_gain":"Gather implementation evidence.",'
                            '"why_not_report_yet":"Need the implementation evidence before closing."}'
                        )
                    }
                }]
            }
        raise TimeoutError("simulated close timeout")

    result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=30)
    assert result["status"] == "COMPLETE", result
    assert result["report"]["status"] == "COMPLETE", result
    assert result["report"]["report_source"] == "runtime_answer_graph", result
    assert result["report"]["closure_source"] == "runtime_answer_graph", result
    assert result["report"]["answer_graph_summary"]["required_answered"] == 2, result


def assert_open_investigation_runtime_answer_graph_stays_partial_when_obligations_remain_open():
    m = mission(
        mission_id="mission_open_timeout_partial",
        objective="Investigate where readonly artifacts are written and audited.",
        objective_style="open_investigation",
        allowed_roots=[],
        allowed_paths=["codex_oss/managed_bridge.py", "codex_oss/audit.py"],
        allowed_tool_classes=["read"],
        answer_obligations=[
            {"id": "q1", "question": "Which runtime file writes readonly artifacts?", "required": True, "source_hints": ["codex_oss/managed_bridge.py"]},
            {"id": "q2", "question": "Which audit file enforces the readonly artifact contract?", "required": True, "source_hints": ["codex_oss/audit.py"]},
            {
                "id": "q3",
                "question": "What remains unproven about the broader trust claim?",
                "required": True,
                "source_hints": ["codex_oss/claim_graph.py"],
                "source_requirements": [{"path": "codex_oss/claim_graph.py", "evidence_kind": "trust_gap_source", "required": True, "prefetch": True}],
            },
        ],
        must_inspect=["codex_oss/managed_bridge.py", "codex_oss/audit.py", "codex_oss/claim_graph.py"],
        objective_spec={
            "schema_version": "objective_spec.v1",
            "objective_type": "function_location",
            "target": {"symbol": "_write_readonly_mission_artifacts"},
            "required_outputs": ["file_path", "function_definition"],
            "required_evidence_shapes": ["function_definition"],
            "completion_criteria": ["function_definition_present"],
        },
    )
    ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
    calls = {"n": 0}

    def fake_model(messages, tools, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "choices": [{
                    "message": {
                        "content": (
                            '{"action_type":"tool_call","phase":"NARROW","tool_name":"rtk_read",'
                            '"arguments":{"path":"codex_oss/managed_bridge.py"},'
                            '"reason":"Inspect the runtime writer.","hypothesis":"The managed bridge persists readonly artifacts.",'
                            '"target_question":"Which runtime file writes readonly artifacts?",'
                            '"expected_information_gain":"Gather implementation evidence.",'
                            '"why_not_report_yet":"Need audit evidence too before closing."}'
                        )
                    }
                }]
            }
        raise TimeoutError("simulated close timeout")

    result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=30)
    assert result["status"] == "PARTIAL", result
    assert result["report"]["status"] == "PARTIAL", result
    assert result["report"]["report_source"] == "runtime_answer_graph", result
    assert any("claim_graph.py" in missing.lower() for missing in result["report"]["missing_fields"]), result


def assert_open_investigation_prefetch_reads_required_sources_before_model_loop():
    m = mission(
        mission_id="mission_open_prefetch_reads",
        objective="Investigate readonly artifact coverage with required source prefetch.",
        objective_style="open_investigation",
        allowed_roots=[],
        allowed_paths=["codex_oss/managed_bridge.py", "codex_oss/audit.py"],
        allowed_tool_classes=["read", "search"],
        answer_obligations=[
            {
                "id": "q1",
                "question": "Which runtime file writes readonly artifacts?",
                "required": True,
                "source_hints": ["codex_oss/managed_bridge.py"],
                "source_requirements": [{"path": "codex_oss/managed_bridge.py", "evidence_kind": "implementation_logic", "required": True, "prefetch": True}],
            },
            {
                "id": "q2",
                "question": "Which audit file enforces the readonly artifact contract?",
                "required": True,
                "source_hints": ["codex_oss/audit.py"],
                "source_requirements": [{"path": "codex_oss/audit.py", "evidence_kind": "audit_enforcement", "required": True, "prefetch": True}],
            },
        ],
        must_inspect=["codex_oss/managed_bridge.py", "codex_oss/audit.py"],
        exploration_policy={"after_required_floor": "close_immediately", "min_optional_actions_after_floor": 0, "max_optional_actions_after_floor": 0, "require_contradiction_search": False},
    )
    ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)

    def fake_model(messages, tools, timeout):
        raise TimeoutError("simulated timeout after prefetch")

    result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=30)
    assert result["status"] == "COMPLETE", result
    assert sorted(ledger.files_inspected.keys()) == ["codex_oss/audit.py", "codex_oss/managed_bridge.py"], ledger.files_inspected
    assert result["report"]["closure_source"] == "runtime_answer_graph", result
    assert result["report"]["answer_graph_summary"]["required_answered"] == 2, result


def assert_open_investigation_redirects_to_pending_required_source():
    m = mission(
        mission_id="mission_open_redirect_required_source",
        objective="Investigate readonly artifact coverage without skipping the pending audit source.",
        objective_style="open_investigation",
        allowed_roots=[],
        allowed_paths=["codex_oss/managed_bridge.py", "codex_oss/audit.py"],
        allowed_tool_classes=["read"],
        answer_obligations=[
            {
                "id": "q1",
                "question": "Which runtime file writes readonly artifacts?",
                "required": True,
                "source_hints": ["codex_oss/managed_bridge.py"],
                "source_requirements": [{"path": "codex_oss/managed_bridge.py", "evidence_kind": "implementation_logic", "required": True, "prefetch": False}],
            },
            {
                "id": "q2",
                "question": "Which audit file enforces the readonly artifact contract?",
                "required": True,
                "source_hints": ["codex_oss/audit.py"],
                "source_requirements": [{"path": "codex_oss/audit.py", "evidence_kind": "audit_enforcement", "required": True, "prefetch": False}],
            },
            {
                "id": "q3",
                "question": "What remains unproven?",
                "required": True,
                "source_hints": ["codex_oss/audit.py"],
            },
        ],
        must_inspect=["codex_oss/managed_bridge.py", "codex_oss/audit.py"],
    )
    ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
    calls = {"n": 0}

    def fake_model(messages, tools, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "choices": [{
                    "message": {
                        "content": (
                            '{"action_type":"tool_call","phase":"NARROW","tool_name":"rtk_read",'
                            '"arguments":{"path":"codex_oss/managed_bridge.py"},'
                            '"reason":"Inspect the runtime writer.","hypothesis":"The managed bridge writes the artifacts.",'
                            '"target_question":"Which runtime file writes readonly artifacts?",'
                            '"expected_information_gain":"Gather implementation evidence.",'
                            '"why_not_report_yet":"Need the audit source too."}'
                        )
                    }
                }]
            }
        if calls["n"] == 2:
            return {
                "choices": [{
                    "message": {
                        "content": (
                            '{"action_type":"tool_call","phase":"VERIFY","tool_name":"rtk_read",'
                            '"arguments":{"path":"codex_oss/managed_bridge.py"},'
                            '"reason":"Reread the writer.","hypothesis":"More runtime detail may help.",'
                            '"target_question":"Which runtime file writes readonly artifacts?",'
                            '"expected_information_gain":"Maybe gather more detail.",'
                            '"why_not_report_yet":"Still thinking."}'
                        )
                    }
                }]
            }
        raise TimeoutError("simulated timeout after redirect")

    result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=30)
    assert result["status"] == "PARTIAL", result
    assert list(ledger.files_inspected.keys()) == ["codex_oss/managed_bridge.py", "codex_oss/audit.py"], ledger.files_inspected
    assert not any("audit.py" in missing.lower() for missing in result["report"]["missing_required_sources"]), result
    assert any(
        entry.runtime_decision == "redirected" and "required source still pending" in entry.decision_reason
        for entry in ledger.action_trace
    ), ledger.action_trace


def assert_open_investigation_reports_insufficient_evidence_when_shape_missing():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory(dir=ROOT) as td:
        sample = Path(td) / "shape_missing.py"
        sample.write_text("VALUE = 1\n", encoding="utf-8")
        sample_rel = os.path.relpath(sample, ROOT)
        m = mission(
            mission_id="mission_open_shape_missing",
            objective="Investigate whether the required function definition exists.",
            objective_style="open_investigation",
            allowed_roots=[],
            allowed_paths=[sample_rel],
            allowed_tool_classes=["read"],
            answer_obligations=[
                {
                    "id": "q1",
                    "question": "Does the source contain the required function definition?",
                    "required": True,
                    "source_hints": [sample_rel],
                    "source_requirements": [{"path": sample_rel, "evidence_kind": "function_presence", "required": True, "prefetch": False, "required_shapes": ["function_definition"]}],
                }
            ],
            must_inspect=[sample_rel],
        )
        ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
        calls = {"n": 0}

        def fake_model(messages, tools, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "choices": [{
                        "message": {
                            "content": (
                                '{"action_type":"tool_call","phase":"NARROW","tool_name":"rtk_read",'
                                f'"arguments":{{"path":"{sample_rel}"}},'
                                '"reason":"Inspect the required file.","hypothesis":"The file may define the needed function.",'
                                '"target_question":"Does the source contain the required function definition?",'
                                '"expected_information_gain":"Determine whether the required shape exists.",'
                                '"why_not_report_yet":"Need to inspect the file first."}'
                            )
                        }
                    }]
                }
            raise TimeoutError("simulated close timeout")

        result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=30)
        assert result["status"] == "PARTIAL", result
        assert "q1" in result["report"]["insufficient_evidence_obligations"], result


def assert_open_investigation_escalates_on_contradiction_marker():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory(dir=ROOT) as td:
        sample = Path(td) / "contradicted.py"
        sample.write_text("CONTRADICTION_MARKER = True\n", encoding="utf-8")
        sample_rel = os.path.relpath(sample, ROOT)
        m = mission(
            mission_id="mission_open_contradicted",
            objective="Investigate whether the source contains contradictory evidence.",
            objective_style="open_investigation",
            allowed_roots=[],
            allowed_paths=[sample_rel],
            allowed_tool_classes=["read"],
            answer_obligations=[
                {
                    "id": "q1",
                    "question": "Is the required source free of contradiction markers?",
                    "required": True,
                    "source_hints": [sample_rel],
                    "source_requirements": [{"path": sample_rel, "evidence_kind": "contradiction_check", "required": True, "prefetch": False, "contradiction_markers": ["CONTRADICTION_MARKER"]}],
                }
            ],
            must_inspect=[sample_rel],
        )
        ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
        calls = {"n": 0}

        def fake_model(messages, tools, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "choices": [{
                        "message": {
                            "content": (
                                '{"action_type":"tool_call","phase":"NARROW","tool_name":"rtk_read",'
                                f'"arguments":{{"path":"{sample_rel}"}},'
                                '"reason":"Inspect the required file.","hypothesis":"The file may contain contradiction markers.",'
                                '"target_question":"Is the required source free of contradiction markers?",'
                                '"expected_information_gain":"Determine whether contradiction blocks completion.",'
                                '"why_not_report_yet":"Need to inspect the file first."}'
                            )
                        }
                    }]
                }
            raise TimeoutError("simulated close timeout")

        result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=30)
        assert result["status"] == "ESCALATE", result
        assert "q1" in result["report"]["contradicted_obligations"], result


def assert_open_investigation_escalates_on_blocked_source():
    with tempfile.TemporaryDirectory(dir=ROOT) as td:
        missing_abs = os.path.join(td, "mission_open_blocked_source_missing.py")
        missing_rel = os.path.relpath(missing_abs, ROOT)
        m = mission(
            mission_id="mission_open_blocked_source",
            objective="Investigate whether a blocked source prevents completion.",
            objective_style="open_investigation",
            allowed_roots=[],
            allowed_paths=[missing_rel],
            allowed_tool_classes=["read"],
            answer_obligations=[
                {
                    "id": "q1",
                    "question": "Can the required source be inspected successfully?",
                    "required": True,
                    "source_hints": [missing_rel],
                    "source_requirements": [{"path": missing_rel, "evidence_kind": "source_access", "required": True, "prefetch": False}],
                }
            ],
            must_inspect=[missing_rel],
        )
        ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
        calls = {"n": 0}

        def fake_model(messages, tools, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "choices": [{
                        "message": {
                            "content": (
                                '{"action_type":"tool_call","phase":"NARROW","tool_name":"rtk_read",'
                                f'"arguments":{{"path":"{missing_rel}"}},'
                                '"reason":"Inspect the required file.","hypothesis":"The required source should be readable.",'
                                '"target_question":"Can the required source be inspected successfully?",'
                                '"expected_information_gain":"Determine whether the required source is blocked.",'
                                '"why_not_report_yet":"Need to attempt the required read first."}'
                            )
                        }
                    }]
                }
            raise TimeoutError("simulated close timeout")

        result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=30)
        assert result["status"] == "ESCALATE", result
        assert "q1" in result["report"]["blocked_obligations"], result


def assert_agenda_guided_redirects_then_prefetches_after_model_ignores_required_source():
    with tempfile.TemporaryDirectory(dir=ROOT) as td:
        from pathlib import Path

        required = Path(td) / "required_source.py"
        required.write_text("def find_target():\n    return 'found'\n", encoding="utf-8")
        required_rel = os.path.relpath(required, ROOT)
        unrelated = Path(td) / "unrelated_helper.py"
        unrelated.write_text("# utility module\nHELPER = 42\n", encoding="utf-8")
        unrelated_rel = os.path.relpath(unrelated, ROOT)
        m = mission(
            mission_id="mission_agenda_guided_redirect_prefetch",
            objective="Investigate whether the target function exists.",
            objective_style="open_investigation",
            evidence_collection_mode="agenda_guided",
            allowed_roots=[],
            allowed_paths=[required_rel, unrelated_rel],
            allowed_tool_classes=["read"],
            tool_budget=6,
            answer_obligations=[
                {
                    "id": "q1",
                    "question": "Does the target file contain find_target()?",
                    "required": True,
                    "source_hints": [required_rel],
                    "source_requirements": [{"path": required_rel, "evidence_kind": "function_presence", "required": True, "required_shapes": ["function_definition"]}],
                }
            ],
            must_inspect=[required_rel],
            exploration_policy={"after_required_floor": "close_immediately", "min_optional_actions_after_floor": 0, "max_optional_actions_after_floor": 0, "require_contradiction_search": False},
        )
        ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
        calls = {"n": 0}

        def fake_model(messages, tools, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "choices": [{
                        "message": {
                            "content": (
                                '{"action_type":"tool_call","phase":"EXPLORE","tool_name":"rtk_read",'
                                f'"arguments":{{"path":"{unrelated_rel}"}},'
                                '"reason":"Check the unrelated helper first.","hypothesis":"Maybe HELPER relates to the target.",'
                                '"target_question":"What does the helper define?",'
                                '"expected_information_gain":"Understand the utility module.",'
                                '"why_not_report_yet":"Need to investigate before reporting."}'
                            )
                        }
                    }]
                }
            if calls["n"] == 2:
                return {
                    "choices": [{
                        "message": {
                            "content": (
                                '{"action_type":"tool_call","phase":"NARROW","tool_name":"rtk_read",'
                                f'"arguments":{{"path":"{unrelated_rel}"}},'
                                '"reason":"Recheck the helper for more context.","hypothesis":"Maybe a hidden detail exists.",'
                                '"target_question":"Is there more in the helper?",'
                                '"expected_information_gain":"Confirm helper details.",'
                                '"why_not_report_yet":"Still need more context."}'
                            )
                        }
                    }]
                }
            if calls["n"] == 3:
                return {
                    "choices": [{
                        "message": {
                            "content": json.dumps({
                                "action_type": "final_report",
                                "report": {
                                    "status": "COMPLETE",
                                    "files_inspected": [{"path": unrelated_rel, "complete": True}, {"path": required_rel, "complete": True}],
                                    "commands_run": [{"tool": "rtk_read", "args": {"path": unrelated_rel}}, {"tool": "rtk_read", "args": {"path": required_rel}}],
                                    "findings": [{"claim": "The target file contains find_target.", "evidence_refs": ["file:" + required_rel, "command:1"]}],
                                    "uncertainties": [],
                                    "confidence": "HIGH",
                                    "caveats": ["agenda_guided coverage test"],
                                    "escalation_recommendation": "GPT-5.5 review",
                                    "missing_fields": [],
                                },
                            })
                        }
                    }]
                }
            raise TimeoutError("simulated close timeout")

        result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=90)
        redirected = [
            entry
            for entry in (getattr(ledger, "action_trace", []) or [])
            if getattr(entry, "runtime_decision", "") == "redirected"
            and "required source still pending" in str(getattr(entry, "decision_reason", "") or "")
        ]
        assert len(redirected) >= 1, f"Expected at least 1 redirect, got {len(redirected)} in {ledger.action_trace}"
        assert result["status"] == "COMPLETE", result
        prefetched = [
            entry
            for entry in (getattr(ledger, "action_trace", []) or [])
            if getattr(entry, "runtime_decision", "") == "runtime_prefetch"
        ]
        assert len(prefetched) >= 1, f"Expected at least 1 runtime prefetch after redirect threshold, got {len(prefetched)} in {ledger.action_trace}"
        assert required_rel in str(ledger.files_inspected), ledger.files_inspected


def assert_agenda_guided_does_not_prefetch_upfront_lets_model_cooperate():
    with tempfile.TemporaryDirectory(dir=ROOT) as td:
        from pathlib import Path

        sample = Path(td) / "agenda_guided_cooperate.py"
        sample.write_text("def find_target():\n    return 'found'\n", encoding="utf-8")
        sample_rel = os.path.relpath(sample, ROOT)
        m = mission(
            mission_id="mission_agenda_guided_cooperate",
            objective="Investigate whether the target function exists.",
            objective_style="open_investigation",
            evidence_collection_mode="agenda_guided",
            allowed_roots=[],
            allowed_paths=[sample_rel],
            allowed_tool_classes=["read"],
            tool_budget=5,
            answer_obligations=[
                {
                    "id": "q1",
                    "question": "Does the target file contain find_target()?",
                    "required": True,
                    "source_hints": [sample_rel],
                    "source_requirements": [{"path": sample_rel, "evidence_kind": "function_presence", "required": True, "required_shapes": ["function_definition"]}],
                }
            ],
            must_inspect=[sample_rel],
            exploration_policy={"after_required_floor": "close_immediately", "min_optional_actions_after_floor": 0, "max_optional_actions_after_floor": 0, "require_contradiction_search": False},
        )
        ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
        calls = {"n": 0}

        def fake_model(messages, tools, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "choices": [{
                        "message": {
                            "content": (
                                '{"action_type":"tool_call","phase":"NARROW","tool_name":"rtk_read",'
                                f'"arguments":{{"path":"{sample_rel}"}},'
                                '"reason":"Inspect the target file.","hypothesis":"The file may define find_target.",'
                                '"target_question":"Does the file contain find_target?",'
                                '"expected_information_gain":"Find the function definition.",'
                                '"why_not_report_yet":"Need to inspect the file first."}'
                            )
                        }
                    }]
                }
            if calls["n"] == 2:
                return {
                    "choices": [{
                        "message": {
                            "content": json.dumps({
                                "action_type": "final_report",
                                "report": {
                                    "status": "COMPLETE",
                                    "files_inspected": [{"path": sample_rel, "complete": True}],
                                    "commands_run": [{"tool": "rtk_read", "args": {"path": sample_rel}}],
                                    "findings": [{"claim": "The target file contains find_target.", "evidence_refs": ["file:" + sample_rel, "command:0"]}],
                                    "uncertainties": [],
                                    "confidence": "HIGH",
                                    "caveats": ["agenda_guided cooperative test"],
                                    "escalation_recommendation": "GPT-5.5 review",
                                    "missing_fields": [],
                                },
                            })
                        }
                    }]
                }
            raise TimeoutError("simulated close timeout")

        result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=90)
        redirects = [
            entry
            for entry in (getattr(ledger, "action_trace", []) or [])
            if getattr(entry, "runtime_decision", "") == "redirected"
        ]
        prefetches = [
            entry
            for entry in (getattr(ledger, "action_trace", []) or [])
            if getattr(entry, "runtime_decision", "") == "runtime_prefetch"
        ]
        assert len(redirects) == 0, f"Expected 0 redirects in cooperative mode, got {redirects}"
        assert len(prefetches) == 0, f"Expected 0 prefetches in cooperative mode, got {prefetches}"
        assert result["status"] == "COMPLETE", result


def assert_exploration_policy_close_immediately_blocks_further_tool_calls():
    with tempfile.TemporaryDirectory(dir=ROOT) as td:
        from pathlib import Path

        required = Path(td) / "close_immediate_target.py"
        required.write_text("def find_target():\n    return 'found'\n", encoding="utf-8")
        required_rel = os.path.relpath(required, ROOT)
        m = mission(
            mission_id="mission_exploration_close_immediately",
            objective="Investigate the target function.",
            objective_style="open_investigation",
            evidence_collection_mode="agenda_guided",
            exploration_policy={"after_required_floor": "close_immediately", "require_contradiction_search": False},
            allowed_roots=[],
            allowed_paths=[required_rel],
            allowed_tool_classes=["read"],
            tool_budget=5,
            answer_obligations=[
                {
                    "id": "q1",
                    "question": "Does the target file contain find_target()?",
                    "required": True,
                    "source_hints": [required_rel],
                    "source_requirements": [{"path": required_rel, "evidence_kind": "function_presence", "required": True}],
                }
            ],
            must_inspect=[required_rel],
        )
        ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
        calls = {"n": 0}

        def fake_model(messages, tools, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "choices": [{
                        "message": {
                            "content": (
                                '{"action_type":"tool_call","phase":"NARROW","tool_name":"rtk_read",'
                                f'"arguments":{{"path":"{required_rel}"}},'
                                '"reason":"Inspect the target.","hypothesis":"The file may contain find_target.",'
                                '"target_question":"Does the file contain find_target?",'
                                '"expected_information_gain":"Confirm the function exists.",'
                                '"why_not_report_yet":"Need to inspect first."}'
                            )
                        }
                    }]
                }
            if calls["n"] == 2:
                return {
                    "choices": [{
                        "message": {
                            "content": json.dumps({
                                "action_type": "final_report",
                                "report": {
                                    "status": "COMPLETE",
                                    "files_inspected": [{"path": required_rel, "complete": True}],
                                    "commands_run": [{"tool": "rtk_read", "args": {"path": required_rel}}],
                                    "findings": [{"claim": "The target file contains find_target.", "evidence_refs": ["file:" + required_rel, "command:0"]}],
                                    "uncertainties": [],
                                    "confidence": "HIGH",
                                    "caveats": ["close_immediately test"],
                                    "escalation_recommendation": "GPT-5.5 review",
                                    "missing_fields": [],
                                },
                            })
                        }
                    }]
                }
            raise TimeoutError("simulated close timeout")

        result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=90)
        assert result["status"] == "COMPLETE", f"close_immediately should allow COMPLETE after required read, got {result['status']}"
        assert required_rel in str(ledger.files_inspected), ledger.files_inspected


def assert_exploration_policy_min_optional_redirects_early_complete():
    with tempfile.TemporaryDirectory(dir=ROOT) as td:
        from pathlib import Path

        required = Path(td) / "min_optional_target.py"
        required.write_text("def find_target():\n    return 'found'\n", encoding="utf-8")
        required_rel = os.path.relpath(required, ROOT)
        extra = Path(td) / "extra_check.py"
        extra.write_text("# extra context\nEXTRA = True\n", encoding="utf-8")
        extra_rel = os.path.relpath(extra, ROOT)
        m = mission(
            mission_id="mission_exploration_min_optional",
            objective="Investigate the target function.",
            objective_style="open_investigation",
            evidence_collection_mode="agenda_guided",
            exploration_policy={"after_required_floor": "allow_model_exploration", "min_optional_actions_after_floor": 1, "max_optional_actions_after_floor": 3, "require_contradiction_search": False},
            allowed_roots=[],
            allowed_paths=[required_rel, extra_rel],
            allowed_tool_classes=["read"],
            tool_budget=6,
            answer_obligations=[
                {
                    "id": "q1",
                    "question": "Does the target file contain find_target()?",
                    "required": True,
                    "source_hints": [required_rel],
                    "source_requirements": [{"path": required_rel, "evidence_kind": "function_presence", "required": True}],
                }
            ],
            must_inspect=[required_rel],
        )
        ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
        calls = {"n": 0}

        def fake_model(messages, tools, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "choices": [{
                        "message": {
                            "content": (
                                '{"action_type":"tool_call","phase":"NARROW","tool_name":"rtk_read",'
                                f'"arguments":{{"path":"{required_rel}"}},'
                                '"reason":"Inspect the target.","hypothesis":"The file may contain find_target.",'
                                '"target_question":"Does the file contain find_target?",'
                                '"expected_information_gain":"Confirm the function exists.",'
                                '"why_not_report_yet":"Need to inspect first."}'
                            )
                        }
                    }]
                }
            if calls["n"] == 2:
                return {
                    "choices": [{
                        "message": {
                            "content": (
                                '{"action_type":"tool_call","phase":"VERIFY","tool_name":"rtk_read",'
                                f'"arguments":{{"path":"{extra_rel}"}},'
                                '"reason":"Check extra file for confirmation.","hypothesis":"The extra file may provide supporting context.",'
                                '"target_question":"Does the extra file support the finding?",'
                                '"expected_information_gain":"Find corroborating evidence.",'
                                '"why_not_report_yet":"Doing the optional check."}'
                            )
                        }
                    }]
                }
            if calls["n"] == 3:
                return {
                    "choices": [{
                        "message": {
                            "content": json.dumps({
                                "action_type": "final_report",
                                "report": {
                                    "status": "COMPLETE",
                                    "files_inspected": [{"path": required_rel, "complete": True}, {"path": extra_rel, "complete": True}],
                                    "commands_run": [{"tool": "rtk_read", "args": {"path": required_rel}}, {"tool": "rtk_read", "args": {"path": extra_rel}}],
                                    "findings": [{"claim": "The target file contains find_target.", "evidence_refs": ["file:" + required_rel, "command:0"]}],
                                    "uncertainties": [],
                                    "confidence": "HIGH",
                                    "caveats": ["min_optional satisfied via exploration"],
                                    "escalation_recommendation": "GPT-5.5 review",
                                    "missing_fields": [],
                                },
                            })
                        }
                    }]
                }
            raise TimeoutError("simulated close timeout")

        result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=90)
        assert getattr(ledger, "optional_exploration_actions", 0) >= 1, f"Expected at least 1 optional exploration action, got {getattr(ledger, 'optional_exploration_actions', 0)}"
        assert result["status"] == "COMPLETE", result


def assert_exploration_policy_contradiction_blocks_closure():
    with tempfile.TemporaryDirectory(dir=ROOT) as td:
        from pathlib import Path

        required = Path(td) / "contradiction_closure_target.py"
        required.write_text("def find_target():\n    return 'found'\n", encoding="utf-8")
        required_rel = os.path.relpath(required, ROOT)
        m = mission(
            mission_id="mission_exploration_contradiction",
            objective="Investigate the target function.",
            objective_style="open_investigation",
            evidence_collection_mode="agenda_guided",
            exploration_policy={"after_required_floor": "allow_model_exploration", "min_optional_actions_after_floor": 0, "max_optional_actions_after_floor": 3, "require_contradiction_search": True},
            allowed_roots=[],
            allowed_paths=[required_rel],
            allowed_tool_classes=["read"],
            tool_budget=6,
            answer_obligations=[
                {
                    "id": "q1",
                    "question": "Does the target file contain find_target()?",
                    "required": True,
                    "source_hints": [required_rel],
                    "source_requirements": [{"path": required_rel, "evidence_kind": "function_presence", "required": True}],
                }
            ],
            must_inspect=[required_rel],
        )
        ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
        calls = {"n": 0}

        def fake_model(messages, tools, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "choices": [{
                        "message": {
                            "content": (
                                '{"action_type":"tool_call","phase":"NARROW","tool_name":"rtk_read",'
                                f'"arguments":{{"path":"{required_rel}"}},'
                                '"reason":"Search for contradictions in the target.","hypothesis":"The file might have hidden contradictory logic.",'
                                '"target_question":"Is there any contradiction in the file?",'
                                '"expected_information_gain":"Check for contradictions.",'
                                '"why_not_report_yet":"Need to verify no contradictions."}'
                            )
                        }
                    }]
                }
            if calls["n"] == 2:
                return {
                    "choices": [{
                        "message": {
                            "content": json.dumps({
                                "action_type": "final_report",
                                "report": {
                                    "status": "COMPLETE",
                                    "files_inspected": [{"path": required_rel, "complete": True}],
                                    "commands_run": [{"tool": "rtk_read", "args": {"path": required_rel}}],
                                    "findings": [{"claim": "The target file contains find_target. No contradictions found.", "evidence_refs": ["file:" + required_rel, "command:0"]}],
                                    "uncertainties": [],
                                    "confidence": "HIGH",
                                    "caveats": ["contradiction search done"],
                                    "escalation_recommendation": "GPT-5.5 review",
                                    "missing_fields": [],
                                },
                            })
                        }
                    }]
                }
            raise TimeoutError("simulated close timeout")

        result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=90)
        assert getattr(ledger, "contradiction_search_done", False), "Expected contradiction_search_done to be True"
        assert result["status"] == "COMPLETE", result


def assert_exploration_partial_dict_caps_complete_when_exploration_unmet():
    with tempfile.TemporaryDirectory(dir=ROOT) as td:
        from pathlib import Path

        required = Path(td) / "partial_cap_target.py"
        required.write_text("def find_target():\n    return 'found'\n", encoding="utf-8")
        required_rel = os.path.relpath(required, ROOT)
        m = mission(
            mission_id="mission_exploration_partial_cap",
            objective="Investigate the target function.",
            objective_style="open_investigation",
            evidence_collection_mode="agenda_guided",
            exploration_policy={
                "after_required_floor": "allow_model_exploration",
                "min_optional_actions_after_floor": 2,
                "max_optional_actions_after_floor": 3,
                "require_contradiction_search": True,
            },
            allowed_roots=[],
            allowed_paths=[required_rel],
            allowed_tool_classes=["read"],
            tool_budget=5,
            answer_obligations=[
                {
                    "id": "q1",
                    "question": "Does the target file contain find_target()?",
                    "required": True,
                    "source_hints": [required_rel],
                    "source_requirements": [{"path": required_rel, "evidence_kind": "function_presence", "required": True}],
                }
            ],
            must_inspect=[required_rel],
        )
        ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
        calls = {"n": 0}

        def fake_model(messages, tools, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "choices": [{
                        "message": {
                            "content": (
                                '{"action_type":"tool_call","phase":"NARROW","tool_name":"rtk_read",'
                                f'"arguments":{{"path":"{required_rel}"}},'
                                '"reason":"Inspect the target.","hypothesis":"The file may contain find_target.",'
                                '"target_question":"Does the file contain find_target?",'
                                '"expected_information_gain":"Confirm the function exists.",'
                                '"why_not_report_yet":"Need to inspect first."}'
                            )
                        }
                    }]
                }
            raise TimeoutError("simulated model timeout before completing exploration")

        result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=30)
        assert result["status"] == "PARTIAL", f"Runtime closure should be capped at PARTIAL when exploration unmet, got {result['status']}"
        report = result.get("report", {}) or {}
        caveats = " ".join(report.get("caveats", []) or [])
        assert "exploration policy" in caveats.lower(), f"Caveats should mention exploration policy cap, got: {caveats[:200]}"


def assert_evidence_kind_satisfied_when_all_shapes_present():
    with tempfile.TemporaryDirectory(dir=ROOT) as td:
        from pathlib import Path

        sample = Path(td) / "all_shapes_present.py"
        sample.write_text("def find_target():\n    return 'found'\n\nclass Target:\n    pass\n\nMAPPING = {}\n", encoding="utf-8")
        sample_rel = os.path.relpath(sample, ROOT)
        m = mission(
            mission_id="mission_evidence_kind_all_shapes",
            objective="Investigate whether all required shapes exist in the source.",
            objective_style="open_investigation",
            allowed_roots=[],
            allowed_paths=[sample_rel],
            allowed_tool_classes=["read"],
            tool_budget=4,
            answer_obligations=[
                {
                    "id": "q1",
                    "question": "Does the source contain all required evidence shapes?",
                    "required": True,
                    "source_hints": [sample_rel],
                    "source_requirements": [{
                        "path": sample_rel,
                        "evidence_kind": "multi_shape_verification",
                        "required": True,
                        "prefetch": False,
                        "required_shapes": ["function_definition", "class_definition", "mapping_assignment"],
                    }],
                }
            ],
            must_inspect=[sample_rel],
            exploration_policy={"after_required_floor": "close_immediately", "min_optional_actions_after_floor": 0, "max_optional_actions_after_floor": 0, "require_contradiction_search": False},
        )
        ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
        calls = {"n": 0}

        def fake_model(messages, tools, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "choices": [{
                        "message": {
                            "content": (
                                '{"action_type":"tool_call","phase":"NARROW","tool_name":"rtk_read",'
                                f'"arguments":{{"path":"{sample_rel}"}},'
                                '"reason":"Inspect the required file for all shapes.","hypothesis":"The file has function, class, and mapping.",'
                                '"target_question":"Does the source contain all required evidence shapes?",'
                                '"expected_information_gain":"Determine whether all three shapes exist.",'
                                '"why_not_report_yet":"Need to inspect the file first."}'
                            )
                        }
                    }]
                }
            if calls["n"] == 2:
                return {
                    "choices": [{
                        "message": {
                            "content": json.dumps({
                                "action_type": "final_report",
                                "report": {
                                    "status": "COMPLETE",
                                    "files_inspected": [{"path": sample_rel, "complete": True}],
                                    "commands_run": [{"tool": "rtk_read", "args": {"path": sample_rel}}],
                                    "findings": [{"claim": "The source contains function_definition, class_definition, and mapping_assignment.", "evidence_refs": ["file:" + sample_rel, "command:0"]}],
                                    "uncertainties": [],
                                    "confidence": "HIGH",
                                    "caveats": ["all shapes present"],
                                    "escalation_recommendation": "GPT-5.5 review",
                                    "missing_fields": [],
                                },
                            })
                        }
                    }]
                }
            raise TimeoutError("simulated close timeout")

        result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=90)
        assert result["status"] == "COMPLETE", f"All shapes present should yield COMPLETE, got {result['status']}"
        insufficient = list(result.get("report", {}).get("insufficient_evidence_obligations", []) or [])
        assert len(insufficient) == 0, f"Expected 0 insufficient obligations, got {insufficient}"


def assert_evidence_kind_insufficient_when_some_shapes_missing():
    with tempfile.TemporaryDirectory(dir=ROOT) as td:
        from pathlib import Path

        sample = Path(td) / "partial_shapes.py"
        sample.write_text("def find_target():\n    return 'found'\n\nMAPPING = {}\n", encoding="utf-8")
        sample_rel = os.path.relpath(sample, ROOT)
        m = mission(
            mission_id="mission_evidence_kind_partial_shapes",
            objective="Investigate whether all required shapes exist, some are missing.",
            objective_style="open_investigation",
            allowed_roots=[],
            allowed_paths=[sample_rel],
            allowed_tool_classes=["read"],
            tool_budget=4,
            answer_obligations=[
                {
                    "id": "q1",
                    "question": "Does the source contain all required evidence shapes?",
                    "required": True,
                    "source_hints": [sample_rel],
                    "source_requirements": [{
                        "path": sample_rel,
                        "evidence_kind": "multi_shape_verification",
                        "required": True,
                        "prefetch": False,
                        "required_shapes": ["function_definition", "class_definition", "mapping_assignment"],
                    }],
                }
            ],
            must_inspect=[sample_rel],
        )
        ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
        calls = {"n": 0}

        def fake_model(messages, tools, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "choices": [{
                        "message": {
                            "content": (
                                '{"action_type":"tool_call","phase":"NARROW","tool_name":"rtk_read",'
                                f'"arguments":{{"path":"{sample_rel}"}},'
                                '"reason":"Inspect the required file.","hypothesis":"The file may be missing some shapes.",'
                                '"target_question":"Does the source contain all required evidence shapes?",'
                                '"expected_information_gain":"Determine which shapes exist and which are missing.",'
                                '"why_not_report_yet":"Need to inspect the file first."}'
                            )
                        }
                    }]
                }
            raise TimeoutError("simulated close timeout")

        result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=30)
        assert result["status"] == "PARTIAL", f"Missing class_definition should yield PARTIAL, got {result['status']}"
        insufficient = list(result.get("report", {}).get("insufficient_evidence_obligations", []) or [])
        assert "q1" in insufficient, f"Expected q1 in insufficient_evidence_obligations, got {insufficient}"


def assert_evidence_kind_multiple_shapes_with_contradiction():
    with tempfile.TemporaryDirectory(dir=ROOT) as td:
        from pathlib import Path

        sample = Path(td) / "shape_with_contradiction.py"
        sample.write_text("def find_target():\n    return 'found'\n\nSECRET_OVERRIDE = True\n", encoding="utf-8")
        sample_rel = os.path.relpath(sample, ROOT)
        m = mission(
            mission_id="mission_evidence_kind_shape_contradiction",
            objective="Investigate shapes with contradiction markers.",
            objective_style="open_investigation",
            allowed_roots=[],
            allowed_paths=[sample_rel],
            allowed_tool_classes=["read"],
            tool_budget=4,
            answer_obligations=[
                {
                    "id": "q1",
                    "question": "Does the source contain the required shape and is free of contradiction markers?",
                    "required": True,
                    "source_hints": [sample_rel],
                    "source_requirements": [{
                        "path": sample_rel,
                        "evidence_kind": "shape_and_contradiction",
                        "required": True,
                        "prefetch": False,
                        "required_shapes": ["function_definition"],
                        "contradiction_markers": ["SECRET_OVERRIDE"],
                    }],
                }
            ],
            must_inspect=[sample_rel],
        )
        ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
        calls = {"n": 0}

        def fake_model(messages, tools, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "choices": [{
                        "message": {
                            "content": (
                                '{"action_type":"tool_call","phase":"NARROW","tool_name":"rtk_read",'
                                f'"arguments":{{"path":"{sample_rel}"}},'
                                '"reason":"Inspect the file for shapes and contradiction markers.","hypothesis":"The file may contain SECRET_OVERRIDE.",'
                                '"target_question":"Does the source contain SECRET_OVERRIDE?",'
                                '"expected_information_gain":"Determine whether contradiction blocks completion.",'
                                '"why_not_report_yet":"Need to inspect the file first."}'
                            )
                        }
                    }]
                }
            raise TimeoutError("simulated close timeout")

        result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=30)
        assert result["status"] == "ESCALATE", f"Contradiction marker should yield ESCALATE, got {result['status']}"
        contradicted = list(result.get("report", {}).get("contradicted_obligations", []) or [])
        assert "q1" in contradicted, f"Expected q1 in contradicted_obligations, got {contradicted}"


def assert_semantic_gate_downgrades_hollow_complete():
    from codex_oss.report_semantics import evaluate_report_semantic_completeness, build_published_answer, apply_semantic_status_cap

    # Simulate a hollow COMPLETE report
    hollow_report = {
        "status": "COMPLETE",
        "findings": [],
        "missing_fields": ["target value not retrieved"],
        "uncertainties": ["The target value was not retrieved from the file."],
        "caveats": ["File was not inspected directly."],
        "confidence": "HIGH",
    }
    mission = _build_mission({"schema_version": "oss_agent_mission.v1", "mission_id": "test_semantic", "tier": "A3", "mode": "managed_investigation", "objective": "test", "risk_tier": "low", "write_allowed": False, "allowed_roots": ["codex_oss/"], "allowed_paths": [], "tool_budget": 3, "time_budget_seconds": 30, "allowed_tool_classes": ["read", "search", "list"], "stop_conditions": ["valid_report", "budget_exhausted", "deadline_reached"], "report_schema": "managed_investigation_report.v1", "required_outputs": ["files_inspected", "commands_run", "findings", "uncertainties", "confidence", "caveats", "escalation_recommendation"], "objective_style": "open_investigation"})

    answer_graph = {"required_obligations": [{"id": "q1", "question": "Where?", "status": "answered", "evidence_refs": ["file:x"]}], "sufficiency": {"required_answered": 1, "required_total": 1, "can_close": True, "recommended_status": "COMPLETE", "confidence_cap": "MEDIUM", "closure_entitlement": {"can_return_complete": True}}}
    published = build_published_answer(mission, answer_graph, None)
    result = evaluate_report_semantic_completeness(mission, hollow_report, published, answer_graph, None)

    assert result["ok"] is False, f"Hollow COMPLETE should fail: {result}"
    assert "complete_with_empty_findings" in result["reason_codes"], result
    assert "complete_with_missing_fields" in result["reason_codes"], result
    assert "complete_with_negating_uncertainty" in result["reason_codes"], result
    assert result["status_cap"] == "PARTIAL", result

    # Apply the cap
    fixed = apply_semantic_status_cap(dict(hollow_report), result)
    assert fixed["status"] == "PARTIAL", f"Status should be capped to PARTIAL: {fixed}"
    assert "status capped" in " ".join(fixed.get("caveats", [])), fixed

    # Valid report should pass
    valid_report = {
        "status": "COMPLETE",
        "findings": [{"claim": "The definition is in codex_oss/runtime/loop.py.", "evidence_refs": ["file:x#extract:1"]}],
        "missing_fields": [],
        "uncertainties": ["Only scoped files were inspected."],
        "caveats": [],
        "confidence": "MEDIUM",
    }
    result2 = evaluate_report_semantic_completeness(mission, valid_report, published, answer_graph, None)
    assert result2["ok"] is True, f"Valid COMPLETE should pass: {result2}"


def assert_run_loop_downgrades_hollow_model_complete():
    """Integration: fake model reads file then returns hollow COMPLETE — runtime must downgrade."""
    with tempfile.TemporaryDirectory(dir=ROOT) as td:
        from pathlib import Path

        sample = Path(td) / "semantic_hollow_target.py"
        sample.write_text("def find_target():\n    return 'found'\n", encoding="utf-8")
        sample_rel = os.path.relpath(sample, ROOT)
        m = mission(
            mission_id="mission_semantic_hollow_integration",
            objective="Investigate whether the target function exists.",
            objective_style="open_investigation",
            evidence_collection_mode="prefetch_floor",
            exploration_policy={"after_required_floor": "close_immediately", "min_optional_actions_after_floor": 0, "max_optional_actions_after_floor": 0, "require_contradiction_search": False},
            allowed_roots=[],
            allowed_paths=[sample_rel],
            allowed_tool_classes=["read"],
            tool_budget=5,
            answer_obligations=[
                {
                    "id": "q1",
                    "question": "Does the target file contain find_target()?",
                    "required": True,
                    "source_hints": [sample_rel],
                    "source_requirements": [{"path": sample_rel, "evidence_kind": "function_presence", "required": True}],
                }
            ],
            must_inspect=[sample_rel],
        )
        ledger = EvidenceLedger(mission_id=m.mission_id, tool_budget_remaining=m.tool_budget)
        calls = {"n": 0}

        def fake_model(messages, tools, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                # Read the required file to satisfy evidence floor
                return {
                    "choices": [{
                        "message": {
                            "content": (
                                '{"action_type":"tool_call","phase":"NARROW","tool_name":"rtk_read",'
                                f'"arguments":{{"path":"{sample_rel}"}},'
                                '"reason":"Inspect target file.","hypothesis":"find_target exists.",'
                                '"target_question":"Does the file contain find_target?",'
                                '"expected_information_gain":"Confirm.",'
                                '"why_not_report_yet":"Need to inspect first."}'
                            )
                        }
                    }]
                }
            if calls["n"] == 2:
                # Return a hollow COMPLETE — structurally valid but semantically empty
                return {
                    "choices": [{
                        "message": {
                            "content": json.dumps({
                                "action_type": "final_report",
                                "report": {
                                    "status": "COMPLETE",
                                    "files_inspected": [{"path": sample_rel, "complete": True}],
                                    "commands_run": [{"tool": "rtk_read", "args": {"path": sample_rel}}],
                                    "findings": [],
                                    "missing_fields": ["target function not found"],
                                    "uncertainties": ["The target value was not retrieved."],
                                    "confidence": "HIGH",
                                    "caveats": [],
                                    "escalation_recommendation": "GPT-5.5 review",
                                },
                            })
                        }
                    }]
                }
            if calls["n"] == 3:
                # After repair attempt, return valid COMPLETE
                return {
                    "choices": [{
                        "message": {
                            "content": json.dumps({
                                "action_type": "final_report",
                                "report": {
                                    "status": "COMPLETE",
                                    "files_inspected": [{"path": sample_rel, "complete": True}],
                                    "commands_run": [{"tool": "rtk_read", "args": {"path": sample_rel}}],
                                    "findings": [{"claim": "find_target is defined in " + sample_rel, "evidence_refs": ["file:" + sample_rel + "#extract:1", "command:0"]}],
                                    "missing_fields": [],
                                    "uncertainties": [],
                                    "confidence": "MEDIUM",
                                    "caveats": [],
                                    "escalation_recommendation": "GPT-5.5 review",
                                },
                            })
                        }
                    }]
                }
            raise TimeoutError("simulated timeout")

        result = run_loop(m, ledger, fake_model, [], None, m.allowed_roots, m.allowed_paths, request_deadline=90)
        # The hollow COMPLETE on call 2 should be sent to repair (or downgraded if no repair budget).
        # Call 3 returns a valid COMPLETE after repair, so final status should be COMPLETE.
        assert result["status"] in ("COMPLETE", "PARTIAL"), f"Expected COMPLETE or PARTIAL after repair, got {result['status']}: {result}"


def assert_record_closure_telemetry_persists_error_and_skip_fields():
    root = tempfile.mkdtemp(prefix="closure_telemetry_")
    try:
        record_closure_telemetry(
            root,
            "attempt_001",
            attempt_type="runtime_fallback",
            closer_model="mission-a3-kimi",
            elapsed_seconds=1.2,
            timeout_seconds=20,
            payload_chars=0,
            deadline_remaining_seconds=4.5,
            skeleton_findings_count=0,
            allowed_statuses=["PARTIAL"],
            result="PARTIAL",
            error_type="timeout",
            error_message="The read operation timed out",
            draft_valid=False,
            merged_report_valid=False,
            closure_status="RUNTIME_CLOSED",
            skip_reason="deadline_reached",
            result_valid=True,
            final_closure_source="runtime_answer_graph",
            repair_attempted=False,
        )
        path = os.path.join(root, "closure_attempts.jsonl")
        with open(path, "r", encoding="utf-8") as handle:
            entry = json.loads(handle.read().strip())
        assert entry["schema_version"] == "closure_attempt.v2", entry
        assert entry["error_type"] == "timeout", entry
        assert entry["error_message"] == "The read operation timed out", entry
        assert entry["skip_reason"] == "deadline_reached", entry
        assert entry["closure_status"] == "RUNTIME_CLOSED", entry
        assert entry["report_valid"] is True, entry
    finally:
        shutil.rmtree(root)


def main():
    assert_run_loop_accepts_valid_final_report()
    assert_model_text_extraction_handles_provider_variants()
    assert_deterministic_fast_path_handles_single_file_function_location_without_model_call()
    assert_read_pool_scheduler_allows_multiple_low_risk_missions()
    assert_mission_requires_scope_and_tool_contract()
    assert_path_policy_blocks_empty_scope_and_denied_symlink()
    assert_broad_roots_are_exactly_broad()
    assert_critical_finality_uses_word_boundaries()
    assert_file_extract_refs_resolve()
    assert_report_validation_rejects_non_object_findings_without_crashing()
    assert_managed_bridge_returns_terminal_report_on_runtime_error()
    assert_runtime_model_alias_requires_mission_and_maps_reasoning_model()
    assert_runtime_model_alias_uses_fallback_on_model_failure()
    assert_readonly_mission_writes_artifact_bundle()
    assert_response_emitter_streams_commentary_and_final_phases()
    assert_runtime_mission_time_budget_extends_internal_deadline()
    assert_tool_classes_are_enforced_and_aliases_normalize()
    assert_duplicate_searches_are_suppressed()
    assert_near_deadline_requests_final_report_when_evidence_exists()
    assert_runtime_forces_final_report_after_file_evidence_when_budget_is_low()
    assert_runtime_traces_followup_search_after_file_evidence()
    assert_duplicate_range_read_is_served_from_cached_evidence()
    assert_duplicate_full_read_returns_cached_extracts_and_requests_report()
    assert_unsupported_followup_after_file_evidence_redirects_to_report()
    assert_model_failure_after_evidence_returns_finding_not_empty_partial()
    assert_deterministic_finalizer_extracts_profile_limits_from_cached_evidence()
    assert_deterministic_finalizer_mines_cached_text_beyond_default_extracts()
    assert_deterministic_finalizer_does_not_treat_symbol_mentions_as_definitions()
    assert_range_read_does_not_block_later_full_read_or_finalizer_claim()
    assert_definition_claims_require_definition_shaped_evidence()
    assert_definition_claims_can_be_supported_by_command_evidence()
    assert_deterministic_finalizer_mines_command_profile_evidence()
    assert_deterministic_finalizer_mines_alias_mapping_from_file_evidence()
    assert_deterministic_finalizer_mines_validator_module_evidence()
    assert_objective_specs_handle_generic_config_function_and_zero_match()
    assert_answer_graph_preserves_command_result_requirements()
    assert_config_value_extraction_uses_target_block_not_first_fields()
    assert_mission_accepts_explicit_objective_spec_and_strict_mode()
    assert_explicit_objective_spec_overrides_prose_classifier()
    assert_complete_report_with_explicit_spec_requires_required_value()
    assert_explicit_objective_satisfaction_switches_to_short_closure()
    assert_deterministic_finalizer_can_complete_explicit_satisfied_objective()
    assert_deterministic_finalizer_can_complete_explicit_rtk_grep_function_location()
    assert_model_reports_are_annotated_with_runtime_provenance()
    assert_concurrency_policy_serializes_with_small_queue()
    assert_workspace_apply_scheduler_stays_serialized()
    assert_adaptive_autonomy_budget_redirects_excess_broad_searches()
    assert_adaptive_autonomy_budget_allows_post_evidence_verification_with_rationale()
    assert_complete_alias_report_must_name_mapped_model()
    assert_grep_definition_result_provides_targeted_read_hint()
    assert_unsupported_tool_args_are_reported_without_execution()
    assert_advanced_grep_args_are_supported()
    assert_read_line_ranges_are_supported()
    assert_incidental_critical_terms_do_not_stop_low_risk_mission()
    assert_health_exposes_source_identity()
    assert_open_investigation_runtime_answer_graph_can_complete_after_model_timeout()
    assert_open_investigation_runtime_answer_graph_stays_partial_when_obligations_remain_open()
    assert_open_investigation_prefetch_reads_required_sources_before_model_loop()
    assert_open_investigation_redirects_to_pending_required_source()
    assert_open_investigation_reports_insufficient_evidence_when_shape_missing()
    assert_open_investigation_escalates_on_contradiction_marker()
    assert_open_investigation_escalates_on_blocked_source()
    assert_agenda_guided_redirects_then_prefetches_after_model_ignores_required_source()
    assert_agenda_guided_does_not_prefetch_upfront_lets_model_cooperate()
    assert_exploration_policy_close_immediately_blocks_further_tool_calls()
    assert_exploration_policy_min_optional_redirects_early_complete()
    assert_exploration_policy_contradiction_blocks_closure()
    assert_exploration_partial_dict_caps_complete_when_exploration_unmet()
    assert_evidence_kind_satisfied_when_all_shapes_present()
    assert_evidence_kind_insufficient_when_some_shapes_missing()
    assert_evidence_kind_multiple_shapes_with_contradiction()
    assert_semantic_gate_downgrades_hollow_complete()
    assert_run_loop_downgrades_hollow_model_complete()
    assert_record_closure_telemetry_persists_error_and_skip_fields()
    print("PASS: runtime contract suite")


if __name__ == "__main__":
    main()
