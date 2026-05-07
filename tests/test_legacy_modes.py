#!/usr/bin/env python3
"""Contract tests for legacy continuation mode helpers."""

from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from codex_oss.legacy_modes import LegacyModeDecision, handle_bounded_patch_continuation, handle_bounded_write_exact


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


def main():
    assert_legacy_mode_decision_defaults_are_initialized()
    assert_exact_write_creates_and_reads_back()
    assert_patch_shell_verification_returns_observed_report()
    assert_patch_write_can_continue_for_verification()
    print("PASS: legacy mode helper suite")


if __name__ == "__main__":
    main()
