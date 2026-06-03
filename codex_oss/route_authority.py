"""RouteAuthorityV1 — claim/provenance classification for OSS subagents.

This module separates bridge-local native UX evidence from Codex Desktop
spawned-agent evidence. It is intentionally small and pure so it can be used
by burn-ins, transcript verifiers, and report gates without importing the
bridge runtime.
"""

from __future__ import annotations

import json
from typing import Any

JSON = dict[str, Any]

ROUTE_AUTHORITY_SCHEMA_VERSION = "route_authority.v1"

ROUTE_KINDS = ["managed_mission", "raw_direct"]
HANDOFF_SCHEMAS = ["mission_v1", "generic_raw", "missing", "invalid"]
CONSUMER_KINDS = ["direct_bridge_harness", "codex_desktop_spawned"]
AUTHORITY_MODES = ["runtime_truth", "raw_model_narrative_only"]
NATIVE_CLAIM_SCOPES = ["desktop_allowed", "bridge_only", "none"]


def classify_model_route(model_alias: str) -> str:
    alias = str(model_alias or "").strip()
    if alias.startswith("mission-"):
        return "managed_mission"
    return "raw_direct"


def classify_handoff_schema(handoff_text: str | None = None, handoff_obj: JSON | None = None) -> str:
    """Classify the authority carried by an OSS_HANDOFF_JSON representation."""
    obj = handoff_obj
    if obj is None and handoff_text is not None:
        obj = _extract_first_handoff_json(handoff_text)
        if obj is None:
            try:
                parsed = json.loads(str(handoff_text))
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                obj = parsed
        if obj is None:
            return "invalid" if "OSS_HANDOFF_JSON" in str(handoff_text) else "missing"

    if obj is None:
        return "missing"
    if not isinstance(obj, dict):
        return "invalid"
    schema = obj.get("schema_version")
    if schema == "oss_agent_mission.v1":
        return "mission_v1"
    if {"mission_id", "tier", "mode", "objective"}.issubset(set(obj.keys())):
        return "mission_v1"
    if schema == 1:
        return "generic_raw"
    return "invalid"


def build_route_authority(
    *,
    agent_name: str = "",
    model_alias: str = "",
    handoff_text: str | None = None,
    handoff_obj: JSON | None = None,
    consumer_kind: str = "direct_bridge_harness",
) -> JSON:
    """Build a RouteAuthorityV1 record with total claim classification.

    Rules:
    - mission-* routes are runtime truth only with MissionV1 handoffs.
    - raw ocg/oss routes are model narrative only and cannot claim Desktop Gold.
    - direct harness evidence can only support bridge-local claims.
    """
    route_kind = classify_model_route(model_alias)
    handoff_schema = classify_handoff_schema(handoff_text, handoff_obj)
    consumer = consumer_kind if consumer_kind in CONSUMER_KINDS else "direct_bridge_harness"

    if route_kind == "managed_mission" and handoff_schema == "mission_v1":
        authority_mode = "runtime_truth"
        native_claim_scope = "desktop_allowed" if consumer == "codex_desktop_spawned" else "bridge_only"
    elif route_kind == "raw_direct" and consumer == "direct_bridge_harness":
        authority_mode = "raw_model_narrative_only"
        native_claim_scope = "bridge_only"
    else:
        authority_mode = "raw_model_narrative_only"
        native_claim_scope = "none"

    return {
        "schema_version": ROUTE_AUTHORITY_SCHEMA_VERSION,
        "agent_name": str(agent_name or ""),
        "model_alias": str(model_alias or ""),
        "route_kind": route_kind,
        "handoff_schema": handoff_schema,
        "consumer_kind": consumer,
        "authority_mode": authority_mode,
        "native_claim_scope": native_claim_scope,
        "native_claim_allowed": native_claim_scope != "none",
        "desktop_native_claim_allowed": native_claim_scope == "desktop_allowed" and consumer == "codex_desktop_spawned",
    }


def route_allows_bridge_gold(route_authority: JSON | None) -> bool:
    if not isinstance(route_authority, dict):
        return True
    return str(route_authority.get("native_claim_scope", "") or "") in {"bridge_only", "desktop_allowed"}


def route_allows_desktop_gold(route_authority: JSON | None) -> bool:
    if not isinstance(route_authority, dict):
        return False
    return bool(route_authority.get("desktop_native_claim_allowed"))


def _extract_first_handoff_json(text: str) -> JSON | None:
    marker = "OSS_HANDOFF_JSON"
    idx = text.find(marker)
    if idx < 0:
        return None
    tail = text[idx + len(marker):].lstrip()
    if tail.startswith(":"):
        tail = tail[1:].lstrip()
    if tail.startswith(">"):
        tail = tail[1:].lstrip()
    if tail.startswith("```"):
        lines = tail.splitlines()
        tail = "\n".join(lines[1:]) if lines else ""
        fence_idx = tail.find("```")
        if fence_idx >= 0:
            tail = tail[:fence_idx]
    try:
        obj, _ = json.JSONDecoder().raw_decode(tail)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None
