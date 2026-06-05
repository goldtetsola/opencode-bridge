"""Authority-preserving MissionV1 and evidence artifacts.

These helpers keep render/report projections separate from certification
authority artifacts. Managed MissionV1 producers call them directly; certifiers
may also use them to backfill derived compatibility artifacts from older
missions without weakening the gate.
"""

from __future__ import annotations

import json
import os
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any

from codex_oss.read_evidence import build_canonical_read_evidence

JSON = dict[str, Any]


def canonical_mission_payload(mission: Any) -> JSON:
    mission_id = str(getattr(mission, "mission_id", "") or "")
    if is_dataclass(mission):
        payload = {field.name: _jsonable(getattr(mission, field.name, None)) for field in fields(mission)}
    else:
        payload = {}
    payload["schema_version"] = "oss_agent_mission.v1"
    payload["mission_id"] = mission_id
    payload.setdefault("write_allowed", bool(getattr(mission, "write_allowed", False)))
    payload.setdefault("allowed_tool_classes", list(getattr(mission, "allowed_tool_classes", []) or []))
    payload.setdefault("required_outputs", list(getattr(mission, "required_outputs", []) or []))
    payload.setdefault("objective_style", str(getattr(mission, "objective_style", "") or ""))
    payload.setdefault("objective_spec", getattr(mission, "objective_spec", None) or {})
    payload.setdefault("answer_obligations", list(getattr(mission, "answer_obligations", []) or []))
    payload.setdefault("must_inspect", list(getattr(mission, "must_inspect", []) or []))
    payload.setdefault("workspace_apply_policy", dict(getattr(mission, "workspace_apply_policy", {}) or {}))
    return payload


def write_mission_authority_artifacts(
    *,
    artifact_dir: str | Path,
    mission: Any,
    summary_payload: JSON,
    validation: JSON | None = None,
) -> JSON:
    artifact_path = Path(artifact_dir)
    artifact_path.mkdir(parents=True, exist_ok=True)
    canonical = canonical_mission_payload(mission)
    raw_handoff = str(getattr(mission, "runtime_handoff_raw", "") or "")
    validation_payload = {
        "schema_version": "mission_validation.v1",
        "mission_id": canonical.get("mission_id", ""),
        "ok": True,
        "parser": "oss_agent_mission.v1",
        "source": "runtime_validated_mission_object",
    }
    if isinstance(validation, dict):
        validation_payload.update(validation)

    _write_json(artifact_path / "mission_canonical.json", canonical)
    _write_json(artifact_path / "mission_validation.json", validation_payload)
    _write_json(artifact_path / "mission_summary.json", summary_payload)
    if raw_handoff:
        (artifact_path / "mission_handoff_raw.txt").write_text(raw_handoff, encoding="utf-8")
    return canonical


def build_canonical_evidence_bundle(
    *,
    mission_id: str,
    task_class: str,
    answer_graph: JSON | None = None,
    coverage_graph: JSON | None = None,
    claim_graph: JSON | None = None,
    ledger_payload: JSON | None = None,
    report: JSON | None = None,
    source_artifacts: JSON | None = None,
) -> JSON:
    answer_graph = answer_graph if isinstance(answer_graph, dict) else {}
    coverage_graph = coverage_graph if isinstance(coverage_graph, dict) else {}
    claim_graph = claim_graph if isinstance(claim_graph, dict) else {}
    ledger_payload = ledger_payload if isinstance(ledger_payload, dict) else {}
    report = report if isinstance(report, dict) else {}

    sufficiency = answer_graph.get("sufficiency") if isinstance(answer_graph.get("sufficiency"), dict) else {}
    coverage_status = coverage_graph.get("coverage_status") if isinstance(coverage_graph.get("coverage_status"), dict) else {}
    if not coverage_status:
        nested_coverage = answer_graph.get("coverage_graph") if isinstance(answer_graph.get("coverage_graph"), dict) else {}
        coverage_status = nested_coverage.get("coverage_status") if isinstance(nested_coverage.get("coverage_status"), dict) else {}
    missing_required = _str_list(sufficiency.get("missing_required_sources"))
    required_sources = _str_list(
        sufficiency.get("required_sources")
        or sufficiency.get("required_source_paths")
        or report.get("must_inspect")
        or report.get("required_sources")
    )
    if not required_sources:
        required_sources = sorted({*missing_required, *[str(item.get("path")) for item in _files_inspected(ledger_payload) if item.get("path")]})
    entitlement = sufficiency.get("closure_entitlement") if isinstance(sufficiency.get("closure_entitlement"), dict) else {}
    can_complete = bool(
        entitlement.get("can_return_complete")
        or entitlement.get("can_complete")
        or sufficiency.get("can_close")
        or sufficiency.get("can_complete")
        or sufficiency.get("coverage_complete")
        or coverage_status.get("can_complete")
        or coverage_status.get("coverage_complete")
    )
    if missing_required:
        can_complete = False

    return {
        "schema_version": "canonical_evidence_bundle.v1",
        "mission_id": str(mission_id),
        "task_class": str(task_class or "read_only"),
        "evidence_authority": "mission_v1_runtime",
        "source_artifacts": source_artifacts
        or {
            "answer_graph": "answer_graph.json",
            "coverage_graph": "coverage_graph.json",
            "claim_graph": "claim_graph.json",
            "ledger": "ledger.json",
            "report": "report.json",
        },
        "required_sources": required_sources,
        "observations": _files_inspected(ledger_payload),
        "claims": _claims_from_graph(claim_graph),
        "coverage": coverage_graph,
        "coverage_status": coverage_status,
        "verification": report.get("verification", {}) if isinstance(report.get("verification"), dict) else {},
        "missing_required_sources": missing_required,
        "status_entitlement": {
            "can_complete": can_complete,
            "can_return_complete": can_complete,
            "reason": "canonical_runtime_evidence_complete" if can_complete else "canonical_runtime_evidence_incomplete",
        },
    }


