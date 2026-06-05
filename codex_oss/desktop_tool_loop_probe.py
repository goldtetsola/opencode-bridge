"""DesktopToolLoopProbeV1 managed MissionV1 probe route.

This module emits one real Responses-compatible function_call from a validated
A2/A3/A4/A5 MissionV1 handoff. The Desktop consumer, not the runtime loop,
must resolve that call with function_call_output before the probe can pass.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from codex_oss.mission import InvalidHandoffError, _build_mission, extract_mission_v1_block
from codex_oss.mission_authority_artifacts import (
    write_adoption_or_recovery,
    write_mission_authority_artifacts,
)
from codex_oss.route_authority import build_route_authority
from codex_oss.tool_call_adoption import ResponsesToolStateMachine, persist_adoption_probes

JSON = dict[str, Any]


@dataclass
class DesktopToolLoopProbeResult:
    handled: bool
    response_obj: JSON | None = None
    mission_id: str = ""
    status: str = ""
    reason: str = ""


def mission_requests_desktop_tool_loop_probe(body: JSON) -> bool:
    handoff = extract_handoff_from_body(body)
    if not handoff:
        return False
    try:
        block = extract_mission_v1_block(handoff)
    except InvalidHandoffError:
        return False
    if not block:
        return False
    try:
        raw = json.loads(block)
    except json.JSONDecodeError:
        return False
    probe = raw.get("desktop_tool_loop_probe")
    return isinstance(probe, dict) and bool(probe.get("enabled"))


def build_desktop_tool_loop_probe_response(
    *,
    body: JSON,
    raw_model_alias: str,
    base_messages: list[JSON],
    project_root: str,
    state_put: Callable[[Any], None],
    stored_response_factory: Callable[..., Any],
    response_id: str,
    created_at: int,
    new_call_id: Callable[[str], str],
    json_dumps: Callable[[Any], str],
) -> DesktopToolLoopProbeResult:
    handoff = extract_handoff_from_body(body)
    if not handoff:
        return DesktopToolLoopProbeResult(handled=False)
    try:
        block = extract_mission_v1_block(handoff)
    except InvalidHandoffError as exc:
        return _terminal_failure(body, raw_model_alias, response_id, created_at, f"invalid MissionV1 handoff: {exc}")
    if not block:
        return DesktopToolLoopProbeResult(handled=False)
    try:
        mission = _build_mission(json.loads(block))
    except Exception as exc:
        return _terminal_failure(body, raw_model_alias, response_id, created_at, f"invalid MissionV1 handoff: {exc}")
    probe = getattr(mission, "desktop_tool_loop_probe", {}) or {}
    if not bool(probe.get("enabled")):
        return DesktopToolLoopProbeResult(handled=False)

    mission.runtime_model_alias = raw_model_alias
    mission.runtime_handoff_raw = handoff
    target_path = str(probe.get("target_path") or "")
    allowed, path_reason = _path_allowed(target_path, project_root, mission.allowed_roots, mission.allowed_paths)
    if not allowed:
        return _probe_failure(
            body,
            raw_model_alias,
            response_id,
            created_at,
            mission.mission_id,
            f"desktop_tool_loop_probe target rejected: {path_reason}",
        )

    selected = select_desktop_probe_tool(
        body.get("tools", []),
        intent=str(probe.get("tool_intent") or "safe_read"),
        target_path=target_path,
    )
    if not selected:
        return _probe_failure(
            body,
            raw_model_alias,
            response_id,
            created_at,
            mission.mission_id,
            "desktop_tool_loop_probe requires a matching read/search/list/git function in the incoming Desktop tool manifest",
        )
    expected_resolution = str(probe.get("expected_resolution") or "adopt_or_recover")
    if expected_resolution == "recover_after_tool_failure":
        injected = _recovery_failure_arguments(
            selected,
            intent=str(probe.get("tool_intent") or "safe_read"),
            target_path=target_path,
        )
        if injected is None:
            return _probe_failure(
                body,
                raw_model_alias,
                response_id,
                created_at,
                mission.mission_id,
                "desktop_tool_loop_probe recovery injection requires a shell-compatible or path-compatible Desktop tool",
            )
        selected = dict(selected)
        selected["arguments"] = injected
        selected["recovery_failure_injected"] = True

    call_id = new_call_id("call")
    item_id = new_call_id("fc")
    args_json = json_dumps(selected["arguments"])
    progress_output = _progress_output_items(
        mission.mission_id,
        new_call_id,
        tool_intent=str(probe.get("tool_intent") or "safe_read"),
        expected_resolution=str(probe.get("expected_resolution") or "adopt_or_recover"),
        tier=str(getattr(mission, "tier", "") or ""),
    )
    output = progress_output + [{
        "type": "function_call",
        "id": item_id,
        "call_id": call_id,
        "name": selected["name"],
        "arguments": args_json,
        "status": "completed",
    }]
    assistant_msg = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": selected["name"], "arguments": args_json},
        }],
    }
    all_messages = list(base_messages or []) + [assistant_msg]
    stored = stored_response_factory(
        response_id=response_id,
        model_alias=raw_model_alias,
        model_upstream=raw_model_alias,
        messages=all_messages,
        pending_call_ids=[call_id],
        created_at=created_at,
        output_items_json=json_dumps(output),
        tool_exchange_count=0,
        task_max_exchanges=1,
        previous_response_id=str(body.get("previous_response_id") or ""),
    )
    sm = ResponsesToolStateMachine(response_id)
    sm.parent_response_id = str(body.get("previous_response_id") or "") or None
    sm.register_tool_call(
        call_id=call_id,
        tool_name=selected["name"],
        arguments=selected["arguments"],
        output_item_id=item_id,
    )
    stored.adoption_probes_json = json_dumps({
        "calls": {cid: {k: v for k, v in state.items() if k != "arguments_preview"} for cid, state in sm.calls.items()},
        "sequence": sm.sequence,
    })
    state_put(stored)
    artifact_dir = _mission_dir(project_root, mission.mission_id)
    _write_probe_start_artifacts(
        artifact_dir=artifact_dir,
        mission=mission,
        selected=selected,
        response_id=response_id,
        item_id=item_id,
        call_id=call_id,
        state_machine=sm,
    )
    response_obj = _response_shell(body, raw_model_alias, response_id, created_at, output, "completed")
    return DesktopToolLoopProbeResult(
        handled=True,
        response_obj=response_obj,
        mission_id=mission.mission_id,
        status="PENDING",
        reason="desktop_function_call_emitted",
    )


def persist_desktop_tool_loop_adoption(
    *,
    project_root: str,
    stored: Any,
    call_id: str,
    tool_output_text: str,
) -> JSON:
    handoff = extract_handoff_from_messages(getattr(stored, "messages", []) or [])
    if not handoff:
        return {"handled": False, "reason": "missing_handoff"}
    try:
        block = extract_mission_v1_block(handoff)
        mission = _build_mission(json.loads(block or "{}"))
    except Exception as exc:
        return {"handled": False, "reason": f"invalid_handoff:{exc}"}
    probe = getattr(mission, "desktop_tool_loop_probe", {}) or {}
    if not bool(probe.get("enabled")):
        return {"handled": False, "reason": "not_desktop_tool_loop_probe"}

    sm = _state_machine_from_stored(stored)
    expected_resolution = str(probe.get("expected_resolution") or "adopt_or_recover")
    recovered = False
    recovery_reason = ""
    if call_id and call_id in sm.calls:
        sm.mark_adopted(call_id)
        sm.mark_completed(call_id, tool_output_text)
        if expected_resolution == "recover_after_tool_failure" and _tool_output_failed(tool_output_text):
            recovery_reason = "desktop_tool_output_failed_after_injected_recovery_probe"
            sm.mark_recovered(call_id, recovery_reason)
            recovered = True
    artifact_dir = _mission_dir(project_root, mission.mission_id)
    persist_adoption_probes(str(artifact_dir), sm.to_probes(), sm)
    write_adoption_or_recovery(
        artifact_dir=artifact_dir,
        mission_id=mission.mission_id,
        route_class="desktop_tool_loop_probe",
        pending_tool_calls_emitted=len(sm.calls),
        runtime_recovery_used=recovered,
        artifacts=["tool_call_adoption_probes.json", "tool_state_machine_ledger.json"],
    )
    if recovered:
        _write_json(artifact_dir / "recovery_proof.json", {
            "schema_version": "recovery_proof.v1",
            "mission_id": mission.mission_id,
            "status": "RECOVERED",
            "injected_failure": True,
            "reason": recovery_reason,
            "call_id": call_id,
            "recovery_class": "desktop_tool_execution_failure",
            "desktop_adoption_observed": True,
        })
    elif expected_resolution == "recover_after_tool_failure":
        _write_json(artifact_dir / "recovery_proof.json", {
            "schema_version": "recovery_proof.v1",
            "mission_id": mission.mission_id,
            "status": "UNCONFIRMED",
            "injected_failure": True,
            "reason": "injected_failure_not_observed_in_desktop_tool_output",
            "call_id": call_id,
            "recovery_class": "desktop_tool_execution_failure",
            "desktop_adoption_observed": True,
        })
    _write_probe_completion_artifacts(
        artifact_dir=artifact_dir,
        mission=mission,
        stored=stored,
        call_id=call_id,
        tool_output_text=tool_output_text,
        recovered=recovered,
    )
    implementation = _run_post_adoption_implementation_if_needed(
        mission=mission,
        raw_model_alias=str(getattr(stored, "model_alias", "") or ""),
        handoff=handoff,
        project_root=project_root,
        recovered=recovered,
    )
    status = str(implementation.get("status") or ("RECOVERED" if recovered else "PASS"))
    return {
        "handled": True,
        "mission_id": mission.mission_id,
        "status": status,
        "report_text": str(implementation.get("text") or ""),
        "implementation_continued": bool(implementation.get("continued")),
    }


def select_desktop_probe_tool(tools: Any, *, intent: str, target_path: str) -> JSON | None:
    candidates = [_normalize_tool(tool) for tool in tools if isinstance(tool, dict)]
    candidates = [item for item in candidates if item]
    if not candidates:
        return None
    buckets = {
        "safe_read": (("read", "cat", "open", "file"), _read_args),
        "safe_search": (("search", "grep", "rg", "find"), _search_args),
        "safe_list": (("list", "ls", "dir", "find"), _list_args),
        "safe_git": (("git", "status", "exec", "shell", "bash", "command"), _safe_git_args),
    }
    keywords, arg_builder = buckets.get(intent, buckets["safe_read"])
    ordered = sorted(candidates, key=lambda item: _tool_score(item["name"], keywords), reverse=True)
    for tool in ordered:
        if _tool_score(tool["name"], keywords) <= 0 and not _is_shell_tool(tool["name"]):
            continue
        args = arg_builder(tool, target_path)
        if args is not None:
            return {"name": tool["name"], "arguments": args, "manifest_tool": tool["raw"]}
    return None


def extract_handoff_from_body(body: JSON) -> str:
    return extract_handoff_from_messages(_messages_from_body(body))


def extract_handoff_from_messages(messages: list[JSON]) -> str:
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        text = _as_text(msg.get("content", ""))
        if "OSS_HANDOFF_JSON" in text:
            return text
    return ""


def _messages_from_body(body: JSON) -> list[JSON]:
    messages: list[JSON] = []
    inp = body.get("input", "")
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
            elif isinstance(item, dict):
                role = str(item.get("role") or "user")
                if role in {"user", "system", "developer", "assistant"}:
                    messages.append({"role": role, "content": item.get("content", item.get("text", ""))})
    return messages


def _normalize_tool(tool: JSON) -> JSON | None:
    raw = dict(tool)
    fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
    name = str(fn.get("name") or tool.get("name") or "").strip()
    if not name:
        return None
    params = fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {}
    props = params.get("properties") if isinstance(params.get("properties"), dict) else {}
    return {"name": name, "parameters": params, "properties": props, "raw": raw}


def _tool_score(name: str, keywords: tuple[str, ...]) -> int:
    lowered = name.lower()
    score = sum(3 for key in keywords if key in lowered)
    if _is_shell_tool(name):
        score += 1
    if any(bad in lowered for bad in ("write", "edit", "patch", "apply", "delete", "remove")):
        score -= 100
    return score


def _is_shell_tool(name: str) -> bool:
    lowered = name.lower()
    return any(part in lowered for part in ("exec", "shell", "bash", "run_command", "command"))


def _read_args(tool: JSON, target_path: str) -> JSON | None:
    if _is_shell_tool(tool["name"]):
        return _shell_args(tool, f"rtk read {shlex.quote(target_path)}")
    return _field_args(tool, target_path, ("path", "file_path", "filepath", "file", "filename", "target"))


def _search_args(tool: JSON, target_path: str) -> JSON | None:
    if _is_shell_tool(tool["name"]):
        return _shell_args(tool, f"rtk grep OSS_HANDOFF_JSON {shlex.quote(target_path)}")
    args = _field_args(tool, target_path, ("path", "file_path", "filepath", "file", "filename", "target"))
    if args is None:
        return None
    if "pattern" in tool["properties"]:
        args["pattern"] = "OSS_HANDOFF_JSON"
    elif "query" in tool["properties"]:
        args["query"] = "OSS_HANDOFF_JSON"
    return args


def _list_args(tool: JSON, target_path: str) -> JSON | None:
    directory = target_path if target_path.endswith("/") else os.path.dirname(target_path) or "."
    if _is_shell_tool(tool["name"]):
        return _shell_args(tool, f"rtk ls {shlex.quote(directory)}")
    return _field_args(tool, directory, ("path", "directory", "dir", "root", "target"))


def _safe_git_args(tool: JSON, target_path: str) -> JSON | None:
    if _is_shell_tool(tool["name"]):
        return _shell_args(tool, "rtk git status --short")
    return None


def _recovery_failure_arguments(selected: JSON, *, intent: str, target_path: str) -> JSON | None:
    tool = _normalize_tool(selected.get("manifest_tool", {})) or {
        "name": str(selected.get("name") or ""),
        "properties": {},
        "raw": selected.get("manifest_tool", {}),
    }
    tool_name = str(selected.get("name") or tool.get("name") or "")
    if _is_shell_tool(tool_name):
        if intent == "safe_git":
            return _shell_args(tool, "rtk git show-ref --verify refs/heads/__codex_missing_recovery_probe__")
        missing = f"{target_path}.__codex_missing_recovery_probe__"
        return _shell_args(tool, f"rtk read {shlex.quote(missing)}")
    missing_path = f"{target_path}.__codex_missing_recovery_probe__"
    return _field_args(tool, missing_path, ("path", "file_path", "filepath", "file", "filename", "target"))


def _tool_output_failed(text: str) -> bool:
    lowered = (text or "").lower()
    return any(
        marker in lowered
        for marker in (
            "exit_code: 1",
            "exit code: 1",
            "no such file",
            "not found",
            "fatal:",
            "error:",
            "command failed",
            "pretooluse hook",
        )
    )


def _run_post_adoption_implementation_if_needed(
    *,
    mission: Any,
    raw_model_alias: str,
    handoff: str,
    project_root: str,
    recovered: bool,
) -> JSON:
    if recovered or str(getattr(mission, "tier", "") or "").upper() not in {"A4", "A5"}:
        return {"continued": False}
    try:
        from codex_oss.implementation import run_implementation_mission
    except Exception as exc:
        return {"continued": False, "status": "FAILED", "text": f"FAILED\nSynthesis status: IMPLEMENTATION_IMPORT_FAILED\nReason: {exc}"}

    def no_model_call(_messages: Any, _tools: Any, _timeout: float) -> JSON:
        return {"choices": [{"message": {"content": ""}}]}

    result = run_implementation_mission(
        mission=mission,
        raw_model_alias=raw_model_alias,
        handoff=handoff,
        call_model=no_model_call,
        timeout=1.0,
        project_root=project_root,
        commentary=None,
    )
    result = result if isinstance(result, dict) else {}
    return {
        "continued": True,
        "status": str(result.get("status") or "FAILED"),
        "text": str(result.get("text") or ""),
    }


def _field_args(tool: JSON, value: str, names: tuple[str, ...]) -> JSON | None:
    props = tool["properties"]
    for name in names:
        if not props or name in props:
            return {name: value}
    return None


def _shell_args(tool: JSON, command: str) -> JSON | None:
    props = tool["properties"]
    for name in ("cmd", "command", "args", "arguments"):
        if not props or name in props:
            return {name: command}
    return None


def _path_allowed(path: str, project_root: str, allowed_roots: list[str], allowed_paths: list[str]) -> tuple[bool, str]:
    if not path or ".." in Path(path).parts:
        return False, "empty_or_escaping_path"
    root = Path(project_root).resolve()
    full = Path(path)
    if full.is_absolute():
        try:
            full.resolve().relative_to(root)
        except ValueError:
            return False, "absolute_path_outside_project"
        rel = str(full.resolve().relative_to(root))
    else:
        rel = str(Path(path))
    if rel in set(str(p).rstrip("/") for p in allowed_paths or []):
        return True, "allowed_path"
    rel_prefix = rel.rstrip("/") + "/"
    for allowed_root in allowed_roots or []:
        clean = str(allowed_root).rstrip("/")
        if rel == clean or rel_prefix.startswith(clean + "/"):
            return True, "allowed_root"
    return False, "path_not_declared_in_mission"


def _write_probe_start_artifacts(
    *,
    artifact_dir: Path,
    mission: Any,
    selected: JSON,
    response_id: str,
    item_id: str,
    call_id: str,
    state_machine: ResponsesToolStateMachine,
) -> None:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "mission_id": mission.mission_id,
        "route_class": "desktop_tool_loop_probe",
        "status": "PENDING",
        "runtime_model_alias": getattr(mission, "runtime_model_alias", ""),
        "tier": str(getattr(mission, "tier", "") or ""),
        "tool_intent": str((getattr(mission, "desktop_tool_loop_probe", {}) or {}).get("tool_intent") or "safe_read"),
        "expected_resolution": str((getattr(mission, "desktop_tool_loop_probe", {}) or {}).get("expected_resolution") or "adopt_or_recover"),
        "selected_tool_name": selected["name"],
        "response_id": response_id,
        "item_id": item_id,
        "call_id": call_id,
        "recovery_failure_injected": bool(selected.get("recovery_failure_injected")),
    }
    write_mission_authority_artifacts(
        artifact_dir=artifact_dir,
        mission=mission,
        summary_payload=summary,
        validation={"desktop_tool_loop_probe": "accepted"},
    )
    _write_json(artifact_dir / "desktop_tool_loop_probe.json", {
        "schema_version": "desktop_tool_loop_probe_result.v1",
        "mission_id": mission.mission_id,
        "status": "PENDING",
        "response_id": response_id,
        "item_id": item_id,
        "call_id": call_id,
        "tool_name": selected["name"],
        "tier": str(getattr(mission, "tier", "") or ""),
        "tool_intent": str((getattr(mission, "desktop_tool_loop_probe", {}) or {}).get("tool_intent") or "safe_read"),
        "expected_resolution": str((getattr(mission, "desktop_tool_loop_probe", {}) or {}).get("expected_resolution") or "adopt_or_recover"),
        "recovery_failure_injected": bool(selected.get("recovery_failure_injected")),
        "arguments_sha256": _sha256(json.dumps(selected["arguments"], sort_keys=True)),
        "manifest_tool_name": selected["manifest_tool"].get("name") or (selected["manifest_tool"].get("function") or {}).get("name", ""),
    })
    _write_json(artifact_dir / "route_authority.json", build_route_authority(
        agent_name="oss_desktop_tool_loop_probe",
        model_alias=str(getattr(mission, "runtime_model_alias", "") or ""),
        handoff_obj={"schema_version": "oss_agent_mission.v1"},
        consumer_kind="codex_desktop_spawned",
    ))
    persist_adoption_probes(str(artifact_dir), state_machine.to_probes(), state_machine)
    probe = getattr(mission, "desktop_tool_loop_probe", {}) or {}
    progress_events = _progress_events(
        mission.mission_id,
        tool_intent=str(probe.get("tool_intent") or "safe_read"),
        expected_resolution=str(probe.get("expected_resolution") or "adopt_or_recover"),
        tier=str(getattr(mission, "tier", "") or ""),
    )
    _write_text(
        artifact_dir / "visible_commentary.jsonl",
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in progress_events),
    )
    _write_json(artifact_dir / "commentary_delivery.json", {
        "schema_version": "commentary_delivery_tracker.v1",
        "mission_id": mission.mission_id,
        "events": {
            event["event_id"]: {
                "schema_version": "commentary_delivery.v1",
                "mission_id": mission.mission_id,
                "event_id": event["event_id"],
                "event_type": event["event_type"],
                "states": {"created": True, "stream_enqueued": True, "sse_emitted": True},
            }
            for event in progress_events
        },
    })
    _write_text(
        artifact_dir / "summary.md",
        "# Desktop Tool Loop Probe\n\n"
        f"- Mission: {mission.mission_id}\n"
        "- Status: PENDING\n"
        "- Progress events emitted before Desktop function_call: 3\n",
    )


def _write_probe_completion_artifacts(
    *,
    artifact_dir: Path,
    mission: Any,
    stored: Any,
    call_id: str,
    tool_output_text: str,
    recovered: bool = False,
) -> None:
    target_path = str((getattr(mission, "desktop_tool_loop_probe", {}) or {}).get("target_path") or "")
    expected_resolution = str((getattr(mission, "desktop_tool_loop_probe", {}) or {}).get("expected_resolution") or "adopt_or_recover")
    output_hash = _sha256(tool_output_text)
    report = {
        "mission_id": mission.mission_id,
        "status": "COMPLETE",
        "report_source": "desktop_tool_loop_probe",
        "closure_source": "desktop_tool_loop_probe",
        "runtime_entitlement_reconciliation": {"decision": "accepted_complete"},
        "sufficiency": {"enough_evidence_to_report": True},
        "findings": [{
            "claim": (
                "Desktop resolved a MissionV1-managed Responses function_call and the runtime recorded recovery after the injected failing tool result."
                if recovered else
                "Desktop resolved a MissionV1-managed Responses function_call for the declared probe target."
            ),
            "evidence": [{"path": target_path, "call_id": call_id, "output_sha256": output_hash, "recovered": recovered}],
        }],
        "caveats": [],
        "expected_resolution": expected_resolution,
        "runtime_recovery_used": recovered,
    }
    canonical = {
        "schema_version": "canonical_read_evidence.v1",
        "mission_id": mission.mission_id,
        "required_paths": [target_path] if target_path else [],
        "fulfilled_paths": [target_path] if target_path else [],
        "missing_required_sources": [],
        "coverage_status": {"blocked_obligations": [], "can_complete": True, "coverage_complete": True},
        "status_entitlement": {"can_complete": True, "can_return_complete": True, "reason": "desktop_tool_loop_probe_completed"},
        "writes_performed": False,
    }
    _write_json(artifact_dir / "report.json", report)
    _write_json(artifact_dir / "canonical_read_evidence.json", canonical)
    _write_json(artifact_dir / "canonical_evidence_bundle.json", {
        "schema_version": "canonical_evidence_bundle.v1",
        "mission_id": mission.mission_id,
        "task_class": "read_only",
        "evidence_authority": "mission_v1_runtime",
        "required_sources": [target_path] if target_path else [],
        "observations": [{"path": target_path, "sha256": output_hash, "tool": "codex_desktop_function_call", "complete": True}],
        "claims": report["findings"],
        "coverage_status": canonical["coverage_status"],
        "missing_required_sources": [],
        "status_entitlement": canonical["status_entitlement"],
    })
    payload = _read_json(artifact_dir / "desktop_tool_loop_probe.json")
    payload.update({
        "status": "RECOVERED" if recovered else "PASS",
        "adopted_call_id": call_id,
        "output_sha256": output_hash,
        "output_chars": len(tool_output_text),
        "completed_at": int(time.time()),
        "stored_response_id": str(getattr(stored, "response_id", "") or ""),
        "runtime_recovery_used": recovered,
    })
    _write_json(artifact_dir / "desktop_tool_loop_probe.json", payload)


def _state_machine_from_stored(stored: Any) -> ResponsesToolStateMachine:
    sm = ResponsesToolStateMachine(str(getattr(stored, "response_id", "") or ""))
    raw = str(getattr(stored, "adoption_probes_json", "") or "")
    if raw:
        try:
            data = json.loads(raw)
            for call_id, state in (data.get("calls") or {}).items():
                sm.calls[str(call_id)] = dict(state)
            sm.sequence = int(data.get("sequence", len(sm.calls)) or len(sm.calls))
        except Exception:
            pass
    return sm


def _probe_failure(body: JSON, model_alias: str, response_id: str, created_at: int, mission_id: str, message: str) -> DesktopToolLoopProbeResult:
    artifact_dir = _mission_dir(os.getcwd(), mission_id)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    _write_json(artifact_dir / "desktop_tool_loop_probe.json", {
        "schema_version": "desktop_tool_loop_probe_result.v1",
        "mission_id": mission_id,
        "status": "FAILED",
        "reason": message,
    })
    write_adoption_or_recovery(
        artifact_dir=artifact_dir,
        mission_id=mission_id,
        route_class="desktop_tool_loop_probe",
        pending_tool_calls_emitted=0,
        runtime_recovery_used=False,
    )
    return _terminal_failure(body, model_alias, response_id, created_at, message, mission_id=mission_id)


def _terminal_failure(
    body: JSON,
    model_alias: str,
    response_id: str,
    created_at: int,
    message: str,
    *,
    mission_id: str = "",
) -> DesktopToolLoopProbeResult:
    text = f"FAILED\nSynthesis status: DESKTOP_TOOL_LOOP_PROBE_FAILED\nReason: {message}"
    output = [{
        "type": "message",
        "id": f"msg_{response_id}",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }]
    return DesktopToolLoopProbeResult(
        handled=True,
        response_obj=_response_shell(body, model_alias, response_id, created_at, output, "completed"),
        mission_id=mission_id,
        status="FAILED",
        reason=message,
    )


def _response_shell(body: JSON, model_alias: str, response_id: str, created_at: int, output: list[JSON], status: str) -> JSON:
    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": status,
        "error": None,
        "incomplete_details": None,
        "instructions": body.get("instructions"),
        "model": model_alias,
        "output": output,
        "parallel_tool_calls": False,
        "previous_response_id": body.get("previous_response_id"),
        "store": False,
        "temperature": body.get("temperature"),
        "top_p": body.get("top_p"),
        "truncation": body.get("truncation", "disabled"),
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "metadata": body.get("metadata") or {},
    }


def _mission_dir(project_root: str, mission_id: str) -> Path:
    return Path(project_root) / ".codex-oss" / "missions" / str(mission_id)


def _progress_events(
    mission_id: str,
    *,
    tool_intent: str = "safe_read",
    expected_resolution: str = "adopt_or_recover",
    tier: str = "",
) -> list[JSON]:
    action = {
        "safe_read": "read request",
        "safe_search": "search request",
        "safe_list": "directory listing request",
        "safe_git": "git status request",
    }.get(tool_intent, "tool request")
    if expected_resolution == "recover_after_tool_failure":
        prepared = f"Desktop tool selected for a safe failing {action} recovery probe."
        awaiting = "Waiting for the Desktop tool result so recovery handling can be checked."
    elif tier in {"A4", "A5"}:
        prepared = f"Desktop tool selected for a safe pre-implementation {action}."
        awaiting = "Waiting for the Desktop tool result before the implementation lane continues."
    else:
        prepared = f"Desktop tool selected for a safe {action}."
        awaiting = "Waiting for the Desktop tool result."
    labels = [
        ("desktop_tool_loop_probe_started", "Desktop tool-loop probe starting."),
        ("desktop_tool_loop_function_call_prepared", prepared),
        ("desktop_tool_loop_awaiting_adoption", awaiting),
    ]
    events: list[JSON] = []
    for idx, (event_type, message) in enumerate(labels, start=1):
        event_id = f"evt_{idx:04d}"
        events.append({
            "schema_version": "visible_commentary_event.v1",
            "mission_id": mission_id,
            "event_id": event_id,
            "seq": idx,
            "event_type": event_type,
            "phase": "PROBE",
            "source": "desktop_tool_loop_probe",
            "safe_for_user": True,
            "title": event_type,
            "message": message,
        })
    return events


def _progress_output_items(
    mission_id: str,
    new_call_id: Callable[[str], str],
    *,
    tool_intent: str = "safe_read",
    expected_resolution: str = "adopt_or_recover",
    tier: str = "",
) -> list[JSON]:
    items: list[JSON] = []
    for event in _progress_events(
        mission_id,
        tool_intent=tool_intent,
        expected_resolution=expected_resolution,
        tier=tier,
    ):
        items.append({
            "type": "message",
            "id": new_call_id("msg"),
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": event["message"], "annotations": []}],
            "metadata": {
                "oss_visible_event": {
                    "mission_id": mission_id,
                    "event_id": event["event_id"],
                    "seq": event["seq"],
                    "event_type": event["event_type"],
                    "safe_for_user": True,
                },
            },
        })
    return items


def _write_json(path: Path, payload: JSON) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _read_json(path: Path) -> JSON:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _sha256(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    return str(value or "")
