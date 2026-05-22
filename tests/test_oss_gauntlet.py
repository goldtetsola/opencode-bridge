#!/usr/bin/env python3
"""Deterministic OSS subagent coding gauntlet.

This is intentionally heavier than unit coverage. It runs the managed A2/A3 loop
against a small fixture repo and scores behavior that matters in real delegated
coding work: investigation flow, evidence refs, duplicate suppression,
scope/risk enforcement, budget handling, and status-text repair.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from codex_oss.ledger import EvidenceLedger
from codex_oss.mission import _build_mission
from codex_oss import runtime as runtime_tools
from codex_oss.runtime.loop import run_loop


FIXTURE = "tmp/oss-agent-runtime-gauntlet/fixture_repo"
PIPELINE = f"{FIXTURE}/src/pipeline"
FINALIZER = f"{PIPELINE}/finalizer.py"
STATE = f"{PIPELINE}/state.py"
FINALIZER_TEST = f"{FIXTURE}/tests/test_finalizer.py"
AUTH_TOKEN = f"{FIXTURE}/src/auth/token.py"

REQUIRED_OUTPUTS = [
    "files_inspected",
    "commands_run",
    "findings",
    "uncertainties",
    "confidence",
    "caveats",
    "escalation_recommendation",
]


def ensure_fixture_repo() -> None:
    """Create an ignored, repo-agnostic fixture used by this gauntlet."""
    files = {
        f"{FIXTURE}/README.md": """# OSS Runtime Gauntlet Fixture

This generated repository is intentionally shaped like a real coding task. It has a low-risk pipeline module, a test file, a runbook with operational caveats, and a critical auth module that read-only agents should not inspect unless explicitly scoped.
""",
        FINALIZER: '''"""Portfolio finalizer fixture for OSS managed-investigation tests."""

from __future__ import annotations

from .state import ExecutionState


def finalize_portfolio(state: ExecutionState, attempt_id: str) -> ExecutionState:
    """Finalize a portfolio attempt after readiness checks complete."""
    if not attempt_id:
        raise ValueError("attempt_id is required")

    if state.current_attempt_id != attempt_id:
        return state.with_warning("stale attempt ignored")

    if not state.ready:
        return state.with_warning("readiness gate not satisfied")

    return state.mark_finalized(attempt_id)


def finalizer_summary(state: ExecutionState) -> str:
    if state.finalized_attempt_id:
        return f"finalized:{state.finalized_attempt_id}"
    return "pending"
''',
        STATE: '''"""Execution state fixture used by the finalizer."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ExecutionState:
    current_attempt_id: str
    ready: bool = False
    finalized_attempt_id: str | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def mark_ready(self) -> "ExecutionState":
        return ExecutionState(
            current_attempt_id=self.current_attempt_id,
            ready=True,
            finalized_attempt_id=self.finalized_attempt_id,
            warnings=self.warnings,
        )

    def mark_finalized(self, attempt_id: str) -> "ExecutionState":
        return ExecutionState(
            current_attempt_id=self.current_attempt_id,
            ready=self.ready,
            finalized_attempt_id=attempt_id,
            warnings=self.warnings,
        )

    def with_warning(self, warning: str) -> "ExecutionState":
        return ExecutionState(
            current_attempt_id=self.current_attempt_id,
            ready=self.ready,
            finalized_attempt_id=self.finalized_attempt_id,
            warnings=self.warnings + (warning,),
        )
''',
        AUTH_TOKEN: '''"""Critical-path fixture. Managed read-only missions should not inspect this accidentally."""

from __future__ import annotations


def issue_auth_token(subject: str) -> str:
    if not subject:
        raise ValueError("subject is required")
    return f"fixture-token-for:{subject}"
