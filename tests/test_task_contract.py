#!/usr/bin/env python3
"""Regression tests for OSS TaskEnvelopeV1 and ReportContractValidatorV1."""

from codex_oss.task_contract import (
    classify_report_output,
    command_aware_context_plan,
    compile_task_envelope_v1,
    is_intent_or_status,
    select_execution_mode_v1,
    validate_report_contract,
)


def assert_task_envelope_selects_context_pack_for_explicit_reads():
    raw = {
        "agent": "oss_kimi_rapid",
        "task_type": "scout",
        "read_only_paths": [".codex/napkin.md", "ORCHESTRATION.md"],
        "deliverable_fields": ["files inspected", "confidence", "caveat"],
    }
    envelope = compile_task_envelope_v1(raw)
    assert envelope["schema_version"] == "task_envelope.v1"
    assert envelope["write_allowed"] is False
    assert envelope["forbidden_actions"] == ["edit", "create", "delete", "stage", "commit"]
    assert select_execution_mode_v1(envelope) == "context_pack_report"


def assert_task_envelope_escalates_proof_critical_work():
    envelope = compile_task_envelope_v1(
        {"task_type": "implementation", "read_only_paths": ["bridge.py"]},
        handoff_text="This touches proof-critical recovery and finalizer paths.",
    )
    assert envelope["proof_critical"] is True
    assert select_execution_mode_v1(envelope) == "escalate"


def assert_command_aware_context_plan_uses_grep_not_full_file():
    envelope = compile_task_envelope_v1(
        {
            "task_type": "context_pack_report",
            "read_only_paths": [".codex/bin/bridge.py", "ORCHESTRATION.md"],
            "required_commands": [
                'rtk grep "bridge_version|v11" .codex/bin/bridge.py',
                "rtk read ORCHESTRATION.md",
            ],
        }
    )
    plan = command_aware_context_plan(envelope)
    bridge_ops = [op for op in plan if op.get("target") == ".codex/bin/bridge.py"]
    assert bridge_ops == [
        {
            "kind": "grep",
            "command": 'rtk grep "bridge_version|v11" .codex/bin/bridge.py',
            "target": ".codex/bin/bridge.py",
        }
    ]


def assert_progress_text_is_not_terminal_report():
    text = "I'm running the first verification step now."
    assert is_intent_or_status(text) is True
    classification, missing = classify_report_output(
        text,
        "context_pack_report",
        {"read_only_paths": ["README.md"], "deliverable_fields": ["confidence", "caveat"]},
    )
    assert classification == "STATUS_OR_INTENT"
    assert "intent_or_status_detected" in missing


def assert_context_pack_report_requires_runtime_fields():
    envelope = compile_task_envelope_v1(
        {
            "read_only_paths": [".codex/napkin.md", "docs/CONTINUITY.md"],
            "required_commands": ["rtk read .codex/napkin.md", "rtk read docs/CONTINUITY.md"],
            "deliverable_fields": ["files inspected", "commands used", "confidence", "caveat"],
        }
    )
    invalid = (
        "PASS\n"
        "Summary: I inspected the requested docs.\n"
        "Confidence: HIGH\n"
        "Caveat: bounded to requested sources.\n"
    )
    ok, missing = validate_report_contract(invalid, "context_pack_report", envelope)
    assert ok is False
    assert "files_inspected" in missing
    assert "commands_used" in missing

    valid = (
        "PASS\n"
        "Files inspected: .codex/napkin.md; docs/CONTINUITY.md\n"
        "Commands used: rtk read .codex/napkin.md; rtk read docs/CONTINUITY.md\n"
        "Findings: both required documents were available for this bounded read.\n"
        "Confidence: HIGH\n"
        "Caveat: only the declared read-only evidence floor was inspected.\n"
    )
    ok, missing = validate_report_contract(valid, "context_pack_report", envelope)
    assert ok is True, missing


def assert_verification_claim_requires_observed_verification():
    envelope = compile_task_envelope_v1(
        {
            "owned_paths": ["scratch.txt"],
            "write_allowed": True,
            "verification_steps": ["run tests"],
            "deliverable_fields": ["file changed", "verification command", "confidence"],
        }
    )
    text = (
        "PASS\n"
        "File changed: scratch.txt\n"
        "Verification command: tests passed\n"
        "Observed content: ok\n"
        "Confidence: HIGH\n"
    )
    ok, missing = validate_report_contract(
        text,
        "bounded_write_exact",
        envelope,
        verification_observed=False,
    )
    assert ok is False
    assert "verification_not_observed" in missing


def main():
    tests = [
        assert_task_envelope_selects_context_pack_for_explicit_reads,
        assert_task_envelope_escalates_proof_critical_work,
        assert_command_aware_context_plan_uses_grep_not_full_file,
        assert_progress_text_is_not_terminal_report,
        assert_context_pack_report_requires_runtime_fields,
        assert_verification_claim_requires_observed_verification,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"Results: {len(tests) - failed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
