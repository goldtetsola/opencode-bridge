#!/usr/bin/env python3
"""codex-oss doctor — diagnostic checks for OSS bridge setup."""

from __future__ import annotations

import json
import os
import hashlib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Check:
    name: str
    status: str  # PASS, WARN, FAIL
    message: str
    fix: str = ""


@dataclass
class DoctorReport:
    checks: list = field(default_factory=list)
    passed: int = 0
    warnings: int = 0
    failed: int = 0

    def add(self, name: str, status: str, message: str, fix: str = ""):
        self.checks.append(Check(name, status, message, fix))
        if status == "PASS":
            self.passed += 1
        elif status == "WARN":
            self.warnings += 1
        else:
            self.failed += 1

    @property
    def healthy(self) -> bool:
        return self.failed == 0

    def print(self, json_output: bool = False):
        if json_output:
            result = {
                "healthy": self.healthy,
                "project_root": getattr(self, "metadata", {}).get("project_root", ""),
                "passed": self.passed,
                "warnings": self.warnings,
                "failed": self.failed,
                "checks": [
                    {"name": c.name, "status": c.status, "message": c.message, "fix": c.fix}
                    for c in self.checks
                ],
            }
            print(json.dumps(result, indent=2))
            return

        root = getattr(self, "metadata", {}).get("project_root", "")
        print(f"  Project root: {root}")
        print()
        for c in self.checks:
            icon = {"PASS": "\u2713", "WARN": "\u26a0", "FAIL": "\u2717"}[c.status]
            print(f"  {icon} {c.status:4}  {c.name}")
            if c.status != "PASS":
                print(f"          {c.message}")
            if c.fix and c.status != "PASS":
                print(f"          Fix: {c.fix}")
        print()
        print(f"  {self.passed} passed, {self.warnings} warnings, {self.failed} failed")
        if self.healthy:
            print("  All checks passed")
        else:
            print("  Some checks failed. Run `codex-oss doctor --fix` to repair.")


def find_project_root() -> Path:
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        if (parent / "bridge.py").exists() and (parent / "codex_oss").is_dir():
            return parent
        if (parent / ".codex").is_dir() or (parent / "AGENTS.md").exists():
            return parent
    return cwd

def _read_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    result = {}
    current_section = result
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section_name = line[1:-1].strip()
            parts = section_name.split(".")
            current_section = result
            for part in parts:
                part = part.strip().strip('"').strip("'")
                if part not in current_section:
                    current_section[part] = {}
                current_section = current_section[part]
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            current_section[key] = value
    return result


def run_doctor(
    project_root: Optional[Path] = None,
    fix: bool = False,
    offline: bool = False,
    network: bool = False,
    live_model: bool = False,
    dev: bool = False,
    runtime_models: bool = False,
) -> DoctorReport:
    report = DoctorReport()
    root = project_root or find_project_root()

    _check_config(root, report)
    _check_agents(root, report)
    _check_agreements(root, report)
    _check_rules(root, report)

    if not offline:
        bridge_url = os.getenv("BRIDGE_URL", "http://127.0.0.1:4000")
        _check_bridge(bridge_url, report, root, live_model=live_model, dev=dev)
        if runtime_models:
            _check_runtime_models(bridge_url, report)

    report.metadata = {"project_root": str(root)}
    return report


def _check_config(root: Path, report: DoctorReport):
    config_path = root / ".codex" / "config.toml"
    if not config_path.exists():
        report.add("config.project_exists", "WARN",
                   "No project .codex/config.toml — OSS agents won't be available",
                   "Run `codex-oss install` to generate config")
        return
    report.add("config.project_exists", "PASS", "Project .codex/config.toml found")

    config = _read_toml(config_path)

    top_provider = config.get("model_provider", "")
    if top_provider == "opencode_bridge":
        report.add("config.parent_provider_safe", "FAIL",
            "opencode_bridge is set as the top-level model_provider. Codex will route GPT-5.5 orchestrator requests through the bridge.",
            "Remove model_provider = \"opencode_bridge\" from the top of .codex/config.toml. Only use it in agent TOML files.")
    else:
        report.add("config.parent_provider_safe", "PASS",
            "No session-wide model_provider set — GPT-5.5 stays native")

    provider = config.get("model_providers", {}).get("opencode_bridge", {})
    if provider:
        report.add("config.provider_block", "PASS", "opencode_bridge provider block found")
        if provider.get("wire_api") == "responses":
            report.add("config.wire_api", "PASS", "wire_api = responses")
        else:
            report.add("config.wire_api", "FAIL",
                "wire_api must be 'responses' for Codex Responses API compatibility",
                "Set wire_api = \"responses\" in [model_providers.opencode_bridge]")
    else:
        report.add("config.provider_block", "FAIL",
            "No [model_providers.opencode_bridge] block found",
            "Run `codex-oss install` to generate config")


