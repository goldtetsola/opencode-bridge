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
    model_inference: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> JSON:
        return {
            "ok": self.ok,
            "failures": self.failures,
            "warnings": self.warnings,
            "runtime_identity": self.runtime_identity,
            "required_aliases": self.required_aliases,
            "model_inference": self.model_inference,
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

    if not os.getenv("OSS_BURNIN_SKIP_LIVE_MODEL_PREFLIGHT"):
        ok, message = _check_live_model_inference(bridge_url, auth)
        result.model_inference = {"ok": ok, "message": message}
        if not ok:
            result.failures.append(f"Live OSS model inference failed: {message}")
    else:
        result.warnings.append("Skipped live OSS model inference preflight")

    result.ok = not result.failures
    return result


def lint_burnin_contracts(cases: list[JSON]) -> list[str]:
    """Validate that burn-in case expectations don't contradict mission contracts."""
    errors = []
    for case in cases:
        case_id = case.get("case_id", "unknown")
        mission = case.get("mission", {}) or {}
        exploration = mission.get("exploration_policy", {}) or {}
        require_contradiction = bool(exploration.get("require_contradiction_search", False))
        min_optional = int(exploration.get("min_optional_actions_after_floor", 0) or 0)
        after_floor = str(exploration.get("after_required_floor", "") or "")

        expected = case.get("expected_outcome", {}) or {}
        exp_final = expected.get("expected_final_statuses", []) or []
        exp_verify = expected.get("expected_verification_statuses", []) or []
        exp_status = str(case.get("expected_requires_closure", "") or "").upper()

        # If COMPLETE expected but mission requires contradiction search
        if "COMPLETE" in (exp_final or [exp_status]) and require_contradiction and after_floor != "close_immediately":
            if "SATISFIED" not in (exp_verify or []):
                errors.append(
                    f"{case_id}: expects COMPLETE but mission requires contradiction_search "
                    f"({after_floor}). Expected verification_statuses should include SATISFIED or mission should use close_immediately."
                )

        # If COMPLETE expected but mission requires optional exploration
        if "COMPLETE" in (exp_final or [exp_status]) and min_optional > 0:
            if "SATISFIED" not in (exp_verify or []):
                errors.append(
                    f"{case_id}: expects COMPLETE but mission requires min_optional={min_optional}. "
                    f"Expected verification_statuses should include SATISFIED or mission should disable optional exploration."
                )

    return errors


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


def _check_live_model_inference(bridge_url: str, auth: str) -> tuple[bool, str]:
    bearer = auth or os.getenv("LITELLM_MASTER_KEY", "sk-local-codex-bridge")
    api_url = bridge_url.rstrip("/") + "/v1/responses"
    payload = {
        "model": "ocg-kimi-k2.6",
        "input": [{"role": "user", "content": "Say OK"}],
        "stream": False,
    }
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(api_url, data=data, method="POST")
        req.add_header("Authorization", f"Bearer {bearer}")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        status = str(body.get("status", "") or "")
        if status == "completed":
            return True, "OSS model inference works"
        return False, f"unexpected status {status or 'unknown'}"
    except Exception as exc:
        return False, str(exc)
