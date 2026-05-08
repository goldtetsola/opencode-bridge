#!/usr/bin/env python3
"""CLI tests for explicit MissionV1 runtime delegation."""

from __future__ import annotations

import http.server
import json
import os
import socketserver
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin" / "codex-oss"
AUTH = "sk-local-codex-bridge"
REQUESTS = []

sys.path.insert(0, str(ROOT))

from codex_oss.implementation import apply_patch_in_isolated_worktree
from codex_oss.mission import _build_mission


class FakeBridge(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        REQUESTS.append(body)
        text = (
            "OSS_REPORT_BEGIN\n"
            "Status: COMPLETE\n"
            "Confidence: LOW\n"
            "Findings:\n"
            "- CLI delegation smoke passed.\n"
            "OSS_REPORT_END"
        )
        data = json.dumps({
            "id": "resp_cli_test",
            "object": "response",
            "status": "completed",
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }],
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class ReusableTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def run_cmd(args, cwd=None, **kwargs):
    env = os.environ.copy()
    env.setdefault("LITELLM_MASTER_KEY", AUTH)
    return subprocess.run(
        [sys.executable, str(BIN)] + args,
        cwd=str(cwd or ROOT),
        env=env,
        text=True,
        capture_output=True,
        **kwargs,
    )


def assert_template_requires_scope():
    result = run_cmd(["mission", "template", "--objective", "Inspect code"])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "requires at least one --allowed-root or --allowed-path" in result.stderr


def assert_template_outputs_mission_v1():
    result = run_cmd([
        "mission", "template",
        "--mission-id", "mission_cli_template",
        "--objective", "Inspect README",
        "--allowed-path", "README.md",
        "--tool-budget", "4",
    ])
    assert result.returncode == 0, result.stdout + result.stderr
    mission = json.loads(result.stdout)
    assert mission["schema_version"] == "oss_agent_mission.v1"
    assert mission["mission_id"] == "mission_cli_template"
    assert mission["allowed_paths"] == ["README.md"]
    assert mission["write_allowed"] is False
    assert mission["mode"] == "managed_investigation"

    a2 = run_cmd([
        "mission", "template",
        "--mission-id", "mission_cli_template_a2",
        "--objective", "Inspect README",
        "--tier", "A2",
        "--allowed-path", "README.md",
    ])
    assert a2.returncode == 0, a2.stdout + a2.stderr
    a2_mission = json.loads(a2.stdout)
    assert a2_mission["mode"] == "guided_exploration", a2_mission


def assert_compile_outputs_strict_implementation_mission():
    result = run_cmd([
        "mission", "compile",
        "--mission-id", "mission_cli_compile",
        "--objective", "Add a regression test.",
        "--tier", "A5",
        "--risk-tier", "low",
        "--owned-path", "tests/test_config.py",
        "--read-only-path", "src/config.py",
        "--objective-type", "implementation_test_only",
        "--required-test-file", "tests/test_config.py",
        "--required-source-file", "src/config.py",
        "--required-test-name", "test_empty_value",
        "--verification-command", "python3 -m py_compile tests/test_config.py",
        "--apply-mode", "workspace_low_risk",
        "--workspace-allow-direct",
        "--workspace-require-clean",
    ])
    assert result.returncode == 0, result.stdout + result.stderr
    mission = json.loads(result.stdout)
    assert mission["tier"] == "A5"
    assert mission["write_allowed"] is True
    assert mission["apply_mode"] == "workspace_low_risk"
    assert mission["objective_spec"]["objective_type"] == "implementation_test_only"
    assert mission["workspace_apply_policy"]["allow_direct_workspace_apply"] is True
    assert mission["workspace_apply_policy"]["require_clean_worktree"] is True
    assert mission["verification_policy"]["allowed_commands"] == [["python3", "-m", "py_compile", "tests/test_config.py"]]


def assert_compile_outputs_critical_workspace_certified_mission():
    result = run_cmd([
        "mission", "compile",
        "--mission-id", "mission_cli_compile_critical",
        "--objective", "Apply a certified critical fixture patch.",
        "--tier", "A6",
        "--risk-tier", "critical",
        "--owned-path", "src/auth/login.py",
        "--read-only-path", "src/auth/login.py",
        "--objective-type", "critical_path_patch",
        "--required-changed-file", "src/auth/login.py",
        "--verification-command", "python3 -m py_compile src/auth/login.py",
        "--apply-mode", "critical_workspace_certified",
        "--workspace-allow-critical",
        "--workspace-certification-required",
        "--workspace-require-gpt-review",
        "--workspace-reviewer-model", "mission-a6-kimi",
        "--workspace-min-reviewer-approvals", "1",
        "--workspace-require-isolated-preflight",
        "--workspace-require-rollback-proof",
        "--workspace-invariant-command", "python3 -c pass",
        "--critical-path-write-allowed",
        "--critical-path-read-allowed",
        "--critical-path-reason", "critical certification CLI test",
    ])
    assert result.returncode == 0, result.stdout + result.stderr
    mission = json.loads(result.stdout)
    assert mission["tier"] == "A6"
    assert mission["apply_mode"] == "critical_workspace_certified"
    assert mission["workspace_apply_policy"]["certification_required"] is True
    assert mission["workspace_apply_policy"]["require_gpt_review"] is True
    assert mission["workspace_apply_policy"]["reviewer_models"] == ["mission-a6-kimi"]
    assert mission["workspace_apply_policy"]["min_reviewer_approvals"] == 1
    assert mission["workspace_apply_policy"]["require_isolated_preflight"] is True
    assert mission["workspace_apply_policy"]["require_rollback_proof"] is True
    assert mission["workspace_apply_policy"]["invariant_commands"] == [["python3", "-c", "pass"]]
    assert mission["critical_path_write_allowed"] is True


def assert_run_posts_mission_to_runtime_bridge():
    REQUESTS.clear()
    server = ReusableTCPServer(("127.0.0.1", 4017), FakeBridge)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        mission = {
            "schema_version": "oss_agent_mission.v1",
            "mission_id": "mission_cli_run",
            "tier": "A3",
            "mode": "managed_investigation",
            "objective": "CLI smoke",
            "risk_tier": "low",
            "write_allowed": False,
            "allowed_roots": [],
            "allowed_paths": ["README.md"],
            "tool_budget": 2,
            "time_budget_seconds": 30,
            "allowed_tool_classes": ["read"],
            "stop_conditions": ["valid_report", "deadline_reached"],
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
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(mission, handle)
            mission_path = handle.name
        try:
            result = run_cmd([
                "mission", "run", mission_path,
                "--model", "mission-a3-kimi",
                "--port", "4017",
            ])
        finally:
            os.unlink(mission_path)
    finally:
        server.shutdown()
        server.server_close()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "OSS_REPORT_BEGIN" in result.stdout
    assert REQUESTS, "fake bridge did not receive mission request"
    request = REQUESTS[-1]
    assert request["model"] == "mission-a3-kimi", request
    assert request["stream"] is False, request
    content = request["input"][0]["content"]
    assert "<OSS_HANDOFF_JSON>" in content, content
    assert "mission_cli_run" in content, content


def assert_audit_mission_reports_runtime_artifacts():
    with tempfile.TemporaryDirectory(prefix="oss_cli_audit_") as root:
        tests_dir = Path(root) / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        target = tests_dir / "test_config.py"
        original = "def test_existing():\n    assert True\n"
        target.write_text(original, encoding="utf-8")
        mission = _build_mission({
            "schema_version": "oss_agent_mission.v1",
            "mission_id": "mission_cli_audit",
            "tier": "A5",
            "mode": "bounded_implementation",
            "objective": "Add a test-only patch.",
            "risk_tier": "low",
            "write_allowed": True,
            "allowed_roots": [],
            "allowed_paths": ["tests/test_config.py"],
            "owned_paths": ["tests/test_config.py"],
            "read_only_paths": ["tests/test_config.py"],
            "forbidden_roots": [".env", ".git", ".codex-oss/"],
            "allowed_tool_classes": ["read", "search"],
            "tool_budget": 4,
            "time_budget_seconds": 60,
            "stop_conditions": ["valid_patch", "deadline_reached"],
            "report_schema": "implementation_report.v1",
            "required_outputs": ["patch", "changed_files", "risk_assessment", "verification_plan", "evidence_refs"],
            "max_files_changed": 1,
            "max_patch_bytes": 12000,
            "apply_mode": "isolated_worktree",
            "verification_policy": {
                "allowed_commands": [["python3", "-m", "py_compile", "tests/test_config.py"]],
                "max_commands": 1,
                "timeout_seconds": 20,
            },
            "objective_spec": {
                "schema_version": "objective_spec.v1",
                "objective_type": "implementation_test_only",
                "target": {
                    "test_file": "tests/test_config.py",
                    "source_files": [],
                    "required_test_names": ["test_empty_value"],
                },
                "required_outputs": ["changed_test_file"],
                "required_evidence_shapes": ["test_definition"],
                "completion_criteria": ["required_test_present"],
            },
        })
        diff = (
            "diff --git a/tests/test_config.py b/tests/test_config.py\n"
            "--- a/tests/test_config.py\n"
            "+++ b/tests/test_config.py\n"
            "@@ -1,2 +1,5 @@\n"
            " def test_existing():\n"
            "     assert True\n"
            "+\n"
            "+def test_empty_value():\n"
            "+    assert True\n"
        )
        proposal = {
            "patch_proposal_version": "1.0",
            "status": "PROPOSED",
            "summary": "Add audit test patch.",
            "base": {"git_head": "fixture", "dirty_worktree_allowed": False},
            "changed_files": [{
                "path": "tests/test_config.py",
                "change_type": "modify",
                "reason": "Audit mission fixture.",
                "base_sha256": __import__("hashlib").sha256(original.encode()).hexdigest(),
            }],
            "unified_diff": diff,
            "risk_assessment": {"risk_tier": "low", "critical_paths_touched": False, "blast_radius": "test-only"},
            "verification_plan": [{"command": ["python3", "-m", "py_compile", "tests/test_config.py"], "reason": "compile"}],
            "evidence_refs": ["file:tests/test_config.py#extract:test_existing"],
            "caveats": [],
        }
        report = apply_patch_in_isolated_worktree(proposal, mission, root)
        assert report["status"] == "VERIFIED", report
        audited = run_cmd(["audit-mission", "mission_cli_audit", "--json"], cwd=root)
        assert audited.returncode == 0, audited.stdout + audited.stderr
        payload = json.loads(audited.stdout)
        assert payload["ok"] is True, payload
        names = {item["name"] for item in payload["checks"]}
        assert "mission_json" in names
        assert "patch_artifact" in names
        assert "rollback_artifact" in names
        assert "ledger_json" in names
        assert "trace_jsonl" in names
        assert "summary_md" in names


def assert_mission_metrics_summarize_runtime_evidence():
    with tempfile.TemporaryDirectory(prefix="oss_cli_metrics_") as root:
        tests_dir = Path(root) / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        target = tests_dir / "test_config.py"
        original = "def test_existing():\n    assert True\n"
        target.write_text(original, encoding="utf-8")

        # Create an implementation mission artifact bundle through the real runtime path.
        mission = _build_mission({
            "schema_version": "oss_agent_mission.v1",
            "mission_id": "mission_cli_metrics_impl",
            "tier": "A5",
            "mode": "bounded_implementation",
            "objective": "Add a metrics test-only patch.",
            "risk_tier": "low",
            "write_allowed": True,
            "allowed_roots": [],
            "allowed_paths": ["tests/test_config.py"],
            "owned_paths": ["tests/test_config.py"],
            "read_only_paths": ["tests/test_config.py"],
            "forbidden_roots": [".env", ".git", ".codex-oss/"],
            "allowed_tool_classes": ["read", "search"],
            "tool_budget": 4,
            "time_budget_seconds": 60,
            "stop_conditions": ["valid_patch", "deadline_reached"],
            "report_schema": "implementation_report.v1",
            "required_outputs": ["patch", "changed_files", "risk_assessment", "verification_plan", "evidence_refs"],
            "max_files_changed": 1,
            "max_patch_bytes": 12000,
            "apply_mode": "isolated_worktree",
            "verification_policy": {
                "allowed_commands": [["python3", "-m", "py_compile", "tests/test_config.py"]],
                "max_commands": 1,
                "timeout_seconds": 20,
            },
            "objective_spec": {
                "schema_version": "objective_spec.v1",
                "objective_type": "implementation_test_only",
                "target": {
                    "test_file": "tests/test_config.py",
                    "source_files": [],
                    "required_test_names": ["test_metrics_value"],
                },
                "required_outputs": ["changed_test_file"],
                "required_evidence_shapes": ["test_definition"],
                "completion_criteria": ["required_test_present"],
            },
        })
        diff = (
            "diff --git a/tests/test_config.py b/tests/test_config.py\n"
            "--- a/tests/test_config.py\n"
            "+++ b/tests/test_config.py\n"
            "@@ -1,2 +1,5 @@\n"
            " def test_existing():\n"
            "     assert True\n"
            "+\n"
            "+def test_metrics_value():\n"
            "+    assert True\n"
        )
        proposal = {
            "patch_proposal_version": "1.0",
            "status": "PROPOSED",
            "summary": "Add metrics test patch.",
            "base": {"git_head": "fixture", "dirty_worktree_allowed": False},
            "changed_files": [{
                "path": "tests/test_config.py",
                "change_type": "modify",
                "reason": "Metrics mission fixture.",
                "base_sha256": __import__("hashlib").sha256(original.encode()).hexdigest(),
            }],
            "unified_diff": diff,
            "risk_assessment": {"risk_tier": "low", "critical_paths_touched": False, "blast_radius": "test-only"},
            "verification_plan": [{"command": ["python3", "-m", "py_compile", "tests/test_config.py"], "reason": "compile"}],
            "evidence_refs": ["file:tests/test_config.py#extract:test_existing"],
            "caveats": [],
        }
        report = apply_patch_in_isolated_worktree(proposal, mission, root)
        assert report["status"] == "VERIFIED", report

        # Add one minimal read-only mission bundle so the summary has both lanes.
        readonly_dir = Path(root) / ".codex-oss" / "missions" / "mission_cli_metrics_readonly"
        readonly_dir.mkdir(parents=True, exist_ok=True)
        (readonly_dir / "mission.json").write_text(json.dumps({
            "mission_id": "mission_cli_metrics_readonly",
            "tier": "A3",
            "mode": "managed_investigation",
            "objective": "Inspect a file",
            "apply_mode": "",
        }, indent=2), encoding="utf-8")
        (readonly_dir / "ledger.json").write_text(json.dumps({"evidence": ["command:0"]}, indent=2), encoding="utf-8")
        (readonly_dir / "report.json").write_text(json.dumps({
            "status": "COMPLETE",
            "report_source": "runtime_finalizer",
            "files_inspected": ["tests/test_config.py"],
        }, indent=2), encoding="utf-8")
        (readonly_dir / "trace_grading.json").write_text(json.dumps({
            "trace_grading_version": "1.0",
            "labels": ["productive_exploration"],
            "reasons": ["Fixture investigation completed."],
        }, indent=2), encoding="utf-8")
        (readonly_dir / "trace.jsonl").write_text("{}\n", encoding="utf-8")
        (readonly_dir / "summary.md").write_text("# Read-only Summary\n", encoding="utf-8")

        result = run_cmd(["mission-metrics", "--project", root, "--json"], cwd=root)
        assert result.returncode == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload["total_missions"] == 2, payload
        assert payload["eligible_missions"] == 2, payload
        assert payload["legacy_or_incomplete_missions"] == 0, payload
        assert payload["implementation"]["count"] == 1, payload
        assert payload["read_only"]["count"] == 1, payload
        assert payload["promotion_evidence"]["runtime_backed_investigation"]["status"] == "SUPPORTED", payload
        assert payload["promotion_evidence"]["bounded_implementation"]["status"] == "SUPPORTED", payload
        assert payload["promotion_evidence"]["workspace_apply_evidence"]["status"] == "UNCONFIRMED", payload


def assert_refresh_proofs_generates_supported_claim_surface():
    with tempfile.TemporaryDirectory(prefix="oss_cli_proofs_") as root:
        result = run_cmd(["refresh-proofs", "--project", root, "--suite", "proof", "--json"], cwd=root)
        assert result.returncode == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload["suite"] == "proof", payload
        assert set(payload["generated_missions"]) == {
            "proof_a3_readonly",
            "proof_a4_proposal",
            "proof_a5_workspace",
            "proof_a6_certified",
        }, payload

        metrics = run_cmd(["mission-metrics", "--project", root, "--proof-only", "--json"], cwd=root)
        assert metrics.returncode == 0, metrics.stdout + metrics.stderr
        summary = json.loads(metrics.stdout)
        assert summary["eligible_missions"] == 4, summary
        assert summary["legacy_or_incomplete_missions"] == 0, summary
        assert summary["promotion_evidence"]["runtime_backed_investigation"]["status"] == "SUPPORTED", summary
        assert summary["promotion_evidence"]["bounded_implementation"]["status"] == "SUPPORTED", summary
        assert summary["promotion_evidence"]["workspace_apply_evidence"]["status"] == "SUPPORTED", summary
        assert summary["promotion_evidence"]["critical_certification_evidence"]["status"] == "SUPPORTED", summary

        claim = run_cmd(["claim-status", "--project", root, "--proof-only", "--json"], cwd=root)
        assert claim.returncode == 0, claim.stdout + claim.stderr
        claim_payload = json.loads(claim.stdout)
        assert claim_payload["claim_status"]["all_supported"] is True, claim_payload
        assert "runtime_backed_investigation" in claim_payload["claim_status"]["supported_claims"], claim_payload

        burnin = run_cmd(["burnin", "--project", root, "--suite", "proof", "--json"], cwd=root)
        assert burnin.returncode == 0, burnin.stdout + burnin.stderr
        burnin_payload = json.loads(burnin.stdout)
        assert burnin_payload["suite"] == "proof", burnin_payload
        assert burnin_payload["claim_status"]["all_supported"] is True, burnin_payload
        assert set(burnin_payload["generated_missions"]) == set(payload["generated_missions"]), burnin_payload


def assert_operational_burnin_generates_supported_claim_surface():
    with tempfile.TemporaryDirectory(prefix="oss_cli_operational_") as root:
        result = run_cmd(["refresh-proofs", "--project", root, "--suite", "operational", "--json"], cwd=root)
        assert result.returncode == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload["suite"] == "operational", payload
        assert set(payload["generated_missions"]) == {
            "operational_a3_readme",
            "operational_a3_impl_site",
            "operational_a4_docs_patch",
            "operational_a5_workspace_docs",
            "operational_a5_multifile_runtime",
            "operational_a6_certified_runtime",
        }, payload

        metrics = run_cmd(["mission-metrics", "--project", root, "--operational-only", "--json"], cwd=root)
        assert metrics.returncode == 0, metrics.stdout + metrics.stderr
        summary = json.loads(metrics.stdout)
        assert summary["eligible_missions"] == 6, summary
        assert summary["legacy_or_incomplete_missions"] == 0, summary
        assert summary["promotion_evidence"]["runtime_backed_investigation"]["status"] == "SUPPORTED", summary
        assert summary["promotion_evidence"]["bounded_implementation"]["status"] == "SUPPORTED", summary
        assert summary["promotion_evidence"]["workspace_apply_evidence"]["status"] == "SUPPORTED", summary
        assert summary["promotion_evidence"]["critical_certification_evidence"]["status"] == "SUPPORTED", summary

        claim = run_cmd(["claim-status", "--project", root, "--operational-only", "--json"], cwd=root)
        assert claim.returncode == 0, claim.stdout + claim.stderr
        claim_payload = json.loads(claim.stdout)
        assert claim_payload["operational_only"] is True, claim_payload
        assert claim_payload["claim_status"]["all_supported"] is True, claim_payload

        burnin = run_cmd(["burnin", "--project", root, "--suite", "operational", "--json"], cwd=root)
        assert burnin.returncode == 0, burnin.stdout + burnin.stderr
        burnin_payload = json.loads(burnin.stdout)
        assert burnin_payload["suite"] == "operational", burnin_payload
        assert burnin_payload["claim_status"]["all_supported"] is True, burnin_payload


def assert_archive_legacy_missions_plans_and_moves_only_legacy_dirs():
    with tempfile.TemporaryDirectory(prefix="oss_cli_archive_legacy_") as root:
        current = Path(root) / ".codex-oss" / "missions" / "proof_a3_readonly"
        current.mkdir(parents=True, exist_ok=True)
        (current / "mission.json").write_text(json.dumps({
            "mission_id": "proof_a3_readonly",
            "tier": "A3",
            "promotion_proof": True,
        }, indent=2), encoding="utf-8")
        (current / "ledger.json").write_text(json.dumps({"evidence": []}, indent=2), encoding="utf-8")
        (current / "report.json").write_text(json.dumps({"status": "COMPLETE"}, indent=2), encoding="utf-8")
        (current / "trace_grading.json").write_text(json.dumps({"labels": ["productive_exploration"]}, indent=2), encoding="utf-8")
        (current / "trace.jsonl").write_text("{}\n", encoding="utf-8")
        (current / "summary.md").write_text("# ok\n", encoding="utf-8")

        legacy = Path(root) / ".codex-oss" / "missions" / "legacy_incomplete"
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / "report.json").write_text(json.dumps({"status": "FAILED"}, indent=2), encoding="utf-8")

        dry_run = run_cmd(["archive-legacy-missions", "--project", root, "--json"], cwd=root)
        assert dry_run.returncode == 0, dry_run.stdout + dry_run.stderr
        dry_payload = json.loads(dry_run.stdout)
        assert dry_payload["planned_count"] == 1, dry_payload
        assert dry_payload["planned"][0]["mission_id"] == "legacy_incomplete", dry_payload
        assert (Path(root) / ".codex-oss" / "missions" / "legacy_incomplete").exists()

        applied = run_cmd(["archive-legacy-missions", "--project", root, "--apply", "--json"], cwd=root)
        assert applied.returncode == 0, applied.stdout + applied.stderr
        applied_payload = json.loads(applied.stdout)
        assert applied_payload["archived_count"] == 1, applied_payload
        assert not (Path(root) / ".codex-oss" / "missions" / "legacy_incomplete").exists()
        assert (Path(root) / ".codex-oss" / "missions-legacy" / "legacy_incomplete").exists()


def assert_claim_status_defaults_to_current_evidence():
    with tempfile.TemporaryDirectory(prefix="oss_cli_claim_scope_") as root:
        result = run_cmd(["refresh-proofs", "--project", root, "--suite", "proof", "--json"], cwd=root)
        assert result.returncode == 0, result.stdout + result.stderr

        legacy = Path(root) / ".codex-oss" / "missions" / "legacy_incomplete"
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / "report.json").write_text(json.dumps({"status": "FAILED"}, indent=2), encoding="utf-8")

        default_claim = run_cmd(["claim-status", "--project", root, "--json"], cwd=root)
        assert default_claim.returncode == 0, default_claim.stdout + default_claim.stderr
        payload = json.loads(default_claim.stdout)
        assert payload["scope"] == "current_only", payload
        assert payload["claim_status"]["all_supported"] is True, payload
        assert payload["claim_tiers"]["runtime_proof_path"]["status"] == "SUPPORTED", payload
        assert payload["claim_tiers"]["raw_research_lane"]["status"] == "UNCONFIRMED", payload

        all_artifacts = run_cmd(["claim-status", "--project", root, "--all-artifacts", "--json"], cwd=root)
        assert all_artifacts.returncode == 0, all_artifacts.stdout + all_artifacts.stderr
        all_payload = json.loads(all_artifacts.stdout)
        assert all_payload["scope"] == "all_artifacts", all_payload


