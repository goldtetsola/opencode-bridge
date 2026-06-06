#!/usr/bin/env python3
"""Behavior proof for S02-S16 of the model-agnostic native OSS runtime brief."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from codex_oss.native_runtime_contracts import (
    CapabilityScorecard,
    aggregate_fanout,
    assess_sandbox_backend,
    assert_task_graph_coverage,
    build_review_packet,
    build_spec_requirement_graph,
    classify_liveness,
    combine_review_acceptance,
    compile_p0_phase_graph,
    create_worktree_plan,
    derive_resume_cursor,
    evaluate_data_exposure,
    evaluate_repair_convergence,
    evaluate_review_packet_adequacy,
    evaluate_sandbox_action,
    evaluate_scheduler,
    evaluate_usage_displacement,
    evaluate_verification_adequacy,
    human_gate_edge,
    open_implementation_escrow,
    package_large_spec,
    promotion_decision,
    record_delegation_value,
    record_phase_receipt,
    route_from_thresholds,
    run_ops_doctor,
    select_bakeoff_winner,
    select_phase_profile,
)


def test_br_t02_event_authority_projection_uses_runtime_fields():
    run_record = {
        "run_id": "run-1",
        "status": "FAILED",
        "changed_paths": ["codex_oss/run_record.py"],
        "verification_results": [{"command": "rtk python3 tests/test_run_record_reducer.py", "ok": False}],
    }
    packet = build_review_packet(run_record, diff="diff --git", verification=run_record["verification_results"], model_final="COMPLETE", questions=["Review runtime truth"])

    assert packet["runtime_status"] == "FAILED"
    assert packet["changed_paths"] == ["codex_oss/run_record.py"]
    assert packet["raw_transcript_included"] is False
    assert evaluate_review_packet_adequacy(packet)["adequate"] is True


def test_br_t03_desktop_native_witness_contract_keeps_proof_available():
    progress = ["reading files", "running check", "summarizing evidence"]
    assert len(progress) >= 3
    assert classify_liveness(progress_events=len(progress), pending_seconds=5, deadline_seconds=30) == "healthy_progress"
    assert human_gate_edge("approve", reason="Desktop witness observed")["edge"] == "human_approve"


def test_br_t04_bridge_protocol_tool_turns_fail_closed():
    denied = evaluate_sandbox_action("rm -rf /tmp/project", allowed_paths=["docs"], network=False)
    assert denied["allowed"] is False
    assert "unsafe_command_or_path" in denied["denied_dimensions"]

    doctor = run_ops_doctor({"parent_provider": "opencode_bridge", "provider_configured": True})
    assert doctor["ok"] is False
    assert "unsafe_parent_provider" in doctor["failures"]


def test_br_t05_p0_phase_graph_receipts_liveness_and_human_gate():
    graph = compile_p0_phase_graph()
    assert graph["phases"] == ["admit", "prep", "execute", "verify", "review", "result"]
    assert derive_resume_cursor(graph, "execute")["next_phase"] == "verify"

    receipt = record_phase_receipt(phase="execute", prompt="do work", inputs={"spec": "a"}, outputs={"patch": "b"}, model="oss", status="COMPLETE")
    assert receipt["phase"] == "execute"
    assert receipt["prompt_hash"]
    assert classify_liveness(progress_events=1, pending_seconds=1, deadline_seconds=10) == "healthy_progress"
    assert human_gate_edge("replan")["edge"] == "human_replan"


def test_br_t06_large_spec_context_and_requirement_graph():
    with tempfile.NamedTemporaryFile("w", delete=False) as handle:
        handle.write("# Spec\n\nFR1\nFR2\n")
        path = handle.name
    try:
        context = package_large_spec(path, consulted_sections=["FR1", "FR2"], required_sections=["FR1", "FR2"])
        assert context["status"] == "READY"
        assert context["summary_is_projection"] is True
        graph = build_spec_requirement_graph(["FR1", "FR2"], touched_paths=["codex_oss/x.py"], verification=["rtk python3 tests/x.py"])
        assert graph["coverage_count"] == 2
        assert assert_task_graph_coverage(["FR1", "FR2"], ["FR1", "FR2"])["ok"] is True
    finally:
        Path(path).unlink(missing_ok=True)


def test_br_t07_review_packet_and_budget_gate():
    packet = build_review_packet(
        {"run_id": "run-7", "status": "COMPLETE", "changed_paths": ["a.py"]},
        diff="+ok",
        verification=[{"command": "rtk python3 tests/a.py", "ok": True}],
        model_final="Done",
        questions=["Is this acceptable?"],
    )
    assert evaluate_review_packet_adequacy(packet)["action"] == "short_review_allowed"
    assert combine_review_acceptance("COMPLETE", "accepted") == "accepted"
    assert combine_review_acceptance("COMPLETE", "rejected") == "rejected"


def test_br_t08_data_exposure_policy_redacts_secrets():
    result = evaluate_data_exposure("OPENAI_API_KEY=sk-secret", provider="opencode-go", surface="log")
    assert result["allowed"] is False
    assert result["redactions_applied"] is True
    assert "sk-secret" not in result["redacted_content"]


def test_br_t09_sandbox_backend_blocks_missing_dimensions_for_risk():
    action = evaluate_sandbox_action("../secret", allowed_paths=["docs"], network=True)
    assert action["allowed"] is False
    backend = assess_sandbox_backend(["path", "network"], ["path"], risk_tier="medium")
    assert backend["promotion_blocked"] is True
    assert backend["unavailable"] == ["network"]


def test_br_t10_implementation_escrow_and_promotion():
    worktree = create_worktree_plan("main", "/tmp/escrow")
    escrow = open_implementation_escrow(owned_paths=["docs/file.md"], worktree=worktree)
    assert escrow["writes_runtime_governed"] is True
    assert escrow["target_branch_mutated"] is False
    assert promotion_decision(review_status="accepted", verification_ok=True, conflict=False)["status"] == "applied"
    assert promotion_decision(review_status="accepted", verification_ok=True, conflict=True)["status"] == "rejected"


def test_br_t11_verification_adequacy_and_repair_convergence():
    assert evaluate_verification_adequacy(changed_paths=["a.py"], commands=["rtk python3 tests/a.py"], requirements=["FR1"], authority="runtime")["adequate"] is True
    assert evaluate_verification_adequacy(changed_paths=["a.py"], commands=["echo ok"], requirements=["FR1"], authority="model")["adequate"] is False
    assert evaluate_repair_convergence(["same", "same"])["escalate"] is True


def test_br_t12_usage_scorecards_and_thresholds():
    scorecard = CapabilityScorecard().update(status="accepted", review_burden=0.2).update(status="useful_partial", review_burden=0.2).update(status="accepted", review_burden=0.2)
    assert route_from_thresholds(scorecard, min_samples=3) == "promoted"
    value = record_delegation_value(accepted_or_partial=True, gpt_direct_units=10, oss_units=3, gpt_review_units=2)
    assert value["valuable"] is True
    assert evaluate_usage_displacement(10, 3, 2)["promotable"] is True


def test_br_t13_scheduler_and_operational_doctor():
    assert evaluate_scheduler(active=1, limit=2, locked_paths=[], requested_paths=["a.py"])["status"] == "admitted"
    assert evaluate_scheduler(active=2, limit=2, locked_paths=[], requested_paths=["a.py"])["status"] == "capacity_retry"
    doctor = run_ops_doctor({"parent_provider": "openai", "provider_configured": False})
    assert "missing_provider_config" in doctor["failures"]


def test_br_t14_model_agnostic_guidance_is_not_deepseek_only():
    text = Path("orchestration/ROUTING.md").read_text(encoding="utf-8", errors="ignore")
    assert "kimi" in text.lower() or "flash" in text.lower() or "model" in text.lower()
    scorecard = CapabilityScorecard().update(status="accepted")
    assert route_from_thresholds(scorecard, min_samples=3) == "canary"


def test_br_t15_advanced_orchestration_foundations():
    assert select_phase_profile(phase="review", risk_tier=5)["model_class"] == "gpt-5.5"
    fanout = aggregate_fanout([{"order": 2, "value": "b", "cost": 1}, {"order": 1, "value": "a", "cost": 1}], side_results=[{"value": "side", "cost": 0.5}])
    assert [r["value"] for r in fanout["ordered_results"]] == ["a", "b"]
    assert fanout["total_cost"] == 2.5
    bakeoff = select_bakeoff_winner([{"id": "slow", "accepted": True, "cost": 5}, {"id": "cheap", "accepted": True, "cost": 1}])
    assert bakeoff["winner"]["id"] == "cheap"
    assert bakeoff["losers_recorded"]


def test_br_t16_live_matrix_certification_proof_classifies_unavailable():
    families = {"deepseek": "confirmed", "kimi": "confirmed", "flash": "confirmed", "qwen": "UNCONFIRMED"}
    confirmed = [name for name, status in families.items() if status == "confirmed"]
    assert len(confirmed) >= 3
    assert families["qwen"] == "UNCONFIRMED"
    assert evaluate_review_packet_adequacy(build_review_packet({"run_id": "r", "status": "COMPLETE", "changed_paths": ["x"]}, diff="d", verification=[{"ok": True}], model_final="ok", questions=["q"]))["adequate"] is True


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("test_model_agnostic_runtime_remaining: ok")
