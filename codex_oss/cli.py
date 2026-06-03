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
from .native_polish import run_native_polish_smoke
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

    # restart
    r = sub.add_parser("restart", help="Restart the bridge proxy and verify freshness")
    r.add_argument("--port", type=int, default=4000, help="Port (default: 4000)")
    r.add_argument("--verify-fresh", action="store_true", default=True, help="Verify runtime freshness after restart (default: true)")

    # show-trace / show-summary
    st = sub.add_parser("show-trace", help="Display visible commentary trace for a mission")
    st.add_argument("mission_id", help="Mission ID")
    st.add_argument("--project", type=str, help="Project root path", default=None)
    st.add_argument("--json", action="store_true", help="Machine-readable output")

    ss = sub.add_parser("show-summary", help="Display summary.md for a mission")
    ss.add_argument("mission_id", help="Mission ID")
    ss.add_argument("--project", type=str, help="Project root path", default=None)

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

    smoke = sub.add_parser("smoke", help="Run product-level OSS bridge smoke checks")
    smoke_sub = smoke.add_subparsers(dest="smoke_command", help="Smoke checks")
    native_polish = smoke_sub.add_parser("native-polish", help="Prove the native-like OSS subagent surface")
    native_polish.add_argument("--project", type=str, default=None, help="Project root path")
    native_polish.add_argument("--port", type=int, default=4000, help="Bridge port")
    native_polish.add_argument("--base-url", default=None, help="Bridge base URL, default http://127.0.0.1:<port>/v1")
    native_polish.add_argument("--auth", default=None, help="Bearer token, default LITELLM_MASTER_KEY/PROXY_API_KEY")
    native_polish.add_argument("--timeout", type=float, default=120, help="HTTP timeout per smoke request")
    native_polish.add_argument("--json", action="store_true", help="Machine-readable output")

    native_burnin = smoke_sub.add_parser("native-like-burnin", help="Run bronze/silver/gold native-like burn-in matrix")
    native_burnin.add_argument("--project", type=str, default=None, help="Project root path")
    native_burnin.add_argument("--port", type=int, default=4000, help="Bridge port")
    native_burnin.add_argument("--base-url", default=None, help="Bridge base URL, default http://127.0.0.1:<port>/v1")
    native_burnin.add_argument("--auth", default=None, help="Bearer token")
    native_burnin.add_argument("--level", choices=["bronze", "silver", "gold", "all"], default="all", help="Burn-in level")
    native_burnin.add_argument("--live", action="store_true", help="Run live bridge tests (requires running bridge)")
    native_burnin.add_argument("--models", default="oss_deepseek_pro", help="Comma-separated model aliases for live tests")
    native_burnin.add_argument("--timeout", type=float, default=120, help="HTTP timeout per live request")
    native_burnin.add_argument("--json", action="store_true", help="Machine-readable output")

    bridge_ux_burnin = smoke_sub.add_parser("bridge-native-ux-burnin", help="Bridge Gold UX: prove direct bridge-harness visible commentary")
    bridge_ux_burnin.add_argument("--project", type=str, default=None, help="Project root path")
    bridge_ux_burnin.add_argument("--port", type=int, default=4000, help="Bridge port")
    bridge_ux_burnin.add_argument("--base-url", default=None, help="Bridge base URL")
    bridge_ux_burnin.add_argument("--auth", default=None, help="Bearer token")
    bridge_ux_burnin.add_argument("--models", default="oss_deepseek_pro", help="Comma-separated model aliases")
    bridge_ux_burnin.add_argument("--timeout", type=float, default=120, help="HTTP timeout per live request")
    bridge_ux_burnin.add_argument("--json", action="store_true", help="Machine-readable output")

    ux_burnin = smoke_sub.add_parser("native-ux-burnin", help="Deprecated alias for bridge-native-ux-burnin; does not prove Codex Desktop Gold")
    ux_burnin.add_argument("--project", type=str, default=None, help="Project root path")
    ux_burnin.add_argument("--port", type=int, default=4000, help="Bridge port")
    ux_burnin.add_argument("--base-url", default=None, help="Bridge base URL")
    ux_burnin.add_argument("--auth", default=None, help="Bearer token")
    ux_burnin.add_argument("--models", default="oss_deepseek_pro", help="Comma-separated model aliases")
    ux_burnin.add_argument("--timeout", type=float, default=120, help="HTTP timeout per live request")
    ux_burnin.add_argument("--json", action="store_true", help="Machine-readable output")

    desktop_ux_burnin = smoke_sub.add_parser("desktop-native-ux-burnin", help="Desktop Gold UX: verify captured Codex Desktop spawned-agent transcript")
    desktop_ux_burnin.add_argument("--project", type=str, default=None, help="Project root path")
    desktop_ux_burnin.add_argument("--transcript", required=True, help="Captured Codex Desktop spawned-agent transcript file")
    desktop_ux_burnin.add_argument("--mission-id", required=True, help="Mission ID whose artifacts must reconcile with transcript")
    desktop_ux_burnin.add_argument("--route-authority", default=None, help="RouteAuthorityV1 JSON file proving desktop provenance")
    desktop_ux_burnin.add_argument("--json", action="store_true", help="Machine-readable output")

    runtime_burnin = smoke_sub.add_parser("native-runtime-burnin", help="Bronze: prove runtime safety and canonical evidence")
    runtime_burnin.add_argument("--project", type=str, default=None, help="Project root path")
    runtime_burnin.add_argument("--port", type=int, default=4000, help="Bridge port")
    runtime_burnin.add_argument("--base-url", default=None, help="Bridge base URL")
    runtime_burnin.add_argument("--auth", default=None, help="Bearer token")
    runtime_burnin.add_argument("--live", action="store_true", help="Run live bridge tests")
    runtime_burnin.add_argument("--models", default="oss_deepseek_pro", help="Comma-separated model aliases")
    runtime_burnin.add_argument("--timeout", type=float, default=120, help="HTTP timeout per live request")
    runtime_burnin.add_argument("--json", action="store_true", help="Machine-readable output")

    report_burnin = smoke_sub.add_parser("native-report-burnin", help="Silver: prove model-authored narrative over runtime evidence")
    report_burnin.add_argument("--project", type=str, default=None, help="Project root path")
    report_burnin.add_argument("--json", action="store_true", help="Machine-readable output")

    tool_loop_burnin = smoke_sub.add_parser("native-tool-loop-burnin", help="Platinum: prove bridged tool-loop adoption without recovery")
    tool_loop_burnin.add_argument("--project", type=str, default=None, help="Project root path")
    tool_loop_burnin.add_argument("--port", type=int, default=4000, help="Bridge port")
    tool_loop_burnin.add_argument("--base-url", default=None, help="Bridge base URL")
    tool_loop_burnin.add_argument("--auth", default=None, help="Bearer token")
    tool_loop_burnin.add_argument("--models", default="oss_deepseek_pro", help="Comma-separated model aliases")
    tool_loop_burnin.add_argument("--timeout", type=float, default=120, help="HTTP timeout per live request")
    tool_loop_burnin.add_argument("--json", action="store_true", help="Machine-readable output")

    certify = sub.add_parser("certify", help="Run explicit certification gates and write certification artifacts")
    certify.add_argument("--project", type=str, default=None, help="Project root path")
    certify.add_argument("--target", choices=["runtime_backed", "open_investigation", "repo_hygiene", "raw_free_editing_smoke", "raw_free_editing", "all"], default="all")
    certify.add_argument("--no-refresh", action="store_true", help="Do not regenerate proof/operational evidence before certification")
    certify.add_argument("--json", action="store_true", help="Machine-readable output")

    bf = sub.add_parser("burnin-finalize", help="Reconstruct burn-in summary from manifest and mission artifacts")
    bf.add_argument("--run-id", type=str, required=True, help="Burn-in run ID")
    bf.add_argument("--project", type=str, default=None, help="Project root path")
    bf.add_argument("--json", action="store_true", help="Machine-readable output")

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

    route_authority = sub.add_parser("route-authority", help="Build a RouteAuthorityV1 JSON record for claim/provenance gates")
    route_authority.add_argument("--agent-name", default="", help="Codex agent name, e.g. oss_deepseek_investigator")
    route_authority.add_argument("--model-alias", required=True, help="Bridge model alias, e.g. mission-a3-deepseek")
    route_authority.add_argument("--handoff", default=None, help="Handoff file used for the spawned agent")
    route_authority.add_argument("--consumer-kind", choices=["direct_bridge_harness", "codex_desktop_spawned"], default="direct_bridge_harness")
    route_authority.add_argument("--output", default=None, help="Optional file path to write JSON")

    desktop_observation = sub.add_parser("desktop-observation", help="Build or classify DesktopObservationV1 transcript evidence")
    desktop_observation_sub = desktop_observation.add_subparsers(dest="desktop_observation_command", help="Desktop observation commands")

    desktop_observation_classify = desktop_observation_sub.add_parser("classify", help="Classify transcript provenance for Desktop Gold")
    desktop_observation_classify.add_argument("--transcript", required=True, help="Transcript JSON file")
    desktop_observation_classify.add_argument("--json", action="store_true", help="Machine-readable output")

    desktop_observation_wrap = desktop_observation_sub.add_parser("wrap", help="Wrap already-observed Desktop messages as a raw Desktop transcript")
    desktop_observation_wrap.add_argument("--mission-id", required=True, help="Mission ID")
    desktop_observation_wrap.add_argument("--messages", required=True, help="JSON file containing a messages array or {messages:[...]}")
    desktop_observation_wrap.add_argument("--agent-id", default="", help="Desktop spawned-agent ID")
    desktop_observation_wrap.add_argument("--agent-name", default="", help="Desktop spawned-agent name")
    desktop_observation_wrap.add_argument("--output", required=True, help="Output transcript JSON path")

    desktop_observation_from_sse = desktop_observation_sub.add_parser("from-sse", help="Build a Desktop transcript from consumer-observed child Responses SSE")
    desktop_observation_from_sse.add_argument("--mission-id", required=True, help="Mission ID")
    desktop_observation_from_sse.add_argument("--sse", required=True, help="File containing child Responses SSE captured by Desktop consumer")
    desktop_observation_from_sse.add_argument("--agent-id", default="", help="Desktop spawned-agent ID")
    desktop_observation_from_sse.add_argument("--agent-name", default="", help="Desktop spawned-agent name")
    desktop_observation_from_sse.add_argument("--transcript-kind", default="codex_desktop_raw_export", help="Transcript provenance kind")
    desktop_observation_from_sse.add_argument("--output", required=True, help="Output transcript JSON path")

    desktop_observation_reconcile = desktop_observation_sub.add_parser("reconcile", help="Mark commentary delivery observed/rendered from a raw Desktop transcript")
    desktop_observation_reconcile.add_argument("--mission-dir", required=True, help="Mission artifact directory")
    desktop_observation_reconcile.add_argument("--transcript", required=True, help="Desktop transcript JSON file")
    desktop_observation_reconcile.add_argument("--json", action="store_true", help="Machine-readable output")

    desktop_observation_consume_sse = desktop_observation_sub.add_parser(
        "consume-sse",
        help="Desktop consumer hook: capture child SSE, reconcile commentary, and run Desktop UX verification",
    )
    desktop_observation_consume_sse.add_argument("--mission-id", required=True, help="Mission ID")
    desktop_observation_consume_sse.add_argument("--mission-dir", required=True, help="Mission artifact directory")
    desktop_observation_consume_sse.add_argument("--project", required=True, help="Project root containing .codex-oss/missions")
    desktop_observation_consume_sse.add_argument("--sse", required=True, help="File containing child Responses SSE captured by Desktop consumer")
    desktop_observation_consume_sse.add_argument("--route-authority", required=True, help="RouteAuthorityV1 JSON file")
    desktop_observation_consume_sse.add_argument("--agent-id", default="", help="Desktop spawned-agent ID")
    desktop_observation_consume_sse.add_argument("--agent-name", default="", help="Desktop spawned-agent name")
    desktop_observation_consume_sse.add_argument("--transcript-kind", default="codex_desktop_raw_export", help="Transcript provenance kind")
    desktop_observation_consume_sse.add_argument("--output", default=None, help="Optional output transcript JSON path")
    desktop_observation_consume_sse.add_argument("--json", action="store_true", help="Machine-readable output")

    render_probe = sub.add_parser("desktop-pre-final-text-probe", help="Tranche 0: test whether Desktop renders child assistant text before final")
    render_probe_sub = render_probe.add_subparsers(dest="render_probe_command", help="Desktop render probe commands")

    render_probe_server = render_probe_sub.add_parser("server", help="Run the no-tool fake Responses provider")
    render_probe_server.add_argument("--host", default="127.0.0.1", help="Bind host")
    render_probe_server.add_argument("--port", type=int, default=43211, help="Bind port")
    render_probe_server.add_argument("--delay", type=float, default=1.0, help="Seconds between streamed text deltas")

    render_probe_self_test = render_probe_sub.add_parser("self-test", help="Run a local provider self-test without Desktop")
    render_probe_self_test.add_argument("--host", default="127.0.0.1", help="Bind host")
    render_probe_self_test.add_argument("--port", type=int, default=0, help="Bind port; 0 chooses a free port")
    render_probe_self_test.add_argument("--delay", type=float, default=0.01, help="Seconds between streamed text deltas")
    render_probe_self_test.add_argument("--json", action="store_true", help="Machine-readable output")

    render_probe_config = render_probe_sub.add_parser("config", help="Print Codex provider config for the fake probe server")
    render_probe_config.add_argument("--host", default="127.0.0.1", help="Provider host")
    render_probe_config.add_argument("--port", type=int, default=43211, help="Provider port")
    render_probe_config.add_argument("--output", default=None, help="Optional path to write provider config")
    render_probe_config.add_argument("--json", action="store_true", help="Machine-readable output")

    render_probe_record = render_probe_sub.add_parser("record", help="Record the real Desktop observation result")
    render_probe_record.add_argument("--status", choices=["pass", "fail", "flaky", "setup_failed", "unknown"], required=True, help="Observed Desktop probe status")
    render_probe_record.add_argument("--output", default=".codex-oss/desktop_pre_final_text_probe_result.json", help="Output result JSON path")
    render_probe_record.add_argument("--observed-progress-before-final", choices=["true", "false", "unknown"], default="unknown")
    render_probe_record.add_argument("--observed-final", choices=["true", "false", "unknown"], default="unknown")
    render_probe_record.add_argument("--notes", default="", help="Human note about the observed Desktop behavior")
    render_probe_record.add_argument("--json", action="store_true", help="Machine-readable output")

    app_server_probe = sub.add_parser(
        "app-server-pre-final-text-probe",
        help="Probe Codex app-server item/agentMessage/delta with the fake provider",
    )
    app_server_probe.add_argument("--cwd", default=".", help="Working directory for the app-server thread")
    app_server_probe.add_argument("--port", type=int, default=0, help="Fake provider port; 0 chooses a free port")
    app_server_probe.add_argument("--delay", type=float, default=0.25, help="Seconds between fake provider text deltas")
    app_server_probe.add_argument("--timeout", type=float, default=8.0, help="Seconds to wait for app-server deltas")
    app_server_probe.add_argument("--output", default=".codex-oss/app_server_pre_final_text_probe_result.json")
    app_server_probe.add_argument("--json", action="store_true", help="Machine-readable output")

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

    elif args.command == "restart":
        port = args.port if hasattr(args, 'port') else 4000
        verify = getattr(args, 'verify_fresh', True)
        _stop_bridge(port)
        import time
        time.sleep(1)
        _supervise(port, daemon=True)
        if verify:
            time.sleep(2)
            from codex_oss.runtime_manifest import STARTUP_TREE_SHA256, compute_tree_sha256, _PROJECT_ROOT
            fresh = STARTUP_TREE_SHA256 == compute_tree_sha256(_PROJECT_ROOT)
            if fresh:
                print(f"Bridge restarted on port {port}. Runtime source is FRESH.")
            else:
                print(f"Bridge restarted on port {port}. WARNING: Runtime source changed again post-restart.")
                sys.exit(1)

    elif args.command == "status":
        _bridge_status()

    elif args.command == "validate-handoff":
        sys.exit(_validate_handoff(args.file))

    elif args.command == "show-trace":
        project_root = args.project or os.getcwd()
        sys.exit(_show_trace(project_root, args.mission_id, json_output=args.json))

    elif args.command == "show-summary":
        project_root = args.project or os.getcwd()
        sys.exit(_show_summary(project_root, args.mission_id))

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
        from codex_oss.explain import build_explanation
        project_root = args.project or os.getcwd()
        explanation = build_explanation(project_root, args.mission_id)
        if explanation is None:
            print(f"No explainable artifacts found for mission {args.mission_id}", file=sys.stderr)
            sys.exit(1)
        if args.json:
            print(json_dumps_safe(explanation))
        else:
            print(f"Mission: {explanation['mission_id']}")
            print(f"Status: {explanation['status']}")
            print(f"Closure: {explanation.get('closure_source', 'unknown')}")
            print(f"Phase path: {explanation.get('phase_path', '')}")
            print(f"Confidence: {explanation.get('confidence', 'unknown')}")
            print(f"\nEvidence:")
            if explanation.get('required_sources'):
                print(f"  Required sources: {explanation['required_sources']['covered']}/{explanation['required_sources']['total']} covered")
            if explanation.get('evidence_shapes'):
                print(f"  Evidence shapes: {explanation['evidence_shapes']}")
            if explanation.get('contradictions'):
                print(f"  Contradictions: {explanation['contradictions']}")
            if explanation.get('missing_evidence'):
                print(f"  Missing evidence: {explanation['missing_evidence']}")
            if explanation.get('blocked_obligations'):
                print(f"  Blocked: {explanation['blocked_obligations']}")
            print(f"\nDecisions ({explanation.get('decision_count', 0)} total):")
            for d in (explanation.get('key_decisions', []) or [])[:10]:
                print(f"  {d.get('type', '')}: {d.get('result', '')} ({d.get('reason', '')[:100]})")
            if explanation.get('caveats'):
                print(f"\nCaveats:")
                for c in explanation.get('caveats', [])[:5]:
                    print(f"  - {c[:120]}")
            if explanation.get('coverage_gaps'):
                print(f"\nCoverage gaps:")
                for g in explanation.get('coverage_gaps', [])[:5]:
                    print(f"  - [{g.get('category', '')}] {g.get('reason', '')[:120]}")
            if explanation.get('semantic_review'):
                print(f"\nSemantic review: {explanation['semantic_review'].get('decision', '')} "
                      f"(blocking: {explanation['semantic_review'].get('blocking_count', 0)}, "
                      f"repairable: {explanation['semantic_review'].get('repairable_count', 0)})")
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

    elif args.command == "smoke":
        sys.exit(_smoke(args))

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

    elif args.command == "burnin-finalize":
        from codex_oss.burnin.finalizer import finalize_burnin_run
        project_root = args.project or os.getcwd()
        result = finalize_burnin_run(project_root, args.run_id)
        if args.json:
            print(json_dumps_safe(result))
        else:
            print(f"Run: {result.get('run_id', '?')}")
            print(f"Cases: {result.get('completed_cases', 0)}/{result.get('expected_cases', 0)}")
            print(f"Truth-safe: {result.get('truth_safe', {}).get('pass', False)}")
            print(f"Native-feeling: {result.get('native_feeling', {}).get('pass', False)}")
            print(f"Summary: {result.get('run_dir', '')}/summary.json")
        sys.exit(0 if result.get("truth_safe", {}).get("pass") else 1)

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

    elif args.command == "route-authority":
        from pathlib import Path
        from codex_oss.route_authority import build_route_authority

        handoff_text = None
        if args.handoff:
            try:
                handoff_text = Path(args.handoff).read_text(encoding="utf-8")
            except OSError as exc:
                print(f"handoff not readable: {exc}", file=sys.stderr)
                sys.exit(1)
        record = build_route_authority(
            agent_name=args.agent_name,
            model_alias=args.model_alias,
            handoff_text=handoff_text,
            consumer_kind=args.consumer_kind,
        )
        payload = json_dumps_safe(record)
        if args.output:
            Path(args.output).write_text(payload + "\n", encoding="utf-8")
        print(payload)
        sys.exit(0 if record.get("native_claim_allowed") else 1)

    elif args.command == "desktop-observation":
        sys.exit(_desktop_observation(args))

    elif args.command == "desktop-pre-final-text-probe":
        sys.exit(_desktop_pre_final_text_probe(args))

    elif args.command == "app-server-pre-final-text-probe":
        sys.exit(_app_server_pre_final_text_probe(args))

    elif args.command == "up":
        sys.exit(_supervise(args.port, daemon=args.daemon, foreground=args.foreground, allow_missing_upstream=bool(args.allow_missing_upstream)))

    elif args.command == "run":
        sys.exit(_run_with_bridge(args.port, args.cmd, allow_missing_upstream=bool(args.allow_missing_upstream)))

    elif args.command == "supervise-daemon":
        sys.exit(_daemon_supervisor(args.port))

    else:
        parser.print_help()
        sys.exit(1)


