"""OSSFinalClaimGateV1 — terminal claim authority gate.

The gate is intentionally independent from transport details. Callers pass the
authority/provenance, pending-tool, implementation-witness, transcript, and
artifact facts they have; the gate returns whether the requested terminal claim
is allowed and which status/scope may be published.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from codex_oss.route_authority import route_allows_desktop_gold

JSON = dict[str, Any]


SUCCESS_STATUSES = {"PASS", "COMPLETE", "VERIFIED", "GOLD", "DESKTOP_GOLD"}


def evaluate_final_claim_gate(
    *,
    claim_type: str,
    requested_status: str,
    route_authority: JSON | None = None,
    mission_id: str = "",
    runtime_admission_id: str = "",
    response_ids: list[str] | None = None,
    pending_call_ids: list[str] | None = None,
    recovery_used: bool = False,
    recovered_call_ids: list[str] | None = None,
    implementation_witness: JSON | None = None,
    consumer_observation_witness: JSON | None = None,
    desktop_render_surface: JSON | None = None,
    changed_owned_paths: list[str] | None = None,
    transcript_path: str = "",
    artifact_paths: list[str] | None = None,
) -> JSON:
    status = str(requested_status or "").upper()
    claim = str(claim_type or "").strip() or "unknown"
    route = route_authority if isinstance(route_authority, dict) else {}
    pending = [str(item) for item in (pending_call_ids or []) if str(item)]
    recovered = set(str(item) for item in (recovered_call_ids or []) if str(item))
    unresolved = [call_id for call_id in pending if call_id not in recovered]
    reasons: list[str] = []

    if status in SUCCESS_STATUSES and unresolved:
        reasons.append("pending_tool_calls_unresolved:" + ",".join(unresolved))

    if claim in {"desktop_gold", "desktop_native"}:
        if not route_allows_desktop_gold(route):
            reasons.append("desktop_route_authority_missing")
        witness = consumer_observation_witness if isinstance(consumer_observation_witness, dict) else {}
        if not witness.get("ok"):
            reasons.append("desktop_consumer_observation_witness_missing_or_failed")
            witness_reasons = witness.get("reasons", []) if isinstance(witness.get("reasons", []), list) else []
            for reason in witness_reasons:
                if str(reason):
                    reasons.append(f"desktop_witness:{reason}")
        if not transcript_path:
            reasons.append("desktop_transcript_hash_missing")
        surface = desktop_render_surface if isinstance(desktop_render_surface, dict) else {}
        probe_status = str(surface.get("probe_status") or "unknown").lower()
        if probe_status != "pass":
            reasons.append(f"desktop_pre_final_text_probe_not_passed:{probe_status or 'unknown'}")

    if claim in {"implementation", "raw_implementation", "bounded_implementation"}:
        witness = implementation_witness if isinstance(implementation_witness, dict) else {}
        if status in {"PASS", "VERIFIED"} and not witness.get("ok"):
            reasons.append("implementation_witness_missing_or_failed")
        if status in {"PASS", "VERIFIED"} and not (changed_owned_paths or []):
            explicit_noop = bool(witness.get("explicit_noop"))
            if not explicit_noop:
                reasons.append("changed_owned_paths_empty_for_success")

    native_scope = str(route.get("native_claim_scope", "") or "")
    if claim in {"desktop_gold", "desktop_native"}:
        effective_scope = "desktop_allowed" if not reasons else "none"
    elif native_scope:
        effective_scope = native_scope if not reasons else "none"
    else:
        effective_scope = "raw_research_only" if not reasons else "none"

    effective_status = status
    if reasons and status in SUCCESS_STATUSES:
        effective_status = "PARTIAL" if recovery_used else "FAIL"

    publication_decision = _publication_decision(status, reasons)
    implementation_decision = _implementation_decision(claim, status, reasons)

    return {
        "schema_version": "oss_final_claim_gate.v1",
        "ok": not reasons,
        "claim_type": claim,
        "route_kind": str(route.get("route_kind", "") or "raw_direct"),
        "consumer_kind": str(route.get("consumer_kind", "") or "direct_bridge_harness"),
        "requested_status": status,
        "effective_status": effective_status,
        "effective_claim_scope": effective_scope,
        "effective_scope": effective_scope,
        "publication_decision": publication_decision,
        "implementation_decision": implementation_decision,
        "claim_tuple": {
            "claim_type": claim,
            "route_kind": str(route.get("route_kind", "") or "raw_direct"),
            "consumer_kind": str(route.get("consumer_kind", "") or "direct_bridge_harness"),
            "effective_status": effective_status,
            "effective_scope": effective_scope,
            "reasons": list(reasons),
        },
        "mission_id": mission_id,
        "runtime_admission_id": runtime_admission_id,
        "response_ids": list(response_ids or []),
        "route_authority": route,
        "pending_call_ids": pending,
        "recovered_call_ids": sorted(recovered),
        "implementation_witness_ok": bool((implementation_witness or {}).get("ok")) if isinstance(implementation_witness, dict) else False,
        "consumer_observation_witness_ok": bool((consumer_observation_witness or {}).get("ok")) if isinstance(consumer_observation_witness, dict) else False,
        "consumer_observation_witness": dict(consumer_observation_witness or {}) if isinstance(consumer_observation_witness, dict) else {},
        "desktop_render_surface": dict(desktop_render_surface or {}) if isinstance(desktop_render_surface, dict) else {},
        "changed_owned_paths": list(changed_owned_paths or []),
        "transcript_hash": _hash_file(transcript_path) if transcript_path else "",
        "artifact_hashes": {path: _hash_file(path) for path in (artifact_paths or []) if path},
        "reasons": reasons,
    }


def _publication_decision(status: str, reasons: list[str]) -> str:
    if status in SUCCESS_STATUSES:
        return "success_claim_allowed" if not reasons else "success_claim_denied"
    return "allowed_to_report_failure" if not reasons else "failure_report_has_unresolved_claims"


def _implementation_decision(claim: str, status: str, reasons: list[str]) -> str:
    if claim not in {"implementation", "raw_implementation", "bounded_implementation"}:
        return "implementation_success_not_requested"
    if status not in {"PASS", "VERIFIED"}:
        return "implementation_success_denied"
    return "implementation_success_allowed" if not reasons else "implementation_success_denied"


def _hash_file(path: str) -> str:
    try:
        p = Path(path)
        if not p.exists() or not p.is_file():
            return ""
        digest = hashlib.sha256()
        with p.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return "sha256:" + digest.hexdigest()
    except OSError:
        return ""
