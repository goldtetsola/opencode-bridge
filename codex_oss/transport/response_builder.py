"""Chat Completion to Responses object builder."""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional

JSON = Dict[str, Any]


def build_response_object_from_chat(
    *,
    body: JSON,
    chat_resp: JSON,
    base_messages: List[JSON],
    model_alias: str,
    model_upstream: str,
    reverse_name_map: Dict[str, str],
    state_put: Callable[[Any], None],
    stored_response_factory: Callable[..., Any],
    repair_chat_history: Callable[[List[JSON], Any], List[JSON]],
    extract_budget: Callable[[List[JSON]], int],
    restore_tool_name: Callable[[str, Dict[str, str]], str],
    new_id: Callable[[str], str],
    now: Callable[[], int],
    json_dumps: Callable[[Any], str],
    as_text: Callable[[Any], str],
    response_id: Optional[str] = None,
    created_at: Optional[int] = None,
) -> JSON:
    """Convert one non-streaming Chat Completion response to a Responses object."""
    choice = (chat_resp.get("choices") or [{}])[0]
    upstream_msg = choice.get("message") or {}

    content = as_text(upstream_msg.get("content", ""))
    reasoning_content = upstream_msg.get("reasoning_content") or upstream_msg.get("reasoning")
    thinking_blocks = upstream_msg.get("thinking_blocks")

    tool_calls_in = upstream_msg.get("tool_calls") or []
    tool_calls_out: List[JSON] = []

    for tc in tool_calls_in:
        if not isinstance(tc, dict):
            continue
        tc_id = str(tc.get("id") or tc.get("call_id") or new_id("call"))
        fn = tc.get("function") or {}
        raw_name = str(fn.get("name") or tc.get("name") or "tool")
        name = restore_tool_name(raw_name, reverse_name_map)
        args = fn.get("arguments", tc.get("arguments", "{}"))
        if not isinstance(args, str):
            args = json_dumps(args)
        replay_tc = {
            "id": tc_id,
            "type": "function",
            "function": {"name": raw_name, "arguments": args},
        }
        tool_calls_out.append({
            "replay": replay_tc,
            "codex": {"id": new_id("fc"), "call_id": tc_id, "name": name, "arguments": args},
        })

    assistant_msg: JSON = {"role": "assistant", "content": content or ""}
    if reasoning_content:
        assistant_msg["reasoning_content"] = reasoning_content
    if thinking_blocks:
        assistant_msg["thinking_blocks"] = thinking_blocks
    if tool_calls_out:
        assistant_msg["tool_calls"] = [x["replay"] for x in tool_calls_out]

    response_id = response_id or new_id("resp")
    created_at = created_at or now()
    all_messages = repair_chat_history(base_messages, None) + [assistant_msg]
    pending_ids = [x["codex"]["call_id"] for x in tool_calls_out]

    output: List[JSON] = []
    if reasoning_content and os.getenv("EXPOSE_EMPTY_REASONING_ITEM", "1") != "0":
        output.append({"type": "reasoning", "id": new_id("rs"), "summary": []})

    for x in tool_calls_out:
        fc = x["codex"]
        output.append(
            {
                "type": "function_call",
                "id": fc["id"],
                "call_id": fc["call_id"],
                "name": fc["name"],
                "arguments": fc["arguments"],
                "status": "completed",
            }
        )

    # A Responses tool-call turn must project as a tool-call turn. Some Codex
    # consumers treat a normal assistant message in the same output as terminal
    # progress and stop before adopting the pending function_call. Preserve the
    # upstream text in chat history for the next model turn, but do not expose it
    # as a public output message when tools are pending.
    if content and not tool_calls_out:
        output.append(
            {
                "type": "message",
                "id": new_id("msg"),
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": content, "annotations": []}],
            }
        )

    state_put(
        stored_response_factory(
            response_id=response_id,
            model_alias=model_alias,
            model_upstream=model_upstream,
            messages=all_messages,
            pending_call_ids=pending_ids,
            created_at=created_at,
            output_items_json=json_dumps(output),
            tool_exchange_count=int(body.get("_codex_oss_tool_exchange_count", 0) or 0),
            task_max_exchanges=int(
                body.get("_codex_oss_task_max_exchanges")
                or extract_budget(base_messages)
            ),
            previous_response_id=str(body.get("previous_response_id") or ""),
        )
    )

    usage = chat_resp.get("usage") or {}
    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": "completed",
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
        "usage": {
            "input_tokens": usage.get("prompt_tokens", usage.get("input_tokens", 0)),
            "output_tokens": usage.get("completion_tokens", usage.get("output_tokens", 0)),
            "total_tokens": usage.get("total_tokens", 0),
        },
        "metadata": body.get("metadata") or {},
    }