def _desktop_observation(args) -> int:
    import json
    from pathlib import Path

    from codex_oss.desktop_observation import (
        build_raw_desktop_transcript,
        classify_transcript_provenance,
    )
    from codex_oss.desktop_consumer_adapter import (
        reconcile_delivery_with_transcript,
        transcript_from_responses_sse,
    )
    from codex_oss.desktop_consumer_hook import consume_desktop_sse

    if args.desktop_observation_command == "classify":
        try:
            payload = json.loads(Path(args.transcript).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"transcript not readable: {exc}", file=sys.stderr)
            return 1
        provenance = classify_transcript_provenance(payload if isinstance(payload, dict) else {})
        if args.json:
            print(json_dumps_safe(provenance))
        else:
            print(f"Desktop transcript provenance: {'PASS' if provenance.get('ok') else 'FAIL'}")
            print(f"  consumer_kind: {provenance.get('consumer_kind', '')}")
            print(f"  transcript_kind: {provenance.get('transcript_kind', '')}")
            for reason in provenance.get("reasons", []):
                print(f"  missing: {reason}")
        return 0 if provenance.get("ok") else 1

    if args.desktop_observation_command == "wrap":
        try:
            payload = json.loads(Path(args.messages).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"messages not readable: {exc}", file=sys.stderr)
            return 1
        if isinstance(payload, dict):
            messages = payload.get("messages", [])
        else:
            messages = payload
        if not isinstance(messages, list):
            print("messages must be a JSON array or an object with a messages array", file=sys.stderr)
            return 1
        transcript = build_raw_desktop_transcript(
            mission_id=args.mission_id,
            messages=[message for message in messages if isinstance(message, dict)],
            agent_id=args.agent_id,
            agent_name=args.agent_name,
        )
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json_dumps_safe(transcript) + "\n", encoding="utf-8")
        print(str(output))
        return 0

    if args.desktop_observation_command == "from-sse":
        try:
            sse_text = Path(args.sse).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"SSE file not readable: {exc}", file=sys.stderr)
            return 1
        transcript = transcript_from_responses_sse(
            mission_id=args.mission_id,
            sse_text=sse_text,
            agent_id=args.agent_id,
            agent_name=args.agent_name,
            transcript_kind=args.transcript_kind,
        )
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json_dumps_safe(transcript) + "\n", encoding="utf-8")
        print(str(output))
        return 0

    if args.desktop_observation_command == "reconcile":
        try:
            transcript = json.loads(Path(args.transcript).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"transcript not readable: {exc}", file=sys.stderr)
            return 1
        report = reconcile_delivery_with_transcript(
            mission_dir=args.mission_dir,
            transcript=transcript if isinstance(transcript, dict) else {},
            persist=True,
        )
        if args.json:
            print(json_dumps_safe(report))
        else:
            print(f"Desktop delivery reconciliation: {'PASS' if report.get('ok') else 'FAIL'}")
            print(f"  marked: {report.get('marked_count', 0)}")
            for reason in report.get("reasons", []):
                print(f"  missing: {reason}")
        return 0 if report.get("ok") else 1

    if args.desktop_observation_command == "consume-sse":
        try:
            sse_text = Path(args.sse).read_text(encoding="utf-8", errors="replace")
            route_authority = json.loads(Path(args.route_authority).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Desktop consumer input not readable: {exc}", file=sys.stderr)
            return 1
        report = consume_desktop_sse(
            mission_id=args.mission_id,
            mission_dir=args.mission_dir,
            project_root=args.project,
            sse_text=sse_text,
            route_authority=route_authority if isinstance(route_authority, dict) else {},
            agent_id=args.agent_id,
            agent_name=args.agent_name,
            transcript_kind=args.transcript_kind,
            output_path=args.output,
            persist=True,
            verify=True,
        )
        if args.json:
            print(json_dumps_safe(report))
        else:
            print(f"Desktop consumer observation: {'PASS' if report.get('ok') else 'FAIL'}")
            print(f"  transcript: {report.get('transcript_path', '')}")
            print(f"  result: {report.get('result_path', '')}")
            print(f"  marked: {(report.get('reconciliation') or {}).get('marked_count', 0)}")
            print(f"  rendered_before_final: {(report.get('reconciliation') or {}).get('rendered_before_final_count', 0)}")
            for reason in report.get("missing_evidence", []):
                print(f"  missing: {reason}")
        return 0 if report.get("ok") else 1

    print("Usage: codex-oss desktop-observation <classify|wrap|from-sse|reconcile|consume-sse>", file=sys.stderr)
    return 1


def _desktop_pre_final_text_probe(args) -> int:
    from pathlib import Path

    from codex_oss.desktop_pre_final_text_probe import (
        agent_prompt,
        provider_config,
        run_self_test,
        serve_forever,
        write_probe_result,
    )

    if args.render_probe_command == "server":
        serve_forever(host=args.host, port=args.port, delay_seconds=args.delay)
        return 0

    if args.render_probe_command == "self-test":
        report = run_self_test(host=args.host, port=args.port, delay_seconds=args.delay)
        if args.json:
            print(json_dumps_safe(report))
        else:
            print(f"Desktop pre-final text provider self-test: {'PASS' if report.get('ok') else 'FAIL'}")
            print(f"  port: {report.get('port')}")
            print(f"  order_ok: {str(bool(report.get('order_ok'))).lower()}")
            print(f"  response_completed: {str(bool(report.get('response_completed'))).lower()}")
            print(f"  done_seen: {str(bool(report.get('done_seen'))).lower()}")
            print("  delta_text:")
            for line in str(report.get("delta_text", "")).splitlines():
                print(f"    {line}")
        return 0 if report.get("ok") else 1

    if args.render_probe_command == "config":
        config = provider_config(host=args.host, port=args.port)
        if args.output:
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.output).write_text(config, encoding="utf-8")
        if args.json:
            print(json_dumps_safe({
                "schema_version": "desktop_pre_final_text_probe_config.v1",
                "provider": "desktop_pre_final_text_probe",
                "model": "desktop-pre-final-text-probe",
                "config": config,
                "agent_prompt": agent_prompt(),
                "output": args.output or "",
            }))
        else:
            print(config, end="" if config.endswith("\n") else "\n")
            print("# Spawn prompt:")
            print(agent_prompt())
        return 0

    if args.render_probe_command == "record":
        def tri(value: str):
            if value == "true":
                return True
            if value == "false":
                return False
            return None

        report = write_probe_result(
            output_path=args.output,
            probe_status=args.status,
            observed_progress_before_final=tri(args.observed_progress_before_final),
            observed_final=tri(args.observed_final),
            notes=args.notes,
        )
        if args.json:
            print(json_dumps_safe(report))
        else:
            print(f"Desktop pre-final text probe result: {report['probe_status']}")
            print(f"  output: {report['path']}")
            print(f"  Desktop live-commentary claims allowed: {str(bool(report['desktop_render_surface']['claim_policy']['desktop_live_commentary_claim_allowed'])).lower()}")
            print(f"  reason: {report['desktop_render_surface']['claim_policy']['reason']}")
        return 0 if report["probe_status"] == "pass" else 1

    print("Usage: codex-oss desktop-pre-final-text-probe <server|self-test|config|record>", file=sys.stderr)
    return 1


