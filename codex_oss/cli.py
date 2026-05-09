#!/usr/bin/env python3
"""codex-oss CLI — one-command setup for OpenCode Bridge."""

from __future__ import annotations

import argparse
import os
import sys

from .doctor import run_doctor
from .installer import install
from .audit import audit_mission
from .certify import certify_project
from .legacy import archive_legacy_missions
from .metrics import broad_low_risk_runtime_status, summarize_missions
from .mission_compiler import compile_handoff_v1, compile_mission_v1
from .decision_trace import read_decision_trace
from .proofs import refresh_claim_proofs
from .raw_lane import run_raw_probe, summarize_raw_probes


def main():
    parser = argparse.ArgumentParser(
        prog="codex-oss",
        description="One-command setup and diagnostics for OpenCode Bridge OSS subagents.",
    )
    sub = parser.add_subparsers(dest="command", help="Commands")

    # doctor
    d = sub.add_parser("doctor", help="Check setup for correctness")
    d.add_argument("--fix", action="store_true", help="Auto-repair issues")
    d.add_argument("--json", action="store_true", help="Machine-readable output")
    d.add_argument("--project", type=str, help="Project root path", default=None)
    d.add_argument("--offline", action="store_true", help="Only check local files/config")
    d.add_argument("--network", action="store_true", help="Include bridge health and network checks")
    d.add_argument("--live-model", action="store_true", help="Run a tiny live OSS model inference smoke")
    d.add_argument("--dev", action="store_true", help="Allow foreground supervisor warnings for development use")
    d.add_argument("--runtime-models", action="store_true", help="Check runtime model aliases (A2-A6) are accepted by bridge")

    # install
    i = sub.add_parser("install", help="Install OSS bridge config into current project")
    i.add_argument("--force", action="store_true", help="Overwrite existing files")
    i.add_argument("--project", type=str, help="Project root path", default=None)

    # start
    s = sub.add_parser("start", help="Start the bridge proxy")
    s.add_argument("--port", type=int, default=4000, help="Port (default: 4000)")
    s.add_argument("--mode", choices=["production", "compat-test", "openai-passthrough"],
                   default="production", help="Bridge mode")

    # stop
    sub.add_parser("stop", help="Stop the bridge proxy")

    # status
    sub.add_parser("status", help="Show bridge health")

    # validate-handoff
    vh = sub.add_parser("validate-handoff", help="Validate an OSS_HANDOFF_JSON handoff file")
    vh.add_argument("file", help="Markdown or text file containing OSS_HANDOFF_JSON")

    # mission — explicit runtime-backed delegation surface
    mission = sub.add_parser("mission", help="Create or run MissionV1 runtime-backed OSS tasks")
    mission_sub = mission.add_subparsers(dest="mission_command", help="Mission commands")

    mt = mission_sub.add_parser("template", help="Print a MissionV1 JSON template")
    mt.add_argument("--mission-id", default="mission_local_001")
    mt.add_argument("--objective", required=True)
    mt.add_argument("--tier", choices=["A2", "A3"], default="A3")
    mt.add_argument("--risk-tier", choices=["low", "medium", "critical"], default="low")
    mt.add_argument("--allowed-root", action="append", default=[])
    mt.add_argument("--allowed-path", action="append", default=[])
    mt.add_argument("--tool-budget", type=int, default=10)
    mt.add_argument("--time-budget-seconds", type=int, default=90)
    mt.add_argument("--allow-broad-read-scope", action="store_true")
    mt.add_argument("--critical-path-read-allowed", action="store_true")
    mt.add_argument("--critical-path-reason", default=None)

    mc = mission_sub.add_parser("compile", help="Compile a stricter MissionV1 with explicit objective_spec")
    mc.add_argument("--mission-id", default="mission_local_compile_001")
    mc.add_argument("--objective", required=True)
    mc.add_argument("--tier", choices=["A2", "A3", "A4", "A5", "A6"], default="A3")
    mc.add_argument("--risk-tier", choices=["low", "medium", "critical"], default="low")
    mc.add_argument("--allowed-root", action="append", default=[])
    mc.add_argument("--allowed-path", action="append", default=[])
    mc.add_argument("--owned-path", action="append", default=[])
    mc.add_argument("--read-only-path", action="append", default=[])
    mc.add_argument("--objective-type", default="")
    mc.add_argument("--objective-style", choices=["deterministic_lookup", "open_investigation", "implementation"], default="")
    mc.add_argument("--answer-obligation", action="append", default=[])
    mc.add_argument("--must-inspect", action="append", default=[])
    mc.add_argument("--evidence-collection-mode", choices=["prefetch_floor", "agenda_guided", "model_led"], default="")
    mc.add_argument("--target-symbol", default="")
    mc.add_argument("--target-key", default="")
    mc.add_argument("--target-pattern", default="")
    mc.add_argument("--required-value", action="append", default=[])
    mc.add_argument("--required-test-name", action="append", default=[])
    mc.add_argument("--required-changed-file", action="append", default=[])
    mc.add_argument("--required-source-file", action="append", default=[])
    mc.add_argument("--required-test-file", action="append", default=[])
    mc.add_argument("--required-symbol", action="append", default=[])
    mc.add_argument("--verification-command", action="append", default=[])
    mc.add_argument("--apply-mode", default="isolated_worktree")
    mc.add_argument("--workspace-allow-direct", action="store_true")
    mc.add_argument("--workspace-allow-critical", action="store_true")
    mc.add_argument("--workspace-require-clean", action="store_true")
    mc.add_argument("--workspace-allow-dirty-targets", action="store_true")
    mc.add_argument("--workspace-certification-required", action="store_true")
    mc.add_argument("--workspace-require-gpt-review", action="store_true")
    mc.add_argument("--workspace-reviewer-model", action="append", default=[])
    mc.add_argument("--workspace-min-reviewer-approvals", type=int, default=0)
    mc.add_argument("--workspace-require-isolated-preflight", action="store_true")
    mc.add_argument("--workspace-require-rollback-proof", action="store_true")
    mc.add_argument("--workspace-invariant-command", action="append", default=[])
    mc.add_argument("--sufficiency-min-main-claims", type=int, default=None)
    mc.add_argument("--sufficiency-min-evidence-refs-per-claim", type=int, default=None)
    mc.add_argument("--sufficiency-must-list-uninspected-areas", action="store_true")
    mc.add_argument("--sufficiency-confidence-cap", choices=["LOW", "MEDIUM", "HIGH"], default=None)
    mc.add_argument("--critical-path-read-allowed", action="store_true")
    mc.add_argument("--critical-path-reason", default=None)
    mc.add_argument("--critical-path-write-allowed", action="store_true")
    mc.add_argument("--allow-broad-read-scope", action="store_true")
    mc.add_argument("--handoff", action="store_true", help="Emit canonical <OSS_HANDOFF_JSON> wrapper instead of raw mission JSON")

    mr = mission_sub.add_parser("run", help="Run a MissionV1 through the local OSS Agent Runtime bridge")
    mr.add_argument("file", help="Mission JSON file, handoff file containing <OSS_HANDOFF_JSON>, or '-' for stdin")
    mr.add_argument("--model", default="mission-a3-kimi", help="Runtime alias, e.g. mission-a3-kimi")
    mr.add_argument("--port", type=int, default=4000)
    mr.add_argument("--base-url", default=None, help="Bridge base URL, default http://127.0.0.1:<port>/v1")
    mr.add_argument("--auth", default=None, help="Bearer token, default LITELLM_MASTER_KEY/PROXY_API_KEY")
    mr.add_argument("--timeout", type=float, default=120)
    mr.add_argument("--json", action="store_true", help="Print raw Responses JSON instead of report text")

    audit = sub.add_parser("audit-mission", help="Audit runtime mission artifacts under .codex-oss/missions/<id>")
    audit.add_argument("mission_id", help="Mission id to audit")
    audit.add_argument("--project", type=str, default=None, help="Project root path")
    audit.add_argument("--json", action="store_true", help="Machine-readable output")

    explain = sub.add_parser("explain", help="Explain mission policy/runtime decisions from the decision trace artifact")
    explain.add_argument("mission_id", help="Mission id to explain")
    explain.add_argument("--project", type=str, default=None, help="Project root path")
    explain.add_argument("--json", action="store_true", help="Machine-readable output")

    metrics = sub.add_parser("mission-metrics", help="Summarize runtime mission artifacts and claim-supporting evidence")
    metrics.add_argument("--project", type=str, default=None, help="Project root path")
    metrics.add_argument("--tier", choices=["A2", "A3", "A4", "A5", "A6"], default=None)
    metrics.add_argument("--proof-only", action="store_true", help="Only summarize promotion_proof-tagged missions")
    metrics.add_argument("--operational-only", action="store_true", help="Only summarize operational_burnin-tagged missions")
    metrics.add_argument("--current-only", action="store_true", help="Only summarize current tagged evidence (proof or operational)")
    metrics.add_argument("--json", action="store_true", help="Machine-readable output")

    proofs = sub.add_parser("refresh-proofs", help="Generate fresh deterministic promotion proof artifacts")
    proofs.add_argument("--project", type=str, default=None, help="Project root path")
    proofs.add_argument("--suite", choices=["proof", "operational", "all"], default="proof", help="Which evidence suite to generate")
    proofs.add_argument("--json", action="store_true", help="Machine-readable output")

    claim = sub.add_parser("claim-status", help="Report the current supported vs unconfirmed claim surface")
    claim.add_argument("--project", type=str, default=None, help="Project root path")
    claim.add_argument("--proof-only", action="store_true", help="Use only promotion_proof-tagged missions")
    claim.add_argument("--operational-only", action="store_true", help="Use only operational_burnin-tagged missions")
    claim.add_argument("--all-artifacts", action="store_true", help="Use all mission artifacts instead of default current tagged evidence")
    claim.add_argument("--json", action="store_true", help="Machine-readable output")

    burnin = sub.add_parser("burnin", help="Run deterministic proof refresh plus claim evaluation")
    burnin.add_argument("--project", type=str, default=None, help="Project root path")
    burnin.add_argument("--suite", choices=["proof", "operational", "all"], default="proof", help="Which evidence suite to generate and evaluate")
    burnin.add_argument("--json", action="store_true", help="Machine-readable output")

    certify = sub.add_parser("certify", help="Run explicit certification gates and write certification artifacts")
    certify.add_argument("--project", type=str, default=None, help="Project root path")
    certify.add_argument("--target", choices=["runtime_backed", "open_investigation", "repo_hygiene", "raw_free_editing_smoke", "raw_free_editing", "all"], default="all")
    certify.add_argument("--no-refresh", action="store_true", help="Do not regenerate proof/operational evidence before certification")
    certify.add_argument("--json", action="store_true", help="Machine-readable output")

    legacy = sub.add_parser("archive-legacy-missions", help="Plan or archive legacy/incomplete mission artifacts")
    legacy.add_argument("--project", type=str, default=None, help="Project root path")
    legacy.add_argument("--apply", action="store_true", help="Move planned legacy missions into .codex-oss/missions-legacy/")
    legacy.add_argument("--json", action="store_true", help="Machine-readable output")

    raw = sub.add_parser("raw-claim-status", help="Evaluate raw OSS probe artifacts in fail-closed research mode")
    raw.add_argument("--project", type=str, default=None, help="Project root path")
    raw.add_argument("--json", action="store_true", help="Machine-readable output")

    raw_probe = sub.add_parser("raw-probe", help="Run a raw-lane probe command and record fail-closed evidence")
    raw_probe.add_argument("--project", type=str, default=None, help="Project root path")
    raw_probe.add_argument("--probe-id", required=True, help="Raw probe id")
    raw_probe.add_argument("--scope-respected", action="store_true", help="Mark the probe as scope-respecting")
    raw_probe.add_argument("--verification-recorded", action="store_true", help="Mark the probe as having verification evidence")
    raw_probe.add_argument("--rollback-recorded", action="store_true", help="Mark the probe as having rollback evidence")
    raw_probe.add_argument("--allow-change", action="append", default=[], help="Expected changed path for behavior-derived scope proof")
    raw_probe.add_argument("--verification-artifact", action="append", default=[], help="Artifact path that must exist to prove verification")
    raw_probe.add_argument("--rollback-artifact", action="append", default=[], help="Artifact path that must exist to prove rollback recording")
    raw_probe.add_argument("--json", action="store_true", help="Machine-readable output")
    raw_probe.add_argument("cmd", nargs=argparse.REMAINDER, help="Command to run for the raw probe")

    # up — foreground supervisor
    up = sub.add_parser("up", help="Start bridge with foreground supervisor (keep terminal open)")
    up.add_argument("--port", type=int, default=4000)
    up.add_argument("--daemon", action="store_true", help="Start bridge as daemon (detach and exit after health check)")
    up.add_argument("--foreground", action="store_true", help="Keep terminal open with live log tailing")
    up.add_argument("--allow-missing-upstream", action="store_true", help="Allow supervised bridge startup without an upstream API key for deterministic/local runtime testing")

    # run — start bridge, run command, cleanup
    run = sub.add_parser("run", help="Start bridge, run command, stop bridge")
    run.add_argument("--port", type=int, default=4000)
    run.add_argument("--allow-missing-upstream", action="store_true", help="Allow bridge startup without an upstream API key for deterministic/local runtime testing")
    run.add_argument("cmd", nargs=argparse.REMAINDER, help="Command to run while bridge is alive")

    sd = sub.add_parser("supervise-daemon", help=argparse.SUPPRESS)
    sd.add_argument("--port", type=int, default=4000)

    args = parser.parse_args()

    if args.command == "doctor":
        from pathlib import Path
        root = Path(args.project) if args.project else None
        report = run_doctor(
            root,
            fix=args.fix,
            offline=args.offline,
            network=args.network or args.live_model,
            live_model=args.live_model,
            dev=args.dev,
            runtime_models=args.runtime_models,
        )
        report.print(json_output=args.json)
        sys.exit(0 if report.healthy else 1)

    elif args.command == "install":
        from pathlib import Path
        root = Path(args.project) if args.project else None
        sys.exit(install(root, force=args.force))

    elif args.command == "start":
        _start_bridge(args.port, args.mode)

    elif args.command == "stop":
        _stop_bridge(args.port if hasattr(args, 'port') else 4000)

    elif args.command == "status":
        _bridge_status()

    elif args.command == "validate-handoff":
        sys.exit(_validate_handoff(args.file))

    elif args.command == "mission":
        sys.exit(_mission(args))

    elif args.command == "audit-mission":
        project_root = args.project or os.getcwd()
        report = audit_mission(project_root, args.mission_id)
        if args.json:
            print(json_dumps_safe(report))
        else:
            print(f"Mission: {report['mission_id']}")
            print(f"OK: {str(bool(report['ok'])).lower()}")
            for item in report.get("checks", []):
                status = "PASS" if item.get("ok") else "FAIL"
                print(f"- {status} {item.get('name')}: {item.get('message')}")
        sys.exit(0 if report.get("ok") else 1)

    elif args.command == "explain":
        project_root = args.project or os.getcwd()
        trace = read_decision_trace(project_root, args.mission_id)
        if trace is None:
            print(f"Decision trace missing for mission {args.mission_id}", file=sys.stderr)
            sys.exit(1)
        phase_path = [
            str(item.get("result", "") or "")
            for item in (trace.get("decisions", []) or [])
            if str(item.get("decision_type", "") or "") == "phase_transition"
        ]
        decision_counts: dict[str, int] = {}
        for item in (trace.get("decisions", []) or []):
            key = str(item.get("decision_type", "") or "unknown")
            decision_counts[key] = decision_counts.get(key, 0) + 1
        explained = dict(trace)
        explained["summary"] = {
            "decision_count": len(trace.get("decisions", []) or []),
            "decision_type_counts": decision_counts,
            "phase_path": phase_path,
        }
        if args.json:
            print(json_dumps_safe(explained))
        else:
            print(f"Mission: {args.mission_id}")
            if phase_path:
                print(f"Phase path: {' -> '.join(phase_path)}")
            for idx, item in enumerate(trace.get("decisions", []) or [], start=1):
                print(f"{idx}. {item.get('decision_type')} -> {item.get('result')}")
                print(f"   policy: {item.get('policy')}")
                print(f"   reason: {item.get('reason')}")
                print(f"   source: {item.get('source_module')}")
        sys.exit(0)

    elif args.command == "mission-metrics":
        project_root = args.project or os.getcwd()
        report = summarize_missions(
            project_root,
            tier_filter=args.tier,
            proof_only=bool(args.proof_only),
            operational_only=bool(args.operational_only),
            current_only=bool(args.current_only),
        )
        if args.json:
            print(json_dumps_safe(report))
        else:
            print(f"Project: {report['project_root']}")
            print(f"Total missions: {report['total_missions']}")
            print(f"Audit OK: {report.get('audit_ok_count', 0)}")
            print(f"Audit Fail: {report.get('audit_fail_count', 0)}")
            print("Promotion evidence:")
            for key, value in (report.get("promotion_evidence", {}) or {}).items():
                print(f"- {key}: {value.get('status')} ({value.get('basis')})")
        sys.exit(0)

    elif args.command == "refresh-proofs":
        project_root = args.project or os.getcwd()
        report = refresh_claim_proofs(project_root, suite=args.suite)
        if args.json:
            print(json_dumps_safe(report))
        else:
            print(f"Project: {report['project_root']}")
            print(f"Suite: {report['suite']}")
            print("Generated proof missions:")
            for mission_id in report.get("generated_missions", []):
                print(f"- {mission_id}")
        sys.exit(0)

    elif args.command == "claim-status":
        project_root = args.project or os.getcwd()
        scope = "all_artifacts"
        if args.proof_only:
            scope = "proof_only"
        elif args.operational_only:
            scope = "operational_only"
        elif not args.all_artifacts:
            scope = "current_only"

        summary = summarize_missions(
            project_root,
            proof_only=scope == "proof_only",
            operational_only=scope == "operational_only",
            current_only=scope == "current_only",
        )
        proof_summary = summarize_missions(project_root, proof_only=True)
        operational_summary = summarize_missions(project_root, operational_only=True)
        current_summary = summarize_missions(project_root, current_only=True)
        raw_summary = summarize_raw_probes(project_root)
        report = {
            "project_root": project_root,
            "scope": scope,
            "proof_only": scope == "proof_only",
            "operational_only": scope == "operational_only",
            "current_only": scope == "current_only",
            "all_artifacts": scope == "all_artifacts",
            "claim_status": summary.get("claim_status", {}),
            "promotion_evidence": summary.get("promotion_evidence", {}),
            "eligible_missions": summary.get("eligible_missions", 0),
            "legacy_or_incomplete_missions": summary.get("legacy_or_incomplete_missions", 0),
            "claim_tiers": {
                "runtime_proof_path": {
                    "status": "SUPPORTED" if proof_summary.get("claim_status", {}).get("all_supported") else "UNCONFIRMED",
                    "basis": f"proof_eligible_missions={proof_summary.get('eligible_missions', 0)}",
                },
                "runtime_operational_path": {
                    "status": "SUPPORTED" if operational_summary.get("claim_status", {}).get("all_supported") else "UNCONFIRMED",
                    "basis": f"operational_eligible_missions={operational_summary.get('eligible_missions', 0)}",
                },
                "runtime_current_path": {
                    "status": "SUPPORTED" if current_summary.get("claim_status", {}).get("all_supported") else "UNCONFIRMED",
                    "basis": f"current_eligible_missions={current_summary.get('eligible_missions', 0)} legacy_or_incomplete={current_summary.get('legacy_or_incomplete_missions', 0)}",
                },
                "broader_low_risk_runtime": broad_low_risk_runtime_status(operational_summary),
                "raw_research_lane": raw_summary.get("claim_status", {}),
            },
        }
        if args.json:
            print(json_dumps_safe(report))
        else:
            claim_status = report["claim_status"]
            print(f"Project: {report['project_root']}")
            print(f"Scope: {report['scope']}")
            print(f"All supported: {str(bool(claim_status.get('all_supported'))).lower()}")
            print(f"Supported claims: {', '.join(claim_status.get('supported_claims', [])) or 'none'}")
            print(f"Unconfirmed claims: {', '.join(claim_status.get('unconfirmed_claims', [])) or 'none'}")
            print("Claim tiers:")
            for key, value in (report.get("claim_tiers", {}) or {}).items():
                print(f"- {key}: {value.get('status')} ({value.get('basis', '')})")
        sys.exit(0 if report["claim_status"].get("all_supported") else 1)

    elif args.command == "burnin":
        project_root = args.project or os.getcwd()
        refresh = refresh_claim_proofs(project_root, suite=args.suite)
        summary = summarize_missions(
            project_root,
            proof_only=args.suite == "proof",
            operational_only=args.suite == "operational",
            current_only=args.suite == "all",
        )
        report = {
            "burnin_version": "1.0",
            "project_root": project_root,
            "suite": args.suite,
            "generated_missions": refresh.get("generated_missions", []),
            "claim_status": summary.get("claim_status", {}),
            "promotion_evidence": summary.get("promotion_evidence", {}),
            "eligible_missions": summary.get("eligible_missions", 0),
            "audit_ok_count": summary.get("audit_ok_count", 0),
            "audit_fail_count": summary.get("audit_fail_count", 0),
        }
        if args.json:
            print(json_dumps_safe(report))
        else:
            print(f"Project: {report['project_root']}")
            print(f"Suite: {report['suite']}")
            print("Generated proof missions:")
            for mission_id in report.get("generated_missions", []):
                print(f"- {mission_id}")
            print(f"All supported: {str(bool(report['claim_status'].get('all_supported'))).lower()}")
            for key, value in (report.get("promotion_evidence", {}) or {}).items():
                print(f"- {key}: {value.get('status')} ({value.get('basis')})")
        sys.exit(0 if report["claim_status"].get("all_supported") else 1)

    elif args.command == "certify":
        project_root = args.project or os.getcwd()
        report = certify_project(project_root, target=args.target, refresh=not bool(args.no_refresh))
        if args.json:
            print(json_dumps_safe(report))
        else:
            print(f"Project: {report['project_root']}")
            print(f"Target: {report['target']}")
            print(f"Verdict: {report['verdict']['status']}")
            print("Gates:")
            for name, gate in (report.get("gates", {}) or {}).items():
                status = "PASS" if gate.get("ok") else "FAIL"
                print(f"- {status} {name}: {gate.get('basis', '')}")
        sys.exit(0 if report["verdict"].get("status") == "CERTIFIED" else 1)

    elif args.command == "archive-legacy-missions":
        project_root = args.project or os.getcwd()
        report = archive_legacy_missions(project_root, apply=bool(args.apply))
        if args.json:
            print(json_dumps_safe(report))
        else:
            print(f"Project: {report['project_root']}")
            print(f"Apply: {str(report['apply']).lower()}")
            print(f"Planned: {report['planned_count']}")
            if args.apply:
                print(f"Archived: {report['archived_count']}")
            for item in report.get("planned", []):
                print(f"- {item['mission_id']}: {item['reason']}")
        sys.exit(0)

    elif args.command == "raw-claim-status":
        project_root = args.project or os.getcwd()
        report = summarize_raw_probes(project_root)
        if args.json:
            print(json_dumps_safe(report))
        else:
            print(f"Project: {report['project_root']}")
            print(f"Total probes: {report['total_probes']}")
            print(f"Raw smoke status: {report['smoke_claim_status']['status']}")
            print(f"Smoke basis: {report['smoke_claim_status']['basis']}")
            print(f"Raw lane status: {report['claim_status']['status']}")
            print(f"Basis: {report['claim_status']['basis']}")
        sys.exit(0 if report["claim_status"].get("status") == "SUPPORTED" else 1)

    elif args.command == "raw-probe":
        project_root = args.project or os.getcwd()
        command = list(args.cmd or [])
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            print("raw-probe requires a command after --", file=sys.stderr)
            sys.exit(1)
        report = run_raw_probe(
            project_root,
            probe_id=args.probe_id,
            command=command,
            scope_respected=bool(args.scope_respected),
            verification_recorded=bool(args.verification_recorded),
            rollback_recorded=bool(args.rollback_recorded),
            allowed_changed_paths=list(args.allow_change or []),
            verification_artifact_paths=list(args.verification_artifact or []),
            rollback_artifact_paths=list(args.rollback_artifact or []),
        )
        if args.json:
            print(json_dumps_safe(report))
        else:
            print(f"Probe: {report['probe_id']}")
            print(f"Exit code: {report['exit_code']}")
            print(f"Structured report: {str(report['structured_report_present']).lower()}")
            print(f"Derived scope respected: {str(report['derived_scope_respected']).lower()}")
            print(f"Derived verification recorded: {str(report['derived_verification_recorded']).lower()}")
            print(f"Derived rollback recorded: {str(report['derived_rollback_recorded']).lower()}")
        sys.exit(0 if report.get("exit_code") == 0 else 1)

    elif args.command == "up":
        sys.exit(_supervise(args.port, daemon=args.daemon, foreground=args.foreground, allow_missing_upstream=bool(args.allow_missing_upstream)))

    elif args.command == "run":
        sys.exit(_run_with_bridge(args.port, args.cmd, allow_missing_upstream=bool(args.allow_missing_upstream)))

    elif args.command == "supervise-daemon":
        sys.exit(_daemon_supervisor(args.port))

    else:
        parser.print_help()
        sys.exit(1)