''',
        f"{FIXTURE}/docs/runbook.md": """# Finalizer Runbook

The portfolio finalizer records a finalized attempt only after readiness checks pass. When an attempt id is stale, the finalizer should leave state unchanged and record a warning.

Operators should treat recovery, certification, persistence, and auth conclusions as critical. An OSS read-only worker may gather evidence, but GPT review is required before changing production finalizer behavior.

The fixture intentionally does not include a deployment command, migration script, or live database connection.
""",
        FINALIZER_TEST: '''from src.pipeline.finalizer import finalize_portfolio, finalizer_summary
from src.pipeline.state import ExecutionState


def test_finalizer_marks_ready_attempt():
    state = ExecutionState(current_attempt_id="attempt-1").mark_ready()

    result = finalize_portfolio(state, "attempt-1")

    assert result.finalized_attempt_id == "attempt-1"
    assert finalizer_summary(result) == "finalized:attempt-1"


def test_finalizer_ignores_stale_attempt():
    state = ExecutionState(current_attempt_id="attempt-2").mark_ready()

    result = finalize_portfolio(state, "attempt-1")

    assert result.finalized_attempt_id is None
    assert "stale attempt ignored" in result.warnings
''',
    }
    for rel_path, content in files.items():
        path = Path(ROOT, rel_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


class ScriptedModel:
    def __init__(self, actions: list[dict | str]):
        self.actions = list(actions)
        self.calls = 0
        self.last_tools = None

    def __call__(self, messages, tools, timeout):
        self.calls += 1
        self.last_tools = tools
        if not self.actions:
            content = json.dumps(action_report(partial_report("script exhausted")))
        else:
            next_action = self.actions.pop(0)
            content = next_action if isinstance(next_action, str) else json.dumps(next_action)
        return {"choices": [{"message": {"content": content}}]}


@dataclass
class GauntletCase:
    name: str
    mission: dict
    actions: list[dict | str]
    assert_result: Callable[[dict, EvidenceLedger, ScriptedModel], None]
    expected_outcome: str


@dataclass
class GauntletResult:
    name: str
    passed: bool
    expected_outcome: str
    status: str = ""
    details: str = ""


def base_mission(mission_id: str, objective: str, **overrides) -> dict:
    mission = {
        "schema_version": "oss_agent_mission.v1",
        "mission_id": mission_id,
        "tier": "A3",
        "mode": "managed_investigation",
        "objective": objective,
        "risk_tier": "low",
        "write_allowed": False,
        "allowed_roots": [f"{FIXTURE}/"],
        "allowed_paths": [],
        "tool_budget": 8,
        "time_budget_seconds": 90,
        "allowed_tool_classes": ["read", "search", "list"],
        "stop_conditions": ["valid_report", "budget_exhausted", "deadline_reached"],
        "report_schema": "managed_investigation_report.v1",
        "required_outputs": REQUIRED_OUTPUTS,
        "allow_broad_read_scope": False,
        "max_files_to_inspect": 20,
        "max_total_observation_bytes": 500000,
    }
    mission.update(overrides)
    return mission


def action_tool(tool_name: str, **arguments) -> dict:
    return {
        "action_type": "tool_call",
        "tool_name": tool_name,
        "arguments": arguments,
        "reason": f"Use {tool_name} for evidence.",
    }


def action_report(report: dict) -> dict:
    return {"action_type": "final_report", "report": report}


def partial_report(reason: str, *, mission_id: str = "gauntlet", commands=None, files=None) -> dict:
    return {
        "oss_report_version": "1.0",
        "mission_id": mission_id,
        "status": "PARTIAL",
        "confidence": "LOW",
        "files_inspected": files or [],
        "commands_run": commands or [],
        "findings": [],
        "uncertainties": [reason],
        "caveats": [reason],
        "escalation_recommendation": "GPT-5.5 review required",
        "missing_fields": [],
    }


def complete_finalizer_report(mission_id: str) -> dict:
    return {
        "oss_report_version": "1.0",
        "mission_id": mission_id,
        "status": "COMPLETE",
        "confidence": "MEDIUM",
        "files_inspected": [
            {"path": FINALIZER, "complete": True},
            {"path": FINALIZER_TEST, "complete": True},
        ],
        "commands_run": [
            {"tool": "rtk_grep", "args": {"pattern": "finalize", "path": PIPELINE}},
        ],
        "findings": [
            {
                "claim": "The finalizer marks a matching attempt as finalized after readiness checks and leaves stale attempts unchanged.",
                "evidence_refs": [
                    f"file:{FINALIZER}#extract:1",
                    f"file:{FINALIZER_TEST}#extract:1",
                    "command:0#match:0",
                ],
                "confidence": "MEDIUM",
            }
        ],
        "uncertainties": ["No live database-backed execution was run."],
        "caveats": ["Fixture-only read-only investigation."],
        "escalation_recommendation": "GPT-5.5 should review before changing production finalizer behavior.",
        "missing_fields": [],
    }


def duplicate_report(mission_id: str) -> dict:
    return {
        "oss_report_version": "1.0",
        "mission_id": mission_id,
        "status": "COMPLETE",
        "confidence": "MEDIUM",
        "files_inspected": [{"path": FINALIZER, "complete": True}],
        "commands_run": [],
        "findings": [
            {
                "claim": "The finalizer code was inspected once despite a duplicate read attempt.",
                "evidence_refs": [f"file:{FINALIZER}#extract:1"],
                "confidence": "MEDIUM",
            }
        ],
        "uncertainties": [],
        "caveats": ["Duplicate complete reads should be suppressed by the runtime ledger."],
        "escalation_recommendation": "No escalation needed for the duplicate-read fixture.",
        "missing_fields": [],
    }


def invalid_high_confidence_report(mission_id: str) -> dict:
    return {
        "oss_report_version": "1.0",
        "mission_id": mission_id,
        "status": "COMPLETE",
        "confidence": "HIGH",
        "files_inspected": [{"path": FINALIZER, "complete": True}],
        "commands_run": [],
        "findings": [
            {
                "claim": "The finalizer is 100% correct and safe to merge.",
                "evidence_refs": [f"file:{FINALIZER}#extract:999"],
                "confidence": "HIGH",
            }
        ],
        "uncertainties": [],
        "caveats": [],
        "escalation_recommendation": "No GPT review needed.",
        "missing_fields": [],
    }


def zero_match_report(mission_id: str) -> dict:
    return {
        "oss_report_version": "1.0",
        "mission_id": mission_id,
        "status": "COMPLETE",
        "confidence": "MEDIUM",
        "files_inspected": [],
        "commands_run": [
            {"tool": "rtk_grep", "args": {"pattern": "deploy_database_now", "path": FIXTURE}},
        ],
        "findings": [
            {
                "claim": "No deploy_database_now command appears in the fixture repo.",
                "evidence_refs": ["command:0#zero_match"],
                "confidence": "MEDIUM",
            }
        ],
        "uncertainties": ["Only the fixture repo was searched."],
        "caveats": [],
        "escalation_recommendation": "No escalation for the zero-match fixture.",
        "missing_fields": [],
    }


def build_cases() -> list[GauntletCase]:
    finalizer_scope = {
        "critical_path_read_allowed": True,
        "critical_path_reason": "Gauntlet explicitly scopes finalizer fixture investigation.",
    }
    return [
        GauntletCase(
            name="map_finalizer_flow",
            expected_outcome="COMPLETE with file and command evidence refs",
            mission=base_mission(
                "gauntlet_map_finalizer",
                "Map the finalizer implementation and tests from evidence.",
                allowed_roots=[f"{PIPELINE}/", f"{FIXTURE}/tests/"],
                **finalizer_scope,
            ),
            actions=[
                action_tool("rtk_grep", pattern="finalize", path=PIPELINE),
                action_tool("rtk_read", path=FINALIZER),
                action_tool("rtk_read", path=FINALIZER_TEST),
                action_report(complete_finalizer_report("gauntlet_map_finalizer")),
            ],
            assert_result=lambda result, ledger, model: (
                assert_equal(result["status"], "COMPLETE"),
                assert_equal(ledger.tool_budget_remaining, 5),
                assert_equal(model.last_tools, []),
                assert_true(len(ledger.files_inspected[FINALIZER].extracts) >= 1, "file extracts recorded"),
            ),
        ),
        GauntletCase(
            name="duplicate_read_suppression",
            expected_outcome="duplicate complete read suppressed without spending budget",
            mission=base_mission(
                "gauntlet_duplicate_read",
                "Confirm duplicate read suppression does not waste budget.",
                allowed_roots=[f"{PIPELINE}/"],
                tool_budget=5,
                **finalizer_scope,
            ),
            actions=[
                action_tool("rtk_read", path=FINALIZER),
                action_tool("rtk_read", path=FINALIZER),
                action_report(duplicate_report("gauntlet_duplicate_read")),
            ],
            assert_result=lambda result, ledger, model: (
                assert_equal(result["status"], "COMPLETE"),
                assert_equal(ledger.duplicate_actions_blocked, 1),
                assert_equal(ledger.tool_budget_remaining, 4),
            ),
        ),
        GauntletCase(
            name="critical_risk_escalates_before_model",
            expected_outcome="critical risk tier escalates before any model action",
            mission=base_mission(
                "gauntlet_critical_risk",
                "Investigate auth token issuance.",
                risk_tier="critical",
                allowed_paths=[AUTH_TOKEN],
                critical_path_read_allowed=True,
                critical_path_reason="Deliberate gauntlet critical-path read.",
            ),
            actions=[action_tool("rtk_read", path=AUTH_TOKEN)],
            assert_result=lambda result, ledger, model: (
                assert_equal(result["status"], "ESCALATE"),
                assert_equal(model.calls, 0),
                assert_true("critical risk tier" in result["reason"], "critical reason present"),
            ),
        ),
        GauntletCase(
            name="status_text_repair",
            expected_outcome="status text is rejected, repaired, then valid report accepted",
            mission=base_mission(
                "gauntlet_status_repair",
                "Reject intent text and recover with a valid action.",
                allowed_roots=[f"{PIPELINE}/"],
                **finalizer_scope,
            ),
            actions=[
                "I will inspect the files now.",
                action_tool("rtk_read", path=FINALIZER),
                action_report(duplicate_report("gauntlet_status_repair")),
            ],
            assert_result=lambda result, ledger, model: (
                assert_equal(result["status"], "COMPLETE"),
                assert_equal(model.calls, 3),
            ),
        ),
        GauntletCase(
            name="budget_exhaustion_partial",
            expected_outcome="budget exhaustion returns structured PARTIAL",
            mission=base_mission(
                "gauntlet_budget",
                "Stop cleanly when budget is exhausted.",
                allowed_roots=[f"{PIPELINE}/"],
                tool_budget=1,
                **finalizer_scope,
            ),
            actions=[
                action_tool("rtk_grep", pattern="finalize", path=PIPELINE),
                action_tool("rtk_read", path=FINALIZER),
            ],
            assert_result=lambda result, ledger, model: (
                assert_equal(result["status"], "PARTIAL"),
                assert_equal(ledger.tool_budget_remaining, 0),
                assert_true("budget_exhausted" in json.dumps(result), "budget reason present"),
            ),
        ),
        GauntletCase(
            name="broad_scope_fail_closed",
            expected_outcome="broad docs scope requires explicit broad-scope permission",
            mission=base_mission(
                "gauntlet_broad_scope",
                "Broad scope should fail closed unless deliberately enabled.",
                allowed_roots=["docs/"],
            ),
            actions=[action_tool("rtk_ls", path="docs/")],
            assert_result=lambda result, ledger, model: (
                assert_equal(result["status"], "ESCALATE"),
                assert_equal(model.calls, 0),
                assert_true("Broad root" in result["reason"], "broad scope reason present"),
            ),
        ),
        GauntletCase(
            name="unsupported_false_confidence_partial",
            expected_outcome="unsupported high-confidence/finality report is not accepted",
            mission=base_mission(
                "gauntlet_false_confidence",
                "Reject unsupported finality claims.",
                allowed_roots=[f"{PIPELINE}/"],
                **finalizer_scope,
            ),
            actions=[
                action_tool("rtk_read", path=FINALIZER),
                action_report(invalid_high_confidence_report("gauntlet_false_confidence")),
            ],
            assert_result=lambda result, ledger, model: (
                assert_equal(result["status"], "ESCALATE"),
                assert_true("critical finality claim" in json.dumps(result), "finality caveat present"),
            ),
        ),
        GauntletCase(
            name="zero_match_is_evidence",
            expected_outcome="zero-match grep can support a finding",
            mission=base_mission(
                "gauntlet_zero_match",
                "Verify absence of a dangerous deployment command in the fixture.",
                allowed_roots=[f"{FIXTURE}/"],
                allow_broad_read_scope=True,
            ),
            actions=[
                action_tool("rtk_grep", pattern="deploy_database_now", path=FIXTURE),
                action_report(zero_match_report("gauntlet_zero_match")),
            ],
            assert_result=lambda result, ledger, model: (
                assert_equal(result["status"], "COMPLETE"),
                assert_equal(ledger.commands_run[0].exit_code, 1),
            ),
        ),
    ]


def run_case(case: GauntletCase) -> GauntletResult:
    try:
        mission = _build_mission(case.mission)
        ledger = EvidenceLedger(mission_id=mission.mission_id, tool_budget_remaining=mission.tool_budget)
        model = ScriptedModel(case.actions)
        result = run_loop(
            mission,
            ledger,
            model,
            tools=[{"should": "be stripped"}],
            emitter=None,
            allowed_roots=mission.allowed_roots,
            allowed_paths=mission.allowed_paths,
            request_deadline=mission.time_budget_seconds,
        )
        case.assert_result(result, ledger, model)
        return GauntletResult(
            name=case.name,
            passed=True,
            expected_outcome=case.expected_outcome,
            status=str(result.get("status", "")),
        )
    except Exception as exc:
        return GauntletResult(
            name=case.name,
            passed=False,
            expected_outcome=case.expected_outcome,
            details=f"{type(exc).__name__}: {exc}",
        )


def assert_equal(actual, expected):
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def assert_true(condition: bool, message: str):
    if not condition:
        raise AssertionError(message)


def assert_runtime_tools_fallback_without_rtk() -> None:
    old_path = os.environ.get("PATH", "")
    try:
        os.environ["PATH"] = ""
        read_result = runtime_tools.exec_read(FINALIZER)
        grep_result = runtime_tools.exec_grep("finalize_portfolio", PIPELINE)
    finally:
        os.environ["PATH"] = old_path
    assert_equal(read_result.exit_code, 0)
    assert_true("finalize_portfolio" in read_result.stdout, "native read fallback returned file content")
    assert_equal(grep_result.exit_code, 0)
    assert_true("finalize_portfolio" in grep_result.stdout, "native grep fallback returned matches")


def main() -> int:
    os.chdir(ROOT)
    ensure_fixture_repo()
    assert_runtime_tools_fallback_without_rtk()
    results = [run_case(case) for case in build_cases()]
    passed = sum(1 for r in results if r.passed)

    print("OSS Agent Runtime coding gauntlet")
    print("=" * 36)
    for result in results:
        mark = "PASS" if result.passed else "FAIL"
        suffix = f" status={result.status}" if result.status else ""
        print(f"{mark} {result.name}{suffix}")
        print(f"  expected: {result.expected_outcome}")
        if result.details:
            print(f"  detail: {result.details}")

    print(f"\nScore: {passed}/{len(results)}")
    if passed != len(results):
        return 1

    if os.getenv("GAUNTLET_LIVE_MODE") == "1":
        print("\nLive mode is intentionally not automatic in this deterministic suite.")
        print("Use bridge-level MissionV1 smoke tests with a supervised sidecar for provider burn-in.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
