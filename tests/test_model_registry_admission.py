#!/usr/bin/env python3
"""Behavior tests for S01/T01 model registry admission."""

from __future__ import annotations

import os
import sys

os.environ["ALLOW_MISSING_OPENCODE_KEY"] = "1"

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from codex_oss.managed_bridge import RUNTIME_MODEL_ALIASES
from codex_oss.model_registry import (
    AdmissionError,
    CapabilityDowngradeError,
    LaneCapability,
    ModelRegistry,
    UnsupportedLaneError,
)


def test_br_t01_resolves_any_configured_opencode_alias_and_preserves_identity():
    registry = ModelRegistry.from_runtime_aliases(RUNTIME_MODEL_ALIASES)

    admitted = registry.admit("mission-a3-kimi", lane="scout")

    assert admitted.requested_model_alias == "mission-a3-kimi"
    assert admitted.resolved_model_alias == "ocg-kimi-k2.6"
    assert admitted.resolved_upstream_model == "kimi-k2.6"
    assert admitted.provider_route == "opencode-go"
    assert admitted.lane == "scout"
    assert admitted.capability.supports_lane("scout")
    assert admitted.to_record()["requested_model_alias"] == "mission-a3-kimi"
    assert admitted.to_record()["resolved_upstream_model"] == "kimi-k2.6"


def test_br_t01_unknown_model_fails_before_spawn_or_tool_execution():
    registry = ModelRegistry.from_runtime_aliases(RUNTIME_MODEL_ALIASES)

    try:
        registry.admit("mission-a3-unknown", lane="scout")
    except AdmissionError as exc:
        assert exc.before_spawn is True
        assert exc.before_tool_execution is True
        assert "unknown model alias" in str(exc)
    else:
        raise AssertionError("unknown model was admitted")


def test_br_t01_unsupported_lane_fails_before_tool_execution():
    registry = ModelRegistry(
        aliases={"mission-a3-readonly": "ocg-kimi-k2.6"},
        capabilities={
            "ocg-kimi-k2.6": LaneCapability(
                lanes=("scout", "review"),
                tool_classes=("read", "search", "list", "safe_git"),
                tier=3,
            )
        },
    )

    try:
        registry.admit("mission-a3-readonly", lane="implementation")
    except UnsupportedLaneError as exc:
        assert exc.before_spawn is True
        assert exc.before_tool_execution is True
        assert exc.lane == "implementation"
    else:
        raise AssertionError("unsupported implementation lane was admitted")


def test_br_t01_weaker_fallback_requires_explicit_approval():
    registry = ModelRegistry.from_runtime_aliases(
        {"mission-a5-deepseek": "ocg-deepseek-v4-pro"},
        fallbacks={"mission-a5-deepseek": ("ocg-deepseek-v4-flash",)},
    )

    try:
        registry.admit("mission-a5-deepseek", lane="implementation", unavailable=("ocg-deepseek-v4-pro",))
    except CapabilityDowngradeError as exc:
        assert exc.before_spawn is True
        assert exc.before_tool_execution is True
        assert exc.requested_model_alias == "mission-a5-deepseek"
        assert exc.fallback_model_alias == "ocg-deepseek-v4-flash"
        assert "explicit approval" in str(exc)
    else:
        raise AssertionError("weaker fallback was admitted without approval")

    admitted = registry.admit(
        "mission-a5-deepseek",
        lane="implementation",
        unavailable=("ocg-deepseek-v4-pro",),
        approve_weaker_fallback=True,
    )

    assert admitted.requested_model_alias == "mission-a5-deepseek"
    assert admitted.resolved_model_alias == "ocg-deepseek-v4-flash"
    assert admitted.fallback_from == "ocg-deepseek-v4-pro"
    assert admitted.fallback_approval == "explicit"
    assert admitted.to_record()["fallback_approval"] == "explicit"


def test_br_t01_parent_provider_and_recursive_bridge_aliases_are_refused():
    registry = ModelRegistry.from_runtime_aliases(RUNTIME_MODEL_ALIASES)

    for alias in ("gpt-5.5", "opencode_bridge"):
        try:
            registry.admit(alias, lane="scout")
        except AdmissionError as exc:
            assert exc.before_spawn is True
            assert exc.before_tool_execution is True
        else:
            raise AssertionError(f"unsafe alias was admitted: {alias}")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("test_model_registry_admission: ok")