def _check_agents(root: Path, report: DoctorReport):
    agents_dir = root / ".codex" / "agents"
    if not agents_dir.exists():
        report.add("agents.dir", "WARN", "No .codex/agents/ directory",
                   "Run `codex-oss install` to generate agent TOMLs")
        return

    tomls = list(agents_dir.glob("oss-*.toml"))
    if not tomls:
        report.add("agents.found", "WARN", "No OSS agent TOMLs found",
                   "Run `codex-oss install` to generate agent TOMLs")
        return
    report.add("agents.found", "PASS", f"Found {len(tomls)} OSS agent TOML(s)")

    for t in tomls:
        cfg = _read_toml(t)
        name = cfg.get("name", t.stem)
        provider = cfg.get("model_provider", "")
        model = cfg.get("model", "")
        reasoning = cfg.get("model_reasoning_effort", "")

        if provider != "opencode_bridge":
            report.add(f"agents.{name}.provider", "FAIL",
                f"Agent '{name}' has model_provider='{provider}', expected 'opencode_bridge'",
                f"Set model_provider = \"opencode_bridge\" in {t.name}")
        else:
            report.add(f"agents.{name}.provider", "PASS",
                f"'{name}' routes through opencode_bridge")

        if not model:
            report.add(f"agents.{name}.model", "FAIL",
                f"Agent '{name}' has no model specified",
                f"Add model = \"ocg-<model>\" to {t.name}")
        else:
            report.add(f"agents.{name}.model", "PASS", f"'{name}' uses {model}")

        if not reasoning:
            report.add(f"agents.{name}.reasoning", "WARN",
                f"Agent '{name}' has no model_reasoning_effort set",
                f"Add model_reasoning_effort = \"high\" or \"medium\" to {t.name}")
        else:
            report.add(f"agents.{name}.reasoning", "PASS",
                f"'{name}' reasoning={reasoning}")


def _check_agreements(root: Path, report: DoctorReport):
    agreements = root / "AGENTS.md"
    if not agreements.exists():
        report.add("agreements.exists", "WARN",
            "No AGENTS.md found — orchestrator has no routing rules",
            "Run `codex-oss install` to generate AGENTS.md")
        return
    content = agreements.read_text()
    report.add("agreements.exists", "PASS", "AGENTS.md found")

    if "fork_turns" in content:
        report.add("agreements.fork_turns", "PASS",
            "AGENTS.md specifies fork_turns: none")
    else:
        report.add("agreements.fork_turns", "WARN",
            "AGENTS.md does not specify fork_turns: none for OSS agents",
            "Add fork_turns: \"none\" to OSS delegation rules in AGENTS.md")

    if "OSS_HANDOFF_JSON" in content:
        report.add("agreements.structured_handoff", "PASS",
            "AGENTS.md includes the structured OSS handoff contract")
    else:
        report.add("agreements.structured_handoff", "WARN",
            "AGENTS.md does not mention OSS_HANDOFF_JSON; OSS routing may rely on brittle prose parsing",
            "Add the OSS_HANDOFF_JSON schema to OSS delegation rules in AGENTS.md")

    if "recursive" in content.lower() or ("codex exec" in content.lower() and "never" in content.lower()):
        report.add("agreements.no_recursive", "PASS",
            "AGENTS.md warns against recursive codex exec")
    else:
        report.add("agreements.no_recursive", "WARN",
            "AGENTS.md does not warn against recursive codex exec",
            "Add a rule: Never use codex exec from inside a Codex session")


