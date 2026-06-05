"""Fail-closed verifier for Codex Desktop spawned-agent native UX evidence."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from codex_oss.desktop_observation import build_consumer_observation_witness, classify_transcript_provenance
from codex_oss.final_claim_gate import evaluate_final_claim_gate
from codex_oss.route_authority import route_allows_desktop_gold

JSON = dict[str, Any]


def verify_desktop_native_ux(
    *,
    transcript_path: str,
    mission_id: str,
    project_root: str,
    route_authority: JSON | None = None,
) -> JSON:
    """Verify actual Codex Desktop spawned transcript evidence.

    This verifier does not run a local bridge harness. Missing transcript or
    missing desktop provenance fails closed as desktop_native_unproven.
    """
    path = Path(transcript_path)
    checks: list[JSON] = []
    if not path.exists():
        return _report(False, mission_id, transcript_path, checks, ["desktop_native_unproven", "transcript_missing"])

    transcript_text = path.read_text(encoding="utf-8", errors="replace")
    transcript_meta = _extract_transcript_metadata(transcript_text)
    messages = _extract_messages(transcript_text)
    progress_before_final = _progress_before_final(messages)
    final_present = any(_is_final_message(message) for message in messages)
    transcript_provenance = classify_transcript_provenance(transcript_meta)
    desktop_transcript_provenance_ok = bool(transcript_provenance.get("ok"))
    artifact_dir = Path(project_root) / ".codex-oss" / "missions" / mission_id
    artifact_path_map = {
        "visible_commentary_jsonl": artifact_dir / "visible_commentary.jsonl",
        "commentary_delivery": artifact_dir / "commentary_delivery.json",
        "summary_md": artifact_dir / "summary.md",
        "canonical_evidence_bundle": artifact_dir / "canonical_evidence_bundle.json",
        "canonical_read_evidence": artifact_dir / "canonical_read_evidence.json",
        "canonical_patch_evidence": artifact_dir / "canonical_patch_evidence.json",
        "report_json": artifact_dir / "report.json",
        "adoption_or_recovery": artifact_dir / "adoption_or_recovery.json",
        "adoption_probes": artifact_dir / "tool_call_adoption_probes.json",
        "consumer_observation_witness": artifact_dir / "consumer_observation_witness.json",
        "spawned_transcript_authority": artifact_dir / "spawned_transcript_authority.json",
        "commentary_delivery_reconciliation": artifact_dir / "commentary_delivery_reconciliation.json",
        "state_machine_ledger": artifact_dir / "tool_state_machine_ledger.json",
        "mission_json": artifact_dir / "mission.json",
        "mission_canonical": artifact_dir / "mission_canonical.json",
    }
    artifact_payloads = {name: _read_json(file_path) for name, file_path in artifact_path_map.items()}
    desktop_render_surface = _desktop_render_surface(
        artifact_dir=artifact_dir,
        project_root=Path(project_root),
    )
    expected_event_ids = _delivery_event_ids(artifact_payloads.get("commentary_delivery"))
    consumer_observation_witness = build_consumer_observation_witness(
        mission_id=mission_id,
        transcript_meta=transcript_meta,
        messages=messages,
        min_progress_before_final=3,
        expected_event_ids=expected_event_ids,
    )
    artifacts = {
        "visible_commentary_jsonl": artifact_path_map["visible_commentary_jsonl"].exists(),
        "commentary_delivery": artifact_path_map["commentary_delivery"].exists(),
        "summary_md": artifact_path_map["summary_md"].exists(),
        "canonical_evidence": _canonical_evidence_present(artifact_payloads),
        "report_json": artifact_path_map["report_json"].exists(),
        "adoption_or_recovery": _adoption_or_recovery_proven(artifact_payloads.get("adoption_or_recovery"), artifact_payloads.get("adoption_probes")),
        "spawned_transcript_authority": _spawned_transcript_authority_proven(artifact_payloads.get("spawned_transcript_authority")),
    }
    route_ok = route_allows_desktop_gold(route_authority)
    mission_identity_ok = _mission_identity_matches(mission_id, artifact_payloads, transcript_meta)
    adoption_ok = bool(artifacts["adoption_or_recovery"])
    final_gate = evaluate_final_claim_gate(
        claim_type="desktop_gold",
        requested_status="DESKTOP_GOLD",
        route_authority=route_authority,
        mission_id=mission_id,
        transcript_path=str(path),
        artifact_paths=[str(p) for p in artifact_path_map.values() if p.exists()],
        consumer_observation_witness=consumer_observation_witness,
        desktop_render_surface=desktop_render_surface,
    )

    checks.extend([
        {"name": "desktop_route_authority", "ok": route_ok},
        {
            "name": "desktop_transcript_provenance",
            "ok": desktop_transcript_provenance_ok,
            "provenance": transcript_provenance,
        },
        {"name": "progress_before_final", "ok": progress_before_final >= 3, "count": progress_before_final},
        {
            "name": "progress_event_identity",
            "ok": consumer_observation_witness.get("identity_progress_before_final", 0) >= 3,
            "count": consumer_observation_witness.get("identity_progress_before_final", 0),
            "expected_event_count": len(expected_event_ids),
        },
        {"name": "consumer_observation_witness", "ok": consumer_observation_witness.get("ok"), "witness": consumer_observation_witness},
        {
            "name": "spawned_transcript_authority",
            "ok": artifacts["spawned_transcript_authority"],
            "authority": artifact_payloads.get("spawned_transcript_authority", {}),
        },
        {"name": "final_present", "ok": final_present},
        {"name": "artifacts_reconcile", "ok": all(artifacts.values()), "artifacts": artifacts},
        {"name": "mission_identity", "ok": mission_identity_ok},
        {"name": "adoption_or_recovery", "ok": adoption_ok},
        {
            "name": "render_surface_proof",
            "ok": bool((final_gate.get("render_surface_proof") or {}).get("ok")),
            "desktop_render_surface": desktop_render_surface,
            "render_surface_proof": final_gate.get("render_surface_proof", {}),
        },
        {"name": "artifact_hashes_present", "ok": all(final_gate.get("artifact_hashes", {}).values())},
        {"name": "oss_final_claim_gate", "ok": final_gate.get("ok"), "reasons": final_gate.get("reasons", [])},
    ])
    missing: list[str] = []
    if not route_ok:
        missing.append("desktop_route_authority_missing")
    if not desktop_transcript_provenance_ok:
        missing.append("desktop_transcript_provenance_missing")
    if progress_before_final < 3:
        missing.append("desktop_progress_before_final_missing")
    if consumer_observation_witness.get("identity_progress_before_final", 0) < 3:
        missing.append("desktop_progress_event_identity_missing")
    if not consumer_observation_witness.get("ok"):
        missing.append("desktop_consumer_observation_witness_missing_or_failed")
    if not final_present:
        missing.append("desktop_final_missing")
    for name, exists in artifacts.items():
        if not exists:
            missing.append(f"artifact_missing:{name}")
    if not mission_identity_ok:
        missing.append("mission_identity_mismatch")
    if not adoption_ok:
        missing.append("adoption_or_recovery_missing")
    render_surface_proof = final_gate.get("render_surface_proof") if isinstance(final_gate.get("render_surface_proof"), dict) else {}
    if not render_surface_proof.get("ok"):
        probe_status = str(desktop_render_surface.get("probe_status") or "unknown").lower()
        missing.append(f"desktop_pre_final_text_probe_not_passed:{probe_status or 'unknown'}")
    if not all(final_gate.get("artifact_hashes", {}).values()):
        missing.append("artifact_hash_missing")
    if not final_gate.get("ok"):
        missing.extend([f"final_claim_gate:{reason}" for reason in final_gate.get("reasons", [])])

    ok = not missing
    if not ok and "desktop_native_unproven" not in missing:
        missing.insert(0, "desktop_native_unproven")
    report = _report(ok, mission_id, transcript_path, checks, missing)
    report["transcript_provenance"] = transcript_provenance
    report["consumer_observation_witness"] = consumer_observation_witness
    report["final_claim_gate"] = final_gate
    report["desktop_render_surface"] = desktop_render_surface
    report["render_surface_proof"] = final_gate.get("render_surface_proof", {})
    return report


def _report(ok: bool, mission_id: str, transcript_path: str, checks: list[JSON], missing: list[str]) -> JSON:
    return {
        "schema_version": "desktop_native_ux_verification.v1",
        "mission_id": mission_id,
        "transcript_path": transcript_path,
        "ok": ok,
        "desktop_gold_pass": ok,
        "checks": checks,
        "missing_evidence": missing,
    }


def _delivery_event_ids(delivery: JSON | None) -> list[str]:
    events = delivery.get("events", {}) if isinstance(delivery, dict) else {}
    if not isinstance(events, dict):
        return []
    event_ids: set[str] = set()
    for key, event in events.items():
        if not isinstance(event, dict):
            continue
        states = event.get("states", {})
        if not isinstance(states, dict):
            states = {}
        if states.get("stream_enqueued") or states.get("sse_emitted") or states.get("created"):
            event_id = str(event.get("event_id") or key or "")
            if event_id:
                event_ids.add(event_id)
    return sorted(event_ids)


def _extract_messages(text: str) -> list[JSON]:
    stripped = text.strip()
    if not stripped:
        return []
    try:
        parsed = json.loads(stripped)
    except Exception:
        return _messages_from_plain_text(text)
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    if isinstance(parsed, dict):
        for key in ("messages", "raw_messages", "transcript"):
            value = parsed.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return _messages_from_plain_text(text)


def _extract_transcript_metadata(text: str) -> JSON:
    try:
        parsed = json.loads(text.strip())
    except Exception:
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}


def _read_json(path: Path) -> JSON | None:
    try:
        if not path.exists():
            return None
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _desktop_render_surface(*, artifact_dir: Path, project_root: Path) -> JSON:
    candidates = [
        artifact_dir / "desktop_pre_final_text_probe_result.json",
        project_root / ".codex-oss" / "desktop_pre_final_text_probe_result.json",
    ]
    for candidate in candidates:
        payload = _read_json(candidate)
        if not isinstance(payload, dict):
            continue
        surface = payload.get("desktop_render_surface")
        if isinstance(surface, dict):
            return dict(surface)
        status = str(payload.get("probe_status") or "unknown").lower()
        if status:
            return {"probe_required": True, "probe_status": status}
    return {"probe_required": True, "probe_status": "unknown"}


def _mission_identity_matches(mission_id: str, artifacts: dict[str, JSON | None], transcript_meta: JSON) -> bool:
    expected = str(mission_id or "").strip()
    if not expected:
        return False
    candidates = [
        transcript_meta.get("mission_id"),
        (artifacts.get("mission_json") or {}).get("mission_id"),
        (artifacts.get("mission_canonical") or {}).get("mission_id"),
        (artifacts.get("report_json") or {}).get("mission_id"),
        (artifacts.get("canonical_evidence_bundle") or {}).get("mission_id"),
        (artifacts.get("canonical_read_evidence") or {}).get("mission_id"),
        (artifacts.get("canonical_patch_evidence") or {}).get("mission_id"),
    ]
    present = [str(item).strip() for item in candidates if str(item or "").strip()]
    return bool(present) and all(item == expected for item in present)


def _canonical_evidence_present(artifact_payloads: dict[str, JSON | None]) -> bool:
    bundle = artifact_payloads.get("canonical_evidence_bundle")
    if isinstance(bundle, dict) and bundle.get("schema_version") == "canonical_evidence_bundle.v1":
        return True
    return bool(artifact_payloads.get("canonical_read_evidence") or artifact_payloads.get("canonical_patch_evidence"))


def _adoption_or_recovery_proven(adoption_or_recovery_payload: JSON | None, adoption_payload: JSON | None = None) -> bool:
    if isinstance(adoption_or_recovery_payload, dict):
        status = str(adoption_or_recovery_payload.get("status") or "").upper()
        if status in {"PASS", "RECOVERED", "NOT_APPLICABLE"}:
            return True
    if not isinstance(adoption_payload, dict):
        return False
    probes = adoption_payload.get("probes")
    if not isinstance(probes, list):
        return False
    if not probes:
        stats = adoption_payload.get("adoption_stats")
        if isinstance(stats, dict) and int(stats.get("total", -1) or 0) == 0:
            return True
        return False
    for probe in probes:
        if not isinstance(probe, dict):
            return False
        if not (probe.get("consumer_adopted") or probe.get("recovery_used")):
            return False
    return True


def _spawned_transcript_authority_proven(authority_payload: JSON | None) -> bool:
    if not isinstance(authority_payload, dict):
        return False
    if authority_payload.get("schema_version") != "spawned_transcript_authority.v1":
        return False
    if not authority_payload.get("ok"):
        return False
    if authority_payload.get("consumer_kind") != "codex_desktop_spawned":
        return False
    if str(authority_payload.get("basis") or "") != "raw_desktop_child_transcript":
        return False
    if int(authority_payload.get("pre_final_progress_count", 0) or 0) < 3:
        return False
    if not authority_payload.get("final_present"):
        return False
    return True


def _messages_from_plain_text(text: str) -> list[JSON]:
    messages: list[JSON] = []
    for idx, line in enumerate(text.splitlines()):
        if line.strip():
            messages.append({"timestamp": float(idx), "text": line, "phase": _phase_for_text(line)})
    return messages


def _phase_for_text(text: str) -> str:
    lowered = text.lower()
    if "final" in lowered or "oss_implementation_report_begin" in lowered:
        return "final_answer"
    if "[oss progress]" in lowered or "progress" in lowered:
        return "commentary"
    return ""


def _is_final_message(message: JSON) -> bool:
    phase = str(message.get("phase", "") or "").lower()
    text = str(message.get("text", "") or "").lower()
    return phase in {"final", "final_answer", "answer"} or "oss_implementation_report_begin" in text


def _is_progress_message(message: JSON) -> bool:
    if _is_final_message(message):
        return False
    phase = str(message.get("phase", "") or "").lower()
    text = str(message.get("text", "") or "").lower()
    metadata = message.get("metadata") if isinstance(message, dict) else {}
    visible = metadata.get("oss_visible_event") if isinstance(metadata, dict) else None
    return (
        phase in {"commentary", "read_floor", "plan", "verify", "report"}
        or "[oss progress]" in text
        or "oss-progress" in text
        or isinstance(visible, dict)
    )


def _progress_before_final(messages: list[JSON]) -> int:
    final_indexes = [idx for idx, message in enumerate(messages) if _is_final_message(message)]
    final_index = min(final_indexes) if final_indexes else len(messages)
    return sum(1 for message in messages[:final_index] if _is_progress_message(message))
