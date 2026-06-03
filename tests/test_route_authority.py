#!/usr/bin/env python3
"""Tests for RouteAuthorityV1 claim classification."""

import json
import sys

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


def assert_raw_desktop_cannot_claim_gold():
    name = "raw_desktop_cannot_claim_gold"
    from codex_oss.route_authority import build_route_authority

    route = build_route_authority(
        agent_name="oss_kimi_rapid",
        model_alias="ocg-kimi-k2.6",
        handoff_obj={"schema_version": 1},
        consumer_kind="codex_desktop_spawned",
    )
    assert route["route_kind"] == "raw_direct", route
    assert route["native_claim_scope"] == "none", route
    assert route["desktop_native_claim_allowed"] is False, route
    _pass(name)


def assert_raw_harness_is_bridge_only():
    name = "raw_harness_is_bridge_only"
    from codex_oss.route_authority import build_route_authority

    route = build_route_authority(
        agent_name="oss_flash_support",
        model_alias="ocg-deepseek-v4-flash",
        handoff_obj={"schema_version": 1},
        consumer_kind="direct_bridge_harness",
    )
    assert route["native_claim_scope"] == "bridge_only", route
    assert route["native_claim_allowed"] is True, route
    assert route["desktop_native_claim_allowed"] is False, route
    _pass(name)


def assert_mission_desktop_can_claim_gold():
    name = "mission_desktop_can_claim_gold"
    from codex_oss.route_authority import build_route_authority

    route = build_route_authority(
        agent_name="oss_deepseek_implementer",
        model_alias="mission-a5-deepseek",
        handoff_obj={"schema_version": "oss_agent_mission.v1"},
        consumer_kind="codex_desktop_spawned",
    )
    assert route["route_kind"] == "managed_mission", route
    assert route["handoff_schema"] == "mission_v1", route
    assert route["authority_mode"] == "runtime_truth", route
    assert route["native_claim_scope"] == "desktop_allowed", route
    assert route["desktop_native_claim_allowed"] is True, route
    _pass(name)


def assert_xml_wrapped_mission_handoff_classifies():
    name = "xml_wrapped_mission_handoff_classifies"
    from codex_oss.route_authority import build_route_authority

    route = build_route_authority(
        model_alias="mission-a3-kimi",
        handoff_text='<OSS_HANDOFF_JSON>\n{"schema_version":"oss_agent_mission.v1"}\n</OSS_HANDOFF_JSON>',
        consumer_kind="codex_desktop_spawned",
    )
    assert route["handoff_schema"] == "mission_v1", route
    assert route["desktop_native_claim_allowed"] is True, route
    _pass(name)


def assert_raw_mission_json_text_classifies():
    name = "raw_mission_json_text_classifies"
    from codex_oss.route_authority import build_route_authority

    route = build_route_authority(
        model_alias="mission-a3-deepseek",
        handoff_text='{"schema_version":"oss_agent_mission.v1","mission_id":"m1"}',
        consumer_kind="codex_desktop_spawned",
    )
    assert route["handoff_schema"] == "mission_v1", route
    assert route["desktop_native_claim_allowed"] is True, route
    _pass(name)


def assert_persisted_mission_json_text_classifies():
    name = "persisted_mission_json_text_classifies"
    from codex_oss.route_authority import build_route_authority

    route = build_route_authority(
        model_alias="mission-a5-deepseek",
        handoff_text='{"mission_id":"m1","tier":"A5","mode":"bounded_implementation","objective":"Patch a file"}',
        consumer_kind="codex_desktop_spawned",
    )
    assert route["handoff_schema"] == "mission_v1", route
    assert route["desktop_native_claim_allowed"] is True, route
    _pass(name)


def assert_mission_with_generic_handoff_fails_claim():
    name = "mission_with_generic_handoff_fails_claim"
    from codex_oss.route_authority import build_route_authority

    route = build_route_authority(
        agent_name="oss_deepseek_investigator",
        model_alias="mission-a3-deepseek",
        handoff_obj={"schema_version": 1},
        consumer_kind="codex_desktop_spawned",
    )
    assert route["handoff_schema"] == "generic_raw", route
    assert route["native_claim_scope"] == "none", route
    assert route["desktop_native_claim_allowed"] is False, route
    _pass(name)


def assert_native_experience_blocks_raw_desktop_gold():
    name = "native_experience_blocks_raw_desktop_gold"
    from codex_oss.native_experience import build_native_experience_contract, evaluate_native_experience
    from codex_oss.route_authority import build_route_authority

    route = build_route_authority(
        agent_name="oss_kimi_rapid",
        model_alias="ocg-kimi-k2.6",
        handoff_obj={"schema_version": 1},
        consumer_kind="codex_desktop_spawned",
    )
    contract = build_native_experience_contract(
        mission_id="route_auth_test",
        task_class="read_floor",
        route_authority=route,
    )
    result = evaluate_native_experience(
        contract,
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
    assert result["gold_pass"] is False, result
    assert result["desktop_gold_pass"] is False, result
    _pass(name)


def main():
    tests = [
        assert_raw_desktop_cannot_claim_gold,
        assert_raw_harness_is_bridge_only,
        assert_mission_desktop_can_claim_gold,
        assert_xml_wrapped_mission_handoff_classifies,
        assert_raw_mission_json_text_classifies,
        assert_persisted_mission_json_text_classifies,
        assert_mission_with_generic_handoff_fails_claim,
        assert_native_experience_blocks_raw_desktop_gold,
    ]
    for test in tests:
        try:
            test()
        except Exception as exc:
            _fail(test.__name__, repr(exc))
    print(f"Results: {PASSED} passed, {FAILED} failed")
    return FAILED == 0


if __name__ == "__main__":
    if not main():
        sys.exit(1)
