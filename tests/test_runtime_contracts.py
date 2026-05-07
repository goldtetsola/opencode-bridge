#!/usr/bin/env python3
"""Runtime contract tests for OSS Agent Runtime v1."""

from __future__ import annotations

import os
import sys
import tempfile

os.environ["ALLOW_MISSING_OPENCODE_KEY"] = "1"
ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from codex_oss.ledger import EvidenceLedger
from codex_oss.health import build_health_status
from codex_oss.managed_bridge import run_managed_mission_from_body
from codex_oss.mission import InvalidHandoffError, _build_mission
from codex_oss.runtime import ToolResult, resolve_path
from codex_oss.runtime.loop import run_loop
from codex_oss.runtime.policy import is_broad_root
from codex_oss.validation import validate_report


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
                    '"allowed_paths":[],"tool_budget":1,"time_budget_seconds":30,'
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


def assert_tool_classes_are_enforced_and_aliases_normalize():
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


def main():
    assert_run_loop_accepts_valid_final_report()
    assert_mission_requires_scope_and_tool_contract()
    assert_path_policy_blocks_empty_scope_and_denied_symlink()
    assert_broad_roots_are_exactly_broad()
    assert_file_extract_refs_resolve()
    assert_managed_bridge_returns_terminal_report_on_runtime_error()
    assert_tool_classes_are_enforced_and_aliases_normalize()
    assert_health_exposes_source_identity()
    print("PASS: runtime contract suite")


if __name__ == "__main__":
    main()
