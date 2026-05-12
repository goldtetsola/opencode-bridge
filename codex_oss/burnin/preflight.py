"""BurninPreflightV1 — runtime freshness, auth, supervision, model alias gates.

No live burn-in runs unless all preflight checks pass.
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field
from typing import Any

JSON = dict[str, Any]


@dataclass
class PreflightResult:
    ok: bool = False
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    runtime_identity: JSON = field(default_factory=dict)
    required_aliases: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> JSON:
        return {
            "ok": self.ok,
            "failures": self.failures,
            "warnings": self.warnings,
            "runtime_identity": self.runtime_identity,
            "required_aliases": self.required_aliases,
        }


def run_burnin_preflight(
    project_root: str,
    suite: str,
    required_aliases: list[str] | None = None,
    bridge_url: str = "http://127.0.0.1:4000",
    auth: str = "",
) -> PreflightResult:
    result = PreflightResult()
    expected_root = os.path.abspath(project_root)

    health = _fetch_health(bridge_url, auth)
    if not health:
        result.failures.append("Bridge not reachable at " + bridge_url)
        return result

    # Runtime freshness
    runtime_id = health.get("runtime_identity", {}) or {}
    source_tree = runtime_id.get("runtime_source_tree", {}) or {}
    if not source_tree.get("fresh", False):
        changed = ", ".join(source_tree.get("changed_files", [])[:3])
        result.failures.append(f"Runtime source tree is stale. Changed: {changed or 'unknown'}")
    else:
        result.runtime_identity = runtime_id

    # Supervised
    supervisor = health.get("supervisor", {}) or {}
    mode = supervisor.get("mode", "unknown")
    durable = supervisor.get("durable", False)
    if mode not in ("daemon-supervisor", "service", "container", "external_verified") or not durable:
        result.failures.append(f"Bridge not under supported supervisor (mode={mode}, durable={durable})")
    else:
        if mode == "foreground":
            result.warnings.append("Foreground supervisor is acceptable for dev only")

    # Project root
    running_root = str(health.get("project_root", "") or health.get("cwd", "") or "")
    if running_root and os.path.abspath(running_root) != expected_root:
        result.failures.append(f"Bridge project root ({running_root}) does not match expected ({expected_root})")

    # OpenCode key
    if not health.get("has_opencode_key", False):
        result.failures.append("OpenCode Go API key missing")

    # Model aliases
    aliases = runtime_id.get("model_aliases", {}) or {}
    required = required_aliases or ["mission-a3-kimi"]
    for alias in required:
        if alias in aliases:
            result.required_aliases[alias] = True
        else:
            result.required_aliases[alias] = False
            result.failures.append(f"Required model alias {alias} not found")

    result.ok = not result.failures
    return result


def _fetch_health(bridge_url: str, auth: str) -> JSON | None:
    bearer = auth or os.getenv("LITELLM_MASTER_KEY", "sk-local-codex-bridge")
    health_url = bridge_url.rstrip("/") + "/health"
    try:
        req = urllib.request.Request(health_url)
        req.add_header("Authorization", f"Bearer {bearer}")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None