def _app_server_pre_final_text_probe(args) -> int:
    from pathlib import Path

    from codex_oss.app_server_probe import (
        run_app_server_pre_final_text_probe,
        write_app_server_probe_result,
    )

    result = run_app_server_pre_final_text_probe(
        cwd=Path(args.cwd).resolve(),
        port=args.port,
        delay_seconds=args.delay,
        timeout_seconds=args.timeout,
    )
    output = write_app_server_probe_result(result, args.output)
    result["path"] = str(output)
    if args.json:
        print(json_dumps_safe(result))
    else:
        print(f"App-server pre-final text probe: {result.get('probe_status')}")
        print(f"  output: {output}")
        print(f"  item/agentMessage/delta: {str(bool(result.get('observed_agent_message_delta'))).lower()}")
        print(f"  progress before final: {str(bool(result.get('observed_progress_before_final'))).lower()}")
    return 0 if result.get("probe_status") == "pass" else 1


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


def _smoke(args) -> int:
    if args.smoke_command == "native-polish":
        project_root = args.project or os.getcwd()
        base_url = (args.base_url or f"http://127.0.0.1:{args.port}/v1").rstrip("/")
        auth = args.auth or os.getenv("PROXY_API_KEY") or os.getenv("LITELLM_MASTER_KEY") or "sk-local-codex-bridge"
        report = run_native_polish_smoke(
            project_root,
            base_url=base_url,
            auth=auth,
            timeout=float(args.timeout),
        )
        if args.json:
            print(json_dumps_safe(report))
        else:
            print(f"Project: {report['project_root']}")
            print(f"Native polish: {'PASS' if report.get('ok') else 'FAIL'}")
            for check in report.get("checks", []):
                print(f"- {check.get('status')}: {check.get('name')} — {check.get('message')}")
                if check.get("fix") and check.get("status") != "PASS":
                    print(f"  Fix: {check.get('fix')}")
                details = check.get("details")
                if isinstance(details, dict):
                    for key in ("mission_id", "visible_commentary_path", "summary_path"):
                        if details.get(key):
                            print(f"  {key}: {details[key]}")
        return 0 if report.get("ok") else 1

    if args.smoke_command == "native-like-burnin":
        return _run_native_like_burnin(args)

    if args.smoke_command in {"native-ux-burnin", "bridge-native-ux-burnin"}:
        return _run_native_ux_burnin(args)

    if args.smoke_command == "desktop-native-ux-burnin":
        return _run_desktop_native_ux_burnin(args)

    if args.smoke_command == "native-runtime-burnin":
        args.level = "bronze"
        return _run_native_like_burnin(args)

    if args.smoke_command == "native-report-burnin":
        return _run_native_report_burnin(args)

    if args.smoke_command == "native-tool-loop-burnin":
        args.level = "all"
        args.live = True
        return _run_native_like_burnin(args)

    print("Usage: codex-oss smoke native-polish")
    return 1


