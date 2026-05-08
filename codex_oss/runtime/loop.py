"""Runtime loop — plan-act-observe cycle for A2/A3 managed investigation."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple

JSON = Dict[str, Any]

ACTION_RE = re.compile(r'\{[^{}]*"action_type"[^{}]*\}', re.DOTALL)


class ModelAction:
    """Parsed model action from ManagedInvestigationActionV1 JSON."""

    def __init__(self, raw: dict):
        self.raw = raw
        self.action_type = str(raw.get("action_type", ""))
        self.tool_name = str(raw.get("tool_name", ""))
        self.arguments = raw.get("arguments", {})
        self.reason = str(raw.get("reason", ""))
        self.hypothesis = str(raw.get("hypothesis", ""))
        self.expected_information_gain = str(raw.get("expected_information_gain", ""))
        self.why_not_report_yet = str(raw.get("why_not_report_yet", ""))
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
        critical_read_allowed_for_path, objective_coverage_errors,
    )
    from codex_oss.runtime.autonomy import assess_action_progress
    from codex_oss.decision_trace import append_decision

    start = _time.time()
    deadline = DeadlinePolicy(request_deadline=request_deadline)
    deadline.max_total_model_calls = int(getattr(mission, "max_model_calls", deadline.max_total_model_calls))
    repair_count = 0
    forced_final_requested = False

    # Concurrency
    acquired, slot_reason = acquire_mission_slot(mission)
    if not acquired:
        append_decision(
            mission,
            decision_type="scheduler",
            result="rejected",
            policy="ConcurrencyPolicyV1",
            reason=slot_reason,
            source_module="codex_oss/runtime/policy.py",
            input_payload={"tier": getattr(mission, "tier", ""), "apply_mode": getattr(mission, "apply_mode", "")},
        )
        report = _partial(mission, ledger, f"capacity_timeout:{slot_reason}", deadline)["report"]
        report["status"] = "FAILED"
        return {"status": "FAILED", "reason": f"concurrency: {slot_reason}", "report": report}
    append_decision(
        mission,
        decision_type="scheduler",
        result="accepted",
        policy="ConcurrencyPolicyV1",
        reason="mission acquired a scheduler slot",
        source_module="codex_oss/runtime/policy.py",
        input_payload={"tier": getattr(mission, "tier", ""), "apply_mode": getattr(mission, "apply_mode", "")},
    )

    try:
        # Risk tier check
        allowed, risk_reason = risk_tier_allows_autonomy(mission.risk_tier, mission.tier)
        if not allowed:
            append_decision(
                mission,
                decision_type="risk_tier",
                result="rejected",
                policy="RiskTierPolicyV1",
                reason=risk_reason,
                source_module="codex_oss/runtime/policy.py",
                input_payload={"risk_tier": mission.risk_tier, "tier": mission.tier},
            )
            return {"status": "ESCALATE", "reason": risk_reason,
                    "report": _partial_dict(mission, ledger, risk_reason)}

        # Broad scope check
        scope_error = validate_broad_scope(mission)
        if scope_error:
            append_decision(
                mission,
                decision_type="scope_validation",
                result="rejected",
                policy="BroadScopePolicyV1",
                reason=scope_error,
                source_module="codex_oss/runtime/policy.py",
                input_payload={"allowed_roots": list(getattr(mission, "allowed_roots", []) or [])},
            )
            return {"status": "ESCALATE", "reason": scope_error,
                    "report": _partial_dict(mission, ledger, scope_error)}
        append_decision(
            mission,
            decision_type="scope_validation",
            result="accepted",
            policy="BroadScopePolicyV1",
            reason="mission scope passed broad-root validation",
            source_module="codex_oss/runtime/policy.py",
            input_payload={"allowed_roots": list(getattr(mission, "allowed_roots", []) or []), "allowed_paths": list(getattr(mission, "allowed_paths", []) or [])},
        )

        # Build initial context — no native tools for A2/A3
        if model_call_uses_internal_tools_only(mission.tier):
            tools = []
        allowed_tool_names = _allowed_tool_names(mission)
        context = _build_context(mission, ledger, allowed_tool_names)

        while True:
            elapsed = _time.time() - start
            deadline._elapsed = elapsed
            if deadline.must_return_partial():
                has_evidence = bool(getattr(ledger, "files_inspected", {}) or getattr(ledger, "commands_run", []))
                if has_evidence and not forced_final_requested and deadline.remaining() > 6:
                    forced_final_requested = True
                    context.append({"role": "user", "content": (
                        "Deadline is near. Return exactly one final_report JSON object now using only the "
                        f"available evidence refs: {_evidence_ref_summary(ledger)}. Do not call another tool."
                    )})
                else:
                    return _partial(mission, ledger, "deadline_reached", deadline)

            if ledger.tool_budget_remaining <= 0:
                return _partial(mission, ledger, "budget_exhausted", deadline)

            if not deadline.can_call_model() and not forced_final_requested:
                return _partial(mission, ledger, "model_call_limit_reached", deadline)

            # Enforce context budget
            context = enforce_context_budget(context, max_chars=6000)

            # Call model
            model_timeout = max(1, deadline.remaining() - 2)
            if forced_final_requested:
                final_timeout = float(os.getenv("RUNTIME_FINAL_REPORT_MODEL_TIMEOUT_SECONDS", "20"))
                model_timeout = max(1, min(model_timeout, final_timeout))
            try:
                response = call_model_fn(context, tools, model_timeout)
                deadline.record_call()
            except Exception as e:
                return _partial(mission, ledger, f"model_call_failed: {e}", deadline)

            text = _extract_model_text(response)

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
                        report["confidence"] = min_confidence(report.get("confidence", "LOW"), "LOW")
                        _annotate_report_provenance(mission, report, "model_report")
                        return {"status": "ESCALATE", "report": report}

                    result = validate_report(report, ledger)
                    if result.is_valid:
                        coverage_errors = objective_coverage_errors(mission, report, ledger)
                        if coverage_errors:
                            if repair_count < max_repair:
                                repair_count += 1
                                context.append({"role": "user", "content":
                                    REPAIR_PROMPT.format(
                                        reason=(
                                            f"objective coverage: {coverage_errors}. "
                                            f"Use available evidence refs: {_evidence_ref_summary(ledger)}"
                                        )
                                    )})
                                continue
                            return _partial(mission, ledger, f"objective_coverage_failed:{','.join(coverage_errors[:3])}", deadline)
                        _annotate_report_provenance(mission, report, "model_report")
                        return {"status": result.status, "report": report}
                    if repair_count < max_repair:
                        repair_count += 1
                        context.append({"role": "user", "content":
                            REPAIR_PROMPT.format(
                                reason=(
                                    f"report validation: {result.errors}. "
                                    f"Use available evidence refs: {_evidence_ref_summary(ledger)}"
                                )
                            )})
                        continue
                    return _partial(mission, ledger, f"report_validation_failed:{','.join(result.errors[:3])}", deadline)
                return _partial(mission, ledger, "report_not_dict", deadline)

            if action.is_tool_call:
                if forced_final_requested:
                    _record_action_trace(
                        mission, ledger, action, _mission_phase(ledger), "blocked",
                        "deadline final report was requested; tool call ignored",
                        deadline, getattr(action, "arguments", {}) if isinstance(action.arguments, dict) else {},
                        getattr(action, "arguments", {}) if isinstance(action.arguments, dict) else {},
                        [],
                    )
                    return _partial(mission, ledger, "deadline_final_report_ignored", deadline)
                tool_name = action.tool_name
                raw_arguments = dict(action.arguments) if isinstance(action.arguments, dict) else {}
                if isinstance(action.arguments, dict):
                    action.arguments, unsupported_arguments = _normalize_action_arguments(tool_name, action.arguments)
                else:
                    unsupported_arguments = []
                has_file_evidence = bool(getattr(ledger, "files_inspected", {}))
                if unsupported_arguments:
                    if has_file_evidence:
                        forced_final_requested = True
                        _record_action_trace(
                            mission, ledger, action, _mission_phase(ledger), "redirected",
                            "unsupported follow-up after file evidence; redirected to report",
                            deadline, raw_arguments, action.arguments, unsupported_arguments,
                        )
                        context.append({"role": "user", "content": (
                            f"Unsupported arguments for {tool_name}: {', '.join(unsupported_arguments)}. "
                            "File evidence is already available, so do not call another tool. "
                            "Return exactly one final_report JSON object now using evidence refs: "
                            f"{_evidence_ref_summary(ledger)}."
                        )})
                        continue
                    _record_action_trace(
                        mission, ledger, action, _mission_phase(ledger), "repaired",
                        "unsupported arguments rejected before tool execution",
                        deadline, raw_arguments, action.arguments, unsupported_arguments,
                    )
                    context.append({"role": "user", "content": (
                        f"Unsupported arguments for {tool_name}: {', '.join(unsupported_arguments)}. "
                        f"Supported arguments are: {', '.join(_supported_tool_args(tool_name))}. "
                        "Return one corrected tool_call or a final_report. The previous tool was not executed "
                        "and no budget was spent."
                    )})
                    continue
                repair_count = 0

                # Block writes
                if tool_name in ("apply_patch", "write_file", "edit", "create", "rtk_write"):
                    ledger.add_risk_flag("write_blocked")
                    _record_action_trace(
                        mission, ledger, action, _mission_phase(ledger), "blocked",
                        "write operation blocked in read-only mission",
                        deadline, raw_arguments, action.arguments, [],
                    )
                    context.append({"role": "user", "content": "Write operations blocked in read-only managed investigation."})
                    continue

                # Resolve path
                path = action.arguments.get("path", "") if isinstance(action.arguments, dict) else ""
                if path:
                    from codex_oss.runtime import resolve_path
                    resolved, error = resolve_path(path, allowed_roots, allowed_paths)
                    if error:
                        ledger.add_risk_flag(f"path_blocked:{error[:80]}")
                        _record_action_trace(
                            mission, ledger, action, _mission_phase(ledger), "blocked",
                            f"path blocked: {error}",
                            deadline, raw_arguments, action.arguments, [],
                        )
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
                        _record_action_trace(
                            mission, ledger, action, _mission_phase(ledger), "blocked",
                            f"critical path blocked: {critical_reason}",
                            deadline, raw_arguments, action.arguments, [],
                        )
                        return _partial(mission, ledger, f"critical_path_detected:{critical_reason}", deadline)
                    ledger.add_risk_flag("critical_path_read_allowed")

                progress = assess_action_progress(mission, ledger, action, deadline)
                if progress.decision == "finalize":
                    forced_final_requested = True
                    _record_action_trace(
                        mission, ledger, action, progress.phase, "redirected",
                        progress.reason,
                        deadline, raw_arguments, action.arguments, [],
                        progress=progress,
                    )
                    context.append({"role": "user", "content": (
                        f"Adaptive autonomy budget requires closure: {progress.reason}. "
                        "Do not call another tool. Return exactly one final_report JSON object now "
                        f"using available evidence refs: {_evidence_ref_summary(ledger)}."
                    )})
                    continue
                if progress.decision == "redirect":
                    _record_action_trace(
                        mission, ledger, action, progress.phase, "redirected",
                        progress.reason,
                        deadline, raw_arguments, action.arguments, [],
                        progress=progress,
                    )
                    context.append({"role": "user", "content": (
                        f"{progress.suggested_message or progress.reason} "
                        f"Available evidence refs: {_evidence_ref_summary(ledger)}"
                    )})
                    continue

                # Duplicate suppression
                if tool_name == "rtk_read" and path and ledger.is_duplicate(path):
                    existing_entry = getattr(ledger, "files_inspected", {}).get(path)
                    wants_full_read = not ("start_line" in action.arguments or "end_line" in action.arguments)
                    if wants_full_read and existing_entry is not None and not getattr(existing_entry, "full_content_cached", False):
                        pass
                    else:
                        cached = _cached_read_range(ledger, path, action.arguments)
                        if cached is not None:
                            extract_ref, cached_text = cached
                            _record_action_trace(
                                mission, ledger, action, _mission_phase(ledger), "cache_hit",
                                "served requested line range from cached file evidence",
                                deadline, raw_arguments, action.arguments, [],
                                result={
                                    "exit_code": 0,
                                    "stdout_chars": len(cached_text),
                                    "stderr_chars": 0,
                                    "matches_count": cached_text.count("\n"),
                                    "complete": True,
                                    "redactions_applied": False,
                                },
                            )
                            context.append({"role": "user", "content": (
                                f"Cached range result ({path}):\n{cached_text[:5000]}\n\n"
                                f"New evidence ref: {extract_ref}\n"
                                f"Available evidence refs for final_report findings: {_evidence_ref_summary(ledger)}\n"
                                "Current phase: VERIFY. If this evidence is sufficient, return final_report now."
                            )})
                            continue
                        ledger.record_duplicate()
                        forced_final_requested = True
                        cached_summary = _cached_extract_summary(mission, ledger, path)
                        _record_action_trace(
                            mission, ledger, action, _mission_phase(ledger), "redirected",
                            "complete file was already read; cached extracts supplied for final report",
                            deadline, raw_arguments, action.arguments, [],
                        )
                        context.append({"role": "user", "content":
                            f"[ALREADY READ] {path}.\n"
                            f"Cached extracts:\n{cached_summary}\n\n"
                            "Do not read the same file again. Return exactly one final_report JSON object now "
                            f"using available evidence refs: {_evidence_ref_summary(ledger)}."})
                        continue
                if tool_name != "rtk_read" and isinstance(action.arguments, dict) and ledger.is_duplicate_command(tool_name, action.arguments):
                    ledger.record_duplicate()
                    _record_action_trace(
                        mission, ledger, action, _mission_phase(ledger), "duplicate",
                        "same tool and equivalent arguments were already run",
                        deadline, raw_arguments, action.arguments, [],
                    )
                    context.append({"role": "user", "content":
                        f"[DUPLICATE TOOL] {tool_name} with these arguments was already run. "
                        f"Use available evidence refs ({_evidence_ref_summary(ledger)}) or return final_report."})
                    continue

                # Execute tool
                from codex_oss.runtime import TOOL_EXECUTORS
                executor = TOOL_EXECUTORS.get(tool_name)
                if not executor or tool_name not in allowed_tool_names:
                    _record_action_trace(
                        mission, ledger, action, _mission_phase(ledger), "blocked",
                        f"tool not allowed: {tool_name}",
                        deadline, raw_arguments, action.arguments, [],
                    )
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
                        context.append({"role": "user", "content": (
                            "Risk note: observation contained critical-domain terms "
                            f"({', '.join(terms[:3])}). Treat this as a caveat, do not make final "
                            "safety/merge/correctness claims, and recommend GPT review if the final "
                            "finding depends on those terms."
                        )})

                # Record
                if tool_name == "rtk_read" and path:
                    ledger.add_file(path, result, len(ledger.commands_run))
                ledger.add_command(tool_name, action.arguments, result, len(ledger.commands_run))
                _record_action_trace(
                    mission, ledger, action, _mission_phase(ledger), "allowed",
                    "tool executed",
                    deadline, raw_arguments, action.arguments, [],
                    result=_tool_result_summary(result),
                )

                # Observation
                obs = result.stdout[:5000] if result and result.stdout else "(empty)"
                if result and result.stderr:
                    obs += f"\nstderr: {result.stderr[:300]}"
                # Scan obs for secrets before sending to model
                obs, _ = scan_secrets(obs)
                next_action_hint = _next_action_hint(tool_name, result)
                hint_text = f"\nRuntime next-action hint: {next_action_hint}\n" if next_action_hint else ""
                objective_satisfied = _objective_satisfied(mission, ledger)
                objective_text = ""
                if objective_satisfied:
                    forced_final_requested = True
                    objective_text = (
                        "\nRuntime objective check: explicit objective_spec appears satisfied by current evidence. "
                        "Return final_report now; do not call another tool.\n"
                    )
                context.append({"role": "user", "content": (
                    f"Tool result ({tool_name}):\n{obs}\n\n"
                    f"Available evidence refs for final_report findings: {_evidence_ref_summary(ledger)}\n"
                    f"{hint_text}"
                    f"{objective_text}"
                    f"Current phase: {_mission_phase(ledger)}. "
                    "If this evidence is sufficient, return final_report now. If more exploration is needed, "
                    "the next action must include hypothesis, expected_information_gain, and why_not_report_yet."
                )})
    finally:
        release_mission_slot(mission.mission_id)


def _exec_tool(tool_name: str, executor, arguments: dict, path: str):
    """Dispatch tool execution based on tool name."""
    if tool_name == "rtk_read" and path:
        result = executor(path, max_bytes=100000)
        return _apply_read_range(result, arguments)
    if tool_name == "rtk_grep" and path:
        return executor(
            arguments.get("pattern", ""),
            path,
            case_sensitive=arguments.get("case_sensitive"),
            include_glob=arguments.get("include_glob"),
            max_results=int(arguments.get("max_results", 100) or 100),
            context_lines=int(arguments.get("context_lines", 0) or 0),
        )
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


def _extract_model_text(response: Any) -> str:
    """Extract assistant text from Chat-like provider responses defensively."""
    if isinstance(response, str):
        return response
    if not isinstance(response, dict):
        return ""
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0]
    if isinstance(choice, str):
        return choice
    if not isinstance(choice, dict):
        return ""
    message = choice.get("message", choice)
    if isinstance(message, str):
        return message
    if not isinstance(message, dict):
        return ""
    content = message.get("content", "")
    return _content_to_text(content)


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (int, float, bool)):
        return str(content)
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "")
    return str(content)


def min_confidence(a: str, b: str) -> str:
    order = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    return a if order.get(a, 0) < order.get(b, 0) else b


def _partial_dict(mission, ledger, reason: str) -> dict:
    from codex_oss.runtime.policy import build_deterministic_partial_report
    return {"status": "PARTIAL", "report": build_deterministic_partial_report(mission, ledger, reason)}


def _partial(mission, ledger, reason: str, deadline) -> dict:
    return _partial_dict(mission, ledger, reason)


def _annotate_report_provenance(mission: Any, report: dict, source: str) -> None:
    report.setdefault("report_source", source)
    report.setdefault("runtime_model_alias", getattr(mission, "runtime_model_alias", ""))
    report.setdefault("explorer_model", getattr(mission, "last_reasoning_model", ""))
    report.setdefault("finalizer_model", None)
    report.setdefault("fallback_model_used", bool(getattr(mission, "fallback_model_used", False)))


def _build_context(mission: Any, ledger: Any, allowed_tool_names: Optional[set] = None) -> list:
    allowed_tool_names = allowed_tool_names or _allowed_tool_names(mission)
    tools_list = "\n".join(f"- {t}" for t in sorted(allowed_tool_names))
    allowed_roots = getattr(mission, "allowed_roots", []) or []
    allowed_paths = getattr(mission, "allowed_paths", []) or []
    roots_list = "\n".join(f"- {p}" for p in allowed_roots) or "- (none)"
    paths_list = "\n".join(f"- {p}" for p in allowed_paths) or "- (none)"
    objective_spec = getattr(mission, "objective_spec", None)
    objective_contract = json.dumps(objective_spec, sort_keys=True) if isinstance(objective_spec, dict) else "(heuristic objective fallback)"
    return [{"role": "system", "content": 
        f"You are an OSS managed investigation agent. Mission: {mission.objective}\n"
        f"Objective contract: {objective_contract}\n"
        f"Allowed roots:\n{roots_list}\n"
        f"Allowed exact paths:\n{paths_list}\n"
        f"Use only these roots/paths. Do not guess alternate filenames or read '.'.\n"
        f"Allowed tools:\n{tools_list}\n"
        f"Required outputs: {', '.join(mission.required_outputs)}\n"
        f"Budget: {ledger.tool_budget_remaining} tool calls remaining. Adaptive limits: "
        f"max_broad_searches={getattr(mission, 'max_broad_searches', 6)}, "
        f"max_duplicate_actions={getattr(mission, 'max_duplicate_actions', 2)}, "
        f"max_low_information_actions={getattr(mission, 'max_low_information_actions', 3)}.\n"
        f"Current phase: {_mission_phase(ledger)}. Phases are EXPLORE, NARROW, VERIFY, REPORT.\n"
        f"Return exactly one JSON action per turn. No markdown, no status text.\n"
        f"To inspect evidence, return: "
        f'{{"action_type":"tool_call","tool_name":"rtk_read","arguments":{{"path":"<allowed path>"}},'
        f'"reason":"...","hypothesis":"...","expected_information_gain":"...",'
        f'"why_not_report_yet":"..."}}\n'
        f"To finish, return: "
        f'{{"action_type":"final_report","report":{{"oss_report_version":"1.0","mission_id":"{mission.mission_id}",'
        f'"status":"COMPLETE","confidence":"LOW","files_inspected":[],"commands_run":[],'
        f'"findings":[{{"claim":"...","evidence_refs":["file:<path>#extract:1"],"confidence":"LOW"}}],'
        f'"uncertainties":[],"caveats":[],"escalation_recommendation":"GPT-5.5 review required","missing_fields":[]}}}}'}]


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


def _evidence_ref_summary(ledger: Any) -> str:
    refs = []
    for path, entry in getattr(ledger, "files_inspected", {}).items():
        extracts = getattr(entry, "extracts", []) or []
        for extract in extracts[:3]:
            extract_id = str(extract.get("id", "extract:1")).replace("extract:", "")
            refs.append(f"file:{path}#extract:{extract_id}")
        if not extracts:
            refs.append(f"file:{path}#extract:1")
    for idx, command in enumerate(getattr(ledger, "commands_run", []) or []):
        if getattr(command, "matches_count", None) == 0 and getattr(command, "exit_code", None) == 1:
            refs.append(f"command:{idx}#zero_match")
        else:
            refs.append(f"command:{idx}")
    return ", ".join(refs) if refs else "(none yet)"


def _normalize_arguments(arguments: dict) -> dict:
    normalized, _unsupported = _normalize_action_arguments("", arguments)
    return normalized


def _normalize_action_arguments(tool_name: str, arguments: dict) -> tuple[dict, list[str]]:
    normalized = dict(arguments)
    if tool_name == "rtk_read":
        if "start_line" not in normalized and "offset" in normalized:
            try:
                normalized["start_line"] = max(1, int(normalized["offset"]) + 1)
            except (TypeError, ValueError):
                pass
        if "end_line" not in normalized and "limit" in normalized:
            try:
                start_line = int(normalized.get("start_line") or 1)
                normalized["end_line"] = max(start_line, start_line + int(normalized["limit"]) - 1)
            except (TypeError, ValueError):
                pass
    if "start_line" not in normalized and "start" in normalized:
        normalized["start_line"] = normalized["start"]
    if "end_line" not in normalized and "end" in normalized:
        normalized["end_line"] = normalized["end"]
    normalized.pop("start", None)
    normalized.pop("end", None)
    normalized.pop("offset", None)
    if tool_name == "rtk_read":
        normalized.pop("limit", None)
    if "pattern" not in normalized:
        for alias in ("query", "regex"):
            if alias in normalized:
                normalized["pattern"] = normalized[alias]
                break
    if "include_glob" not in normalized:
        for alias in ("include", "glob"):
            if alias in normalized:
                normalized["include_glob"] = normalized[alias]
                break
    if "context_lines" not in normalized:
        for alias in ("output_context_lines", "context", "context_line_count"):
            if alias in normalized:
                normalized["context_lines"] = normalized[alias]
                break
    if tool_name == "rtk_grep" and "options" in normalized:
        option_tokens = str(normalized.get("options") or "").split()
        safe_tokens = {"-r", "--recursive", "-n", "--line-number"}
        ignore_case_tokens = {"-i", "--ignore-case"}
        if option_tokens and all(token in safe_tokens | ignore_case_tokens for token in option_tokens):
            if any(token in ignore_case_tokens for token in option_tokens):
                normalized["case_sensitive"] = False
            normalized.pop("options", None)
    if "path" not in normalized:
        for plural_alias in ("paths", "files"):
            value = normalized.get(plural_alias)
            if isinstance(value, list) and value:
                normalized["path"] = value[0]
                break
        for alias in ("file_path", "file", "filename", "root", "dir", "directory", "folder"):
            if "path" not in normalized and alias in normalized:
                normalized["path"] = normalized[alias]
                break
    for alias in (
        "file_path", "file", "filename", "root", "dir", "directory", "folder",
        "query", "regex", "include", "glob", "output_context_lines", "context",
        "context_line_count", "paths", "files",
    ):
        normalized.pop(alias, None)
    for noise in ("recurse", "recursive"):
        normalized.pop(noise, None)
    supported = set(_supported_tool_args(tool_name))
    if tool_name:
        unsupported = sorted(k for k in normalized if k not in supported)
        for key in unsupported:
            normalized.pop(key, None)
    else:
        unsupported = []
    return normalized, unsupported


def _supported_tool_args(tool_name: str) -> list[str]:
    supported = {
        "rtk_read": ["path", "start_line", "end_line"],
        "rtk_grep": ["pattern", "path", "case_sensitive", "include_glob", "max_results", "context_lines"],
        "rtk_ls": ["path"],
        "rtk_git_status": [],
        "rtk_git_log": ["limit"],
        "rtk_git_show_stat": ["rev"],
        "rtk_git_diff_stat": ["path"],
    }
    return supported.get(tool_name, ["path", "pattern"])


def _apply_read_range(result: Any, arguments: dict) -> Any:
    if not result or not isinstance(arguments, dict):
        return result
    if "start_line" not in arguments and "end_line" not in arguments:
        return result
    original_chars_total = getattr(result, "chars_total", len(getattr(result, "stdout", "") or ""))
    try:
        start_line = int(arguments.get("start_line") or 1)
        end_line = int(arguments.get("end_line") or start_line)
    except (TypeError, ValueError):
        result.stderr = (getattr(result, "stderr", "") or "") + "\ninvalid line range ignored"
        return result
    start_line = max(1, start_line)
    end_line = max(start_line, end_line)
    lines = (getattr(result, "stdout", "") or "").splitlines()
    selected = lines[start_line - 1:end_line]
    result.stdout = "\n".join(selected)
    if selected:
        result.stdout += "\n"
    result.args = dict(getattr(result, "args", {}) or {})
    result.args.update({"start_line": start_line, "end_line": end_line})
    result.chars_total = original_chars_total
    result.complete = False
    return result


def _cached_read_range(ledger: Any, path: str, arguments: dict) -> Optional[Tuple[str, str]]:
    if not isinstance(arguments, dict):
        return None
    if "start_line" not in arguments and "end_line" not in arguments:
        return None
    entry = getattr(ledger, "files_inspected", {}).get(path)
    if not entry:
        return None
    cached_text = getattr(entry, "cached_text", "") or ""
    if not cached_text:
        return None
    try:
        start_line = int(arguments.get("start_line") or 1)
        end_line = int(arguments.get("end_line") or start_line)
    except (TypeError, ValueError):
        return None
    start_line = max(1, start_line)
    end_line = max(start_line, end_line)
    lines = cached_text.splitlines()
    selected = lines[start_line - 1:end_line]
    if not selected:
        return None
    text = "\n".join(selected) + "\n"
    extract_ref = ledger.add_cached_extract(path, text)
    if not extract_ref:
        return None
    return extract_ref, text


def _next_action_hint(tool_name: str, result: Any) -> str:
    if tool_name != "rtk_grep" or not result:
        return ""
    text = str(getattr(result, "stdout", "") or "")
    definition = re.search(r"(?m)^([^:\n]+):(\d+):\s*RUNTIME_AUTONOMY_PROFILES\s*=", text)
    if definition:
        path = definition.group(1)
        try:
            line = int(definition.group(2))
        except ValueError:
            line = 1
        start = max(1, line)
        end = line + 30
        return (
            "Definition candidate found. Prefer a targeted read next: "
            f'{{"path":"{path}","start_line":{start},"end_line":{end}}}.'
        )
    rtk_definition = _rtk_grep_match_location(text, r"\bRUNTIME_AUTONOMY_PROFILES\s*=")
    if rtk_definition:
        path, line = rtk_definition
        start = max(1, line)
        end = line + 30
        return (
            "Definition candidate found. Prefer a targeted read next: "
            f'{{"path":"{path}","start_line":{start},"end_line":{end}}}.'
        )
    profile = re.search(r'(?m)^([^:\n]+):(\d+):.*"mission-a3-deepseek"', text)
    if profile:
        path = profile.group(1)
        try:
            line = int(profile.group(2))
        except ValueError:
            line = 1
        start = max(1, line - 5)
        end = line + 5
        return (
            "DeepSeek A3 profile candidate found. Prefer a targeted read next: "
            f'{{"path":"{path}","start_line":{start},"end_line":{end}}}.'
        )
    return ""


def _rtk_grep_match_location(text: str, pattern: str) -> Optional[Tuple[str, int]]:
    current_file = ""
    compiled = re.compile(pattern)
    for line in text.splitlines():
        file_match = re.match(r"\[file\]\s+(.+?)\s+\(\d+\):", line)
        if file_match:
            current_file = file_match.group(1).strip()
            continue
        line_match = re.match(r"\s*(\d+):\s*(.*)$", line)
        if current_file and line_match and compiled.search(line_match.group(2)):
            try:
                return current_file, int(line_match.group(1))
            except ValueError:
                return current_file, 1
    return None


def _cached_extract_summary(mission: Any, ledger: Any, path: str, max_chars: int = 1800) -> str:
    entry = getattr(ledger, "files_inspected", {}).get(path)
    if not entry:
        return "(none)"
    try:
        from codex_oss.runtime.policy import _append_objective_extracts
        _append_objective_extracts(mission, path, entry)
    except Exception:
        pass
    lines = []
    for extract in (getattr(entry, "extracts", []) or [])[:3]:
        extract_id = str(extract.get("id", "extract:1"))
        text = str(extract.get("text", ""))[:500]
        lines.append(f"- file:{path}#{extract_id}: {text}")
    return "\n".join(lines)[:max_chars] if lines else "(none)"


def _mission_phase(ledger: Any) -> str:
    try:
        from codex_oss.runtime.autonomy import mission_phase
        return mission_phase(ledger)
    except Exception:
        if getattr(ledger, "files_inspected", {}):
            return "VERIFY"
        if getattr(ledger, "commands_run", []):
            return "NARROW"
        return "EXPLORE"


def _record_action_trace(
    mission: Any,
    ledger: Any,
    action: Any,
    phase: str,
    decision: str,
    reason: str,
    deadline: Any,
    raw_arguments: dict,
    normalized_arguments: dict,
    unsupported_arguments: list[str],
    result: Optional[dict] = None,
    progress: Any = None,
) -> None:
    try:
        from codex_oss.ledger import ActionTraceEntry
        entry = ActionTraceEntry(
            turn=len(getattr(ledger, "action_trace", [])) + 1,
            phase=phase,
            action_type=getattr(action, "action_type", ""),
            tool_name=getattr(action, "tool_name", ""),
            raw_arguments=dict(raw_arguments or {}),
            normalized_arguments=dict(normalized_arguments or {}),
            unsupported_arguments=list(unsupported_arguments or []),
            model_rationale=getattr(action, "reason", ""),
            hypothesis=getattr(action, "hypothesis", ""),
            expected_information_gain=getattr(action, "expected_information_gain", ""),
            why_not_report_yet=getattr(action, "why_not_report_yet", ""),
            runtime_decision=decision,
            decision_reason=reason,
            information_gain=_estimate_information_gain(decision, result or {}),
            novelty=str(getattr(progress, "novelty", "unknown") or "unknown") if progress else "unknown",
            specificity=str(getattr(progress, "specificity", "unknown") or "unknown") if progress else "unknown",
            evidence_linkage=bool(getattr(progress, "evidence_linkage", False)) if progress else False,
            broad_searches_used=int(getattr(progress, "broad_searches_used", 0) or 0) if progress else 0,
            low_information_actions_used=int(getattr(progress, "low_information_actions_used", 0) or 0) if progress else 0,
            duplicate_actions_used=int(getattr(progress, "duplicate_actions_used", 0) or 0) if progress else 0,
            deadline_remaining_seconds=round(float(deadline.remaining()), 2) if deadline else 0,
            budget_remaining=int(getattr(ledger, "tool_budget_remaining", 0)),
            tool_result_summary=result or {},
        )
        ledger.add_action_trace(entry)
        _write_action_trace(mission, entry)
    except Exception:
        return


def _write_action_trace(mission: Any, entry: Any) -> None:
    root = os.getcwd()
    mission_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(getattr(mission, "mission_id", "unknown")))
    trace_dir = os.path.join(root, ".codex-oss", "missions", mission_id)
    os.makedirs(trace_dir, exist_ok=True)
    path = os.path.join(trace_dir, "trace.jsonl")
    payload = {
        "turn": entry.turn,
        "phase": entry.phase,
        "action_type": entry.action_type,
        "tool_name": entry.tool_name,
        "raw_arguments": entry.raw_arguments,
        "normalized_arguments": entry.normalized_arguments,
        "unsupported_arguments": entry.unsupported_arguments,
        "model_rationale": entry.model_rationale,
        "hypothesis": entry.hypothesis,
        "expected_information_gain": entry.expected_information_gain,
        "why_not_report_yet": entry.why_not_report_yet,
        "runtime_decision": entry.runtime_decision,
        "decision_reason": entry.decision_reason,
        "information_gain": entry.information_gain,
        "novelty": entry.novelty,
        "specificity": entry.specificity,
        "evidence_linkage": entry.evidence_linkage,
        "broad_searches_used": entry.broad_searches_used,
        "low_information_actions_used": entry.low_information_actions_used,
        "duplicate_actions_used": entry.duplicate_actions_used,
        "deadline_remaining_seconds": entry.deadline_remaining_seconds,
        "budget_remaining": entry.budget_remaining,
        "tool_result_summary": entry.tool_result_summary,
    }
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _tool_result_summary(result: Any) -> dict:
    if not result:
        return {}
    return {
        "exit_code": getattr(result, "exit_code", None),
        "stdout_chars": len(getattr(result, "stdout", "") or ""),
        "stderr_chars": len(getattr(result, "stderr", "") or ""),
        "matches_count": (getattr(result, "stdout", "") or "").count("\n"),
        "complete": bool(getattr(result, "complete", False)),
        "redactions_applied": bool(getattr(result, "redactions_applied", False)),
    }


def _estimate_information_gain(decision: str, result: dict) -> str:
    if decision in ("blocked", "duplicate", "repaired"):
        return "none"
    if result.get("stdout_chars", 0) > 0:
        return "medium"
    return "low"


def _objective_satisfied(mission: Any, ledger: Any) -> bool:
    if not isinstance(getattr(mission, "objective_spec", None), dict):
        return False
    try:
        from codex_oss.runtime.objectives import synthesize_objective_finding
        return bool(synthesize_objective_finding(mission, ledger))
    except Exception:
        return False


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
