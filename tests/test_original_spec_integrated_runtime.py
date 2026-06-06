#!/usr/bin/env python3
"""Original-spec integration proof for NativeOSSSubagentRuntime."""

from __future__ import annotations

import os
import sys
import json
import hashlib
import shutil
import tempfile

os.environ["ALLOW_MISSING_OPENCODE_KEY"] = "1"

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from codex_oss.bridge_protocol import validate_bridge_route
from codex_oss.implementation import apply_patch_in_isolated_worktree, build_patch_proposal_from_intent
from codex_oss.ledger import EvidenceLedger
from codex_oss.managed_bridge import RUNTIME_MODEL_ALIASES, run_managed_mission_from_body
from codex_oss.mission import _build_mission
from codex_oss.model_registry import ModelRegistry
from codex_oss.native_subagent import NativeOSSSubagentRuntime
from codex_oss.result_schema import REQUIRED_RESULT_FIELDS, markdown_projection
from codex_oss.runtime.loop import run_loop
from codex_oss.tool_turn_transaction import ToolTurnTransaction, recover_orphan_continuation


def test_native_oss_subagent_runtime_exposes_original_spec_interface():
    runtime = NativeOSSSubagentRuntime(ModelRegistry.from_runtime_aliases(RUNTIME_MODEL_ALIASES))

    view = runtime.delegate_oss_subagent(
        task="Inspect docs and report status",
        model="mission-a3-kimi",
        mode="scout",
        scope={"read_only_paths": ["docs"], "owned_paths": []},
        risk_tier="low",
        verification=["rtk ls docs"],
        review_policy={"required": True, "gpt_direct_units": 2, "oss_units": 0.5, "gpt_review_units": 0.5},
    )
    record = view.to_record()

    assert record["schema_version"] == "native_oss_subagent_run_view.v1"
    assert record["audit_projection"]["selected_model_alias"] == "mission-a3-kimi"
    assert record["audit_projection"]["resolved_model_upstream"] == "kimi-k2.6"
    assert record["audit_projection"]["provider_route"] == "opencode-go"
    assert record["audit_projection"]["phase_graph_hash"]
    assert record["result"]["runtime_status"] == "COMPLETE"
    assert record["review_packet"]["raw_transcript_included"] is False
    assert set(REQUIRED_RESULT_FIELDS).issubset(record["result"])
    assert "Status: COMPLETE" in markdown_projection(record["result"])


def test_native_oss_subagent_runtime_rejects_parent_provider_leakage():
    runtime = NativeOSSSubagentRuntime(ModelRegistry.from_runtime_aliases(RUNTIME_MODEL_ALIASES))

    try:
        runtime.delegate_oss_subagent(
            task="Do work",
            model="mission-a3-kimi",
            mode="scout",
            scope={"read_only_paths": ["docs"]},
            risk_tier="low",
            verification=[],
            review_policy={},
            parent_provider="opencode_bridge",
        )
    except ValueError as exc:
        assert "parent_provider_leak" in str(exc)
    else:
        raise AssertionError("parent provider leakage was admitted")


def test_tool_turn_transactions_terminalize_and_orphan_recovery_fails_closed():
    resolved = ToolTurnTransaction("call-1", "read").resolve("event-1").to_record()
    assert resolved["terminalized"] is True
    assert resolved["state"] == "resolved"

    recovered = recover_orphan_continuation("call-1", {"call-1"})
    assert recovered["state"] == "recovered"
    unknown = recover_orphan_continuation("call-2", {"call-1"})
    assert unknown["state"] == "failed_closed"
    assert unknown["terminalized"] is True


def test_bridge_protocol_rejects_gpt_family_child_route():
    route = validate_bridge_route(parent_provider="openai", child_model_alias="gpt-5.5", recursive_codex=False)
    assert route["ok"] is False
    assert "gpt_family_child_route" in route["failures"]