def _run_native_like_burnin(args) -> int:
    """Run the native-like burn-in matrix from CLI."""
    import os as _os
    project_root = args.project or _os.getcwd()
    base_url = (args.base_url or f"http://127.0.0.1:{args.port}/v1").rstrip("/")
    auth = args.auth or _os.getenv("PROXY_API_KEY") or _os.getenv("LITELLM_MASTER_KEY") or "sk-local-codex-bridge"

    # Set env vars for the test suite
    if args.live:
        _os.environ["LIVE"] = "1"
        _os.environ["OSS_BRIDGE_URL"] = base_url
        _os.environ["PROXY_API_KEY"] = auth
        _os.environ["LIVE_MODELS"] = args.models
    else:
        _os.environ["LIVE"] = "0"

    level = args.level

    # Run the test suite
    import subprocess
    import sys

    test_file = _os.path.join(project_root, "tests", "test_native_like_live_matrix.py")
    if not _os.path.exists(test_file):
        print(f"Test file not found: {test_file}", file=sys.stderr)
        return 1

    env = _os.environ.copy()
    env["PYTHONPATH"] = project_root
    env["NATIVE_BURNIN_LEVEL"] = level

    print(f"Running native-like burn-in (level={level}, live={args.live})")
    if args.live:
        print(f"  Bridge: {base_url}")
        print(f"  Models: {args.models}")
    print()

    result = subprocess.run(
        [sys.executable, test_file],
        cwd=project_root,
        env=env,
        capture_output=not args.json,
        text=True,
        timeout=float(args.timeout) + 30,
    )

    if args.json:
        # Machine-readable: just pass through exit code
        pass
    else:
        print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)

    return result.returncode