def assert_raw_claim_status_is_fail_closed():
    with tempfile.TemporaryDirectory(prefix="oss_cli_raw_lane_") as root:
        result = run_cmd(["raw-claim-status", "--project", root, "--json"], cwd=root)
        assert result.returncode == 1, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload["claim_status"]["status"] == "UNCONFIRMED", payload

        probe = Path(root) / ".codex-oss" / "raw-probes" / "probe_001"
        probe.mkdir(parents=True, exist_ok=True)
        (probe / "result.json").write_text(json.dumps({
            "structured_report_present": True,
            "scope_respected": True,
            "verification_recorded": False,
            "rollback_recorded": False,
        }, indent=2), encoding="utf-8")
        failed = run_cmd(["raw-claim-status", "--project", root, "--json"], cwd=root)
        assert failed.returncode == 1, failed.stdout + failed.stderr
        failed_payload = json.loads(failed.stdout)
        assert failed_payload["total_probes"] == 1, failed_payload
        assert failed_payload["claim_status"]["status"] == "UNCONFIRMED", failed_payload


def assert_raw_probe_runner_records_artifacts():
    with tempfile.TemporaryDirectory(prefix="oss_cli_raw_probe_runner_") as root:
        result = run_cmd([
            "raw-probe",
            "--project", root,
            "--probe-id", "probe_passish",
            "--scope-respected",
            "--verification-recorded",
            "--rollback-recorded",
            "--json",
            "--",
            "python3",
            "-c",
            "print('OSS_REPORT_BEGIN\\nStatus: COMPLETE\\nOSS_REPORT_END')",
        ], cwd=root)
        assert result.returncode == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload["structured_report_present"] is True, payload

        claim = run_cmd(["raw-claim-status", "--project", root, "--json"], cwd=root)
        assert claim.returncode == 0, claim.stdout + claim.stderr
        claim_payload = json.loads(claim.stdout)
        assert claim_payload["claim_status"]["status"] == "SUPPORTED", claim_payload