def test_runtime_loop_terminalizes_tool_turn_transaction_for_live_tool_call():
    fixture_path = "original_spec_tool_transaction_fixture.txt"
    with open(fixture_path, "w", encoding="utf-8") as handle:
        handle.write("native oss transaction proof\n")
    try:
        mission = _build_mission(
            {
                "schema_version": "oss_agent_mission.v1",
                "mission_id": "mission_original_spec_tool_turn",
                "tier": "A3",
                "mode": "managed_investigation",
                "objective": "Read one fixture and report.",
                "risk_tier": "low",
                "write_allowed": False,
                "allowed_roots": [],
                "allowed_paths": [fixture_path],
                "allowed_tool_classes": ["read"],
                "tool_budget": 3,
                "time_budget_seconds": 30,
                "stop_conditions": ["valid_report", "budget_exhausted", "deadline_reached"],
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
        )
        ledger = EvidenceLedger(mission_id=mission.mission_id, tool_budget_remaining=mission.tool_budget)
        calls = {"n": 0}

        def fake_model(messages, tools, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                payload = {
                    "action_type": "tool_call",
                    "tool_name": "rtk_read",
                    "arguments": {"path": fixture_path},
                    "reason": "Read the transaction fixture.",
                    "hypothesis": "The fixture contains the proof text.",
                    "expected_information_gain": "Confirm the fixture contents.",
                    "why_not_report_yet": "Need evidence before reporting.",
                }
            else:
                payload = {
                    "action_type": "final_report",
                    "report": {
                        "oss_report_version": "1.0",
                        "mission_id": mission.mission_id,
                        "status": "COMPLETE",
                        "confidence": "LOW",
                        "files_inspected": [{"path": fixture_path, "complete": True}],
                        "commands_run": [{"tool": "rtk_read", "args": {"path": fixture_path}}],
                        "findings": [
                            {
                                "claim": "The fixture contains native OSS transaction proof text.",
                                "evidence_refs": [f"file:{fixture_path}#extract:1", "command:0"],
                                "confidence": "LOW",
                            }
                        ],
                        "uncertainties": [],
                        "caveats": [],
                        "escalation_recommendation": "No escalation required",
                        "missing_fields": [],
                    },
                }
            return {"choices": [{"message": {"content": json.dumps(payload)}}]}

        result = run_loop(
            mission,
            ledger,
            fake_model,
            [],
            None,
            mission.allowed_roots,
            mission.allowed_paths,
            request_deadline=30,
        )
        assert result["status"] == "COMPLETE", result
        executed = [
            entry.tool_result_summary.get("tool_turn_transaction")
            for entry in ledger.action_trace
            if entry.runtime_decision in {"allowed", "runtime_prefetch"}
        ]
        assert executed, ledger.action_trace
        assert all(item and item["schema_version"] == "tool_turn_transaction.v1" for item in executed), executed
        assert all(item["terminalized"] is True for item in executed), executed
        assert all(item["state"] == "resolved" for item in executed), executed
        receipts = [
            entry.tool_result_summary.get("phase_receipt")
            for entry in ledger.action_trace
            if entry.runtime_decision in {"allowed", "runtime_prefetch"}
        ]
        assert receipts, ledger.action_trace
        assert all(item and item["schema_version"] == "phase_receipt.v1" for item in receipts), receipts
        assert all(item["phase"] in {"execute", "verify"} for item in receipts), receipts
    finally:
        try:
            os.unlink(fixture_path)
        except FileNotFoundError:
            pass


def test_managed_bridge_persists_runtime_authority_chain_in_mission_artifacts():
    mission_id = "mission_original_spec_authority_chain"
    artifact_dir = os.path.join(ROOT, ".codex-oss", "missions", mission_id)
    import shutil

    shutil.rmtree(artifact_dir, ignore_errors=True)
    body = {
        "input": [
            {
                "role": "user",
                "content": (
                    "<OSS_HANDOFF_JSON>\n"
                    + json.dumps(
                        {
                            "schema_version": "oss_agent_mission.v1",
                            "mission_id": mission_id,
                            "tier": "A3",
                            "mode": "managed_investigation",
                            "objective": "Produce a deterministic authority-chain proof.",
                            "risk_tier": "low",
                            "write_allowed": False,
                            "allowed_roots": [],
                            "allowed_paths": ["AGENTS.md"],
                            "allowed_tool_classes": ["read"],
                            "tool_budget": 2,
                            "time_budget_seconds": 30,
                            "stop_conditions": ["valid_report", "budget_exhausted", "deadline_reached"],
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
                    )
                    + "\n</OSS_HANDOFF_JSON>"
                ),
            }
        ]
    }

    def fake_payload(payload, timeout):
        report = {
            "action_type": "final_report",
            "report": {
                "oss_report_version": "1.0",
                "mission_id": mission_id,
                "status": "PARTIAL",
                "confidence": "LOW",
                "files_inspected": [],
                "commands_run": [],
                "findings": [],
                "uncertainties": [],
                "caveats": ["authority-chain artifact smoke"],
                "escalation_recommendation": "No escalation required",
                "missing_fields": [],
            },
        }
        return {"choices": [{"message": {"content": json.dumps(report)}}]}

    result = run_managed_mission_from_body(
        body,
        "mission-a3-kimi",
        lambda *args, **kwargs: None,
        fake_payload,
        lambda model: model,
        30,
    )
    assert result.handled is True
    mission_path = os.path.join(artifact_dir, "mission.json")
    scorecard_path = os.path.join(ROOT, ".codex-oss", "capability_scorecards.json")
    assert os.path.exists(mission_path), mission_path
    with open(mission_path, encoding="utf-8") as handle:
        mission_payload = json.load(handle)

    assert mission_payload["runtime_model_admission"]["requested_model_alias"] == "mission-a3-kimi"
    assert mission_payload["runtime_model_admission"]["resolved_model_alias"] == "ocg-kimi-k2.6"
    assert mission_payload["runtime_model_admission"]["resolved_upstream_model"] == "kimi-k2.6"
    assert mission_payload["runtime_model_admission"]["provider_route"] == "opencode-go"
    assert mission_payload["phase_graph"]["schema_version"] == "phase_graph.v1"
    assert mission_payload["phase_graph"]["graph_hash"]
    assert mission_payload["resume_cursor"]["schema_version"] == "resume_cursor.v1"
    assert mission_payload["resume_cursor"]["next_phase"] == "prep"
    assert mission_payload["sandbox_backend"]["schema_version"] == "sandbox_backend.v1"
    assert mission_payload["sandbox_backend"]["promotion_blocked"] is False
    assert mission_payload["implementation_escrow"]["schema_version"] == "implementation_escrow.v1"
    assert mission_payload["implementation_escrow"]["writes_runtime_governed"] is True
    native_packet = mission_payload["native_authority_packet"]
    assert native_packet["schema_version"] == "native_authority_packet.v1"
    assert native_packet["ok"] is True
    assert native_packet["data_exposure"]["schema_version"] == "data_exposure_policy.v1"
    assert native_packet["data_exposure"]["redactions_applied"] is False
    assert native_packet["scheduler_decision"]["schema_version"] == "scheduler_policy.v1"
    assert native_packet["scheduler_decision"]["admitted"] is True
    assert native_packet["run_view_authority"]["schema_version"] == "native_run_view_authority_map.v1"
    assert native_packet["run_view_authority"]["projection_role"] == "authority_preserving"
    assert native_packet["source_discriminator"]["schema_version"] == "source_discriminator.v1"
    assert native_packet["source_discriminator"]["inference_from_field_shape_allowed"] is False
    assert native_packet["wiring_probe"]["schema_version"] == "wiring_probe.v1"
    assert native_packet["wiring_probe"]["ran_before_first_model_call"] is True
    assert native_packet["override_action"]["schema_version"] == "override_action.v1"
    assert native_packet["worker_fanout"]["schema_version"] == "worker_fanout.v1"
    assert native_packet["profile_bakeoff"]["schema_version"] == "profile_bakeoff.v1"
    assert len(native_packet["phase_profiles"]) == 6
    assert native_packet["verification_adequacy"]["trusted_authority"] is True
    assert native_packet["repair_convergence"]["reason"] == "continue"
    assert mission_payload["routing_decision"]["schema_version"] == "capability_route_decision.v1"
    assert mission_payload["routing_decision"]["decision"] in {"canary", "promoted", "downgraded", "blocked"}
    assert mission_payload["usage_displacement"]["schema_version"] == "gpt55_usage_displacement.v1"
    assert mission_payload["review_economics"]["schema_version"] == "review_economics.v1"
    assert mission_payload["review_economics"]["review_economics_failed"] is False
    assert os.path.exists(scorecard_path), scorecard_path
    with open(scorecard_path, encoding="utf-8") as handle:
        scorecards = json.load(handle)
    key = "mission-a3-kimi:scout"
    assert key in scorecards, scorecards
    assert scorecards[key]["samples"] >= 1


def test_managed_bridge_blocks_implementation_when_sandbox_backend_is_insufficient():
    mission_id = "mission_original_spec_sandbox_block"
    body = {
        "input": [
            {
                "role": "user",
                "content": (
                    "<OSS_HANDOFF_JSON>\n"
                    + json.dumps(
                        {
                            "schema_version": "oss_agent_mission.v1",
                            "mission_id": mission_id,
                            "tier": "A5",
                            "mode": "bounded_implementation",
                            "objective": "Attempt a bounded implementation that requires sandbox enforcement.",
                            "risk_tier": "medium",
                            "write_allowed": True,
                            "allowed_roots": [],
                            "allowed_paths": ["AGENTS.md"],
                            "owned_paths": ["AGENTS.md"],
                            "read_only_paths": ["AGENTS.md"],
                            "allowed_tool_classes": ["read", "search", "safe_git"],
                            "tool_budget": 2,
                            "time_budget_seconds": 30,
                            "stop_conditions": ["valid_patch", "deadline_reached"],
                            "required_outputs": ["patch", "changed_files", "risk_assessment", "verification_plan", "evidence_refs"],
                            "apply_mode": "isolated_worktree",
                        }
                    )
                    + "\n</OSS_HANDOFF_JSON>"
                ),
            }
        ]
    }
    calls = {"count": 0}

    def should_not_call_model(payload, timeout):
        calls["count"] += 1
        raise AssertionError("sandbox-blocked implementation must not call the model")

    result = run_managed_mission_from_body(
        body,
        "mission-a5-kimi",
        lambda *args, **kwargs: None,
        should_not_call_model,
        lambda model: model,
        30,
    )
    assert result.handled is True
    assert result.status == "FAILED", result
    assert calls["count"] == 0
    assert "sandbox_backend" in result.report_text


def test_isolated_implementation_records_escrow_and_promotion_transaction():
    root = tempfile.mkdtemp(prefix="original_spec_impl_")
    try:
        target = os.path.join(root, "tests/test_config.py")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        original = "def test_existing():\n    assert True\n"
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(original)
        mission = _build_mission(
            {
                "schema_version": "oss_agent_mission.v1",
                "mission_id": "mission_original_spec_impl_escrow",
                "tier": "A5",
                "mode": "bounded_implementation",
                "objective": "Apply a test-only patch in isolation.",
                "risk_tier": "low",
                "write_allowed": True,
                "allowed_roots": [],
                "allowed_paths": ["tests/test_config.py"],
                "owned_paths": ["tests/test_config.py"],
                "read_only_paths": ["tests/test_config.py"],
                "allowed_tool_classes": ["read", "search", "safe_git"],
                "tool_budget": 4,
                "time_budget_seconds": 60,
                "stop_conditions": ["valid_patch", "deadline_reached"],
                "required_outputs": ["patch", "changed_files", "risk_assessment", "verification_plan", "evidence_refs"],
                "apply_mode": "isolated_worktree",
                "verification_policy": {
                    "allowed_commands": [["python3", "-m", "py_compile", "tests/test_config.py"]],
                    "max_commands": 1,
                    "timeout_seconds": 20,
                },
            }
        )
        intent = {
            "patch_intent_version": "1.0",
            "summary": "Add an isolated implementation proof test.",
            "edits": [
                {
                    "operation": "insert_after",
                    "path": "tests/test_config.py",
                    "anchor": "def test_existing():\n    assert True",
                    "content": "\n\ndef test_original_spec_impl_escrow():\n    assert True\n",
                    "reason": "Prove isolated implementation is governed by escrow.",
                }
            ],
            "risk_assessment": {"risk_tier": "low", "critical_paths_touched": False, "blast_radius": "test-only"},
            "verification_plan": [{"command": ["python3", "-m", "py_compile", "tests/test_config.py"], "reason": "Compile test file."}],
            "evidence_refs": ["file:tests/test_config.py#extract:1"],
            "caveats": [],
        }
        proposal = build_patch_proposal_from_intent(intent, mission, root)
        report = apply_patch_in_isolated_worktree(proposal, mission, root)

        assert report["status"] == "VERIFIED", report
        assert report["main_workspace_mutated"] is False, report
        assert report["implementation_escrow"]["schema_version"] == "implementation_escrow.v1"
        assert report["implementation_escrow"]["writes_runtime_governed"] is True
        assert report["promotion_transaction"]["schema_version"] == "promotion_transaction.v1"
        assert report["promotion_transaction"]["status"] == "rejected"
        assert report["promotion_transaction"]["accepted"] is False
        with open(target, encoding="utf-8") as handle:
            assert hashlib.sha256(handle.read().encode()).hexdigest() == hashlib.sha256(original.encode()).hexdigest()
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("test_original_spec_integrated_runtime: ok")
