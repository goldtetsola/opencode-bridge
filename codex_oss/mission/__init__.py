"""MissionV1 parser and tier classifier."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

JSON = Dict[str, Any]


# ── Tier constants ──

TIER_A0 = "A0"  # No-tool exact output
TIER_A1 = "A1"  # Context-pack compatibility
TIER_A2 = "A2"  # Guided exploration
TIER_A3 = "A3"  # Managed investigation
TIER_A4 = "A4"  # Patch proposal (future)
TIER_A5 = "A5"  # Bounded implementation (future)
TIER_A6 = "A6"  # Critical autonomous engineer (future)

VALID_TIERS_V1 = {TIER_A0, TIER_A1, TIER_A2, TIER_A3}
TOOL_AUTONOMY_TIERS = {TIER_A2, TIER_A3}

# ── Mode constants ──

MODE_NO_TOOL_EXACT = "no_tool_exact"
MODE_CONTEXT_PACK = "context_pack_report"
MODE_GUIDED_EXPLORATION = "guided_exploration"
MODE_MANAGED_INVESTIGATION = "managed_investigation"
MODE_BOUNDED_WRITE_EXACT = "bounded_write_exact"
MODE_BOUNDED_WRITE_PATCH = "bounded_write_patch"
MODE_ESCALATE = "escalate"
MODE_INVALID_HANDOFF = "invalid_handoff"

TIER_MODE_MAP = {
    TIER_A0: MODE_NO_TOOL_EXACT,
    TIER_A1: MODE_CONTEXT_PACK,
    TIER_A2: MODE_GUIDED_EXPLORATION,
    TIER_A3: MODE_MANAGED_INVESTIGATION,
}

# ── Tool classes ──

TOOL_CLASS_READ = "read"
TOOL_CLASS_SEARCH = "search"
TOOL_CLASS_LIST = "list"
TOOL_CLASS_SAFE_GIT = "safe_git"

ALLOWED_TOOL_CLASSES_V1 = {TOOL_CLASS_READ, TOOL_CLASS_SEARCH, TOOL_CLASS_LIST, TOOL_CLASS_SAFE_GIT}

# ── Default deny roots ──

DEFAULT_DENY_ROOTS = [
    ".env", "*.env", "secrets/", ".git/", "node_modules/",
    ".codex-oss/env/", ".codex-oss/state/", ".codex-oss/logs/",
]

DEFAULT_DENY_PATTERNS = [
    "private key", "-----BEGIN", "sk-", "OPENCODE_GO_API_KEY",
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DATABASE_URL",
]


@dataclass
class MissionV1:
    mission_id: str
    tier: str
    mode: str
    objective: str
    risk_tier: str = "low"
    write_allowed: bool = False
    allowed_roots: List[str] = field(default_factory=list)
    allowed_paths: List[str] = field(default_factory=list)
    forbidden_roots: List[str] = field(default_factory=list)
    forbidden_topics: List[str] = field(default_factory=list)
    critical_path_read_allowed: bool = False
    critical_path_reason: Optional[str] = None
    allow_broad_read_scope: bool = False
    max_files_to_inspect: int = 20
    max_total_observation_bytes: int = 500000
    tool_budget: int = 20
    time_budget_seconds: int = 180
    allowed_tool_classes: List[str] = field(default_factory=list)
    stop_conditions: List[str] = field(default_factory=list)
    report_schema: str = "managed_investigation_report.v1"
    required_outputs: List[str] = field(default_factory=list)
    fallback_policy: str = "fallback_policy.v1"
    deadline_policy: str = "deadline_policy.v1"

    # Budget clamping metadata
    tool_budget_requested: int = 0
    tool_budget_effective: int = 0
    budget_reduction_reason: str = ""


def parse_mission_v1(handoff_text: str) -> MissionV1:
    """Parse a MissionV1 from OSS_HANDOFF_JSON block or structured JSON."""
    block = _extract_handoff_json_block(handoff_text)
    if not block:
        raise InvalidHandoffError("No OSS_HANDOFF_JSON block found")

    try:
        raw = json.loads(block)
    except json.JSONDecodeError as e:
        raise InvalidHandoffError(f"Malformed JSON: {e}")

    return _build_mission(raw)


def _extract_handoff_json_block(text: str) -> Optional[str]:
    """Extract exactly one OSS_HANDOFF_JSON block from text."""
    import re
    blocks = re.findall(r"<OSS_HANDOFF_JSON>(.*?)</OSS_HANDOFF_JSON>", text, re.DOTALL)
    if not blocks:
        return None
    if len(blocks) > 1:
        raise InvalidHandoffError("Multiple OSS_HANDOFF_JSON blocks found — exactly one required")
    return blocks[0].strip()


def _build_mission(raw: dict) -> MissionV1:
    schema_version = raw.get("schema_version", "")
    if schema_version != "oss_agent_mission.v1":
        raise InvalidHandoffError(f"Expected schema_version 'oss_agent_mission.v1', got '{schema_version}'")

    mission_id = raw.get("mission_id", "")
    if not mission_id:
        mission_id = f"mission_{uuid.uuid4().hex[:12]}"

    tier = str(raw.get("tier", "")).upper()
    if tier not in VALID_TIERS_V1:
        raise InvalidHandoffError(f"Invalid tier '{tier}'. v1 supports: {sorted(VALID_TIERS_V1)}")

    mode = str(raw.get("mode", "")).lower()
    expected_mode = TIER_MODE_MAP.get(tier, "")
    if mode and mode != expected_mode:
        raise InvalidHandoffError(f"Tier {tier} requires mode '{expected_mode}', got '{mode}'")
    mode = expected_mode

    # v1 write_allowed must be false
    write_allowed = bool(raw.get("write_allowed", False))
    if write_allowed:
        raise InvalidHandoffError("v1 managed investigation must be read-only (write_allowed=false)")

    objective = str(raw.get("objective", ""))
    if not objective:
        raise InvalidHandoffError("objective is required")

    risk_tier = str(raw.get("risk_tier", "low")).lower()
    if risk_tier not in ("low", "medium", "critical"):
        risk_tier = "low"

    allowed_roots = _as_str_list(raw.get("allowed_roots", []))
    allowed_paths = _as_str_list(raw.get("allowed_paths", []))
    forbidden_roots = _as_str_list(raw.get("forbidden_roots", []))
    forbidden_topics = _as_str_list(raw.get("forbidden_topics", []))

    critical_path_read_allowed = bool(raw.get("critical_path_read_allowed", False))
    critical_path_reason = raw.get("critical_path_reason")

    tool_budget = int(raw.get("tool_budget", 20))
    time_budget_seconds = int(raw.get("time_budget_seconds", 180))
    allowed_tool_classes = _as_str_list(raw.get("allowed_tool_classes", []))

    # Validate tool classes
    for tc in allowed_tool_classes:
        if tc not in ALLOWED_TOOL_CLASSES_V1:
            raise InvalidHandoffError(f"Invalid tool class '{tc}'. v1 allows: {sorted(ALLOWED_TOOL_CLASSES_V1)}")

    # Budget clamping
    tier_max_budget = 10 if tier == TIER_A2 else 20
    tier_max_time = 90 if tier == TIER_A2 else 180
    effective_budget = min(tool_budget, tier_max_budget)
    effective_time = min(time_budget_seconds, tier_max_time)

    # Abusive values fail closed
    if tool_budget > 1000 or time_budget_seconds > 3600:
        raise InvalidHandoffError(f"Abusive budget values: tool_budget={tool_budget}, time={time_budget_seconds}")

    stop_conditions = _as_str_list(raw.get("stop_conditions", []))
    if not stop_conditions:
        stop_conditions = ["valid_report", "budget_exhausted", "deadline_reached"]

    required_outputs = _as_str_list(raw.get("required_outputs", []))
    if not required_outputs:
        required_outputs = ["files_inspected", "commands_run", "findings", "uncertainties",
                           "confidence", "caveats", "escalation_recommendation"]

    budget_reduction_reason = ""
    if effective_budget < tool_budget:
        budget_reduction_reason = "tier_cap"
    if effective_time < time_budget_seconds:
        budget_reduction_reason = "tier_cap" if not budget_reduction_reason else budget_reduction_reason + "+tier_cap"

    return MissionV1(
        mission_id=mission_id,
        tier=tier,
        mode=mode,
        objective=objective,
        risk_tier=risk_tier,
        write_allowed=False,
        allowed_roots=allowed_roots,
        allowed_paths=allowed_paths,
        forbidden_roots=forbidden_roots + DEFAULT_DENY_ROOTS,
        forbidden_topics=forbidden_topics,
        critical_path_read_allowed=critical_path_read_allowed,
        critical_path_reason=critical_path_reason,
        allow_broad_read_scope=bool(raw.get("allow_broad_read_scope", False)),
        max_files_to_inspect=int(raw.get("max_files_to_inspect", 20)),
        max_total_observation_bytes=int(raw.get("max_total_observation_bytes", 500000)),
        tool_budget=effective_budget,
        time_budget_seconds=effective_time,
        allowed_tool_classes=allowed_tool_classes if allowed_tool_classes else list(ALLOWED_TOOL_CLASSES_V1),
        stop_conditions=stop_conditions,
        report_schema=str(raw.get("report_schema", "managed_investigation_report.v1")),
        required_outputs=required_outputs,
        tool_budget_requested=tool_budget,
        tool_budget_effective=effective_budget,
        budget_reduction_reason=budget_reduction_reason,
    )


def _as_str_list(val: Any) -> List[str]:
    if isinstance(val, list):
        return [str(v) for v in val]
    if isinstance(val, str):
        return [v.strip() for v in val.split(",") if v.strip()]
    return []


class InvalidHandoffError(Exception):
    pass


def classify_handoff(text: str) -> tuple:
    """Classify a handoff text, returning (mission, errors) or fallback mode."""
    try:
        mission = parse_mission_v1(text)
        return mission, []
    except InvalidHandoffError as e:
        return None, [str(e)]
