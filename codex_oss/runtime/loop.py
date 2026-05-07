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


def parse_action(text: str, strict: bool = True) -> Optional[ModelAction]:
    """Parse a model response into EXACTLY ONE action.

    Spec: ActionProtocolEnforcementV1.
    - Only raw JSON or a single JSON-fenced block is valid.
    - Markdown fences surrounding non-JSON, prose only, multiple JSON objects,
      arrays, or multiple actions are INVALID.
    - In strict mode, returns None immediately on any violation.
    - In non-strict mode, tries best-effort extraction (compatibility only).
    """
    text = text.strip()

    # ── Strict mode: enforce spec ──
    if strict:
        # Reject if text contains markdown fence but NOT exactly one JSON block
        fence_count = text.count("```json") + text.count("```")
        if fence_count > 0:
            m = re.match(r'^```json\s*\n(.*?)\n```\s*$', text, re.DOTALL)
            if not m:
                return None  # Has fences but not wrapping exactly one JSON block
            text = m.group(1).strip()

        # Try parsing as exactly one JSON object
        try:
            d = json.loads(text)
            if isinstance(d, list):
                return None  # Arrays are invalid
            if isinstance(d, dict) and "action_type" in d:
                # Check for multiple action markers
                if "tool_name" in d and "report" in d:
                    return None  # Can't be both tool_call and final_report
                return ModelAction(d)
        except (json.JSONDecodeError, ValueError):
            pass

        # Check for multiple JSON objects
        obj_count = len(re.findall(r'\{"action_type"\s*:', text))
        if obj_count == 0:
            return None  # Prose only — no action JSON found
        if obj_count > 1:
            return None  # Multiple actions in one response
        return None  # Found action_type but couldn't parse as clean JSON

    # ── Non-strict mode: best-effort (legacy compatibility) ──
    try:
        d = json.loads(text)
        if isinstance(d, dict) and "action_type" in d:
            return ModelAction(d)
    except (json.JSONDecodeError, ValueError):
        pass
    m = re.search(r'```json\s*\n(.*?)\n```', text, re.DOTALL)
    if m:
        try:
            d = json.loads(m.group(1))
            if isinstance(d, dict) and "action_type" in d:
                return ModelAction(d)
        except (json.JSONDecodeError, ValueError):
            pass
    matches = ACTION_RE.findall(text)
    if len(matches) == 1:
        try:
            d = json.loads(matches[0])
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
    """Run the plan-act-observe loop for a managed investigation mission."""
    import time as _time
    from codex_oss.runtime.policy import (
        DeadlinePolicy, is_critical_path, detect_critical_terms,
        detect_critical_finality, enforce_context_budget,
        acquire_mission_slot, release_mission_slot, scan_secrets,
        validate_broad_scope, risk_tier_allows_autonomy,
        risk_tier_confidence_ceiling, model_call_uses_internal_tools_only,
        critical_read_allowed_for_path,
    )

    start = _time.time()
    deadline = DeadlinePolicy(request_deadline=request_deadline)
    repair_count = 0

    # Concurrency
    acquired, slot_reason = acquire_mission_slot(mission.mission_id)
    if not acquired:
        return {"status": "FAILED", "reason": f"concurrency: {slot_reason}",
                "report": {"oss_report_version": "1.0", "mission_id": mission.mission_id,
                           "status": "FAILED", "confidence": "LOW",
                           "caveats": [slot_reason], "missing_fields": []}}

    try:
        # Risk tier check
        allowed, risk_reason = risk_tier_allows_autonomy(mission.risk_tier, mission.tier)
        if not allowed:
            return {"status": "ESCALATE", "reason": risk_reason,
                    "report": _partial_dict(mission, ledger, risk_reason)}

        # Broad scope check
        scope_error = validate_broad_scope(mission)
        if scope_error:
            return {"status": "ESCALATE", "reason": scope_error,
                    "report": _partial_dict(mission, ledger, scope_error)}

        # Build initial context — no native tools for A2/A3
        if model_call_uses_internal_tools_only(mission.tier):
            tools = []
        allowed_tool_names = _allowed_tool_names(mission)
        context = _build_context(mission, ledger, allowed_tool_names)

        while True:
            elapsed = _time.time() - start
            deadline._elapsed = elapsed
            if deadline.must_return_partial():
                return _partial(mission, ledger, "deadline_reached", deadline)

            if ledger.tool_budget_remaining <= 0:
                return _partial(mission, ledger, "budget_exhausted", deadline)

            if not deadline.can_call_model():
                return _partial(mission, ledger, "model_call_limit_reached", deadline)

            # Enforce context budget
            context = enforce_context_budget(context, max_chars=6000)

            # Call model
            try:
                response = call_model_fn(context, tools, deadline.remaining() - 2)
                deadline.record_call()
            except Exception as e:
                return _partial(mission, ledger, f"model_call_failed: {e}", deadline)

            text = response.get("choices", [{}])[0].get("message", {}).get("content", "")

            # Scan for secrets before ANY processing
            text, found_secret = scan_secrets(text)
            if found_secret:
                ledger.redactions_applied = True

            action = parse_action(text)
            if not action:
                repair_count += 1
                if repair_count <= max_repair:
                    context.append({"role": "user", "content": REPAIR_PROMPT.format(reason="no valid action found")})
                    continue
                return _partial(mission, ledger, "action_parse_failed", deadline)

            if not action.is_valid:
                repair_count += 1
                if repair_count <= max_repair:
                    context.append({"role": "user", "content": REPAIR_PROMPT.format(reason=f"invalid action: {text[:200]}")})
                    continue
                return _partial(mission, ledger, "invalid_action", deadline)

            if action.is_final_report:
                from codex_oss.validation import validate_report
                report = action.report
                if isinstance(report, dict):
                    # Check critical finality claims
                    rendered = ""
                    from codex_oss.validation import render_report
                    try:
                        rendered = render_report(report)
                    except Exception:
                        pass
                    if detect_critical_finality(rendered):
                        report["escalation_recommendation"] = "GPT-5.5 review required — critical finality claim detected"
                        report["caveats"] = report.get("caveats", []) + ["critical finality claim requires GPT review"]
                        report["confidence"] = min_confidence(report.get("confidence", "LOW"),
                                                              risk_tier_confidence_ceiling(mission.risk_tier))
                        return {"status": "ESCALATE", "report": report}

                    result = validate_report(report, ledger)
                    if result.is_valid:
                        return {"status": result.status, "report": report}
                    if repair_count < max_repair:
                        repair_count += 1
                        context.append({"role": "user", "content":
                            REPAIR_PROMPT.format(reason=f"report validation: {result.errors}")})
                        continue
                    return _partial(mission, ledger, f"report_validation_failed:{','.join(result.errors[:3])}", deadline)
                return _partial(mission, ledger, "report_not_dict", deadline)

            if action.is_tool_call:
                repair_count = 0
                tool_name = action.tool_name
                if isinstance(action.arguments, dict):
                    action.arguments = _normalize_arguments(action.arguments)

                # Block writes
                if tool_name in ("apply_patch", "write_file", "edit", "create", "rtk_write"):
                    ledger.add_risk_flag("write_blocked")
                    context.append({"role": "user", "content": "Write operations blocked in read-only managed investigation."})
                    continue

                # Resolve path
                path = action.arguments.get("path", "") if isinstance(action.arguments, dict) else ""
                if path:
                    from codex_oss.runtime import resolve_path
                    resolved, error = resolve_path(path, allowed_roots, allowed_paths)
                    if error:
                        ledger.add_risk_flag(f"path_blocked:{error[:80]}")
                        if is_critical_path(path):
                            ledger.add_risk_flag("critical_path_blocked")
                            return _partial(mission, ledger, "critical_path_detected", deadline)
                        context.append({"role": "user", "content": f"Path blocked: {error}"})
                        continue
                    if isinstance(action.arguments, dict):
                        action.arguments["path"] = resolved
                        path = resolved

                # Check critical paths
                if path and is_critical_path(path):
                    allowed_critical, critical_reason = critical_read_allowed_for_path(path, mission)
                    if not allowed_critical:
                        ledger.add_risk_flag("critical_path_blocked")
                        return _partial(mission, ledger, f"critical_path_detected:{critical_reason}", deadline)
                    ledger.add_risk_flag("critical_path_read_allowed")

                # Duplicate suppression
                if tool_name == "rtk_read" and path and ledger.is_duplicate(path):
                    ledger.record_duplicate()
                    context.append({"role": "user", "content":
                        f"[ALREADY READ] {path}. Continue with next unread source. Budget: {ledger.tool_budget_remaining}"})
                    continue

                # Execute tool
                from codex_oss.runtime import TOOL_EXECUTORS
                executor = TOOL_EXECUTORS.get(tool_name)
                if not executor or tool_name not in allowed_tool_names:
                    context.append({"role": "user", "content": f"Tool not allowed for this mission: {tool_name}"})
                    continue

                ledger.spend_budget()
                result = _exec_tool(tool_name, executor, action.arguments, path)

                # Scan for secrets in result
                if result and result.stdout:
                    result.stdout, found_secret = scan_secrets(result.stdout)
                    if found_secret:
                        result.redactions_applied = True
                        ledger.redactions_applied = True

                # Check critical terms in observation
                if result and result.stdout:
                    terms = detect_critical_terms(result.stdout)
                    if terms and not mission.critical_path_read_allowed:
                        ledger.add_risk_flag(f"critical_terms_detected:{','.join(terms[:3])}")
                        return _partial(mission, ledger, "critical_terms_detected", deadline)

                # Record
                if tool_name == "rtk_read" and path:
                    ledger.add_file(path, result, len(ledger.commands_run))
                ledger.add_command(tool_name, action.arguments, result, len(ledger.commands_run))

                # Observation
                obs = result.stdout[:5000] if result and result.stdout else "(empty)"
                if result and result.stderr:
                    obs += f"\nstderr: {result.stderr[:300]}"
                # Scan obs for secrets before sending to model
                obs, _ = scan_secrets(obs)
                context.append({"role": "user", "content": f"Tool result ({tool_name}):\n{obs}"})
    finally:
        release_mission_slot(mission.mission_id)