def _validate_handoff(path: str) -> int:
    import json
    from pathlib import Path

    from .handoff import validate_handoff_text

    try:
        text = Path(path).read_text()
    except OSError as exc:
        print(json.dumps({"valid": False, "error": str(exc)}, indent=2))
        return 1

    envelope = validate_handoff_text(text)
    if envelope.get("schema_error"):
        print(json.dumps({
            "valid": False,
            "error": envelope["schema_error"],
        }, indent=2))
        return 1

    print(json.dumps({
        "valid": True,
        "role": envelope["role"],
        "task_type": envelope["task_type"],
        "read_only_paths": envelope["read_only_paths"],
        "owned_paths": envelope["owned_paths"],
        "deliverable_fields": envelope["deliverable_fields"],
        "write_allowed": envelope["write_allowed"],
    }, indent=2))
    return 0


def _mission(args) -> int:
    if args.mission_command == "template":
        return _mission_template(args)
    if args.mission_command == "compile":
        return _mission_compile(args)
    if args.mission_command == "run":
        return _mission_run(args)
    print("Usage: codex-oss mission <template|compile|run>")
    return 1


def _mission_template(args) -> int:
    import json

    allowed_roots = args.allowed_root or []
    allowed_paths = args.allowed_path or []
    if not allowed_roots and not allowed_paths:
        print("Mission template requires at least one --allowed-root or --allowed-path", file=sys.stderr)
        return 1

    mission = {
        "schema_version": "oss_agent_mission.v1",
        "mission_id": args.mission_id,
        "tier": args.tier,
        "mode": "guided_exploration" if args.tier == "A2" else "managed_investigation",
        "objective": args.objective,
        "risk_tier": args.risk_tier,
        "write_allowed": False,
        "allowed_roots": allowed_roots,
        "allowed_paths": allowed_paths,
        "forbidden_roots": [],
        "forbidden_topics": [],
        "tool_budget": args.tool_budget,
        "time_budget_seconds": args.time_budget_seconds,
        "allowed_tool_classes": ["read", "search", "list", "safe_git"],
        "stop_conditions": [
            "valid_report",
            "budget_exhausted",
            "critical_path_detected",
            "deadline_reached",
        ],
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
        "allow_broad_read_scope": bool(args.allow_broad_read_scope),
        "critical_path_read_allowed": bool(args.critical_path_read_allowed),
        "critical_path_reason": args.critical_path_reason,
    }
    print(json.dumps(mission, indent=2))
    return 0


