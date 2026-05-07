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

    return {
        "ok": True,
        "service": "responses-chat-proxy",
        "bridge_version": bridge_version,
        "time": int(time.time()),
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "uptime_seconds": int(time.time() - start_time),
        "argv": sys.argv,
        "source_path": os.path.abspath(bridge_path),
        "source_sha256": sha256_file(os.path.abspath(bridge_path)),
        "supervisor": {
            "mode": os.getenv("CODEX_OSS_SUPERVISOR_MODE", "unknown"),
            "durable": os.getenv("CODEX_OSS_SUPERVISOR_MODE", "") != "",
        },
        "gpt_model_strategy": app.gpt_model_strategy,
        "upstream_stream": getattr(app, "upstream_streaming", True),
        "has_opencode_key": bool(app.upstream_key),
        "state_db": getattr(app.state, "path", os.getenv("PROXY_STATE_DB", "unknown")),
        "model_health": model_health,
        "concurrency": {
            "max_global": app.max_global_concurrency,
        },
        "transport_contract": {
            "stream_terminal_guarantee": True,
        },
    }


def sha256_file(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return "unknown"
