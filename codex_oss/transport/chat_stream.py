"""Chat Completions stream to Responses SSE assembler."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

JSON = Dict[str, Any]


@dataclass
class ToolStreamState:
    index: int
    item_id: str
    call_id: str
    raw_name_parts: List[str]
    args_parts: List[str]
    output_index: int = -1
    added: bool = False
    done: bool = False

    @property
    def raw_name(self) -> str:
        return "".join(self.raw_name_parts).strip()

    @property
    def arguments(self) -> str:
        return "".join(self.args_parts)


class ChatStreamAssembler:
    """Convert live Chat Completions SSE chunks into Responses SSE events."""

    def __init__(
        self,
        body: JSON,
        base_messages: List[JSON],
        model_alias: str,
        model_upstream: str,
        reverse_name_map: Dict[str, str],
        response_id: str,
        created_at: int,
        *,
        write_sse: Callable[[str, Any], None],
        write_progress: Callable[[str], None],
        state_put: Callable[[Any], None],
        stored_response_factory: Callable[..., Any],
        build_response_shell: Callable[..., JSON],
        repair_chat_history: Callable[[List[JSON], Any], List[JSON]],
        extract_budget: Callable[[List[JSON]], int],
        restore_tool_name: Callable[[str, Dict[str, str]], str],
        new_id: Callable[[str], str],
        json_dumps: Callable[[Any], str],
        as_text: Callable[[Any], str],
    ):
        self.body = body
        self.base_messages = base_messages
        self.model_alias = model_alias
        self.model_upstream = model_upstream
        self.reverse_name_map = reverse_name_map
        self.response_id = response_id
        self.created_at = created_at
        self.write_sse = write_sse
        self.write_progress = write_progress
        self.state_put = state_put
        self.stored_response_factory = stored_response_factory
        self.build_response_shell = build_response_shell
        self.repair_chat_history = repair_chat_history
        self.extract_budget = extract_budget
        self.restore_tool_name = restore_tool_name
        self.new_id = new_id
        self.json_dumps = json_dumps
        self.as_text = as_text

        self.output_order: List[Tuple[str, Any]] = []
        self.next_output_index = 0
        self.text_started = False
        self.text_done = False
        self.text_item_id = self.new_id("msg")
        self.text_parts: List[str] = []
        self.buffer_text_until_tool_decision = os.getenv("OSS_BUFFER_TOOL_TURN_TEXT", "1") != "0"
        self.reasoning_parts: List[str] = []
        self.thinking_blocks: List[Any] = []
        self.tool_states: Dict[int, ToolStreamState] = {}
        self.usage: JSON = {}
        self.last_progress = time.time()
        self.sequence_number = 0

    def _write_sse(self, event: str, data: Any) -> None:
        if isinstance(data, dict):
            self.sequence_number += 1
            data = dict(data)
            data.setdefault("response_id", self.response_id)
            data.setdefault("sequence_number", self.sequence_number)
        self.write_sse(event, data)
        self.last_progress = time.time()

    def maybe_progress(self, note: str = "upstream_stream") -> None:
        interval = float(os.getenv("SSE_VISIBLE_PROGRESS_SECONDS", "8"))
        if time.time() - self.last_progress >= interval:
            self.write_progress(note)
            self.last_progress = time.time()

    def _assign_output_index(self, kind: str, key: Any) -> int:
        idx = self.next_output_index
        self.next_output_index += 1
        self.output_order.append((kind, key))
        return idx

    def ensure_text_item(self) -> int:
        if self.text_started:
            for i, (kind, key) in enumerate(self.output_order):
                if kind == "message" and key == "text":
                    return i
        idx = self._assign_output_index("message", "text")
        self.text_started = True
        item = {
            "type": "message",
            "id": self.text_item_id,
            "status": "in_progress",
            "role": "assistant",
            "content": [],
        }
        self._write_sse("response.output_item.added", {"type": "response.output_item.added", "output_index": idx, "item": item})
        self._write_sse(
            "response.content_part.added",
            {
                "type": "response.content_part.added",
                "output_index": idx,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
                "item_id": self.text_item_id,
            },
        )
        return idx

    def on_content_delta(self, text: str) -> None:
        if not text:
            return
        if self.buffer_text_until_tool_decision:
            self.text_parts.append(text)
            return
        idx = self.ensure_text_item()
        chunk_size = int(os.getenv("SSE_CHUNK_SIZE", "256"))
        for start in range(0, len(text), chunk_size):
            delta = text[start:start + chunk_size]
            self.text_parts.append(delta)
            self._write_sse(
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "output_index": idx,
                    "content_index": 0,
                    "delta": delta,
                    "item_id": self.text_item_id,
                },
            )

    def on_reasoning_delta(self, text: str) -> None:
        if text:
            self.reasoning_parts.append(text)
        self.maybe_progress("hidden_reasoning")

    def _get_tool_state(self, index: int) -> ToolStreamState:
        if index not in self.tool_states:
            self.tool_states[index] = ToolStreamState(
                index=index,
                item_id=self.new_id("fc"),
                call_id=self.new_id("call"),
                raw_name_parts=[],
                args_parts=[],
            )
        return self.tool_states[index]

    def _ensure_tool_added(self, st: ToolStreamState, force: bool = False) -> None:
        if st.added:
            return
        if not st.raw_name and not force:
            return
        raw_name = st.raw_name or "tool"
        name = self.restore_tool_name(raw_name, self.reverse_name_map)
        st.output_index = self._assign_output_index("function_call", st.index)
        item = {
            "type": "function_call",
            "id": st.item_id,
            "call_id": st.call_id,
            "name": name,
            "arguments": "",
            "status": "in_progress",
        }
        self._write_sse(
            "response.output_item.added",
            {"type": "response.output_item.added", "output_index": st.output_index, "item": item},
        )
        st.added = True

    def _emit_args_delta(self, st: ToolStreamState, delta: str) -> None:
        if not delta:
            return
        self._ensure_tool_added(st, force=bool(st.raw_name))
        if not st.added:
            return
        arg_chunk_size = int(os.getenv("SSE_FUNCTION_ARGS_CHUNK_SIZE", "512"))
        for start in range(0, len(delta), arg_chunk_size):
            part = delta[start:start + arg_chunk_size]
            self._write_sse(
                "response.function_call_arguments.delta",
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": st.output_index,
                    "item_id": st.item_id,
                    "delta": part,
                },
            )

    def on_tool_call_delta(self, tc: JSON) -> None:
        try:
            idx = int(tc.get("index", 0))
        except Exception:
            idx = 0
        st = self._get_tool_state(idx)
        if tc.get("id") and st.call_id.startswith("call") and not st.added:
            st.call_id = str(tc["id"])
        fn = tc.get("function") or {}
        name_part = fn.get("name") or tc.get("name")
        if name_part:
            st.raw_name_parts.append(str(name_part))
            self._ensure_tool_added(st, force=False)
            if st.added and st.arguments:
                emitted = getattr(st, "_already_emitted_chars", 0)
                remaining = st.arguments[emitted:]
                if remaining:
                    self._emit_args_delta(st, remaining)
                    setattr(st, "_already_emitted_chars", len(st.arguments))

        args_delta = fn.get("arguments", tc.get("arguments"))
        if args_delta is not None:
            args_text = args_delta if isinstance(args_delta, str) else self.json_dumps(args_delta)
            st.args_parts.append(args_text)
            if st.added:
                self._emit_args_delta(st, args_text)
                setattr(st, "_already_emitted_chars", len(st.arguments))
            elif not hasattr(st, "_already_emitted_chars"):
                setattr(st, "_already_emitted_chars", 0)

    def on_chunk(self, chunk: JSON) -> None:
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                continue
            for key in ("reasoning_content", "reasoning", "thinking"):
                if delta.get(key):
                    self.on_reasoning_delta(self.as_text(delta.get(key)))
            if delta.get("thinking_blocks"):
                self.thinking_blocks.append(delta.get("thinking_blocks"))
                self.maybe_progress("hidden_thinking_blocks")
            if delta.get("content") is not None:
                self.on_content_delta(self.as_text(delta.get("content")))
            for tc in delta.get("tool_calls") or []:
                if isinstance(tc, dict):
                    self.on_tool_call_delta(tc)
            if choice.get("finish_reason"):
                self.maybe_progress(f"finish:{choice.get('finish_reason')}")

    def _final_message_item(self) -> Optional[JSON]:
        if not self.text_started:
            return None
        text = "".join(self.text_parts)
        return {
            "type": "message",
            "id": self.text_item_id,
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }

    def _final_function_item(self, st: ToolStreamState) -> JSON:
        raw_name = st.raw_name or "tool"
        name = self.restore_tool_name(raw_name, self.reverse_name_map)
        return {
            "type": "function_call",
            "id": st.item_id,
            "call_id": st.call_id,
            "name": name,
            "arguments": st.arguments,
            "status": "completed",
        }

    def finalize(self) -> JSON:
        buffered_text = "".join(self.text_parts)
        for idx in sorted(self.tool_states):
            st = self.tool_states[idx]
            self._ensure_tool_added(st, force=True)
            if not st.done:
                args = st.arguments
                emitted = getattr(st, "_already_emitted_chars", 0)
                if emitted < len(args):
                    self._emit_args_delta(st, args[emitted:])
                    setattr(st, "_already_emitted_chars", len(args))
                self._write_sse(
                    "response.function_call_arguments.done",
                    {
                        "type": "response.function_call_arguments.done",
                        "output_index": st.output_index,
                        "item_id": st.item_id,
                        "name": self.restore_tool_name(st.raw_name or "tool", self.reverse_name_map),
                        "arguments": args,
                    },
                )
                self._write_sse(
                    "response.output_item.done",
                    {"type": "response.output_item.done", "output_index": st.output_index, "item": self._final_function_item(st)},
                )
                st.done = True

        if self.buffer_text_until_tool_decision and buffered_text and not self.tool_states:
            self.buffer_text_until_tool_decision = False
            self.text_parts = []
            self.on_content_delta(buffered_text)

        if self.text_started and not self.text_done and not self.tool_states:
            text = "".join(self.text_parts)
            idx = next(i for i, (kind, key) in enumerate(self.output_order) if kind == "message" and key == "text")
            self._write_sse(
                "response.output_text.done",
                {
                    "type": "response.output_text.done",
                    "output_index": idx,
                    "content_index": 0,
                    "text": text,
                    "item_id": self.text_item_id,
                },
            )
            part = {"type": "output_text", "text": text, "annotations": []}
            self._write_sse(
                "response.content_part.done",
                {
                    "type": "response.content_part.done",
                    "output_index": idx,
                    "content_index": 0,
                    "part": part,
                    "item_id": self.text_item_id,
                },
            )
            self._write_sse(
                "response.output_item.done",
                {"type": "response.output_item.done", "output_index": idx, "item": self._final_message_item()},
            )
            self.text_done = True

        output_by_index: Dict[int, JSON] = {}
        for idx, (kind, key) in enumerate(self.output_order):
            if kind == "message" and not self.tool_states:
                item = self._final_message_item()
                if item:
                    output_by_index[idx] = item
            elif kind == "function_call":
                st = self.tool_states[int(key)]
                output_by_index[idx] = self._final_function_item(st)
        output = [output_by_index[i] for i in sorted(output_by_index)]

        content = "".join(self.text_parts)
        reasoning_content = "".join(self.reasoning_parts)
        assistant_msg: JSON = {"role": "assistant", "content": content or ""}
        if reasoning_content:
            assistant_msg["reasoning_content"] = reasoning_content
        if self.thinking_blocks:
            assistant_msg["thinking_blocks"] = self.thinking_blocks

        replay_tool_calls: List[JSON] = []
        for idx in sorted(self.tool_states):
            st = self.tool_states[idx]
            replay_tool_calls.append(
                {
                    "id": st.call_id,
                    "type": "function",
                    "function": {"name": st.raw_name or "tool", "arguments": st.arguments},
                }
            )
        if replay_tool_calls:
            assistant_msg["tool_calls"] = replay_tool_calls

        all_messages = self.repair_chat_history(self.base_messages, None) + [assistant_msg]
        stored = self.stored_response_factory(
            response_id=self.response_id,
            model_alias=self.model_alias,
            model_upstream=self.model_upstream,
            messages=all_messages,
            pending_call_ids=[tc["id"] for tc in replay_tool_calls],
            created_at=self.created_at,
            output_items_json=self.json_dumps(output),
            tool_exchange_count=int(self.body.get("_codex_oss_tool_exchange_count", 0) or 0),
            task_max_exchanges=int(
                self.body.get("_codex_oss_task_max_exchanges")
                or self.extract_budget(self.base_messages)
            ),
            previous_response_id=str(self.body.get("previous_response_id") or ""),
        )
        self._initialize_adoption_state(stored, replay_tool_calls)
        self.state_put(stored)

        usage = self.usage or {}
        resp_obj = self.build_response_shell(
            self.body,
            self.model_alias,
            response_id=self.response_id,
            created_at=self.created_at,
            status="completed",
            output=output,
        )
        resp_obj["usage"] = {
            "input_tokens": usage.get("prompt_tokens", usage.get("input_tokens", 0)),
            "output_tokens": usage.get("completion_tokens", usage.get("output_tokens", 0)),
            "total_tokens": usage.get("total_tokens", 0),
        }
        return resp_obj

    def _initialize_adoption_state(self, stored: Any, replay_tool_calls: List[JSON]) -> None:
        if not replay_tool_calls:
            return
        try:
            from codex_oss.tool_call_adoption import ResponsesToolStateMachine
            sm = ResponsesToolStateMachine(self.response_id)
            sm.parent_response_id = str(self.body.get("previous_response_id") or "") or None
            sm.previous_response_id = sm.parent_response_id
            output_by_call_id = {
                item.get("call_id"): item
                for item in self._final_output_items_for_adoption()
                if isinstance(item, dict) and item.get("type") == "function_call"
            }
            for tc in replay_tool_calls:
                call_id = str(tc.get("id") or "")
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                raw_name = str(fn.get("name") or "tool")
                args = fn.get("arguments", "{}")
                if not isinstance(args, str):
                    args = self.json_dumps(args)
                rendered = output_by_call_id.get(call_id, {})
                sm.register_tool_call(
                    call_id=call_id,
                    tool_name=self.restore_tool_name(raw_name, self.reverse_name_map),
                    arguments={"raw": args},
                    output_item_id=str(rendered.get("id") or f"fc_{call_id}"),
                )
            stored.adoption_probes_json = self.json_dumps({
                "calls": {
                    call_id: {k: v for k, v in state.items() if k not in ("arguments_preview",)}
                    for call_id, state in sm.calls.items()
                },
                "sequence": sm.sequence,
            })
        except Exception:
            return

    def _final_output_items_for_adoption(self) -> List[JSON]:
        items: List[JSON] = []
        for kind, key in self.output_order:
            if kind == "function_call":
                st = self.tool_states[int(key)]
                items.append(self._final_function_item(st))
        return items
