#!/usr/bin/env python3
"""QualityValidityV1 contract tests."""

from __future__ import annotations

import os
import sys

os.environ["ALLOW_MISSING_OPENCODE_KEY"] = "1"
ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from codex_oss.burnin.quality import classify_quality_validity, detect_suspicious_complete


def assert_earned_complete_with_observations():
    qv = classify_quality_validity(
        mission={"objective_style": "open_investigation", "answer_obligations": [{"question": "q"}]},
        report={"status": "COMPLETE", "closure_source": "model_report", "caveats": []},
        ledger={"commands_run": [{"tool": "rtk_read", "args": {"path": "test.py"}}]},
        answer_graph={"sufficiency": {"required_answered": 1, "required_total": 1, "can_close": True, "closure_entitlement": {"can_return_complete": True}}},
        decision_trace=None,
    )
    assert qv["grade"] == "earned_complete", qv
    assert qv["earned_complete"] is True, qv
    assert qv["suspicious_complete"] is False, qv


def assert_suspicious_complete_no_observations():
    qv = classify_quality_validity(
        mission={"objective_style": "open_investigation"},
        report={"status": "COMPLETE", "closure_source": "model_report", "caveats": []},
        ledger={},
        answer_graph={"sufficiency": {"required_answered": 0, "required_total": 0, "can_close": True, "closure_entitlement": {"can_return_complete": True}}},
        decision_trace=None,
    )
    assert qv["grade"] == "suspicious_complete", qv
    assert qv["suspicious_complete"] is True, qv


def assert_suspicious_complete_operator_asserted():
    qv = classify_quality_validity(
        mission={"objective_style": "open_investigation", "answer_obligations": [{"question": "q"}]},
        report={"status": "COMPLETE", "closure_source": "model_report", "caveats": ["Operator asserted evidence floor was covered"]},
        ledger={"commands_run": [{"tool": "rtk_read"}]},
        answer_graph={"sufficiency": {"required_answered": 1, "required_total": 1, "can_close": True, "closure_entitlement": {"can_return_complete": True}}},
        decision_trace=None,
    )
    assert qv["grade"] == "suspicious_complete", qv
    assert qv["operator_asserted_evidence_floor"] is True, qv


def assert_false_complete_detected():
    qv = classify_quality_validity(
        mission={},
        report={"status": "COMPLETE", "closure_source": "runtime_answer_graph", "caveats": []},
        ledger={"commands_run": [{"tool": "rtk_read"}]},
        answer_graph={"sufficiency": {"required_answered": 0, "required_total": 1, "missing_required_sources": ["missing.py"], "can_close": False, "closure_entitlement": {"can_return_complete": False}}},
        decision_trace=None,
    )
    assert qv["false_complete"] is True, qv


def assert_truthful_partial():
    qv = classify_quality_validity(
        mission={},
        report={"status": "PARTIAL", "closure_source": "runtime_answer_graph", "caveats": ["missing source"]},
        ledger={"commands_run": [{"tool": "rtk_read"}]},
        answer_graph={"sufficiency": {"required_answered": 0, "required_total": 1, "missing_required_sources": ["missing.py"]}},
        decision_trace=None,
    )
    assert qv["grade"] == "truthful_partial", qv


def assert_deterministic_complete():
    qv = classify_quality_validity(
        mission={},
        report={"status": "COMPLETE", "closure_source": "deterministic_fast_path", "caveats": []},
        ledger={},
        answer_graph={},
        decision_trace=None,
    )
    assert qv["grade"] == "deterministic_complete", qv
    assert qv["earned_complete"] is True, qv


def assert_quality_grade_not_confused():
    """Verify that suspicious + false flags don't get confused."""
    suspicious = detect_suspicious_complete(
        mission={"objective_style": "open_investigation"},
        report={"status": "COMPLETE", "caveats": ["Operator asserted evidence floor was covered, but this agent performed zero file reads prior to forced closure."]},
        ledger={},
    )
    assert suspicious["suspicious_complete"] is True, suspicious
    assert "no_ledger_observations" in suspicious["reasons"], suspicious
    assert "operator_asserted_evidence_floor" in suspicious["reasons"], suspicious


def main():
    assert_earned_complete_with_observations()
    assert_suspicious_complete_no_observations()
    assert_suspicious_complete_operator_asserted()
    assert_false_complete_detected()
    assert_truthful_partial()
    assert_deterministic_complete()
    assert_quality_grade_not_confused()
    print("PASS: quality validity suite")


if __name__ == "__main__":
    main()
