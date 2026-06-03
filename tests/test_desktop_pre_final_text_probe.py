#!/usr/bin/env python3
"""Tests for the Desktop pre-final text renderer probe."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

PASSED = 0
FAILED = 0


def _pass(name):
    global PASSED
    PASSED += 1
    print(f"  PASS {name}")


def _fail(name, detail=""):
    global FAILED
    FAILED += 1
    print(f"  FAIL {name}: {detail}")


def assert_probe_self_test_streams_progress_before_final():
    name = "probe_self_test_streams_progress_before_final"
    from codex_oss.desktop_pre_final_text_probe import FINAL_LINE, PROGRESS_LINES, run_self_test

    report = run_self_test(delay_seconds=0.001)
    assert report["ok"] is True, report
    assert report["order_ok"] is True, report
    assert report["response_completed"] is True, report
    assert report["done_seen"] is True, report
    text = report["delta_text"]
    positions = [text.find(marker) for marker in [*PROGRESS_LINES, FINAL_LINE]]
    assert positions == sorted(positions), report
    _pass(name)


def assert_probe_config_is_responses_provider():
    name = "probe_config_is_responses_provider"
    from codex_oss.desktop_pre_final_text_probe import provider_config

    config = provider_config(port=43211)
    assert "[model_providers.desktop_pre_final_text_probe]" in config, config
    assert 'wire_api = "responses"' in config, config
    assert "http://127.0.0.1:43211/v1" in config, config
    _pass(name)


def assert_probe_result_records_three_way_status_and_policy():
    name = "probe_result_records_three_way_status_and_policy"
    from codex_oss.desktop_pre_final_text_probe import write_probe_result

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "desktop_pre_final_text_probe_result.json"
        fail = write_probe_result(
            output_path=path,
            probe_status="fail",
            observed_progress_before_final=False,
            observed_final=True,
            notes="Only final appeared.",
        )
        assert fail["probe_status"] == "fail", fail
        assert fail["desktop_render_surface"]["claim_policy"]["desktop_live_commentary_claim_allowed"] is False, fail
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded["observed_final"] is True, loaded

        passed = write_probe_result(
            output_path=path,
            probe_status="pass",
            observed_progress_before_final=True,
            observed_final=True,
        )
        assert passed["desktop_render_surface"]["claim_policy"]["desktop_live_commentary_claim_allowed"] is True, passed
    _pass(name)


def assert_native_contract_blocks_desktop_gold_until_probe_passes():
    name = "native_contract_blocks_desktop_gold_until_probe_passes"
    from codex_oss.native_experience import build_native_experience_contract, evaluate_native_experience
    from codex_oss.route_authority import build_route_authority

    route = build_route_authority(
        model_alias="mission-a3-deepseek",
        handoff_obj={"schema_version": "oss_agent_mission.v1"},
        consumer_kind="codex_desktop_spawned",
    )
    common = dict(
        artifacts_exist={
            "visible_commentary_jsonl": True,
            "summary_md": True,
            "canonical_evidence": True,
            "adoption_probes": True,
        },
        runtime_owns_status=True,
        model_narrative_valid=True,
        commentary_events_count=3,
        commentary_event_classes={"mission_or_action_start", "tool_or_evidence_progress", "closure_or_verification_progress"},
        commentary_observed=True,
        commentary_rendered_before_final=True,
    )
    contract = build_native_experience_contract(
        mission_id="desktop_probe_gate",
        task_class="read_floor",
        route_authority=route,
        consumer_observation_witness={"schema_version": "consumer_observation_witness.v1", "ok": True},
    )
    unknown = evaluate_native_experience(contract, **common)
    assert unknown["desktop_gold_pass"] is False, unknown
    assert "desktop_pre_final_text_probe_not_passed:unknown" in unknown["missing_evidence"], unknown

    contract["desktop_render_surface"] = {"probe_required": True, "probe_status": "pass"}
    passed = evaluate_native_experience(contract, **common)
    assert passed["desktop_gold_pass"] is True, passed
    _pass(name)


def main() -> bool:
    for test in [
        assert_probe_self_test_streams_progress_before_final,
        assert_probe_config_is_responses_provider,
        assert_probe_result_records_three_way_status_and_policy,
        assert_native_contract_blocks_desktop_gold_until_probe_passes,
    ]:
        try:
            test()
        except Exception as exc:
            _fail(test.__name__, repr(exc))
    print(f"Results: {PASSED} passed, {FAILED} failed")
    return FAILED == 0


if __name__ == "__main__":
    if not main():
        sys.exit(1)