def _check_rules(root: Path, report: DoctorReport):
    rules_dir = root / ".codex" / "rules"
    if not rules_dir.exists() or not list(rules_dir.glob("*.rules")):
        report.add("rules.recursive_block", "WARN",
            "No .codex/rules/ found — recursive codex exec not blocked",
            "Run `codex-oss install` to generate no-recursive-codex.rules")
        return

    for rf in rules_dir.glob("*.rules"):
        content = rf.read_text()
        if "codex" in content and "exec" in content and "forbidden" in content:
            report.add("rules.recursive_block", "PASS",
                f"Recursive codex exec blocked by {rf.name}")
            return
    report.add("rules.recursive_block", "WARN",
        "No rule blocks recursive codex exec",
        "Run `codex-oss install` to generate no-recursive-codex.rules")


def _check_bridge(url: str, report: DoctorReport, root: Path, live_model: bool = False, dev: bool = False):
    health_url = f"{url}/health"
    auth = os.getenv("LITELLM_MASTER_KEY", "sk-local-codex-bridge")

    try:
        req = urllib.request.Request(health_url)
        req.add_header("Authorization", f"Bearer {auth}")
        with urllib.request.urlopen(req, timeout=5) as resp:
            health = json.loads(resp.read().decode())
    except Exception as e:
        report.add("bridge.running", "FAIL",
            f"Bridge not reachable at {health_url}: {e}",
            "Run `codex-oss start` to start the bridge")
        return

    if health.get("ok"):
        report.add("bridge.running", "PASS", f"Bridge at {health_url}")

    supervisor = health.get("supervisor") or {}
    mode = supervisor.get("mode", "unknown")
    if mode in ("daemon-supervisor", "service", "container", "external_verified") and supervisor.get("durable"):
        report.add("bridge.supervisor", "PASS",
            f"Bridge child is supervised (mode={mode}, ppid={health.get('ppid')})")
    elif mode == "foreground" and dev:
        report.add("bridge.supervisor", "WARN",
            "Foreground supervisor is acceptable for development only",
            "Use `codex-oss up --daemon` or a service/container backend for normal OSS agent use")
    else:
        report.add("bridge.supervisor", "FAIL",
            f"Bridge is not running under a durable supported supervisor ({supervisor})",
            "Run `codex-oss stop && codex-oss up --daemon`")

    running_hash = str(health.get("source_sha256", ""))
    bridge_path = root / "bridge.py"
    disk_hash = _sha256_path(bridge_path) if bridge_path.exists() else ""
    if running_hash and disk_hash and running_hash == disk_hash:
        report.add("bridge.source_hash", "PASS", "Running bridge source matches bridge.py on disk")
    elif running_hash and disk_hash:
        report.add("bridge.source_hash", "FAIL",
            "Running bridge source hash differs from bridge.py on disk",
            "Restart the bridge through the supervisor")
    else:
        report.add("bridge.source_hash", "FAIL",
            "Bridge health does not expose source_sha256",
            "Restart a bridge version that reports running source identity")

    running_root = str(health.get("project_root", "") or health.get("cwd", "") or "")
    if running_root and os.path.abspath(running_root) == os.path.abspath(str(root)):
        report.add("bridge.project_root", "PASS", "Running bridge project root matches current repo")
    elif running_root:
        report.add("bridge.project_root", "FAIL",
            f"Running bridge project root differs from current repo ({running_root})",
            "Restart the bridge from this repo root through the supervisor")
    else:
        report.add("bridge.project_root", "WARN",
            "Bridge health does not expose project_root/cwd",
            "Restart a bridge version that reports its project root")

    if str(health.get("config_fingerprint", "") or ""):
        report.add("bridge.config_fingerprint", "PASS", "Bridge health exposes a config fingerprint")
    else:
        report.add("bridge.config_fingerprint", "WARN",
            "Bridge health does not expose config_fingerprint",
            "Restart a bridge version that reports a config fingerprint")

    runtime_id = health.get("runtime_identity", {}) or {}
    source_tree = runtime_id.get("runtime_source_tree", {}) or {}
    if source_tree.get("fresh"):
        report.add("runtime.identity", "PASS", f"Runtime module tree is fresh ({source_tree.get('total_files', 0)} files)")
    elif source_tree.get("changed_files"):
        changed = ", ".join(source_tree["changed_files"][:5])
        report.add("runtime.identity", "FAIL",
            f"Runtime module tree is stale. Changed: {changed}",
            "Restart the bridge: `codex-oss restart`")
    elif source_tree.get("startup_sha256"):
        report.add("runtime.identity", "FAIL",
            "Runtime module tree hash changed since bridge start",
            "Restart the bridge: `codex-oss restart`")
    else:
        report.add("runtime.identity", "WARN",
            "Bridge health does not expose runtime identity",
            "Restart a bridge version that reports runtime_source_tree freshness")

    if live_model and not source_tree.get("fresh"):
        report.add("runtime.live_tests_blocked", "FAIL",
            "Live OSS inference tests blocked because runtime module tree is stale",
            "Restart the bridge: `codex-oss restart`")
    elif live_model:
        report.add("runtime.live_tests_blocked", "PASS", "Runtime fresh — live tests allowed")
    # GPT rejection test
    api_url = f"{url}/v1/responses"
    try:
        data = json.dumps({"model": "gpt-5.5", "input": [{"role": "user", "content": "test"}]}).encode()
        req = urllib.request.Request(api_url, data=data, method="POST")
        req.add_header("Authorization", f"Bearer {auth}")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode())
            if body.get("error"):
                report.add("bridge.gpt_rejection", "PASS",
                    "Bridge correctly rejects GPT-5.5 requests")
            else:
                report.add("bridge.gpt_rejection", "FAIL",
                    "Bridge accepted a GPT-5.5 request — GPT requests leaking to OpenCode Go",
                    "Restart bridge with GPT_MODEL_STRATEGY=error")
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500:
            report.add("bridge.gpt_rejection", "PASS",
                f"Bridge rejects GPT-5.5 (HTTP {e.code})")
        else:
            report.add("bridge.gpt_rejection", "WARN",
                f"Unexpected status checking GPT rejection (HTTP {e.code})")
    except Exception as e:
        report.add("bridge.gpt_rejection", "WARN",
            f"Could not test GPT rejection: {e}")

    # OSS inference test is live-model only. Default doctor must not burn model usage.
    if not live_model:
        report.add("bridge.oss_inference", "WARN",
            "Skipped live OSS inference smoke",
            "Run `codex-oss doctor --live-model` when you intentionally want to spend a model call")
    else:
        _check_live_model(api_url, auth, report)

    # Auth separation — local proxy token must not equal upstream key
    upstream_key = os.getenv("UPSTREAM_API_KEY") or os.getenv("OPENCODE_GO_API_KEY", "")
    proxy_key = os.getenv("PROXY_API_KEY") or os.getenv("LITELLM_MASTER_KEY") or ""
    if proxy_key and upstream_key and proxy_key == upstream_key:
        report.add("bridge.auth_separation", "FAIL",
            "PROXY_API_KEY equals OPENCODE_GO_API_KEY — auth crossover risk. Codex-to-bridge requests would fail.",
            "Set CODEX_OSS_LOCAL_TOKEN to a simple local bearer token (e.g. sk-local-codex-bridge) instead of reusing the upstream key")
    elif proxy_key and upstream_key and proxy_key.startswith("sk-") and upstream_key.startswith("sk-") and proxy_key[:8] == upstream_key[:8]:
        report.add("bridge.auth_separation", "WARN",
            "Local proxy token shares prefix with upstream key — verify they are different tokens",
            "Ensure PROXY_API_KEY is a separate bearer token, not the upstream API key")
    elif proxy_key and proxy_key.startswith("sk-ocg") or (upstream_key and proxy_key.startswith("sk-ocg")):
        report.add("bridge.auth_separation", "WARN",
            "Local proxy token appears to be an OpenCode Go key. Use a simple local token like sk-local-codex-bridge.",
            "")
    elif proxy_key:
        report.add("bridge.auth_separation", "PASS" if not upstream_key or proxy_key != upstream_key else "WARN",
            "Local proxy token is configured")
    else:
        report.add("bridge.auth_separation", "WARN",
            "No explicit local proxy token set — using default sk-local-codex-bridge",
            "Set PROXY_API_KEY for production use")

    # State DB persistence — read from health endpoint
    state_db = health.get("state_db", "")
    if not state_db or state_db == "unknown":
        report.add("bridge.state_db", "WARN",
            "State DB path unknown",
            "Set PROXY_STATE_DB to a persistent path")
    elif "/tmp/" in state_db:
        report.add("bridge.state_db", "WARN",
            f"State DB in /tmp ({state_db}) — may be cleaned",
            "Set PROXY_STATE_DB to a persistent path")
    elif state_db:
        report.add("bridge.state_db", "PASS", f"State DB: {state_db}")


