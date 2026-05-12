"""BurninHarness — manifest-before-run, case tracking, summary-in-finally, missing-case detection."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable

from codex_oss.burnin.models import (
    BurninRunManifest,
    BurninCaseResult,
    BurninSummary,
    CaseState,
    QualityGrade,
)
from codex_oss.burnin.preflight import run_burnin_preflight, PreflightResult

JSON = dict[str, Any]


class BurninHarness:
    def __init__(
        self,
        run_id: str,
        suite: str,
        project_root: str,
        output_dir: str = "",
        required_aliases: list[str] | None = None,
        bridge_url: str = "http://127.0.0.1:4000",
        auth: str = "",
    ):
        self.run_id = run_id
        self.suite = suite
        self.project_root = os.path.abspath(project_root)
        self.output_dir = output_dir or os.path.join(project_root, ".codex-oss", "burnins", run_id)
        self.required_aliases = required_aliases or ["mission-a3-kimi"]
        self.bridge_url = bridge_url
        self.auth = auth or os.getenv("LITELLM_MASTER_KEY", "sk-local-codex-bridge")
        self.manifest = BurninRunManifest(
            run_id=run_id, suite=suite, project_root=self.project_root,
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        self.cases: dict[str, BurninCaseResult] = {}
        self.summary = BurninSummary(run_id=run_id, suite=suite)
        self._case_result_dir = os.path.join(self.output_dir, "cases")
        os.makedirs(self._case_result_dir, exist_ok=True)

    def _write_manifest(self):
        path = os.path.join(self.output_dir, "manifest.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.manifest.to_dict(), f, indent=2, default=str)

    def _write_summary(self):
        path = os.path.join(self.output_dir, "summary.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.summary.to_dict(), f, indent=2, default=str)

    def _write_case_result(self, case_id: str):
        if case_id not in self.cases:
            return
        path = os.path.join(self._case_result_dir, f"{case_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.cases[case_id].to_dict(), f, indent=2, default=str)

    def register_case(
        self, case_id: str, mission_id: str, agent: str = "", model_alias: str = "",
        category: str = "", expected_outcome: str = "",
    ):
        self.cases[case_id] = BurninCaseResult(
            case_id=case_id, mission_id=mission_id, state=CaseState.QUEUED,
            agent=agent, model_alias=model_alias,
        )
        self.manifest.expected_cases.append({
            "case_id": case_id, "mission_id": mission_id,
            "agent": agent, "model_alias": model_alias,
            "category": category, "expected_outcome": expected_outcome,
        })
        self.manifest.expected_case_count = len(self.manifest.expected_cases)

    def set_case_state(self, case_id: str, state: str, failure_class: str = ""):
        if case_id not in self.cases:
            return
        self.cases[case_id].state = state
        if failure_class:
            self.cases[case_id].failure_class = failure_class
        self._write_case_result(case_id)

    def record_case_completion(
        self, case_id: str, *, elapsed: float = 0, mission_artifact_dir: str = "",
        mission_status: str = "", closure_source: str = "", quality_result: JSON | None = None,
    ):
        if case_id not in self.cases:
            return
        c = self.cases[case_id]
        c.state = CaseState.COMPLETED
        c.elapsed_seconds = elapsed
        c.mission_artifact_dir = mission_artifact_dir
        c.mission_status = mission_status
        c.closure_source = closure_source
        if quality_result:
            c.quality_grade = str(quality_result.get("grade", "") or "")
            c.earned_complete = bool(quality_result.get("earned_complete", False))
            c.suspicious_complete = bool(quality_result.get("suspicious_complete", False))
            c.false_complete = bool(quality_result.get("false_complete", False))
            c.model_self_closed = bool(quality_result.get("model_self_closed", False))
            c.runtime_closed = bool(quality_result.get("runtime_closed", False))
            c.tool_observations_count = int(quality_result.get("tool_observations_count", 0) or 0)
            c.evidence_refs_resolve = bool(quality_result.get("evidence_refs_resolve", False))
            c.raw_dump_detected = bool(quality_result.get("raw_dump_detected", False))
            c.gpt_cleanup_rating = str(quality_result.get("gpt_cleanup_rating", "") or "")
        c.started_at = ""
        c.finished_at = datetime.now(timezone.utc).isoformat()
        self._write_case_result(case_id)
        self._update_summary()

    def scan_missing_artifacts(self, missions_dir: str = ""):
        """Detect expected cases that didn't materialize as mission directories."""
        missions_root = missions_dir or os.path.join(self.project_root, ".codex-oss", "missions")
        for case_id, case in self.cases.items():
            if case.state in (CaseState.COMPLETED, CaseState.FAILED):
                continue
            mission_dir = os.path.join(missions_root, case.mission_id)
            if os.path.isdir(mission_dir):
                report_path = os.path.join(mission_dir, "report.json")
                if os.path.exists(report_path):
                    self.set_case_state(case_id, CaseState.MISSION_CREATED)
                else:
                    self.set_case_state(case_id, CaseState.MISSING_ARTIFACT)
            else:
                self.set_case_state(case_id, CaseState.MISSING_ARTIFACT, "harness_missing_case")

    def _update_summary(self):
        results = list(self.cases.values())
        completed = [c for c in results if c.state == CaseState.COMPLETED]
        failed = [c for c in results if c.state in (CaseState.FAILED, CaseState.MISSING_ARTIFACT)]
        self.summary.completed_cases = len(completed)
        self.summary.failed_cases = [c.case_id for c in failed]
        self.summary.missing_cases = [
            c.case_id for c in results if c.state not in (CaseState.COMPLETED, CaseState.FAILED)
        ]
        self.summary.submitted_cases = sum(1 for c in results if c.state != CaseState.QUEUED)
        self.summary.mission_artifacts_created = len(
            [c for c in results if c.mission_artifact_dir]
        )
        self.summary.false_complete_count = len([c for c in completed if c.false_complete])
        self.summary.suspicious_complete_count = len([c for c in completed if c.suspicious_complete])
        self.summary.earned_complete_count = len([c for c in completed if c.earned_complete])
        self.summary.runtime_complete_count = len(
            [c for c in completed if c.quality_grade == QualityGrade.RUNTIME_COMPLETE]
        )
        self.summary.deterministic_complete_count = len(
            [c for c in completed if c.quality_grade == QualityGrade.DETERMINISTIC_COMPLETE]
        )
        self.summary.truthful_partial_count = len(
            [c for c in completed if c.quality_grade == QualityGrade.TRUTHFUL_PARTIAL]
        )
        self.summary.raw_dump_incidents = len([c for c in completed if c.raw_dump_detected])
        self.summary.model_self_close_rate = _rate(
            len([c for c in completed if c.model_self_closed]), len(completed)
        )
        self.summary.runtime_rescue_rate = _rate(
            len([c for c in completed if c.runtime_closed]), len(completed)
        )
        useful = len(completed) - self.summary.false_complete_count - self.summary.suspicious_complete_count
        self.summary.useful_complete_or_partial_rate = _rate(useful, len(completed))
        self.summary.earned_complete_rate = _rate(self.summary.earned_complete_count, len(completed))

        self.summary.result = "PASS" if (
            self.summary.false_complete_count == 0
            and self.summary.raw_dump_incidents == 0
            and self.summary.earned_complete_rate >= 0.5
        ) else "FAIL"

    def finalize(self, result: str = ""):
        self.summary.finished_at = datetime.now(timezone.utc).isoformat()
        self.summary.summary_complete = True
        if result:
            self.summary.result = result
        self._update_summary()
        self._write_summary()

    def run_preflight(self) -> PreflightResult:
        preflight = run_burnin_preflight(
            self.project_root, self.suite,
            required_aliases=self.required_aliases,
            bridge_url=self.bridge_url, auth=self.auth,
        )
        self.manifest.bridge_preflight = preflight.to_dict()
        self.manifest.runtime_identity = preflight.runtime_identity
        self._write_manifest()
        if not preflight.ok:
            self.finalize("PRECHECK_FAILED")
        return preflight


def _rate(part: int, total: int) -> float:
    if total == 0:
        return 0.0
    return part / total
