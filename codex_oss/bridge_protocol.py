"""BridgeProxyProtocolV1 checks for native OSS child routes."""

from __future__ import annotations

from typing import Any, Mapping

JSON = dict[str, Any]


def validate_bridge_route(*, parent_provider: str, child_model_alias: str, recursive_codex: bool = False) -> JSON:
    failures: list[str] = []
    alias = str(child_model_alias or "").lower()
    if parent_provider == "opencode_bridge":
        failures.append("parent_provider_leak")
    if recursive_codex:
        failures.append("recursive_codex_execution")
    if alias.startswith("gpt-"):
        failures.append("gpt_family_child_route")
    return {"schema_version": "bridge_proxy_protocol.v1", "ok": not failures, "failures": failures}


def user_visible_stream_frame(frame: Mapping[str, Any]) -> JSON:
    return {
        "schema_version": "bridge_stream_frame.v1",
        "type": str(frame.get("type") or "progress"),
        "message": str(frame.get("message") or ""),
        "heartbeat": bool(frame.get("heartbeat", False)),
    }