def build_canonical_read_projection(bundle: JSON, *, mission_id: str = "") -> JSON:
    mission = str(mission_id or bundle.get("mission_id", "") or "")
    required = _str_list(bundle.get("required_sources"))
    evidence: dict[str, JSON] = {}
    failures: list[str] = []
    for item in bundle.get("observations", []) if isinstance(bundle.get("observations"), list) else []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        if not path:
            continue
        if item.get("complete") is False and not item.get("sha256"):
            failures.append(path)
            continue
        evidence[path] = {
            "output_sha256": str(item.get("sha256") or ""),
            "output_chars": int(item.get("chars_returned", item.get("chars_total", 0)) or 0),
            "output_excerpt": "",
            "source": str(item.get("tool") or "mission_runtime"),
            "redactions_applied": False,
        }
    for path in _str_list(bundle.get("missing_required_sources")):
        failures.append(path)
    canonical = build_canonical_read_evidence(
        mission_id=mission,
        required_paths=required,
        evidence=evidence,
        failures=failures,
        writes_performed=False,
    )
    canonical["coverage_status"] = dict(bundle.get("coverage_status") or {})
    canonical["missing_required_sources"] = _str_list(bundle.get("missing_required_sources"))
    if not bool(bundle.get("status_entitlement", {}).get("can_complete", False)):
        canonical["status_entitlement"]["can_complete"] = False
        canonical["status_entitlement"]["reason"] = str(
            bundle.get("status_entitlement", {}).get("reason") or "canonical_runtime_evidence_incomplete"
        )
    return canonical


def write_canonical_evidence_artifacts(
    *,
    artifact_dir: str | Path,
    mission_id: str,
    task_class: str,
    answer_graph: JSON | None = None,
    coverage_graph: JSON | None = None,
    claim_graph: JSON | None = None,
    ledger_payload: JSON | None = None,
    report: JSON | None = None,
) -> JSON:
    artifact_path = Path(artifact_dir)
    artifact_path.mkdir(parents=True, exist_ok=True)
    bundle = build_canonical_evidence_bundle(
        mission_id=mission_id,
        task_class=task_class,
        answer_graph=answer_graph,
        coverage_graph=coverage_graph,
        claim_graph=claim_graph,
        ledger_payload=ledger_payload,
        report=report,
    )
    _write_json(artifact_path / "canonical_evidence_bundle.json", bundle)
    if task_class == "read_only":
        _write_json(artifact_path / "canonical_read_evidence.json", build_canonical_read_projection(bundle, mission_id=mission_id))
    return bundle


def adoption_or_recovery_payload(
    *,
    mission_id: str,
    route_class: str = "managed_read_only",
    pending_tool_calls_emitted: int = 0,
    runtime_recovery_used: bool = False,
    artifacts: list[str] | None = None,
) -> JSON:
    if pending_tool_calls_emitted <= 0 and route_class in {"managed_read_only", "context_pack", "read_only"}:
        status = "NOT_APPLICABLE"
        reason = "no_pending_desktop_tool_calls"
    elif pending_tool_calls_emitted <= 0:
        status = "FAIL"
        reason = "missing_required_probe"
    elif runtime_recovery_used:
        status = "RECOVERED"
        reason = "recovered_by_runtime"
    else:
        status = "PASS"
        reason = "adoption_observed"
    return {
        "schema_version": "adoption_or_recovery.v1",
        "mission_id": str(mission_id),
        "route_class": route_class,
        "status": status,
        "reason": reason,
        "pending_tool_calls_emitted": int(pending_tool_calls_emitted),
        "runtime_recovery_used": bool(runtime_recovery_used),
        "artifacts": list(artifacts or []),
    }