def _mission_compile(args) -> int:
    import json

    allowed_roots = args.allowed_root or []
    allowed_paths = args.allowed_path or []
    owned_paths = args.owned_path or []
    read_only_paths = args.read_only_path or []
    if args.tier in ("A2", "A3") and not (allowed_roots or allowed_paths):
        print("Mission compile requires at least one --allowed-root or --allowed-path for A2/A3", file=sys.stderr)
        return 1
    if args.tier in ("A4", "A5", "A6") and not owned_paths:
        print("Mission compile requires at least one --owned-path for A4/A5/A6", file=sys.stderr)
        return 1
    verification_commands = [_split_shell_words(command) for command in (args.verification_command or [])]
    sufficiency_policy = {}
    if args.sufficiency_min_main_claims is not None:
        sufficiency_policy["min_main_claims"] = int(args.sufficiency_min_main_claims)
    if args.sufficiency_min_evidence_refs_per_claim is not None:
        sufficiency_policy["min_evidence_refs_per_claim"] = int(args.sufficiency_min_evidence_refs_per_claim)
    if args.sufficiency_must_list_uninspected_areas:
        sufficiency_policy["must_list_uninspected_areas"] = True
    if args.sufficiency_confidence_cap:
        sufficiency_policy["confidence_cap_if_partial_extracts"] = str(args.sufficiency_confidence_cap).upper()
    try:
        mission = compile_mission_v1(
            mission_id=args.mission_id,
            objective=args.objective,
            tier=args.tier,
            risk_tier=args.risk_tier,
            allowed_roots=allowed_roots,
            allowed_paths=allowed_paths,
            owned_paths=owned_paths,
            read_only_paths=read_only_paths,
            objective_type=args.objective_type,
            target_symbol=args.target_symbol,
            target_key=args.target_key,
            target_pattern=args.target_pattern,
            required_values=args.required_value or [],
            required_test_names=args.required_test_name or [],
            required_changed_files=args.required_changed_file or [],
            required_source_files=args.required_source_file or [],
            required_test_files=args.required_test_file or [],
            required_symbols=args.required_symbol or [],
            verification_commands=verification_commands,
            apply_mode=args.apply_mode,
            objective_style=args.objective_style,
            sufficiency_policy=sufficiency_policy or None,
            answer_obligations=[{"question": text} for text in (args.answer_obligation or []) if str(text).strip()],
            must_inspect=list(args.must_inspect or []),
            evidence_collection_mode=args.evidence_collection_mode,
            allow_broad_read_scope=bool(args.allow_broad_read_scope),
            critical_path_read_allowed=bool(args.critical_path_read_allowed),
            critical_path_reason=args.critical_path_reason,
            critical_path_write_allowed=bool(args.critical_path_write_allowed),
            workspace_apply_policy={
                "allow_direct_workspace_apply": bool(args.workspace_allow_direct),
                "allow_critical_workspace_apply": bool(args.workspace_allow_critical),
                "require_clean_worktree": bool(args.workspace_require_clean),
                "allow_dirty_target_files": bool(args.workspace_allow_dirty_targets),
                "certification_required": bool(args.workspace_certification_required),
                "require_gpt_review": bool(args.workspace_require_gpt_review or args.workspace_certification_required),
                "reviewer_models": list(args.workspace_reviewer_model or []),
                "min_reviewer_approvals": int(args.workspace_min_reviewer_approvals or 0),
                "require_isolated_preflight": bool(args.workspace_require_isolated_preflight),
                "require_rollback_proof": bool(args.workspace_require_rollback_proof),
                "invariant_commands": [_split_shell_words(command) for command in (args.workspace_invariant_command or [])],
            },
        )
    except ValueError as exc:
        print(f"Mission compile failed: {exc}", file=sys.stderr)
        return 1
    if args.handoff:
        print(compile_handoff_v1(mission), end="")
    else:
        print(json.dumps(mission, indent=2))
    return 0


