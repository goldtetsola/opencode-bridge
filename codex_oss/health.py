"""Health response builder for the bridge HTTP surface."""

from __future__ import annotations

import hashlib
import os
import sys
import time
from typing import Any, Dict

JSON = Dict[str, Any]


def build_health_status(app: Any, bridge_version: str, bridge_path: str, start_time: float) -> JSON:
    model_health = {}
    for model, health in getattr(app, "model_health", {}).items():
        model_health[model] = {
            "status": health.get("status", "ok"),
            "errors": health.get("errors", 0),
            "since": health.get("since", 0),
        }

    project_root = os.getcwd()
    source_path = os.path.abspath(bridge_path)
    source_sha = sha256_file(source_path)
    state_db = getattr(app.state, "path", os.getenv("PROXY_STATE_DB", "unknown"))
    supervisor_mode = os.getenv("CODEX_OSS_SUPERVISOR_MODE", "unknown")
    config_fingerprint = hashlib.sha256(
        f"{project_root}|{source_path}|{source_sha}|{state_db}|{supervisor_mode}".encode("utf-8")
    ).hexdigest()[:16]
    return {
        "ok": True,
        "service": "responses-chat-proxy",
        "bridge_version": bridge_version,
        "time": int(time.time()),
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "uptime_seconds": int(time.time() - start_time),
        "argv": sys.argv,
        "source_path": source_path,
        "source_sha256": source_sha,
        "project_root": project_root,
        "cwd": project_root,
        "runtime_identity": _build_runtime_identity(project_root),
        "supervisor": {
            "mode": supervisor_mode,
            "durable": supervisor_mode != "",
        },
        "gpt_model_strategy": app.gpt_model_strategy,
        "upstream_stream": getattr(app, "upstream_streaming", True),
        "has_opencode_key": bool(app.upstream_key),
        "state_db": state_db,
        "config_fingerprint": config_fingerprint,
        "model_health": model_health,
        "concurrency": {
            "max_global": app.max_global_concurrency,
        },
        "transport_contract": {
            "stream_terminal_guarantee": True,
        },
    }


def _build_runtime_identity(project_root: str) -> dict:
    from codex_oss.runtime_manifest import runtime_identity as _identity
    try:
        return _identity(project_root)
    except Exception:
        return {"runtime_identity_version": "1.0", "error": "failed to compute runtime identity"}


def sha256_file(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return "unknown"