def write_adoption_or_recovery(
    *,
    artifact_dir: str | Path,
    mission_id: str,
    route_class: str = "managed_read_only",
    pending_tool_calls_emitted: int = 0,
    runtime_recovery_used: bool = False,
    artifacts: list[str] | None = None,
) -> JSON:
    payload = adoption_or_recovery_payload(
        mission_id=mission_id,
        route_class=route_class,
        pending_tool_calls_emitted=pending_tool_calls_emitted,
        runtime_recovery_used=runtime_recovery_used,
        artifacts=artifacts,
    )
    artifact_path = Path(artifact_dir)
    artifact_path.mkdir(parents=True, exist_ok=True)
    _write_json(artifact_path / "adoption_or_recovery.json", payload)
    if route_class in {"managed_read_only", "context_pack", "read_only"}:
        _write_json(
            artifact_path / "tool_call_adoption_probes.json",
            {
                "schema_version": "tool_call_adoption_probes.v1",
                "adoption_stats": {"total": 0, "adopted": 0, "not_adopted": 0, "recovery_used": 0},
                "probes": [],
                "compatibility_projection": "adoption_or_recovery.v1",
            },
        )
    return payload


def ensure_derived_desktop_gold_artifacts(mission_dir: str | Path) -> JSON:
    mission_path = Path(mission_dir)
    mission_path.mkdir(parents=True, exist_ok=True)
    mission_id = mission_path.name
    answer_graph = _read_json(mission_path / "answer_graph.json")
    coverage_graph = _read_json(mission_path / "coverage_graph.json")
    claim_graph = _read_json(mission_path / "claim_graph.json")
    ledger = _read_json(mission_path / "ledger.json")
    report = _read_json(mission_path / "report.json")
    outputs: JSON = {"schema_version": "desktop_gold_artifact_backfill.v1", "mission_id": mission_id, "written": []}
    bundle_path = mission_path / "canonical_evidence_bundle.json"
    read_path = mission_path / "canonical_read_evidence.json"
    read_payload = _read_json(read_path)
    if not bundle_path.exists() and read_payload:
        entitlement = read_payload.get("status_entitlement") if isinstance(read_payload.get("status_entitlement"), dict) else {}
        bundle = {
            "schema_version": "canonical_evidence_bundle.v1",
            "mission_id": mission_id,
            "task_class": "read_only",
            "evidence_authority": "mission_v1_runtime",
            "source_artifacts": {
                "canonical_read_evidence": "canonical_read_evidence.json",
                "report": "report.json",
            },
            "required_sources": _str_list(read_payload.get("required_paths")),
            "observations": read_payload.get("fulfilled_paths", []) if isinstance(read_payload.get("fulfilled_paths"), list) else [],
            "claims": [],
            "coverage": {},
            "coverage_status": read_payload.get("coverage_status", {}) if isinstance(read_payload.get("coverage_status"), dict) else {},
            "verification": {},
            "missing_required_sources": _str_list(read_payload.get("missing_required_sources")),
            "status_entitlement": {
                "can_complete": bool(entitlement.get("can_complete", False)),
                "can_return_complete": bool(entitlement.get("can_return_complete", entitlement.get("can_complete", False))),
                "reason": str(entitlement.get("reason") or "canonical_read_evidence_projection"),
            },
        }
        _write_json(bundle_path, bundle)
        outputs["written"].append("canonical_evidence_bundle.json")
    elif not bundle_path.exists() and (answer_graph or ledger or report):
        write_canonical_evidence_artifacts(
            artifact_dir=mission_path,
            mission_id=mission_id,
            task_class="read_only",
            answer_graph=answer_graph,
            coverage_graph=coverage_graph,
            claim_graph=claim_graph,
            ledger_payload=ledger,
            report=report,
        )
        outputs["written"].extend(["canonical_evidence_bundle.json", "canonical_read_evidence.json"])
    elif bundle_path.exists() and not (mission_path / "canonical_read_evidence.json").exists():
        bundle = _read_json(bundle_path)
        _write_json(mission_path / "canonical_read_evidence.json", build_canonical_read_projection(bundle, mission_id=mission_id))
        outputs["written"].append("canonical_read_evidence.json")
    if not (mission_path / "adoption_or_recovery.json").exists():
        write_adoption_or_recovery(artifact_dir=mission_path, mission_id=mission_id)
        outputs["written"].extend(["adoption_or_recovery.json", "tool_call_adoption_probes.json"])
    return outputs


def _files_inspected(ledger_payload: JSON) -> list[JSON]:
    files = ledger_payload.get("files_inspected") if isinstance(ledger_payload.get("files_inspected"), list) else []
    return [dict(item) for item in files if isinstance(item, dict)]


def _claims_from_graph(claim_graph: JSON) -> list[JSON]:
    claims = claim_graph.get("claims") if isinstance(claim_graph.get("claims"), list) else []
    if claims:
        return [dict(item) for item in claims if isinstance(item, dict)]
    main = claim_graph.get("main_claims") if isinstance(claim_graph.get("main_claims"), list) else []
    return [dict(item) if isinstance(item, dict) else {"claim": str(item)} for item in main]


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        if isinstance(value, dict):
            return {str(k): _jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [_jsonable(item) for item in value]
        return str(value)


def _read_json(path: Path) -> JSON:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