def _exec_tool(tool_name: str, executor, arguments: dict, path: str):
    """Dispatch tool execution based on tool name."""
    if tool_name == "rtk_read" and path:
        return executor(path, max_bytes=100000)
    if tool_name == "rtk_grep" and path:
        return executor(arguments.get("pattern", ""), path)
    if tool_name == "rtk_ls" and path:
        return executor(path)
    if tool_name in ("rtk_git_status",):
        return executor()
    if tool_name == "rtk_git_log":
        return executor(arguments.get("limit", 5))
    if tool_name == "rtk_git_show_stat":
        return executor(arguments.get("rev", "HEAD"))
    if tool_name == "rtk_git_diff_stat":
        return executor(arguments.get("path", ""))
    if isinstance(arguments, dict):
        return executor(**arguments)
    return executor()


def min_confidence(a: str, b: str) -> str:
    order = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    return a if order.get(a, 0) < order.get(b, 0) else b


def _partial_dict(mission, ledger, reason: str) -> dict:
    from codex_oss.runtime.policy import build_deterministic_partial_report
    return {"status": "PARTIAL", "report": build_deterministic_partial_report(mission, ledger, reason)}


def _partial(mission, ledger, reason: str, deadline) -> dict:
    return _partial_dict(mission, ledger, reason)


