#!/usr/bin/env python3
"""Burn-in harness regression tests."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from codex_oss.burnin.harness import BurninHarness
from codex_oss.burnin.models import CaseState
from tests.test_a3_open_burnin_pack import _run_mission_id, _select_case_entries, build_burnin_cases


def assert_run_mission_id_is_run_unique():
    mission_id = _run_mission_id("mission_a3_open_burnin_shape_func_def", 4, "burnin_a3_open_2026_05_12_101500")
    assert mission_id == "mission_a3_open_burnin_shape_func_def_4__burnin_a3_open_2026_05_12_101500", mission_id


def assert_scan_missing_artifacts_marks_missing_case():
    temp_root = tempfile.mkdtemp(prefix="burnin_harness_")
    try:
        missions_root = os.path.join(temp_root, ".codex-oss", "missions")
        os.makedirs(missions_root, exist_ok=True)

        harness = BurninHarness(
            run_id="burnin_test",
            suite="a3_open",
            project_root=temp_root,
            output_dir=os.path.join(temp_root, ".codex-oss", "burnins", "burnin_test"),
        )
        harness.register_case("case_001", "mission_present")
        harness.register_case("case_002", "mission_missing")

        present_dir = os.path.join(missions_root, "mission_present")
        os.makedirs(present_dir, exist_ok=True)
        with open(os.path.join(present_dir, "report.json"), "w", encoding="utf-8") as f:
            json.dump({"status": "COMPLETE"}, f)

        harness.scan_missing_artifacts(missions_root)

        assert harness.cases["case_001"].state == CaseState.MISSION_CREATED, harness.cases["case_001"].to_dict()
        assert harness.cases["case_002"].state == CaseState.MISSING_ARTIFACT, harness.cases["case_002"].to_dict()
    finally:
        shutil.rmtree(temp_root)


def assert_finalize_writes_summary_even_with_missing_cases():
    temp_root = tempfile.mkdtemp(prefix="burnin_harness_")
    try:
        output_dir = os.path.join(temp_root, ".codex-oss", "burnins", "burnin_test")
        harness = BurninHarness(
            run_id="burnin_test",
            suite="a3_open",
            project_root=temp_root,
            output_dir=output_dir,
        )
        harness.register_case("case_001", "mission_missing")
        harness.scan_missing_artifacts(os.path.join(temp_root, ".codex-oss", "missions"))
        harness.finalize("FAIL")

        summary_path = os.path.join(output_dir, "summary.json")
        assert os.path.exists(summary_path), summary_path
        with open(summary_path, "r", encoding="utf-8") as f:
            summary = json.load(f)
        assert summary["summary_complete"] is True, summary
        assert summary["result"] == "FAIL", summary
    finally:
        shutil.rmtree(temp_root)


def assert_autonomy_subset_selection_preserves_original_case_ids():
    previous = os.environ.get("LIVE_AUTONOMY_ONLY")
    try:
        os.environ["LIVE_AUTONOMY_ONLY"] = "1"
        entries = _select_case_entries(build_burnin_cases(), start=1, limit=25)
        case_ids = [f"case_{idx:03d}" for idx, _ in entries]
        assert case_ids == ["case_001", "case_008", "case_009", "case_010", "case_011", "case_023"], case_ids
    finally:
        if previous is None:
            os.environ.pop("LIVE_AUTONOMY_ONLY", None)
        else:
            os.environ["LIVE_AUTONOMY_ONLY"] = previous


def main():
    assert_run_mission_id_is_run_unique()
    assert_scan_missing_artifacts_marks_missing_case()
    assert_finalize_writes_summary_even_with_missing_cases()
    assert_autonomy_subset_selection_preserves_original_case_ids()
    print("PASS: burnin harness suite")


if __name__ == "__main__":
    main()