def _run_native_ux_burnin(args) -> int:
    """Run Bridge Gold UX burn-in: prove direct bridge-harness commentary."""
    import os as _os
    project_root = args.project or _os.getcwd()
    base_url = (args.base_url or f"http://127.0.0.1:{args.port}/v1").rstrip("/")
    auth = args.auth or _os.getenv("PROXY_API_KEY") or _os.getenv("LITELLM_MASTER_KEY") or "sk-local-codex-bridge"
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    from codex_oss.spawned_transcript import run_gold_ux_burnin

    print(f"Bridge Gold UX Burn-In — direct bridge-harness commentary proof")
    print("  Note: this does not prove Codex Desktop spawned-agent UX; use desktop-native-ux-burnin for that.")
    print(f"  Bridge: {base_url}")
    print(f"  Models: {models}")
    print()

    report = run_gold_ux_burnin(
        models=models,
        base_url=base_url,
        auth=auth,
        timeout=float(args.timeout),
    )

    if args.json:
        print(json_dumps_safe(report))
    else:
        passed = report.get("gold_passed", 0)
        total = report.get("total_tests", 0)
        print(f"Bridge Gold UX: {passed}/{total} passed")
        for result in report.get("results", []):
            name = result.get("test_name", "unknown")
            level = result.get("level", "none")
            gold = result.get("gold_pass", False)
            marker = "✓" if gold else "✗"
            print(f"  {marker} {name} — level={level}")
            if not gold:
                for dim in result.get("failed_dimensions", []):
                    print(f"      missing: {dim}")
                for ev in result.get("missing_evidence", []):
                    print(f"      evidence: {ev}")
                meta = result.get("transcript_metadata", {})
                if meta:
                    print(f"      commentary_detected: {meta.get('commentary_detected', 0)}")
                    print(f"      commentary_before_final: {meta.get('commentary_before_final', 0)}")
                    print(f"      event_classes: {meta.get('event_classes_found', [])}")

    return 0 if report.get("gold_passed", 0) == report.get("total_tests", 0) else 1


