"""Burn-in data models: manifest, case result, summary, failure taxonomy."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

JSON = dict[str, Any]


class CaseState:
    QUEUED = "queued"
    SUBMITTED = "submitted"
    MISSION_CREATED = "mission_created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    MISSING_ARTIFACT = "missing_artifact"
    TIMEOUT = "timeout"
    PREFLIGHT_BLOCKED = "preflight_blocked"
    HOST_CAPACITY_FAILURE = "host_capacity_failure"
    CANCELLED = "cancelled"


class FailureClass:
    PREFLIGHT_RUNTIME_STALE = "preflight_runtime_stale"
    PREFLIGHT_AUTH_MISMATCH = "preflight_auth_mismatch"
    PREFLIGHT_UNSUPERVISED = "preflight_unsupervised_bridge"
    PREFLIGHT_MISSING_KEY = "preflight_missing_opencode_key"
    PREFLIGHT_ALIAS_MISSING = "preflight_model_alias_missing"
    PREFLIGHT_PROJECT_MISMATCH = "preflight_project_root_mismatch"
    HOST_AGENT_CAPACITY = "host_agent_capacity"
    INVALID_HANDOFF = "invalid_handoff"
    RUNTIME_POLICY_FAILURE = "runtime_policy_failure"
    RUNTIME_ARTIFACT_MISSING = "runtime_artifact_missing"
    PROVIDER_TIMEOUT = "provider_timeout"
    MODEL_REPORT_INVALID = "model_report_invalid"
    MODEL_RAW_DUMP = "model_raw_dump"
    MISSION_TIMEOUT = "mission_timeout"
    HARNESS_TIMEOUT = "harness_timeout"
    HARNESS_MISSING_CASE = "harness_missing_case"
    QUALITY_SUSPICIOUS_COMPLETE = "quality_suspicious_complete"
    QUALITY_FALSE_COMPLETE = "quality_false_complete"

    DOES_NOT_COUNT_AGAINST_MODEL = frozenset({
        HOST_AGENT_CAPACITY, PREFLIGHT_RUNTIME_STALE, PREFLIGHT_AUTH_MISMATCH,
        PREFLIGHT_UNSUPERVISED, PREFLIGHT_MISSING_KEY, PREFLIGHT_ALIAS_MISSING,
        PREFLIGHT_PROJECT_MISMATCH, HARNESS_TIMEOUT, HARNESS_MISSING_CASE,
    })


class QualityGrade:
    EARNED_COMPLETE = "earned_complete"
    RUNTIME_COMPLETE = "runtime_complete"
    DETERMINISTIC_COMPLETE = "deterministic_complete"
    MODEL_COMPLETE = "model_complete"
    SUSPICIOUS_COMPLETE = "suspicious_complete"
    TRUTHFUL_PARTIAL = "truthful_partial"
    RUNTIME_RESCUED_PARTIAL = "runtime_rescued_partial"
    FAILED = "failed"
    HOST_FAILURE = "host_failure"
    PREFLIGHT_FAILURE = "preflight_failure"


@dataclass
class BurninCaseResult:
    case_id: str = ""
    mission_id: str = ""
    state: str = CaseState.QUEUED
    failure_class: str = ""
    runtime_entered: bool = False
    mission_artifact_dir: str = ""
    started_at: str = ""
    finished_at: str = ""
    elapsed_seconds: float = 0
    agent: str = ""
    model_alias: str = ""
    mission_status: str = ""
    closure_source: str = ""
    quality_grade: str = ""
    earned_complete: bool = False
    suspicious_complete: bool = False
    false_complete: bool = False
    model_self_closed: bool = False
    runtime_closed: bool = False
    operator_asserted_floor: bool = False
    tool_observations_count: int = 0
    required_sources_covered: bool = False
    evidence_refs_resolve: bool = False
    raw_dump_detected: bool = False
    gpt_cleanup_rating: str = ""
    artifacts_present: dict[str, bool] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> JSON:
        return {
            "case_id": self.case_id,
            "mission_id": self.mission_id,
            "state": self.state,
            "failure_class": self.failure_class or None,
            "runtime_entered": self.runtime_entered,
            "mission_artifact_dir": self.mission_artifact_dir,
            "elapsed_seconds": self.elapsed_seconds,
            "agent": self.agent,
            "model_alias": self.model_alias,
            "mission_status": self.mission_status,
            "closure_source": self.closure_source,
            "quality_validity": {
                "grade": self.quality_grade,
                "earned_complete": self.earned_complete,
                "suspicious_complete": self.suspicious_complete,
                "model_self_closed": self.model_self_closed,
                "runtime_closed": self.runtime_closed,
                "operator_asserted_evidence_floor": self.operator_asserted_floor,
                "tool_observations_count": self.tool_observations_count,
                "required_sources_covered": self.required_sources_covered,
                "evidence_refs_resolve": self.evidence_refs_resolve,
                "raw_dump_detected": self.raw_dump_detected,
                "gpt_cleanup_rating": self.gpt_cleanup_rating,
            },
            "artifacts": self.artifacts_present,
            "notes": self.notes,
        }


@dataclass
class BurninRunManifest:
    run_id: str = ""
    suite: str = ""
    project_root: str = ""
    started_at: str = ""
    expected_case_count: int = 0
    expected_cases: list[JSON] = field(default_factory=list)
    bridge_preflight: JSON = field(default_factory=dict)
    runtime_identity: JSON = field(default_factory=dict)

    def to_dict(self) -> JSON:
        return {
            "schema_version": "burnin_run_manifest.v1",
            "run_id": self.run_id,
            "suite": self.suite,
            "project_root": self.project_root,
            "started_at": self.started_at,
            "expected_case_count": self.expected_case_count,
            "expected_cases": self.expected_cases,
            "bridge_preflight": self.bridge_preflight,
            "runtime_identity": self.runtime_identity,
        }


@dataclass
class BurninSummary:
    run_id: str = ""
    suite: str = ""
    summary_complete: bool = False
    started_at: str = ""
    finished_at: str = ""
    elapsed_seconds: float = 0
    expected_cases: int = 0
    submitted_cases: int = 0
    mission_artifacts_created: int = 0
    completed_cases: int = 0
    missing_cases: list[str] = field(default_factory=list)
    failed_cases: list[str] = field(default_factory=list)
    preflight_failures: list[str] = field(default_factory=list)
    host_capacity_failures: int = 0
    runtime_failures: int = 0
    provider_failures: int = 0
    model_report_invalid_count: int = 0
    false_complete_count: int = 0
    suspicious_complete_count: int = 0
    earned_complete_count: int = 0
    runtime_complete_count: int = 0
    deterministic_complete_count: int = 0
    truthful_partial_count: int = 0
    useful_complete_or_partial_rate: float = 0
    earned_complete_rate: float = 0
    model_self_close_rate: float = 0
    model_narrated_close_rate: float = 0
    runtime_rescue_rate: float = 0
    raw_dump_incidents: int = 0
    forbidden_read_incidents: int = 0
    evidence_ref_failures: int = 0
    gpt_cleanup_distribution: dict[str, int] = field(default_factory=dict)
    result: str = ""
    result_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> JSON:
        return {
            "schema_version": "burnin_summary.v1",
            "run_id": self.run_id,
            "suite": self.suite,
            "summary_complete": self.summary_complete,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_seconds": self.elapsed_seconds,
            "expected_cases": self.expected_cases,
            "submitted_cases": self.submitted_cases,
            "mission_artifacts_created": self.mission_artifacts_created,
            "completed_cases": self.completed_cases,
            "missing_cases": self.missing_cases,
            "failed_cases": self.failed_cases,
            "preflight_failures": self.preflight_failures,
            "host_capacity_failures": self.host_capacity_failures,
            "runtime_failures": self.runtime_failures,
            "provider_failures": self.provider_failures,
            "model_report_invalid_count": self.model_report_invalid_count,
            "false_complete_count": self.false_complete_count,
            "suspicious_complete_count": self.suspicious_complete_count,
            "earned_complete_count": self.earned_complete_count,
            "runtime_complete_count": self.runtime_complete_count,
            "deterministic_complete_count": self.deterministic_complete_count,
            "truthful_partial_count": self.truthful_partial_count,
            "useful_complete_or_partial_rate": round(self.useful_complete_or_partial_rate, 2),
            "earned_complete_rate": round(self.earned_complete_rate, 2),
            "model_self_close_rate": round(self.model_self_close_rate, 2),
            "model_narrated_close_rate": round(self.model_narrated_close_rate, 2),
            "runtime_rescue_rate": round(self.runtime_rescue_rate, 2),
            "raw_dump_incidents": self.raw_dump_incidents,
            "forbidden_read_incidents": self.forbidden_read_incidents,
            "evidence_ref_failures": self.evidence_ref_failures,
            "gpt_cleanup_rating_distribution": self.gpt_cleanup_distribution,
            "result": self.result,
            "result_reasons": self.result_reasons,
        }