def _read_mission_text(path: str) -> str:
    import sys
    from pathlib import Path

    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def _mission_json_from_text(text: str) -> str:
    import json
    from codex_oss.runtime.policy import extract_single_handoff_block

    stripped = text.strip()
    if "<OSS_HANDOFF_JSON>" in stripped:
        return extract_single_handoff_block(stripped)
    parsed = json.loads(stripped)
    if not isinstance(parsed, dict):
        raise ValueError("Mission file must contain a JSON object")
    return json.dumps(parsed)


def _mission_run(args) -> int:
    import json
    import os
    import urllib.error
    import urllib.request

    try:
        mission_json = _mission_json_from_text(_read_mission_text(args.file))
        mission = json.loads(mission_json)
    except Exception as exc:
        print(f"Invalid mission: {exc}", file=sys.stderr)
        return 1

    base_url = (args.base_url or f"http://127.0.0.1:{args.port}/v1").rstrip("/")
    token = args.auth or os.getenv("PROXY_API_KEY") or os.getenv("LITELLM_MASTER_KEY") or "sk-local-codex-bridge"
    body = {
        "model": args.model,
        "stream": False,
        "input": [
            {
                "role": "user",
                "content": "<OSS_HANDOFF_JSON>\n" + json.dumps(mission) + "\n</OSS_HANDOFF_JSON>",
            }
        ],
    }
    request = urllib.request.Request(
        f"{base_url}/responses",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        print(f"Mission request failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(payload, indent=2))
        return 0 if payload.get("status") == "completed" else 1

    text = _extract_response_text(payload)
    print(text)
    if payload.get("status") != "completed":
        return 1
    if "Status: FAILED" in text:
        return 1
    return 0


def _extract_response_text(payload: dict) -> str:
    parts = []
    for item in payload.get("output", []):
        for content in item.get("content", []):
            if isinstance(content, dict) and content.get("text"):
                parts.append(str(content["text"]))
    if parts:
        return "\n".join(parts)
    return json_dumps_safe(payload)


def json_dumps_safe(value) -> str:
    import json
    try:
        return json.dumps(value, indent=2)
    except Exception:
        return str(value)


def _split_shell_words(command: str) -> list[str]:
    import shlex

    return [part for part in shlex.split(command) if part]


def _start_bridge(port: int, mode: str):
    import os
    import subprocess
    import time

    # Check if already running
    import urllib.request
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
        key = os.getenv("LITELLM_MASTER_KEY", "sk-local-codex-bridge")
        req.add_header("Authorization", f"Bearer {key}")
        urllib.request.urlopen(req, timeout=2)
        print(f"Bridge already running on port {port}")
        return
    except Exception:
        pass

    # Find bridge.py relative to this package
    package_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(package_dir)
    bridge_path = os.path.join(repo_root, "bridge.py")

    if not os.path.exists(bridge_path):
        print(f"ERROR: bridge.py not found at {bridge_path}")
        sys.exit(1)

    # Build env
    env = os.environ.copy()
    env["PROXY_PORT"] = str(port)

    # Use project-local state/log paths when running from a project directory
    project_root = env.get("CODEX_OSS_PROJECT", os.getcwd())
    state_dir = os.path.join(project_root, ".codex-oss", "state")
    os.makedirs(state_dir, exist_ok=True)
    env["PROXY_STATE_DB"] = env.get("PROXY_STATE_DB", os.path.join(state_dir, "proxy.sqlite3"))

    gpt_strategies = {
        "production": "error",
        "compat-test": "oss",
        "openai-passthrough": "openai",
    }
    env["GPT_MODEL_STRATEGY"] = gpt_strategies[mode]

    # Validate key is present
    if not env.get("OPENCODE_GO_API_KEY"):
        # Try env file first
        env_file = os.path.join(repo_root, ".codex-oss", "env", "opencode-go.env")
        if os.path.exists(env_file):
            with open(env_file) as f:
                for line in f:
                    if line.startswith("OPENCODE_GO_API_KEY="):
                        env["OPENCODE_GO_API_KEY"] = line.strip().split("=", 1)[1]
                        break

    if not env.get("OPENCODE_GO_API_KEY"):
        print("ERROR: OPENCODE_GO_API_KEY is not set.")
        print("  Set it in the environment or create .codex-oss/env/opencode-go.env")
        sys.exit(1)

    # Launch bridge in background
    log_dir = os.path.join(repo_root, ".codex-oss", "logs")
    os.makedirs(log_dir, exist_ok=True)
    out_log = open(os.path.join(log_dir, "bridge.log"), "a")
    err_log = open(os.path.join(log_dir, "bridge.err.log"), "a")

    proc = subprocess.Popen(
        [sys.executable, bridge_path],
        env=env, stdout=out_log, stderr=err_log,
        start_new_session=True
    )

    pid_file = os.path.join(repo_root, ".codex-oss", "run", "bridge.pid")
    os.makedirs(os.path.dirname(pid_file), exist_ok=True)
    with open(pid_file, "w") as f:
        f.write(str(proc.pid))

    time.sleep(1)
    print(f"Bridge started on port {port} (mode: {mode}, PID: {proc.pid})")
    print(f"Logs: .codex-oss/logs/bridge.log")


def _stop_bridge(port: int = 4000):
    import os
    import signal

    run_dir = os.path.join(os.getcwd(), ".codex-oss", "run")
    pid_file = os.path.join(run_dir, "bridge.pid")
    supervisor_pid_file = os.path.join(run_dir, "supervisor.pid")
    supervisor_file = os.path.join(run_dir, "supervisor.json")

    if os.path.exists(supervisor_pid_file):
        with open(supervisor_pid_file) as f:
            supervisor_pid = int(f.read().strip())
        try:
            os.kill(supervisor_pid, signal.SIGTERM)
            os.remove(supervisor_pid_file)
            print(f"Bridge supervisor stopped (PID: {supervisor_pid})")
        except ProcessLookupError:
            os.remove(supervisor_pid_file)
            print("Bridge supervisor was not running (stale PID file removed)")

    if os.path.exists(pid_file):
        with open(pid_file) as f:
            pid = int(f.read().strip())
        try:
            os.kill(pid, signal.SIGTERM)
            os.remove(pid_file)
            os.remove(supervisor_file) if os.path.exists(supervisor_file) else None
            print(f"Bridge stopped (PID: {pid})")
        except ProcessLookupError:
            os.remove(pid_file)
            os.remove(supervisor_file) if os.path.exists(supervisor_file) else None
            print("Bridge was not running (stale PID file removed)")
    else:
        # Fallback: kill by port
        import subprocess
        result = subprocess.run(["lsof", "-i", f":{port}", "-t"],
                                capture_output=True, text=True)
        if result.stdout.strip():
            for pid_str in result.stdout.strip().split("\n"):
                try:
                    os.kill(int(pid_str), signal.SIGTERM)
                    print(f"Bridge stopped (PID: {pid_str})")
                except ProcessLookupError:
                    pass
        else:
            print(f"No bridge found on port {port}")


def _bridge_status():
    import os
    import urllib.request
    import json

    port = int(os.getenv("PROXY_PORT", "4000"))
    key = os.getenv("LITELLM_MASTER_KEY", "sk-local-codex-bridge")

    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
        req.add_header("Authorization", f"Bearer {key}")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            print(json.dumps(data, indent=2))
    except Exception as e:
        print(f"Bridge not running: {e}")
        sys.exit(1)


# ── Supervisor ──

def _supervise(port: int, daemon: bool = False, foreground: bool = False, allow_missing_upstream: bool = False) -> int:
    """Foreground supervisor: start bridge, monitor health, stream logs, handle Ctrl+C.
    With --daemon: exit after health, bridge keeps running independently.
    """
    import os, signal, time, threading, subprocess, urllib.request, json, hashlib

    package_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(package_dir)
    bridge_path = os.path.join(repo_root, "bridge.py")
    if not os.path.exists(bridge_path):
        print(f"ERROR: bridge.py not found at {bridge_path}")
        return 1

    env = os.environ.copy()
    env["PROXY_PORT"] = str(port)
    env["CODEX_OSS_SUPERVISOR_MODE"] = "foreground"
    state_dir = os.path.join(os.getcwd(), ".codex-oss", "state")
    os.makedirs(state_dir, exist_ok=True)
    env["PROXY_STATE_DB"] = env.get("PROXY_STATE_DB", os.path.join(state_dir, "proxy.sqlite3"))

    if not _prepare_bridge_auth_env(env, os.getcwd(), allow_missing_upstream=allow_missing_upstream):
        return 1

    log_dir = os.path.join(os.getcwd(), ".codex-oss", "logs")
    os.makedirs(log_dir, exist_ok=True)
    out_log = open(os.path.join(log_dir, "bridge.log"), "a")
    err_log = open(os.path.join(log_dir, "bridge.err.log"), "a")

    def _get_hash():
        try:
            with open(bridge_path, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()[:12]
        except Exception:
            return "unknown"

    source_hash = _get_hash()
    if env.get("OPENCODE_GO_API_KEY"):
        print("  OpenCode Go key: loaded")
    else:
        print("  OpenCode Go key: missing (allowed for deterministic/local runtime testing)")
    print(f"  bridge.py source hash: {source_hash}")

    if daemon:
        return _start_daemon_supervisor(port, env, source_hash)

    print(f"  Starting bridge on port {port}...")

    proc = subprocess.Popen(
        [sys.executable, bridge_path],
        env=env, stdout=out_log, stderr=err_log,
        start_new_session=True,
    )

    pid_file = os.path.join(os.getcwd(), ".codex-oss", "run", "bridge.pid")
    os.makedirs(os.path.dirname(pid_file), exist_ok=True)
    with open(pid_file, "w") as f:
        f.write(str(proc.pid))

    # Supervisory JSON
    supervisor_info = {
        "mode": "foreground", "pid": proc.pid, "port": port,
        "source_hash": source_hash, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "project_root": os.getcwd(),
    }
    supervisor_file = os.path.join(os.getcwd(), ".codex-oss", "run", "supervisor.json")
    with open(supervisor_file, "w") as f:
        json.dump(supervisor_info, f)

    # Wait for health
    print(f"  Waiting for health...") if not daemon else None
    for i in range(60):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
            key = env.get("LITELLM_MASTER_KEY", "sk-local-codex-bridge")
            req.add_header("Authorization", f"Bearer {key}")
            d = json.loads(urllib.request.urlopen(req, timeout=2).read())
            if d.get("ok"):
                if daemon:
                    print(f"  Bridge started on port {port} (PID: {proc.pid})")
                    print(f"  Logs: .codex-oss/logs/")
                    print(f"  Stop with: codex-oss stop")
                    return 0
                print(f"  Provider listening: http://127.0.0.1:{port}/v1")
                print(f"  State DB: {d.get('state_db', 'unknown')}")
                print()
                print("  Ready for Codex.")
                print("  Keep this terminal open. Open Codex Desktop/CLI in another window.")
                print()
                break
        except Exception:
            time.sleep(0.5)
    else:
        print("  WARN: Bridge did not respond to health check within 30s")
        print("  Check .codex-oss/logs/bridge.err.log")
        if daemon:
            return 1

    if daemon:
        return 0  # Already returned above on success, here on timeout

    if not daemon:
        # Handle Ctrl+C gracefully
        def _shutdown(sig=None, frame=None):
            print("\n  Shutting down bridge...")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            os.remove(pid_file) if os.path.exists(pid_file) else None
            os.remove(supervisor_file) if os.path.exists(supervisor_file) else None
            print("  Bridge stopped.")
            sys.exit(0)

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

        # Log tailer
        def _tail():
            try:
                while proc.poll() is None:
                    time.sleep(3)
                    if os.path.exists(os.path.join(log_dir, "bridge.err.log")):
                        with open(os.path.join(log_dir, "bridge.err.log")) as f:
                            lines = f.readlines()
                            if lines:
                                last = lines[-1].strip()
                                if "error" in last.lower() or "fatal" in last.lower():
                                    print(f"  [bridge] {last[:120]}")
            except Exception:
                pass
        threading.Thread(target=_tail, daemon=True).start()

        # Wait for bridge process
        proc.wait()
        print("  Bridge process exited unexpectedly.")
        os.remove(pid_file) if os.path.exists(pid_file) else None
        return 1
    return 0


def _start_daemon_supervisor(port: int, env: dict, source_hash: str) -> int:
    import os, subprocess, sys, time, urllib.request, json

    run_dir = os.path.join(os.getcwd(), ".codex-oss", "run")
    log_dir = os.path.join(os.getcwd(), ".codex-oss", "logs")
    os.makedirs(run_dir, exist_ok=True)
    os.makedirs(log_dir, exist_ok=True)

    supervisor_pid_file = os.path.join(run_dir, "supervisor.pid")
    if os.path.exists(supervisor_pid_file):
        try:
            with open(supervisor_pid_file) as f:
                existing = int(f.read().strip())
            os.kill(existing, 0)
            print(f"  Bridge supervisor already running (PID: {existing})")
            return 0
        except (OSError, ValueError):
            os.remove(supervisor_pid_file)

    package_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(package_dir)
    cli_path = os.path.join(repo_root, "bin", "codex-oss")
    env = env.copy()
    env["CODEX_OSS_SUPERVISOR_MODE"] = "daemon-supervisor"
    out = open(os.path.join(log_dir, "supervisor.log"), "a")
    err = open(os.path.join(log_dir, "supervisor.err.log"), "a")
    proc = subprocess.Popen(
        [sys.executable, cli_path, "supervise-daemon", "--port", str(port)],
        cwd=os.getcwd(), env=env, stdout=out, stderr=err, start_new_session=True,
    )
    with open(supervisor_pid_file, "w") as f:
        f.write(str(proc.pid))

    for _ in range(60):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
            key = env.get("LITELLM_MASTER_KEY", "sk-local-codex-bridge")
            req.add_header("Authorization", f"Bearer {key}")
            if json.loads(urllib.request.urlopen(req, timeout=2).read()).get("ok"):
                print(f"  Bridge supervisor started (PID: {proc.pid})")
                print(f"  bridge.py source hash: {source_hash}")
                print(f"  Logs: .codex-oss/logs/")
                print(f"  Stop with: codex-oss stop")
                return 0
        except Exception:
            time.sleep(0.5)

    print("  WARN: supervised bridge did not respond to health check within 30s")
    return 1


def _daemon_supervisor(port: int) -> int:
    import os, signal, subprocess, sys, time, json, hashlib

    package_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(package_dir)
    bridge_path = os.path.join(repo_root, "bridge.py")
    run_dir = os.path.join(os.getcwd(), ".codex-oss", "run")
    log_dir = os.path.join(os.getcwd(), ".codex-oss", "logs")
    os.makedirs(run_dir, exist_ok=True)
    os.makedirs(log_dir, exist_ok=True)

    env = os.environ.copy()
    env["PROXY_PORT"] = str(port)
    env["CODEX_OSS_SUPERVISOR_MODE"] = "daemon-supervisor"
    state_dir = os.path.join(os.getcwd(), ".codex-oss", "state")
    os.makedirs(state_dir, exist_ok=True)
    env["PROXY_STATE_DB"] = env.get("PROXY_STATE_DB", os.path.join(state_dir, "proxy.sqlite3"))

    child = None
    stopping = False
    pid_file = os.path.join(run_dir, "bridge.pid")
    supervisor_file = os.path.join(run_dir, "supervisor.json")

    def _source_hash() -> str:
        try:
            with open(bridge_path, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()[:12]
        except Exception:
            return "unknown"

    def _write_supervisor(child_pid: int):
        with open(supervisor_file, "w") as f:
            json.dump({
                "mode": "daemon-supervisor",
                "pid": os.getpid(),
                "child_pid": child_pid,
                "port": port,
                "source_hash": _source_hash(),
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "project_root": os.getcwd(),
            }, f)

    def _shutdown(signum=None, frame=None):
        nonlocal stopping, child
        stopping = True
        if child and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    while not stopping:
        out_log = open(os.path.join(log_dir, "bridge.log"), "a")
        err_log = open(os.path.join(log_dir, "bridge.err.log"), "a")
        child = subprocess.Popen(
            [sys.executable, bridge_path],
            env=env, stdout=out_log, stderr=err_log, start_new_session=True,
        )
        with open(pid_file, "w") as f:
            f.write(str(child.pid))
        _write_supervisor(child.pid)
        rc = child.wait()
        out_log.close()
        err_log.close()
        if stopping:
            break
        with open(os.path.join(log_dir, "supervisor.log"), "a") as log:
            log.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} bridge exited rc={rc}; restarting\n")
        time.sleep(1)

    for path in (pid_file, supervisor_file):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    return 0


def _run_with_bridge(port: int, cmd: list, allow_missing_upstream: bool = False) -> int:
    """Start bridge, run command, stop bridge when done."""
    import os, time, subprocess, urllib.request, json

    if not cmd:
        print("Usage: codex-oss run -- <command>")
        print("Example: codex-oss run -- codex")
        return 1

    # Start bridge
    env = os.environ.copy()
    env["PROXY_PORT"] = str(port)
    state_dir = os.path.join(os.getcwd(), ".codex-oss", "state")
    os.makedirs(state_dir, exist_ok=True)
    env["PROXY_STATE_DB"] = env.get("PROXY_STATE_DB", os.path.join(state_dir, "proxy.sqlite3"))
    if not _prepare_bridge_auth_env(env, os.getcwd(), allow_missing_upstream=allow_missing_upstream):
        return 1

    package_dir = os.path.dirname(os.path.abspath(__file__))
    bridge_path = os.path.join(os.path.dirname(package_dir), "bridge.py")
    if not os.path.exists(bridge_path):
        print(f"ERROR: bridge.py not found at {bridge_path}")
        return 1

    log_dir = os.path.join(os.getcwd(), ".codex-oss", "logs")
    os.makedirs(log_dir, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, bridge_path], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    # Wait for health
    for _ in range(30):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
            key = env.get("LITELLM_MASTER_KEY", "sk-local-codex-bridge")
            req.add_header("Authorization", f"Bearer {key}")
            if json.loads(urllib.request.urlopen(req, timeout=2).read()).get("ok"):
                print(f"Bridge started on port {port}")
                break
        except Exception:
            time.sleep(0.5)
    else:
        proc.terminate()
        print("Bridge failed to start")
        return 1

    # Run user command (strip leading '--' if present)
    user_cmd = cmd[1:] if cmd and cmd[0] == "--" else cmd
    result = subprocess.run(user_cmd, env={**os.environ, "LITELLM_MASTER_KEY": "sk-local-codex-bridge"})
    rc = result.returncode

    # Stop bridge
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    print(f"Bridge stopped (command exited with {rc})")
    return rc


def _prepare_bridge_auth_env(env: dict, cwd: str, allow_missing_upstream: bool = False) -> bool:
    if not env.get("OPENCODE_GO_API_KEY"):
        env_file = os.path.join(cwd, ".codex-oss", "env", "opencode-go.env")
        if os.path.exists(env_file):
            with open(env_file) as f:
                for line in f:
                    if line.startswith("OPENCODE_GO_API_KEY="):
                        env["OPENCODE_GO_API_KEY"] = line.strip().split("=", 1)[1]
                        break
    if env.get("OPENCODE_GO_API_KEY"):
        return True
    if allow_missing_upstream:
        env["ALLOW_MISSING_OPENCODE_KEY"] = "1"
        return True
    print("ERROR: OPENCODE_GO_API_KEY not set")
    print("  Set it via: export OPENCODE_GO_API_KEY=sk-...")
    print("  Or create .codex-oss/env/opencode-go.env")
    print("  Or use --allow-missing-upstream for deterministic/local runtime testing")
    return False


if __name__ == "__main__":
    main()