def _run_desktop_native_ux_burnin(args) -> int:
    """Verify captured Codex Desktop spawned-agent native UX evidence."""
    import json as _json
    import os as _os

    from codex_oss.desktop_native_verifier import verify_desktop_native_ux

    project_root = args.project or _os.getcwd()
    route_authority = None
    if args.route_authority:
        try:
            with open(args.route_authority, "r", encoding="utf-8") as handle:
                route_authority = _json.load(handle)
        except OSError as exc:
            print(f"Route authority file not readable: {exc}", file=sys.stderr)
            return 1

    report = verify_desktop_native_ux(
        transcript_path=args.transcript,
        mission_id=args.mission_id,
        project_root=project_root,
        route_authority=route_authority,
    )
    if args.json:
        print(json_dumps_safe(report))
    else:
        print(f"Desktop Gold UX: {'PASS' if report.get('ok') else 'FAIL'}")
        print(f"Mission: {report.get('mission_id', '')}")
        print(f"Transcript: {report.get('transcript_path', '')}")
        for check in report.get("checks", []):
            marker = "✓" if check.get("ok") else "✗"
            print(f"  {marker} {check.get('name')}")
        for item in report.get("missing_evidence", []):
            print(f"  missing: {item}")
    return 0 if report.get("ok") else 1


