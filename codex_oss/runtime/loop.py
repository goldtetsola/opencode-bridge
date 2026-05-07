"""Runtime loop — plan-act-observe cycle for A2/A3 managed investigation."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple

JSON = Dict[str, Any]

ACTION_RE = re.compile(r'\{[^{}]*"action_type"[^{}]*\}', re.DOTALL)


class ModelAction:
    """Parsed model action from ManagedInvestigationActionV1 JSON."""

    def __init__(self, raw: dict):
        self.action_type = str(raw.get("action_type", ""))
        self.tool_name = str(raw.get("tool_name", ""))
        self.arguments = raw.get("arguments", {})
        self.reason = str(raw.get("reason", ""))
        self.report = raw.get("report")

    @property
    def is_tool_call(self) -> bool:
        return self.action_type == "tool_call"

    @property
    def is_final_report(self) -> bool:
        return self.action_type == "final_report"

    @property
    def is_valid(self) -> bool:
        if self.action_type == "tool_call":
            return bool(self.tool_name)
        return self.action_type == "final_report" and self.report is not None


def parse_action(text: str) -> Optional[ModelAction]:
    """Parse a model response into exactly one action. Returns None on failure."""
    # Try raw JSON first
    text = text.strip()
    try:
        d = json.loads(text)
        if isinstance(d, dict) and "action_type" in d:
            return ModelAction(d)
    except (json.JSONDecodeError, ValueError):
        pass

    # Try JSON block
    m = re.search(r'```json\s*\n(.*?)\n```', text, re.DOTALL)
    if m:
        try:
            d = json.loads(m.group(1))
            if isinstance(d, dict) and "action_type" in d:
                return ModelAction(d)
        except (json.JSONDecodeError, ValueError):
            pass

    # Try to find any JSON object with action_type
    matches = ACTION_RE.findall(text)
    for match in matches:
        try:
            d = json.loads(match)
            if isinstance(d, dict) and "action_type" in d:
                return ModelAction(d)
        except (json.JSONDecodeError, ValueError):
            pass

    return None


REPAIR_PROMPT = """Your previous response was invalid because: {reason}.
Return exactly one ManagedInvestigationActionV1 JSON object.
Do not include markdown. Do not include status text. Do not include more than one action.
Valid action_types: tool_call (with tool_name and arguments) or final_report (with report object)."""


def run_loop(mission: Any, ledger: Any, call_model_fn, tools, emitter, 
             allowed_roots: list, allowed_paths: list,
             max_repair: int = 1, request_deadline: float = 90) -> dict:
    """Run the plan-act-observe loop for a managed investigation mission.

    Returns: (status, report_dict or partial_dict)
    """
    start = time.time()
    repair_count = 0

    # Build initial model context
    context = _build_context(mission, ledger, len(tools))

    while True:
        elapsed = time.time() - start
        if elapsed > request_deadline - 5:  # reserve 5s for deterministic partial
            return _deterministic_partial(mission, ledger, "deadline_reached")

        if ledger.tool_budget_remaining <= 0:
            return _deterministic_partial(mission, ledger, "budget_exhausted")

        # Call model with context
        try:
            response = call_model_fn(context, tools, request_deadline - elapsed - 5)
        except Exception as e:
            if ledger.tool_budget_remaining > 0:
                return _deterministic_partial(mission, ledger, f"model_call_failed: {e}")
            return _deterministic_partial(mission, ledger, "budget_exhausted")

        text = response.get("choices", [{}])[0].get("message", {}).get("content", "")

        action = parse_action(text)
        if not action:
            repair_count += 1
            if repair_count <= max_repair:
                context.append({"role": "user", "content": REPAIR_PROMPT.format(reason="no valid action found in response")})
                continue
            return _deterministic_partial(mission, ledger, "action_parse_failed")

        if not action.is_valid:
            repair_count += 1
            if repair_count <= max_repair:
                context.append({"role": "user", "content": REPAIR_PROMPT.format(reason=f"invalid action: {text[:200]}")})
                continue
            return _deterministic_partial(mission, ledger, "invalid_action")

        if action.is_final_report:
            # Validate and return
            from codex_oss.validation import validate_report
            report = action.report
            if isinstance(report, dict):
                result = validate_report(report, ledger)
                if result.is_valid:
                    return {"status": result.status, "report": report}
                # Try repair once
                if repair_count < max_repair:
                    repair_count += 1
                    context.append({"role": "user", "content": 
                        REPAIR_PROMPT.format(reason=f"report validation failed: {result.errors}")})
                    continue
                return {"status": "PARTIAL", "report": report, "missing_fields": result.missing_fields}
            return _deterministic_partial(mission, ledger, "report_not_dict")

        if action.is_tool_call:
            repair_count = 0  # valid action resets repair count

            # Check writes are blocked
            if action.tool_name in ("apply_patch", "write_file", "edit", "create"):
                ledger.add_risk_flag("write_blocked")
                context.append({"role": "user", "content": "Write operations are blocked in read-only managed investigation. Use read/search/list/git tools only."})
                continue

            # Resolve and validate path
            path = action.arguments.get("path", "") if isinstance(action.arguments, dict) else ""
            if path:
                from codex_oss.runtime import resolve_path
                resolved, error = resolve_path(path, allowed_roots, allowed_paths)
                if error:
                    ledger.add_risk_flag(f"path_blocked:{error[:80]}")
                    context.append({"role": "user", "content": f"Path blocked: {error}"})
                    continue
                if isinstance(action.arguments, dict):
                    action.arguments["path"] = resolved

            # Check duplicate
            if action.tool_name == "rtk_read" and path and ledger.is_duplicate(path):
                ledger.record_duplicate()
                remaining = mission.tool_budget - len(ledger.files_inspected)
                context.append({"role": "user", "content":
                    f"[ALREADY READ] {path} was already inspected completely. "
                    f"Remaining budget: {ledger.tool_budget_remaining}. Continue with next unread source."})
                continue

            # Execute tool
            from codex_oss.runtime import TOOL_EXECUTORS
            executor = TOOL_EXECUTORS.get(action.tool_name)
            if not executor:
                ledger.add_risk_flag(f"unknown_tool:{action.tool_name}")
                context.append({"role": "user", "content": f"Unknown tool: {action.tool_name}. Allowed: {list(TOOL_EXECUTORS.keys())}"})
                continue

            ledger.spend_budget()
            result = executor(path, max_bytes=100000) if path and action.tool_name == "rtk_read" else \
                     executor(action.arguments.get("pattern", ""), path) if action.tool_name == "rtk_grep" and path else \
                     executor(path) if action.tool_name == "rtk_ls" and path else \
                     executor() if action.tool_name == "rtk_git_status" else \
                     executor(action.arguments.get("limit", 5)) if action.tool_name == "rtk_git_log" else \
                     executor(action.arguments.get("rev", "HEAD")) if action.tool_name == "rtk_git_show_stat" else \
                     executor(action.arguments.get("path", "")) if action.tool_name == "rtk_git_diff_stat" else \
                     executor(**action.arguments) if isinstance(action.arguments, dict) else executor()

            # Record in ledger
            if action.tool_name == "rtk_read" and path:
                ledger.add_file(path, result, len(ledger.commands_run))
            ledger.add_command(action.tool_name, action.arguments, result, len(ledger.commands_run))

            # Feed observation back
            obs = result.stdout[:6000] if result.stdout else "(empty output)"
            if result.stderr:
                obs += f"\nstderr: {result.stderr[:500]}"
            context.append({"role": "user", "content": f"Tool result ({action.tool_name}):\n{obs}"})

            # Check stop conditions
            if "critical_path_detected" in mission.stop_conditions and ledger.risk_flags:
                return _deterministic_partial(mission, ledger, "critical_path_detected")


def _build_context(mission: Any, ledger: Any, tool_count: int) -> list:
    tools_list = "\n".join(f"- {t}" for t in ["rtk_read", "rtk_grep", "rtk_ls", 
        "rtk_git_status", "rtk_git_log", "rtk_git_show_stat", "rtk_git_diff_stat"])
    return [{"role": "system", "content": 
        f"You are an OSS managed investigation agent. Mission: {mission.objective}\n"
        f"Allowed tools:\n{tools_list}\n"
        f"Required outputs: {', '.join(mission.required_outputs)}\n"
        f"Budget: {ledger.tool_budget_remaining} tool calls remaining.\n"
        f"Return exactly one JSON action per turn. No markdown, no status text."}]


def _deterministic_partial(mission: Any, ledger: Any, reason: str) -> dict:
    return {
        "status": "PARTIAL",
        "report": {
            "oss_report_version": "1.0",
            "mission_id": mission.mission_id,
            "status": "PARTIAL",
            "confidence": "LOW",
            "files_inspected": [{"path": p, "complete": e.complete} for p, e in ledger.files_inspected.items()],
            "commands_run": [{"tool": c.tool, "args": c.args} for c in ledger.commands_run],
            "findings": [],
            "uncertainties": [],
            "caveats": [f"Mission terminated: {reason}"],
            "escalation_recommendation": "GPT-5.5 review required",
            "missing_fields": [],
        }
    }