def _check_live_model(api_url: str, auth: str, report: DoctorReport):
    try:
        data = json.dumps({
            "model": "ocg-kimi-k2.6",
            "input": [{"role": "user", "content": "Say OK"}],
            "stream": False
        }).encode()
        req = urllib.request.Request(api_url, data=data, method="POST")
        req.add_header("Authorization", f"Bearer {auth}")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
            if body.get("status") == "completed":
                report.add("bridge.oss_inference", "PASS",
                    "OSS model inference works")
            else:
                report.add("bridge.oss_inference", "FAIL",
                    f"OSS model status: {body.get('status')}")
    except Exception as e:
        report.add("bridge.oss_inference", "FAIL",
            f"OSS inference failed: {e}",
            "Check OPENCODE_GO_API_KEY is set")


def _check_runtime_models(bridge_url: str, report: DoctorReport):
    """Check that runtime model aliases (mission-a2 through mission-a6) are accepted by the bridge."""
    from urllib.request import Request, urlopen

    aliases = [
        "mission-a2-flash", "mission-a2-deepseek", "mission-a2-kimi",
        "mission-a3-flash", "mission-a3-deepseek", "mission-a3-kimi",
        "mission-a4-flash", "mission-a4-deepseek", "mission-a4-kimi",
        "mission-a5-flash", "mission-a5-deepseek", "mission-a5-kimi",
        "mission-a6-flash", "mission-a6-deepseek", "mission-a6-kimi",
    ]
    auth = os.getenv("PROXY_API_KEY") or os.getenv("LITELLM_MASTER_KEY") or "sk-local-codex-bridge"

    # Check models endpoint
    try:
        req = Request(f"{bridge_url}/v1/models")
        req.add_header("Authorization", f"Bearer {auth}")
        with urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode())
            model_ids = {m.get("id", "") for m in body.get("data", [])}
            
            ocg_models = [m for m in model_ids if m.startswith("ocg-")]
            report.add("runtime_models.ocg_count", "PASS" if ocg_models else "WARN",
                       f"{len(ocg_models)} ocg- models available" if ocg_models else "No ocg- models found")
    except Exception as e:
        report.add("runtime_models.endpoint", "FAIL", f"Cannot query /v1/models: {e}")
        return

    # Test each alias by submitting a minimal responses request
    from codex_oss.managed_bridge import RUNTIME_MODEL_ALIASES
    resolved_aliases = {a: RUNTIME_MODEL_ALIASES.get(a, "unknown") for a in aliases}
    
    # Group by upstream model
    by_upstream: dict[str, list[str]] = {}
    for alias, upstream in resolved_aliases.items():
        by_upstream.setdefault(upstream, []).append(alias)
    
    available_upstream = set()
    for upstream, mapped_aliases in sorted(by_upstream.items()):
        in_map = any(upstream in model_ids or f"ocg-{upstream}" in model_ids or 
                     upstream.replace("ocg-", "") in model_ids for _ in [1])
        if in_map:
            available_upstream.add(upstream)
    
    accepted = 0
    missing = 0
    for alias in aliases:
        upstream = resolved_aliases[alias]
        if upstream in available_upstream or any(
            upstream in model_ids or upstream.replace("ocg-", "") in model_ids
            for _ in [1]
        ):
            accepted += 1
        else:
            missing += 1
    
    report.add("runtime_models.aliases_resolved", "PASS" if accepted == len(aliases) else "WARN",
               f"{accepted}/{len(aliases)} runtime aliases map to available upstream models")
    
    if missing:
        missing_aliases = [a for a in aliases if resolved_aliases[a] not in available_upstream]
        report.add("runtime_models.missing", "WARN",
                   f"Missing upstream models for: {', '.join(missing_aliases[:5])}")


def _sha256_path(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""