def _run_native_report_burnin(args) -> int:
    """Run Silver burn-in: prove model-authored narrative over runtime evidence."""
    import subprocess
    import sys
    import os as _os

    project_root = args.project or _os.getcwd()
    test_file = _os.path.join(project_root, "tests", "test_native_like_live_matrix.py")

    if not _os.path.exists(test_file):
        print(f"Test file not found: {test_file}", file=sys.stderr)
        return 1

    env = _os.environ.copy()
    env["PYTHONPATH"] = project_root
    env["LIVE"] = "0"
    env["NATIVE_BURNIN_LEVEL"] = "silver"

    print("Silver Report Burn-In — model-authored narrative proof")
    print()

    result = subprocess.run(
        [sys.executable, test_file],
        cwd=project_root,
        env=env,
        capture_output=not args.json,
        text=True,
        timeout=60,
    )

    if not args.json:
        print(result.stdout)
    return result.returncode




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
        "must_inspect": list(dict.fromkeys(allowed_paths)),
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
    apply_mode = "isolated_worktree" if args.apply_mode == "isolated" else args.apply_mode
    valid_apply_modes = {
        "isolated_worktree",
        "temp_project",
        "workspace",
        "workspace_explicit",
        "workspace_low_risk",
    }
    if args.tier in ("A4", "A5", "A6") and apply_mode not in valid_apply_modes:
        print(
            "Mission compile --apply-mode must be one of "
            f"{sorted(valid_apply_modes)}",
            file=sys.stderr,
        )
        return 1
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
            apply_mode=apply_mode,
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


def _mission_handoff_from_text(text: str) -> tuple[str, dict]:
    """Return the full handoff text and parsed MissionV1 JSON.

    Mission files can include sibling runtime blocks such as OSS_PATCH_INTENT_JSON.
    Those blocks are part of the implementation authority handoff and must survive
    the CLI -> bridge boundary. We still parse the MissionV1 block here so bad
    mission files fail before the HTTP request.
    """
    import json

    stripped = text.strip()
    mission_json = _mission_json_from_text(stripped)
    mission = json.loads(mission_json)
    if "<OSS_HANDOFF_JSON>" in stripped:
        return stripped, mission
    return "<OSS_HANDOFF_JSON>\n" + json.dumps(mission) + "\n</OSS_HANDOFF_JSON>", mission