def _build_context(mission: Any, ledger: Any, allowed_tool_names: Optional[set] = None) -> list:
    allowed_tool_names = allowed_tool_names or _allowed_tool_names(mission)
    tools_list = "\n".join(f"- {t}" for t in sorted(allowed_tool_names))
    return [{"role": "system", "content": 
        f"You are an OSS managed investigation agent. Mission: {mission.objective}\n"
        f"Allowed tools:\n{tools_list}\n"
        f"Required outputs: {', '.join(mission.required_outputs)}\n"
        f"Budget: {ledger.tool_budget_remaining} tool calls remaining.\n"
        f"Return exactly one JSON action per turn. No markdown, no status text."}]


def _allowed_tool_names(mission: Any) -> set:
    classes = set(getattr(mission, "allowed_tool_classes", []) or [])
    tool_map = {
        "read": {"rtk_read"},
        "search": {"rtk_grep"},
        "list": {"rtk_ls"},
        "safe_git": {"rtk_git_status", "rtk_git_log", "rtk_git_show_stat", "rtk_git_diff_stat"},
    }
    names = set()
    for cls in classes:
        names.update(tool_map.get(cls, set()))
    return names


def _normalize_arguments(arguments: dict) -> dict:
    normalized = dict(arguments)
    if "path" not in normalized:
        for alias in ("file_path", "file", "filename"):
            if alias in normalized:
                normalized["path"] = normalized[alias]
                break
    return normalized


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
