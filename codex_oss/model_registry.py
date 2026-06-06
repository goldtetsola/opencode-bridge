"""ModelRegistryV1 admission for native OSS subagent routes.

The bridge keeps permissive raw model mapping for compatibility. Native OSS
runtime admission is stricter: configured aliases must resolve before spawn,
lane capabilities must be known, and weaker fallbacks require approval.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

JSON = dict[str, Any]


DEFAULT_OPENCODE_MODEL_ALIASES: dict[str, str] = {
    "ocg-deepseek-v4-pro": "deepseek-v4-pro",
    "ocg-deepseek-v4-flash": "deepseek-v4-flash",
    "ocg-kimi-k2.6": "kimi-k2.6",
    "ocg-kimi-k2.5": "kimi-k2.5",
    "ocg-qwen3.6-plus": "qwen3.6-plus",
    "ocg-qwen3.5-plus": "qwen3.5-plus",
    "ocg-glm-5.1": "glm-5.1",
    "ocg-glm-5": "glm-5",
    "ocg-minimax-m2.7": "minimax-m2.7",
    "ocg-minimax-m2.5": "minimax-m2.5",
    "opencode-go/deepseek-v4-pro": "deepseek-v4-pro",
    "opencode-go/deepseek-v4-flash": "deepseek-v4-flash",
    "opencode-go/kimi-k2.6": "kimi-k2.6",
    "opencode-go/kimi-k2.5": "kimi-k2.5",
    "opencode-go/qwen3.6-plus": "qwen3.6-plus",
    "opencode-go/qwen3.5-plus": "qwen3.5-plus",
    "opencode-go/glm-5.1": "glm-5.1",
    "opencode-go/glm-5": "glm-5",
    "opencode-go/minimax-m2.7": "minimax-m2.7",
    "opencode-go/minimax-m2.5": "minimax-m2.5",
    "oss_deepseek_pro": "deepseek-v4-pro",
    "oss_flash_support": "deepseek-v4-flash",
    "oss_kimi_rapid": "kimi-k2.6",
}

DEFAULT_PROVIDER_ROUTE = "opencode-go"
_DEFAULT_LANES = ("scout", "review", "docs_support", "bounded_write", "implementation")
_DEFAULT_TOOLS = ("read", "search", "list", "safe_git", "shell", "test", "edit")
_LANE_REQUIRED_TOOLS: dict[str, tuple[str, ...]] = {
    "scout": ("read", "search", "list"),
    "review": ("read", "search", "list"),
    "docs_support": ("read", "search", "list", "edit"),
    "bounded_write": ("read", "search", "list", "edit"),
    "implementation": ("read", "search", "list", "safe_git", "shell", "test", "edit"),
}
_DEFAULT_TIERS: dict[str, int] = {
    "deepseek-v4-pro": 5,
    "kimi-k2.6": 5,
    "qwen3.6-plus": 5,
    "glm-5.1": 5,
    "minimax-m2.7": 5,
    "kimi-k2.5": 4,
    "qwen3.5-plus": 4,
    "glm-5": 4,
    "minimax-m2.5": 4,
    "deepseek-v4-flash": 3,
}


class AdmissionError(ValueError):
    """Raised when native runtime admission fails before work starts."""

    before_spawn = True
    before_tool_execution = True


class UnsupportedLaneError(AdmissionError):
    def __init__(self, model_alias: str, lane: str, missing_tools: Sequence[str] = ()):
        self.model_alias = model_alias
        self.lane = lane
        self.missing_tools = tuple(missing_tools)
        detail = f"unsupported lane {lane!r} for model alias {model_alias!r}"
        if self.missing_tools:
            detail += f"; missing tool classes: {', '.join(self.missing_tools)}"
        super().__init__(detail)


class CapabilityDowngradeError(AdmissionError):
    def __init__(self, requested_model_alias: str, fallback_model_alias: str):
        self.requested_model_alias = requested_model_alias
        self.fallback_model_alias = fallback_model_alias
        super().__init__(
            "weaker fallback requires explicit approval: "
            f"{requested_model_alias!r} -> {fallback_model_alias!r}"
        )


@dataclass(frozen=True)
class LaneCapability:
    lanes: tuple[str, ...] = _DEFAULT_LANES
    tool_classes: tuple[str, ...] = _DEFAULT_TOOLS
    tier: int = 5
    provider_route: str = DEFAULT_PROVIDER_ROUTE
    available: bool = True

    def supports_lane(self, lane: str) -> bool:
        return str(lane or "") in self.lanes

    def missing_tools_for_lane(self, lane: str) -> tuple[str, ...]:
        required = _LANE_REQUIRED_TOOLS.get(str(lane or ""), ())
        available = set(self.tool_classes)
        return tuple(tool for tool in required if tool not in available)


@dataclass(frozen=True)
class ModelAdmission:
    requested_model_alias: str
    resolved_model_alias: str
    resolved_upstream_model: str
    provider_route: str
    lane: str
    capability: LaneCapability
    fallback_from: str = ""
    fallback_approval: str = "not_required"

    def to_record(self) -> JSON:
        return {
            "requested_model_alias": self.requested_model_alias,
            "resolved_model_alias": self.resolved_model_alias,
            "resolved_upstream_model": self.resolved_upstream_model,
            "provider_route": self.provider_route,
            "lane": self.lane,
            "capability_tier": self.capability.tier,
            "tool_classes": list(self.capability.tool_classes),
            "fallback_from": self.fallback_from,
            "fallback_approval": self.fallback_approval,
        }


class ModelRegistry:
    def __init__(
        self,
        aliases: Mapping[str, str] | None = None,
        *,
        capabilities: Mapping[str, LaneCapability] | None = None,
        fallbacks: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        self.aliases = dict(DEFAULT_OPENCODE_MODEL_ALIASES)
        self.aliases.update(dict(aliases or {}))
        self.capabilities = dict(capabilities or {})
        self.fallbacks = {str(k): tuple(str(v) for v in values) for k, values in (fallbacks or {}).items()}

    @classmethod
    def from_runtime_aliases(
        cls,
        runtime_aliases: Mapping[str, str],
        *,
        fallbacks: Mapping[str, Sequence[str]] | None = None,
        extra_aliases: Mapping[str, str] | None = None,
    ) -> "ModelRegistry":
        aliases = dict(DEFAULT_OPENCODE_MODEL_ALIASES)
        aliases.update({str(k): str(v) for k, v in runtime_aliases.items()})
        if extra_aliases:
            aliases.update({str(k): str(v) for k, v in extra_aliases.items()})
        capabilities = _default_capabilities_for_aliases(aliases)
        return cls(aliases, capabilities=capabilities, fallbacks=fallbacks)

    def admit(
        self,
        requested_model_alias: str,
        *,
        lane: str,
        unavailable: Sequence[str] = (),
        approve_weaker_fallback: bool = False,
    ) -> ModelAdmission:
        requested = str(requested_model_alias or "").strip()
        lane_name = str(lane or "").strip()
        if _is_forbidden_child_route(requested):
            raise AdmissionError(f"forbidden native OSS child model alias: {requested!r}")
        primary_alias = self._resolve_alias(requested)
        unavailable_set = {str(item) for item in unavailable}

        if primary_alias in unavailable_set:
            selected_alias = self._select_fallback(requested, primary_alias, approve_weaker_fallback)
            fallback_from = primary_alias
            fallback_approval = "explicit" if _tier(self._capability(selected_alias)) < _tier(self._capability(primary_alias)) else "not_required"
        else:
            selected_alias = primary_alias
            fallback_from = ""
            fallback_approval = "not_required"

        upstream = self._resolve_alias(selected_alias)
        capability = self._capability(selected_alias)
        if not capability.available:
            raise AdmissionError(f"unavailable model alias: {selected_alias!r}")
        if not capability.supports_lane(lane_name):
            raise UnsupportedLaneError(selected_alias, lane_name)
        missing_tools = capability.missing_tools_for_lane(lane_name)
        if missing_tools:
            raise UnsupportedLaneError(selected_alias, lane_name, missing_tools)

        return ModelAdmission(
            requested_model_alias=requested,
            resolved_model_alias=selected_alias,
            resolved_upstream_model=upstream,
            provider_route=capability.provider_route,
            lane=lane_name,
            capability=capability,
            fallback_from=fallback_from,
            fallback_approval=fallback_approval,
        )

    def _resolve_alias(self, alias: str) -> str:
        if alias in self.aliases:
            return self.aliases[alias]
        if alias in _DEFAULT_TIERS or alias in self.capabilities:
            return alias
        raise AdmissionError(f"unknown model alias: {alias!r}")

    def _capability(self, alias_or_upstream: str) -> LaneCapability:
        if alias_or_upstream in self.capabilities:
            return self.capabilities[alias_or_upstream]
        upstream = self._resolve_alias(alias_or_upstream) if alias_or_upstream in self.aliases else alias_or_upstream
        if upstream in self.capabilities:
            return self.capabilities[upstream]
        return _default_capability(upstream)

    def _select_fallback(self, requested: str, primary_alias: str, approve_weaker_fallback: bool) -> str:
        primary_capability = self._capability(primary_alias)
        for fallback in self.fallbacks.get(requested, ()):
            fallback_upstream = self._resolve_alias(fallback)
            fallback_capability = self._capability(fallback)
            if _tier(fallback_capability) < _tier(primary_capability) and not approve_weaker_fallback:
                raise CapabilityDowngradeError(requested, fallback)
            if fallback_upstream:
                return fallback
        raise AdmissionError(f"unavailable model alias: {requested!r}")


def _default_capabilities_for_aliases(aliases: Mapping[str, str]) -> dict[str, LaneCapability]:
    capabilities: dict[str, LaneCapability] = {}
    for alias, upstream in aliases.items():
        capability = _default_capability(upstream)
        capabilities[str(alias)] = capability
        capabilities[str(upstream)] = capability
    return capabilities


def _default_capability(upstream: str) -> LaneCapability:
    return LaneCapability(tier=_DEFAULT_TIERS.get(str(upstream), 4))


def _tier(capability: LaneCapability) -> int:
    return int(capability.tier)


def _is_forbidden_child_route(alias: str) -> bool:
    lower = alias.lower()
    return lower.startswith("gpt-") or lower in {"opencode_bridge", "openai", "chatgpt"}