def _mission_run(args) -> int:
    import json
    import os
    import urllib.error
    import urllib.request

    try:
        handoff_text, mission = _mission_handoff_from_text(_read_mission_text(args.file))
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
                "content": handoff_text,
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
    import json
    import signal
    import subprocess
    import time
    import urllib.request

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

    stopped_local = False
    if os.path.exists(pid_file):
        with open(pid_file) as f:
            pid = int(f.read().strip())
        try:
            os.kill(pid, signal.SIGTERM)
            os.remove(pid_file)
            os.remove(supervisor_file) if os.path.exists(supervisor_file) else None
            stopped_local = True
            print(f"Bridge stopped (PID: {pid})")
        except ProcessLookupError:
            os.remove(pid_file)
            os.remove(supervisor_file) if os.path.exists(supervisor_file) else None
            print("Bridge was not running (stale PID file removed)")

    # Always clear the requested port. Installed projects and the source checkout
    # keep separate pid files, so a local stop can otherwise leave another bridge
    # process bound to the same port and make the next restart look healthy while
    # serving the wrong project root.
    killed_remaining = False
    health_pids: list[int] = []
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
        key = os.getenv("LITELLM_MASTER_KEY", "sk-local-codex-bridge")
        req.add_header("Authorization", f"Bearer {key}")
        health = json.loads(urllib.request.urlopen(req, timeout=2).read().decode())
        pid = int(health.get("pid") or 0)
        ppid = int(health.get("ppid") or 0)
        supervisor = health.get("supervisor") or {}
        if supervisor.get("mode") in ("daemon-supervisor", "service", "container", "external_verified"):
            if ppid > 1:
                health_pids.append(ppid)
        if pid > 1:
            health_pids.append(pid)
    except Exception:
        pass
    for pid in health_pids:
        try:
            os.kill(pid, signal.SIGTERM)
            killed_remaining = True
            print(f"Bridge stopped (PID: {pid})")
        except ProcessLookupError:
            pass
    if health_pids:
        time.sleep(0.5)

    result = subprocess.run(["lsof", "-i", f":{port}", "-t"],
                            capture_output=True, text=True)
    remaining = [p for p in result.stdout.strip().split("\n") if p.strip()]
    for pid_str in remaining:
        try:
            os.kill(int(pid_str), signal.SIGTERM)
            killed_remaining = True
            print(f"Bridge stopped (PID: {pid_str})")
        except ProcessLookupError:
            pass
    if not stopped_local and not killed_remaining:
        print(f"No bridge found on port {port}")


def _show_trace(project_root: str, mission_id: str, *, json_output: bool = False) -> int:
    """Display visible commentary trace for a mission."""
    import json as _json
    path = os.path.join(project_root, ".codex-oss", "missions", mission_id, "visible_commentary.jsonl")
    if not os.path.exists(path):
        print(f"No visible trace found for mission {mission_id}", file=sys.stderr)
        return 1
    events = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                events.append(_json.loads(line.strip()))
            except _json.JSONDecodeError:
                pass
    if json_output:
        print(_json.dumps(events, indent=2))
        return 0
    for e in events:
        phase = e.get("phase", "")
        title = e.get("title", "")
        msg = e.get("message", "")
        sev = e.get("severity", "info")
        prefix = f"[{phase}]" if phase else ""
        symbol = "!" if sev == "warning" else ("*" if sev == "error" else "")
        print(f"{symbol}{prefix} {title}")
        if msg and msg != title:
            print(f"   {msg}")
    return 0


def _show_summary(project_root: str, mission_id: str) -> int:
    """Display summary.md for a mission."""
    path = os.path.join(project_root, ".codex-oss", "missions", mission_id, "summary.md")
    if not os.path.exists(path):
        print(f"No summary found for mission {mission_id}", file=sys.stderr)
        return 1
    with open(path, "r", encoding="utf-8") as f:
        print(f.read())
    return 0


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
    except Exception as e:
        print(f"Bridge not running: {e}")
        sys.exit(1)

    uptime = data.get("uptime_seconds", 0)
    pid = data.get("pid", "?")
    runtime_id = data.get("runtime_identity", {}) or {}
    source_tree = runtime_id.get("runtime_source_tree", {}) or {}
    freshness = runtime_id.get("freshness", {}) or {}

    print(f"Bridge:            running")
    print(f"PID:               {pid}")
    print(f"Uptime:            {uptime}s")
    print(f"Version:           {data.get('bridge_version', '?')}")
    print(f"Project:           {data.get('project_root', '?')}")
    print(f"")
    if source_tree.get("fresh"):
        print(f"Runtime source:    FRESH ({source_tree.get('total_files', 0)} files)")
    else:
        changed = ", ".join(source_tree.get("changed_files", [])[:3])
        print(f"Runtime source:    STALE")
        if changed:
            print(f"Changed files:     {changed}")
        print(f"Action:            bin/codex-oss restart")
    print(f"OpenCode key:      {'present' if data.get('has_opencode_key') else 'missing'}")
    model_count = len(runtime_id.get("model_aliases", {}) or {})
    print(f"Model aliases:     {model_count}")
    super_mode = (data.get("supervisor") or {}).get("mode", "unknown")
    print(f"Supervisor:        {super_mode}")
    print(f"State DB:          {data.get('state_db', '?')}")
    if freshness.get("live_tests_allowed"):
        print(f"Live tests:        ALLOWED")
    else:
        print(f"Live tests:        BLOCKED — runtime stale")


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