def assert_certify_reports_runtime_backed_and_raw_statuses():
    with tempfile.TemporaryDirectory(prefix="oss_cli_certify_") as root:
        runtime = run_cmd(["certify", "--project", root, "--target", "runtime_backed", "--json"], cwd=root)
        assert runtime.returncode == 0, runtime.stdout + runtime.stderr
        runtime_payload = json.loads(runtime.stdout)
        assert runtime_payload["verdict"]["status"] == "CERTIFIED", runtime_payload
        assert runtime_payload["targets"]["runtime_backed"]["status"] == "CERTIFIED", runtime_payload
        assert (Path(root) / ".codex-oss" / "certifications" / "runtime_backed.json").exists()

        raw = run_cmd(["certify", "--project", root, "--target", "raw_free_editing", "--no-refresh", "--json"], cwd=root)
        assert raw.returncode == 1, raw.stdout + raw.stderr
        raw_payload = json.loads(raw.stdout)
        assert raw_payload["verdict"]["status"] == "UNCONFIRMED", raw_payload
        assert raw_payload["gates"]["raw_lane_supported"]["ok"] is False, raw_payload


def main() -> int:
    assert_template_requires_scope()
    assert_template_outputs_mission_v1()
    assert_compile_outputs_strict_implementation_mission()
    assert_compile_outputs_critical_workspace_certified_mission()
    assert_run_posts_mission_to_runtime_bridge()
    assert_audit_mission_reports_runtime_artifacts()
    assert_mission_metrics_summarize_runtime_evidence()
    assert_refresh_proofs_generates_supported_claim_surface()
    assert_operational_burnin_generates_supported_claim_surface()
    assert_archive_legacy_missions_plans_and_moves_only_legacy_dirs()
    assert_claim_status_defaults_to_current_evidence()
    assert_raw_claim_status_is_fail_closed()
    assert_raw_probe_runner_records_artifacts()
    assert_certify_reports_runtime_backed_and_raw_statuses()
    print("PASS: MissionV1 CLI delegation suite")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
