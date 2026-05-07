"""Legacy continuation execution-mode helpers.

These functions keep deterministic mode behavior out of the HTTP handler while
preserving the existing text contracts used by older tests.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
import time
from typing import Callable, Dict, List

JSON = Dict[str, object]


@dataclass
class LegacyModeDecision:
    handled: bool
    text: str = ""
    log_event: str = ""
    log_fields: dict = None
    wait_for_model_write: bool = False
    continue_for_verification: bool = False
    ledger_text: str = ""
    response_obj: dict = None
    degraded_report: dict = None

    def __post_init__(self):
        if self.log_fields is None:
            self.log_fields = {}
        if self.response_obj is None:
            self.response_obj = {}
        if self.degraded_report is None:
            self.degraded_report = {}


def handle_context_pack_report(
    body: dict,
    envelope: dict,
    handoff_text: str,
    prev_id: str,
    prev_state_messages: list,
    mode: str,
    tool_output_text: str,
    request_deadline: float,
    request_start: float,
    project_root: str,
    continuation_model: str,
    model_map: dict,
    continuation_deadline: float,
    continuation_fallbacks: list,
    max_tool_output_chars: int,
    log_fn: Callable[..., None],
    map_model: Callable[[str, dict], str],
    call_continuation_with_deadline: Callable[[dict, float], dict],
    build_task_session: Callable[..., object],
    extract_read_paths_from_history: Callable[[list], list],
    build_context_pack: Callable[[object, str], str],
    evaluate_evidence_coverage: Callable[[dict, str], tuple],
    is_intent_or_status: Callable[[str], bool],
    validate_report_output: Callable[..., tuple],
    build_context_pack_deterministic_report: Callable[[object, str, str], str],
) -> LegacyModeDecision:
    """Run legacy context-pack synthesis and fallback, returning terminal text."""
    log_fn("context_pack", mode=mode, paths=envelope.get("read_only_paths", []))
    session = build_task_session(body, handoff_text, prev_id or "unknown")
    read_paths = extract_read_paths_from_history(prev_state_messages)
    session.read_paths = {path: {"complete": True} for path in read_paths}
    pack = build_context_pack(session, project_root)
    evidence_coverage_complete, _, _, _ = evaluate_evidence_coverage(envelope, pack)
    finalizer_payload = {
        "model": map_model(continuation_model, model_map),
        "messages": [{"role": "system", "content":
            f"You are producing a report from the provided source pack.\n"
            f"Required outputs: {', '.join(session.required_outputs)}\n\n"
            f"SOURCE PACK:\n{pack}\n\n"
            f"Do not request tools. Produce a structured report including all required outputs. "
            f"Distinguish source-document claims, planned success criteria, and actually observed verification. "
            f"Do not say tests passed, commands ran, files changed, or routing occurred unless the source pack explicitly contains that executed result."}],
        "stream": False,
        "tools": [],
    }

    try:
        chat_resp = call_continuation_with_deadline(finalizer_payload, continuation_deadline)
        text = chat_resp.get("choices", [{}])[0].get("message", {}).get("content", "")
        if is_intent_or_status(text):
            log_fn("intent_rejected", text_len=len(text))
            remaining = request_deadline - (time.time() - request_start)
            if remaining > 20:
                retry_payload = {
                    "model": finalizer_payload["model"],
                    "messages": finalizer_payload["messages"] + [
                        {"role": "user", "content":
                         "You returned status/intent text instead of a report. "
                         "That is invalid. Return the final report now. Do not describe future actions. "
                         "Include PASS, FAIL, or PARTIAL; confidence; caveats; and every required output field."}
                    ],
                    "stream": False,
                    "tools": [],
                }
                try:
                    chat_resp = call_continuation_with_deadline(retry_payload, continuation_deadline * 0.7)
                    text = chat_resp.get("choices", [{}])[0].get("message", {}).get("content", "")
                    log_fn("intent_retry_ok", text_len=len(text))
                except Exception:
                    text = "PARTIAL\nConfidence: LOW\nCaveats: model returned intent/status text; retry failed."
                    log_fn("intent_retry_failed")
        if is_intent_or_status(text):
            log_fn("context_pack_intent_fallback", text_len=len(text))
            text = build_context_pack_deterministic_report(session, pack, tool_output_text)
        else:
            is_valid, missing = validate_report_output(
                text, mode, envelope, evidence_coverage_complete=evidence_coverage_complete)
            if not is_valid:
                log_fn("context_pack_report_contract_fallback", missing=missing, text_len=len(text))
                text = build_context_pack_deterministic_report(session, pack, tool_output_text)
        return LegacyModeDecision(True, text=text, log_event="context_pack_report_complete",
                                  log_fields={"text_len": len(text)})
    except Exception as exc:
        log_fn("context_pack_failed", error=str(exc))
        text = ""
        for fb_model in continuation_fallbacks:
            remaining = request_deadline - (time.time() - request_start)
            if remaining <= 5:
                break
            fallback_payload = dict(finalizer_payload)
            fallback_payload["model"] = map_model(fb_model, model_map)
            try:
                log_fn("context_pack_fallback_try", fallback_model=fallback_payload["model"])
                chat_resp = call_continuation_with_deadline(
                    fallback_payload, min(continuation_deadline, remaining - 2))
                text = chat_resp.get("choices", [{}])[0].get("message", {}).get("content", "")
                is_valid, missing = (
                    validate_report_output(
                        text, mode, envelope,
                        evidence_coverage_complete=evidence_coverage_complete)
                    if text else (False, ["empty_report"])
                )
                if text and is_valid:
                    log_fn("context_pack_fallback_ok", fallback_model=fallback_payload["model"], text_len=len(text))
                    break
                log_fn("context_pack_fallback_invalid", fallback_model=fallback_payload["model"], missing=missing, text_len=len(text))
                text = ""
            except Exception as fallback_error:
                log_fn("context_pack_fallback_failed", fallback_model=fallback_payload["model"], error=str(fallback_error))
        if not text:
            text = build_context_pack_deterministic_report(session, pack, tool_output_text)
        return LegacyModeDecision(True, text=text, log_event="context_pack_degraded_report",
                                  log_fields={"text_len": len(text)})


def handle_read_finalizer(
    body: dict,
    prev_state_messages: list,
    tool_outputs: list,
    tool_kind: str,
    compacted_output: str,
    model_alias: str,
    reverse_name_map: dict,
    continuation_model: str,
    model_map: dict,
    continuation_tools: str,
    continuation_deadline: float,
    continuation_fallbacks: list,
    max_tool_output_chars: int,
    degraded_completion_on_timeout: bool,
    log_fn: Callable[..., None],
    map_model: Callable[[str, dict], str],
    repair_chat_history: Callable[[list, list], list],
    merge_new_user_messages: Callable[[list, list], list],
    extract_handoff_text: Callable[[list], str],
    extract_required_deliverables: Callable[[str], list],
    validate_report: Callable[[str, list], tuple],
    call_continuation_with_deadline: Callable[[dict, float], dict],
    build_response_object: Callable[..., dict],
    build_degraded_completion: Callable[[str, str, str], dict],
) -> LegacyModeDecision:
    """Run the legacy read finalizer/fallback ladder."""
    finalizer_model = map_model(continuation_model, model_map)
    original_task = ""
    for msg in prev_state_messages:
        if msg.get("role") == "user" and msg.get("content"):
            original_task = str(msg["content"])[:500]
            break

    finalizer_messages = list(repair_chat_history(prev_state_messages, tool_outputs))
    finalizer_messages = merge_new_user_messages(finalizer_messages, [])
    finalizer_messages.append({
        "role": "user",
        "content": (
            f"Task: {original_task}\n\n"
            f"Tool executed: {tool_kind} operation\n"
            f"Tool result:\n{compacted_output[:max_tool_output_chars]}"
        )
    })

    finalizer_payload = {
        "model": finalizer_model,
        "messages": finalizer_messages,
        "stream": False,
    }
    if continuation_tools == "none":
        finalizer_payload["tools"] = []
        finalizer_payload["tool_choice"] = "none"

    log_fn("continuation_finalizer", model=finalizer_model, deadline=continuation_deadline)
    try:
        chat_resp = call_continuation_with_deadline(finalizer_payload, continuation_deadline)
        resp_obj = build_response_object(
            body, chat_resp, finalizer_messages, model_alias,
            map_model(model_alias, model_map), reverse_name_map)
        text = _first_response_text(resp_obj)
        handoff_text = extract_handoff_text(finalizer_messages)
        required = extract_required_deliverables(handoff_text)
        is_valid, missing = validate_report(text, required)
        if not is_valid and text:
            log_fn("report_invalid", missing=missing, text_len=len(text))
        elif not is_valid and not text:
            log_fn("report_empty")
        return LegacyModeDecision(True, text=text or "Finalizer completed.",
                                  response_obj=resp_obj, log_event="continuation_finalizer_ok")
    except Exception as exc:
        log_fn("continuation_finalizer_failed", error=str(exc))

    for fb_model_name in continuation_fallbacks:
        fb_model = map_model(fb_model_name, model_map)
        fb_payload = dict(finalizer_payload)
        fb_payload["model"] = fb_model
        try:
            chat_resp = call_continuation_with_deadline(fb_payload, continuation_deadline * 0.7)
            resp_obj = build_response_object(
                body, chat_resp, finalizer_messages, model_alias,
                map_model(model_alias, model_map), reverse_name_map)
            return LegacyModeDecision(True, text=_first_response_text(resp_obj) or "Fallback finalizer completed.",
                                      response_obj=resp_obj,
                                      log_event="continuation_fallback_ok",
                                      log_fields={"fallback_model": fb_model})
        except Exception:
            log_fn("continuation_fallback_failed", model=fb_model)

    if degraded_completion_on_timeout:
        degraded = build_degraded_completion(model_alias, "all finalizers failed", tool_kind)
        return LegacyModeDecision(True, text=degraded["content"][0]["text"],
                                  degraded_report=degraded,
                                  log_event="continuation_degraded_complete")
    return LegacyModeDecision(False, log_event="continuation_finalizer_failed_all")


def _first_response_text(resp_obj: dict) -> str:
    for output in resp_obj.get("output", []):
        if output.get("type") == "message":
            content = output.get("content") or []
            if content:
                return content[0].get("text", "")
    return ""


def handle_bounded_write_exact(
    envelope: dict,
    mode: str,
    tool_kind: str,
    project_root: str,
    log_fn: Callable[..., None],
) -> LegacyModeDecision:
    """Deterministically handle bounded exact writes when enough data is present."""
    owned = envelope.get("owned_paths", [])
    if not owned:
        return LegacyModeDecision(False)

    path = owned[0]
    exact_content = envelope.get("exact_content", "")
    full = os.path.normpath(os.path.join(project_root, path))

    if exact_content and not os.path.exists(full):
        try:
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(exact_content + "\n")
            log_fn("bounded_write_runtime_write", path=path, bytes=len(exact_content) + 1)
        except Exception as exc:
            log_fn("bounded_write_runtime_write_failed", path=path, error=str(exc))

    try:
        observed = open(full).read().strip()
        if observed != exact_content:
            return LegacyModeDecision(
                True,
                text=(
                    f"FAIL\n"
                    f"File checked: {path}\n"
                    f"Observed content: {observed}\n"
                    f"Expected content: {exact_content}\n"
                    f"Confidence: HIGH — deterministic read-back did not match declared exact_content; "
                    f"Caveat: final OSS model report bypassed by bridge runtime (mode={mode})."
                ),
                log_event="bounded_write_mismatch",
                log_fields={"path": path},
            )
        return LegacyModeDecision(
            True,
            text=(
                f"PASS\n"
                f"File changed: {path}\n"
                f"Observed content: {observed}\n"
                f"Confidence: HIGH — deterministic read-back after tool execution; "
                f"Caveat: final OSS model report bypassed by bridge runtime (mode={mode})."
            ),
            log_event="bounded_write_complete",
            log_fields={"path": path},
        )
    except Exception as exc:
        log_fn("bounded_write_failed", error=str(exc))
        if tool_kind != "write":
            return LegacyModeDecision(False, wait_for_model_write=True, log_fields={"path": path})
        return LegacyModeDecision(
            True,
            text=(
                f"FAIL\n"
                f"File changed: {path}\n"
                f"Observed content: [read-back failed: {exc}]\n"
                f"Confidence: LOW — assigned file could not be read after tool execution; "
                f"Caveat: no model finalizer was allowed to infer success."
            ),
        )


def handle_bounded_patch_continuation(
    envelope: dict,
    tool_kind: str,
    tool_output_text: str,
    compacted_output: str,
    exit_code: int,
    target_path: str,
    turn: int,
    max_exchanges: int,
    project_root: str,
    collect_owned_path_changes: Callable[[List[str], str], List[str]],
    path_is_within_owned_paths: Callable[[str, list, str], bool],
    build_patch_contract_report: Callable[..., str],
    tool_output_indicates_failure: Callable[[str], bool],
) -> LegacyModeDecision:
    """Handle bounded patch verification/write continuation branches."""
    if tool_kind == "shell":
        changed_paths = collect_owned_path_changes(envelope.get("owned_paths", []), project_root)
        failed = exit_code != 0 or tool_output_indicates_failure(tool_output_text)
        if not changed_paths:
            status = "FAIL" if failed else "PARTIAL"
            reason = "verification tool was observed, but no changed owned path was visible in git status"
        elif failed:
            status = "FAIL"
            reason = "verification tool output indicated failure"
        else:
            status = "PASS"
            reason = "owned path changes and verification tool output were both observed"
        return LegacyModeDecision(
            True,
            text=build_patch_contract_report(
                envelope,
                changed_paths,
                status,
                reason,
                verification_seen=True,
                verification_output=compacted_output,
            ),
            log_event="bounded_patch_verification_complete",
            log_fields={"status": status, "changed_paths": changed_paths},
        )

    if tool_kind != "write" or exit_code != 0:
        return LegacyModeDecision(False)

    owned_paths = envelope.get("owned_paths", [])
    if target_path and not path_is_within_owned_paths(target_path, owned_paths, project_root):
        return LegacyModeDecision(
            True,
            text=build_patch_contract_report(
                envelope,
                [],
                "FAIL",
                f"write target {target_path} is outside the declared owned paths",
            ),
            log_event="bounded_patch_scope_violation",
            log_fields={"target_path": target_path, "owned_paths": owned_paths},
        )

    changed_paths = collect_owned_path_changes(owned_paths, project_root)
    if not changed_paths:
        return LegacyModeDecision(
            True,
            text=build_patch_contract_report(
                envelope,
                [],
                "FAIL",
                "write tool returned success but no changed owned path was visible in git status",
            ),
            log_event="bounded_patch_no_owned_change",
            log_fields={"owned_paths": owned_paths},
        )

    if turn < max_exchanges:
        ledger_text = (
            "PATCH CONTRACT LEDGER\n"
            "Execution mode: bounded_write_patch\n"
            f"Owned paths: {', '.join(owned_paths)}\n"
            f"Changed owned paths observed: {', '.join(changed_paths)}\n"
            f"Required outputs: {', '.join(envelope.get('deliverable_fields', []))}\n"
            f"Verification steps: {', '.join(envelope.get('verification_steps', []))}\n"
            "Next: run the requested verification if possible, then return the final report. "
            "Do not edit outside the owned paths."
        )
        return LegacyModeDecision(
            True,
            continue_for_verification=True,
            ledger_text=ledger_text,
            log_event="bounded_patch_continue_for_verification",
            log_fields={"changed_paths": changed_paths},
        )

    return LegacyModeDecision(
        True,
        text=build_patch_contract_report(
            envelope,
            changed_paths,
            "PARTIAL",
            "owned path changes were observed, but verification was not observed before the tool budget ended",
        ),
        log_event="bounded_patch_partial_no_verification",
        log_fields={"changed_paths": changed_paths},
    )
