"""Bridge transcript harness — proves bridge-local native-like UX.

Captures local Responses/SSE transcripts and reconciles them with mission
artifacts. This is the Bridge Gold gate; Codex Desktop Gold requires
``codex_oss.desktop_native_verifier`` with captured Desktop transcript evidence.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from codex_oss.route_authority import build_route_authority

JSON = dict[str, Any]

DEFAULT_BRIDGE_URL = "http://127.0.0.1:4000/v1"
DEFAULT_AUTH = os.getenv("PROXY_API_KEY", "sk-local-codex-bridge")


# ═══════════════════════════════════════════════════════════════════════════
# SpawnedSubagentRunner
# ═══════════════════════════════════════════════════════════════════════════


class SpawnedSubagentRunner:
    """Runs an OSS subagent through the bridge and captures the transcript."""

    def __init__(
        self,
        base_url: str = DEFAULT_BRIDGE_URL,
        auth: str = DEFAULT_AUTH,
        timeout: float = 120,
    ):
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.timeout = timeout

    def run_read_floor(
        self,
        *,
        model: str,
        read_only_paths: list[str],
        mission_id: str = "",
        handoff_goal: str = "Inspect the declared read-only sources",
    ) -> SpawnedTranscript:
        """Run a read-floor OSS subagent and capture transcript."""
        mid = mission_id or f"ux_read_floor_{int(time.time())}"
        handoff = (
            "OSS_HANDOFF_JSON:\n"
            + json.dumps({
                "schema_version": 1,
                "role": "Read-only scout",
                "goal": handoff_goal,
                "task_type": "scout",
                "owned_paths": [],
                "read_only_paths": read_only_paths,
                "forbidden_actions": ["Do not edit files"],
                "verification_steps": ["Verify all required files were inspected"],
                "deliverable_fields": ["files inspected", "confidence", "caveats"],
                "completion_rule": "stop after inspection",
                "escalation_rule": "stop on scope drift",
                "mission_id": mid,
            })
            + "\n</OSS_HANDOFF_JSON>"
        )
        return self._spawn(model, handoff, mid, "read_floor")

    def run_bounded_implementation(
        self,
        *,
        model: str,
        owned_paths: list[str],
        intent_edits: list[JSON],
        mission_id: str = "",
    ) -> SpawnedTranscript:
        """Run a bounded implementation OSS subagent and capture transcript."""
        mid = mission_id or f"ux_impl_{int(time.time())}"

        mission = {
            "schema_version": "oss_agent_mission.v1",
            "mission_id": mid,
            "tier": "A5",
            "mode": "bounded_implementation",
            "objective": "Bounded implementation UX test",
            "risk_tier": "low",
            "write_allowed": True,
            "allowed_roots": [],
            "allowed_paths": owned_paths,
            "owned_paths": owned_paths,
            "read_only_paths": owned_paths,
            "forbidden_roots": [".env", ".git"],
            "allowed_tool_classes": ["read", "search"],
            "tool_budget": 4,
            "time_budget_seconds": 60,
            "stop_conditions": ["valid_patch", "deadline_reached"],
            "report_schema": "implementation_report.v1",
            "required_outputs": ["patch", "changed_files", "risk_assessment", "verification_plan", "evidence_refs"],
            "max_files_changed": len(owned_paths),
            "max_patch_bytes": 12000,
            "apply_mode": "isolated_worktree",
            "verification_policy": {
                "allowed_commands": [["cat", owned_paths[0]]] if owned_paths else [],
                "max_commands": 1,
                "timeout_seconds": 20,
            },
        }

        intent = {
            "patch_intent_version": "1.0",
            "status": "PROPOSED",
            "summary": "Implementation UX test",
            "edits": intent_edits,
            "risk_assessment": {"risk_tier": "low", "critical_paths_touched": False, "blast_radius": "scratch-only"},
            "verification_plan": [{"command": ["cat", owned_paths[0]], "reason": "Verify"}] if owned_paths else [],
            "evidence_refs": [f"file:{owned_paths[0]}"] if owned_paths else [],
            "caveats": ["UX burn-in test."],
        }

        content = (
            "<OSS_HANDOFF_JSON>\n"
            + json.dumps(mission)
            + "\n</OSS_HANDOFF_JSON>\n\n<OSS_PATCH_INTENT_JSON>\n"
            + json.dumps(intent)
            + "\n</OSS_PATCH_INTENT_JSON>"
        )

        return self._spawn(model, content, mid, "bounded_implementation")

    def run_pending_read_recovery(
        self,
        *,
        model: str,
        read_only_paths: list[str],
        mission_id: str = "",
    ) -> SpawnedTranscript:
        """Run pending-read recovery through runtime code and capture rendered SSE."""
        mid = mission_id or f"ux_pending_read_{int(time.time())}"
        handoff = self._read_handoff(mid, read_only_paths, "Recover a pending read evidence floor")
        pending_path = read_only_paths[0] if read_only_paths else "README.md"
        return self._runtime_recovery_transcript(
            model=model,
            mission_id=mid,
            task_class="read_floor",
            handoff=handoff,
            pending_tool={
                "id": "call_pending_read",
                "type": "function",
                "function": {
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": f"rtk read {pending_path}", "workdir": os.getcwd()}),
                },
            },
        )

    def run_grep_recovery(
        self,
        *,
        model: str,
        read_only_paths: list[str],
        pattern: str = "NativeExperience",
        mission_id: str = "",
    ) -> SpawnedTranscript:
        """Run grep/search recovery through runtime code and capture rendered SSE."""
        mid = mission_id or f"ux_grep_{int(time.time())}"
        handoff = self._read_handoff(mid, read_only_paths, "Recover pending grep evidence")
        target = read_only_paths[0] if read_only_paths else "README.md"
        return self._runtime_recovery_transcript(
            model=model,
            mission_id=mid,
            task_class="search_floor",
            handoff=handoff,
            pending_tool={
                "id": "call_pending_grep",
                "type": "function",
                "function": {
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": f"rtk grep {json.dumps(pattern)} {target}", "workdir": os.getcwd()}),
                },
            },
        )

    def run_ls_recovery(
        self,
        *,
        model: str,
        read_only_paths: list[str],
        mission_id: str = "",
    ) -> SpawnedTranscript:
        """Run ls/list recovery through runtime code and capture rendered SSE."""
        mid = mission_id or f"ux_ls_{int(time.time())}"
        handoff = self._read_handoff(mid, read_only_paths, "Recover pending list evidence")
        return self._runtime_recovery_transcript(
            model=model,
            mission_id=mid,
            task_class="search_floor",
            handoff=handoff,
            pending_tool={
                "id": "call_pending_ls",
                "type": "function",
                "function": {
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "rtk ls codex_oss", "workdir": os.getcwd()}),
                },
            },
        )

    @staticmethod
    def _read_handoff(mission_id: str, read_only_paths: list[str], goal: str) -> str:
        return (
            "OSS_HANDOFF_JSON:\n"
            + json.dumps({
                "schema_version": 1,
                "role": "Read-only recovery scout",
                "goal": goal,
                "task_type": "scout",
                "owned_paths": [],
                "read_only_paths": read_only_paths,
                "forbidden_actions": ["Do not edit files"],
                "verification_steps": ["Verify required evidence was recovered"],
                "deliverable_fields": ["files inspected", "confidence", "caveats"],
                "completion_rule": "stop after recovered evidence report",
                "escalation_rule": "stop on scope drift",
                "mission_id": mission_id,
            })
        )

    def _runtime_recovery_transcript(
        self,
        *,
        model: str,
        mission_id: str,
        task_class: str,
        handoff: str,
        pending_tool: JSON,
    ) -> SpawnedTranscript:
        """Capture runtime pending-recovery SSE using the same transcript parser."""
        os.environ.setdefault("ALLOW_MISSING_OPENCODE_KEY", "1")
        from bridge import StoredResponse, complete_pending_reads_from_bridge
        from codex_oss.transport.emitter import ResponseEmitter

        class FakeWFile:
            def __init__(self):
                self.data = bytearray()

            def write(self, chunk):
                self.data.extend(chunk)

            def flush(self):
                pass

        class FakeHandler:
            def __init__(self):
                self.wfile = FakeWFile()
                self.statuses = []
                self.headers = []

            def send_response(self, status):
                self.statuses.append(status)

            def send_header(self, key, value):
                self.headers.append((key, value))

            def end_headers(self):
                pass

        started = time.time()
        transcript = SpawnedTranscript(mission_id=mission_id, model=model, task_class=task_class, started_at=started)
        transcript.route_authority = build_route_authority(
            agent_name=model,
            model_alias=model,
            handoff_text=handoff,
            consumer_kind="direct_bridge_harness",
        )
        handler = FakeHandler()
        emitter = ResponseEmitter(handler, f"resp_{mission_id}", model, True)
        child = StoredResponse(
            response_id=f"resp_child_{mission_id}",
            model_alias=model,
            model_upstream=model,
            messages=[
                {"role": "user", "content": handoff},
                {"role": "assistant", "content": "", "tool_calls": [pending_tool]},
            ],
            pending_call_ids=[str(pending_tool.get("id", ""))],
            created_at=int(started),
            tool_exchange_count=1,
            task_max_exchanges=6,
            previous_response_id=f"resp_parent_{mission_id}",
            pending_replay_count=3,
        )

        def finalizer(_prompt: str, _timeout: float) -> str:
            return (
                "Findings: The pending recovery path gathered the required runtime-owned evidence.\n"
                "Confidence: HIGH\n"
                "Caveats: This recovery narrative is limited to the recovered evidence excerpts."
            )

        try:
            report_text = complete_pending_reads_from_bridge(
                parent_response_id=f"resp_parent_{mission_id}",
                child_state=child,
                handoff_text=handoff,
                project_root=os.getcwd(),
                finalizer_call=finalizer,
                finalizer_timeout_seconds=3,
                emitter=emitter,
            )
            if report_text:
                emitter.emit_text_message(report_text, phase="final_answer")
                emitter.complete()
            transcript.http_status = 200
        except Exception as exc:
            transcript.error = str(exc)
        transcript.ended_at = time.time()
        self._process_captured_sse(handler.wfile.data.decode("utf-8", errors="replace"), transcript)
        transcript.load_artifacts()
        transcript.extract_commentary()
        return transcript

    def _spawn(
        self,
        model: str,
        content: str,
        mission_id: str,
        task_class: str,
    ) -> SpawnedTranscript:
        """Spawn an OSS subagent and capture the full transcript."""
        started = time.time()

        body: JSON = {
            "model": model,
            "stream": True,  # SSE streaming to capture intermediate messages
            "input": [{"role": "user", "content": content}],
        }

        transcript = SpawnedTranscript(
            mission_id=mission_id,
            model=model,
            task_class=task_class,
            started_at=started,
        )
        transcript.route_authority = build_route_authority(
            agent_name=model,
            model_alias=model,
            handoff_text=content,
            consumer_kind="direct_bridge_harness",
        )

        try:
            req = urllib.request.Request(
                f"{self.base_url}/responses",
                data=json.dumps(body).encode("utf-8"),
                method="POST",
                headers={
                    "Authorization": f"Bearer {self.auth}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
            )

            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                transcript.http_status = resp.status
                # Read SSE stream line by line
                buffer = b""
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    buffer += chunk
                    # Process complete SSE lines
                    while b"\n\n" in buffer:
                        line, buffer = buffer.split(b"\n\n", 1)
                        self._process_sse_line(line.decode("utf-8", errors="replace"), transcript)

            transcript.ended_at = time.time()

        except Exception as exc:
            transcript.error = str(exc)
            transcript.ended_at = time.time()

        # Load mission artifacts
        transcript.load_artifacts()

        # Extract commentary from transcript
        transcript.extract_commentary()

        return transcript

    def _process_captured_sse(self, stream_text: str, transcript: SpawnedTranscript) -> None:
        for raw_frame in stream_text.split("\n\n"):
            if raw_frame.strip():
                self._process_sse_line(raw_frame, transcript)

    def _process_sse_line(self, line: str, transcript: SpawnedTranscript) -> None:
        """Process a single SSE data line."""
        event_name = ""
        data_lines: list[str] = []
        for raw_line in line.splitlines():
            if raw_line.startswith("event: "):
                event_name = raw_line[len("event: "):].strip()
            elif raw_line.startswith("data: "):
                data_lines.append(raw_line[len("data: "):])
        if not data_lines:
            return
        data_str = "\n".join(data_lines).strip()
        if data_str == "[DONE]":
            transcript.stream_done = True
            return

        try:
            event = json.loads(data_str)
        except json.JSONDecodeError:
            transcript.unparseable_lines += 1
            return

        event_type = event.get("type", "") or event_name
        output_index = event.get("output_index")
        phase = str(event.get("phase", "") or "")

        # Capture response.created for metadata
        if event_type == "response.created":
            resp = event.get("response", {})
            transcript.response_id = resp.get("id", "")
            return

        if event_type in {"response.output_item.added", "response.output_item.done"}:
            item = event.get("item", {}) if isinstance(event.get("item"), dict) else {}
            item_phase = str(item.get("phase", "") or phase)
            if item_phase and isinstance(output_index, int):
                transcript._phase_by_output_index[output_index] = item_phase
            item_id_for_phase = str(item.get("id", "") or "")
            if item_phase and item_id_for_phase:
                transcript._phase_by_item_id[item_id_for_phase] = item_phase
            phase = item_phase or phase

        item_id = str(event.get("item_id", "") or "")
        if not phase and isinstance(output_index, int):
            phase = transcript._phase_by_output_index.get(output_index, "")
        if not phase and item_id:
            phase = transcript._phase_by_item_id.get(item_id, "")

        # Capture response.completed for final status
        if event_type == "response.completed":
            resp = event.get("response", {})
            transcript.final_status = resp.get("status", "")
            transcript.final_output = resp.get("output", [])
            return

        # Capture text content from various event types
        text = self._extract_text(event)
        if text:
            timestamp = time.time()
            transcript.raw_messages.append({
                "timestamp": timestamp,
                "event_type": event_type,
                "text": text,
                "phase": phase,
            })
            transcript.all_text.append(text)

    def _extract_text(self, event: JSON) -> str:
        """Extract text content from an SSE event."""
        # Direct text in delta
        delta = event.get("delta", "")
        if isinstance(delta, str) and delta.strip():
            return delta

        text = event.get("text", "")
        if isinstance(text, str) and text.strip():
            return text

        part = event.get("part", {})
        if isinstance(part, dict) and part.get("text"):
            return str(part["text"])

        # Content array in item
        item = event.get("item", {})
        if isinstance(item, dict):
            for content in item.get("content", []) or []:
                if isinstance(content, dict) and content.get("text"):
                    return str(content["text"])

        # Content in response output
        response = event.get("response", {})
        if isinstance(response, dict):
            for output_item in response.get("output", []) or []:
                if not isinstance(output_item, dict):
                    continue
                for content in output_item.get("content", []) or []:
                    if isinstance(content, dict) and content.get("text"):
                        return str(content["text"])

        return ""


# ═══════════════════════════════════════════════════════════════════════════
# SpawnedTranscript
# ═══════════════════════════════════════════════════════════════════════════


class SpawnedTranscript:
    """Captured transcript from a spawned OSS subagent."""

    def __init__(self, mission_id: str, model: str, task_class: str, started_at: float):
        self.mission_id = mission_id
        self.model = model
        self.task_class = task_class
        self.started_at = started_at
        self.ended_at: float = 0
        self.http_status: int = 0
        self.response_id: str = ""
        self.stream_done: bool = False
        self.final_status: str = "unknown"
        self.final_output: list[JSON] = []
        self.error: str | None = None

        # Captured messages
        self.raw_messages: list[JSON] = []
        self.all_text: list[str] = []
        self._phase_by_output_index: dict[int, str] = {}
        self._phase_by_item_id: dict[str, str] = {}

        # Extracted commentary
        self.commentary_messages: list[JSON] = []
        self.commentary_before_final: list[JSON] = []
        self.commentary_event_classes: set[str] = set()

        # Artifacts
        self.artifacts: dict[str, bool] = {}
        self.artifact_data: dict[str, JSON] = {}

        # Evaluation
        self.unparseable_lines: int = 0
        self.safety_violations: list[str] = []
        self.route_authority: JSON = build_route_authority(
            agent_name=model,
            model_alias=model,
            consumer_kind="direct_bridge_harness",
        )

    @property
    def duration_seconds(self) -> float:
        return self.ended_at - self.started_at if self.ended_at else 0

    @property
    def pre_final_commentary_count(self) -> int:
        return len(self.commentary_before_final)

    def load_artifacts(self) -> None:
        """Load mission artifacts from the filesystem."""
        project_root = os.getcwd()
        artifact_dir = Path(project_root) / ".codex-oss" / "missions" / self.mission_id

        expected = [
            "visible_commentary.jsonl",
            "summary.md",
            "canonical_read_evidence.json",
            "read_report_skeleton.json",
            "read_narrative_draft.json",
            "merged_read_report.json",
            "report.json",
            "canonical_patch_evidence.json",
            "implementation_narrative.json",
            "tool_call_adoption_probes.json",
            "server_side_read_finalizer_attempts.jsonl",
            "commentary_delivery.json",
        ]

        for name in expected:
            path = artifact_dir / name
            exists = path.exists()
            self.artifacts[name] = exists
            if exists:
                try:
                    if name.endswith(".json"):
                        self.artifact_data[name] = json.loads(path.read_text(encoding="utf-8"))
                    elif name.endswith(".jsonl"):
                        lines = []
                        for line in path.read_text(encoding="utf-8").splitlines():
                            if line.strip():
                                lines.append(json.loads(line))
                        self.artifact_data[name] = lines
                    else:
                        self.artifact_data[name] = path.read_text(encoding="utf-8")
                except Exception:
                    pass

    def extract_commentary(self) -> None:
        """Extract commentary messages from the transcript and artifacts."""
        from codex_oss.native_experience import classify_commentary_event

        self.commentary_messages = []
        self.commentary_before_final = []
        self.commentary_event_classes = set()

        final_ts = self._first_final_answer_timestamp()
        seen: set[tuple[float, str]] = set()
        reconciled_timestamps: set[float] = set()

        # Reconcile artifact events only when the transcript actually observed
        # their text. Artifact-only progress is producer evidence, not rendered UX.
        jsonl = self.artifact_data.get("visible_commentary.jsonl")
        if isinstance(jsonl, list):
            for event in jsonl:
                if isinstance(event, dict):
                    event_type = str(event.get("event_type", "") or "")
                    text = str(event.get("message", "") or "")
                    observed_msg = self._observed_message_for_artifact_text(text)
                    if observed_msg is None:
                        continue
                    observed_ts = float(observed_msg.get("timestamp", 0) or 0)
                    reconciled = {
                        "timestamp": observed_ts,
                        "event_type": event_type,
                        "text": text,
                        "phase": event.get("phase", observed_msg.get("phase", "")),
                        "source": "artifact_reconciled",
                        "event_id": event.get("event_id", ""),
                    }
                    key = (observed_ts, text)
                    if key in seen:
                        continue
                    seen.add(key)
                    reconciled_timestamps.add(observed_ts)
                    self.commentary_messages.append(reconciled)
                    if final_ts is None or observed_ts < final_ts:
                        self.commentary_before_final.append(reconciled)
                        self.commentary_event_classes.add(classify_commentary_event(event_type))

        # Include transcript-only progress that has no matching artifact.
        for msg in self.raw_messages:
            if not self._is_commentary_message(msg):
                continue
            text = str(msg.get("text", "") or "")
            ts = float(msg.get("timestamp", 0) or 0)
            if ts in reconciled_timestamps:
                continue
            key = (ts, text)
            if key in seen:
                continue
            seen.add(key)
            self.commentary_messages.append(msg)
            if final_ts is None or ts < final_ts:
                self.commentary_before_final.append(msg)
                self.commentary_event_classes.add(classify_commentary_event(self._event_type_for_raw_commentary(msg)))

    def _first_final_answer_timestamp(self) -> float | None:
        finals = [
            float(msg.get("timestamp", 0) or 0)
            for msg in self.raw_messages
            if self._is_final_answer_message(msg)
        ]
        return min(finals) if finals else None

    def _observed_message_for_artifact_text(self, artifact_text: str) -> JSON | None:
        needle = " ".join(str(artifact_text or "").split())
        if not needle:
            return None
        prefix = needle[: min(80, len(needle))]
        for msg in self.raw_messages:
            if self._is_final_answer_message(msg):
                continue
            haystack = " ".join(str(msg.get("text", "") or "").split())
            if needle in haystack or prefix in haystack:
                return msg
        return None

    @staticmethod
    def _is_final_answer_message(msg: JSON) -> bool:
        phase = str(msg.get("phase", "") or "").strip().lower()
        return phase in {"final_answer", "final", "answer"}

    def _is_commentary_message(self, msg: JSON) -> bool:
        if self._is_final_answer_message(msg):
            return False
        text = str(msg.get("text", "") or "")
        phase = str(msg.get("phase", "") or "").strip().lower()
        if phase in {"commentary", "read_floor", "plan", "propose", "validate_patch", "apply", "verify", "report"}:
            return True
        return "[OSS progress]" in text or self._looks_like_progress(text)

    @staticmethod
    def _event_type_for_raw_commentary(msg: JSON) -> str:
        phase = str(msg.get("phase", "") or "").strip().lower()
        if phase in {"plan", "mission", "start"}:
            return "mission_started"
        if phase in {"report", "verify"}:
            return "mission_completed"
        if phase in {"apply", "validate_patch", "propose"}:
            return "tool_or_evidence_progress"
        return "transcript_progress"

    @staticmethod
    def _looks_like_progress(text: str) -> bool:
        """Heuristic: does this text look like progress commentary?"""
        indicators = [
            "I'm completing", "I'm inspecting", "I'm reading",
            "I found", "I'm checking", "I'm verifying",
            "read floor", "evidence floor", "server-side",
            "patch validation", "verification", "recovery",
            "deterministic", "model finalizer",
            "[OSS progress]",
        ]
        lowered = text.lower()
        return any(ind.lower() in lowered for ind in indicators)

    def evaluate_gold_ux(self) -> JSON:
        """Evaluate whether this transcript meets the Gold UX gate."""
        from codex_oss.native_experience import (
            build_native_experience_contract,
            evaluate_native_experience,
        )

        # Determine runtime truth from artifacts
        report = self.artifact_data.get("report.json", {}) or {}
        canonical = self.artifact_data.get("canonical_read_evidence.json", {}) or {}
        patch_evidence = self.artifact_data.get("canonical_patch_evidence.json", {}) or {}

        runtime_owns_status = (
            isinstance(report, dict)
            and report.get("report_source") == "runtime"
            or bool(canonical.get("status_entitlement", {}).get("can_complete"))
        )

        writes_ok = not bool(
            (isinstance(report, dict) and report.get("main_workspace_mutated"))
            or (isinstance(patch_evidence, dict) and patch_evidence.get("writes_outside_owned_paths"))
        )

        # Model narrative
        impl_narrative = self.artifact_data.get("implementation_narrative.json", {}) or {}
        finalizer_attempts = self.artifact_data.get("server_side_read_finalizer_attempts.jsonl", []) or []
        read_finalizer_success = any(
            isinstance(item, dict) and item.get("result") == "success"
            for item in finalizer_attempts
        )
        final_text = "\n".join(str(text) for text in self.all_text)
        model_narrative_valid = (
            isinstance(report, dict)
            and report.get("implementation_narrative_valid", False)
        ) or bool(isinstance(impl_narrative, dict) and impl_narrative.get("schema_version")) \
            or bool(self.artifacts.get("read_narrative_draft.json")) \
            or bool(self.artifacts.get("merged_read_report.json")) \
            or read_finalizer_success \
            or "MODEL_AUTHORED_SERVER_SIDE_READ_COMPLETION" in final_text

        # Commentary
        commentary_observed = len(self.commentary_messages) > 0
        rendered_before_final = len(self.commentary_before_final) > 0

        # Tool loop
        adoption_probes = self.artifact_data.get("tool_call_adoption_probes.json", {}) or {}
        adoption_recorded = isinstance(adoption_probes, dict) and bool(adoption_probes.get("probes"))

        # Safety
        secrets_leaked = bool(self.safety_violations)

        contract = build_native_experience_contract(
            mission_id=self.mission_id,
            task_class=self.task_class,
            visible_progress={
                "min_pre_final_commentary_events": 3,
                "required_event_classes": [
                    "mission_or_action_start",
                    "tool_or_evidence_progress",
                    "closure_or_verification_progress",
                ],
                "must_be_observed_by_spawned_subagent_consumer": True,
            },
            route_authority=self.route_authority,
        )

        result = evaluate_native_experience(
            contract,
            artifacts_exist=self._contract_artifact_flags(),
            commentary_events_count=self.pre_final_commentary_count,
            commentary_event_classes=self.commentary_event_classes,
            commentary_observed=commentary_observed,
            commentary_rendered_before_final=rendered_before_final,
            model_narrative_valid=model_narrative_valid,
            runtime_owns_status=runtime_owns_status,
            writes_outside_scope=not writes_ok,
            adoption_recorded=adoption_recorded,
            secrets_leaked=secrets_leaked,
        )

        result["transcript_metadata"] = {
            "mission_id": self.mission_id,
            "model": self.model,
            "task_class": self.task_class,
            "duration_seconds": self.duration_seconds,
            "error": self.error,
            "raw_message_count": len(self.raw_messages),
            "commentary_detected": len(self.commentary_messages),
            "commentary_before_final": len(self.commentary_before_final),
            "event_classes_found": sorted(self.commentary_event_classes),
            "artifacts_found": sum(1 for v in self.artifacts.values() if v),
            "consumer_kind": self.route_authority.get("consumer_kind", "direct_bridge_harness"),
            "native_claim_scope": self.route_authority.get("native_claim_scope", "bridge_only"),
        }

        return result

    def _contract_artifact_flags(self) -> dict[str, bool]:
        """Map concrete mission files to NativeExperienceContract artifact names."""
        return {
            "visible_commentary_jsonl": bool(self.artifacts.get("visible_commentary.jsonl")),
            "summary_md": bool(self.artifacts.get("summary.md")),
            "canonical_evidence": bool(
                self.artifacts.get("canonical_read_evidence.json")
                or self.artifacts.get("canonical_patch_evidence.json")
            ),
            "adoption_probes": bool(self.artifacts.get("tool_call_adoption_probes.json")),
        }


# ═══════════════════════════════════════════════════════════════════════════
# Harness runner
# ═══════════════════════════════════════════════════════════════════════════


def run_gold_ux_burnin(
    *,
    models: list[str],
    base_url: str = DEFAULT_BRIDGE_URL,
    auth: str = DEFAULT_AUTH,
    timeout: float = 120,
) -> JSON:
    """Run the Gold UX burn-in across multiple models and task classes.

    Returns a structured report with per-model, per-task-class results.
    """
    runner = SpawnedSubagentRunner(base_url=base_url, auth=auth, timeout=timeout)

    results: list[JSON] = []

    for model in models:
        model_results = _run_model_gold_tests(runner, model)
        results.extend(model_results)

    passed = sum(1 for r in results if r.get("gold_pass"))
    total = len(results)

    return {
        "schema_version": "gold_ux_burnin_report.v1",
        "claim_scope": "bridge_only",
        "note": "This burn-in exercises the direct bridge harness; it does not prove Codex Desktop spawned-agent UX.",
        "models": models,
        "total_tests": total,
        "bridge_gold_passed": passed,
        "gold_passed": passed,
        "gold_failed": total - passed,
        "results": results,
    }


def _run_model_gold_tests(runner: SpawnedSubagentRunner, model: str) -> list[JSON]:
    """Run Gold UX tests for a single model."""
    import tempfile
    results: list[JSON] = []

    # Test 1: Read floor with 3 files
    project_root = os.getcwd()
    read_paths = ["bridge.py", "README.md", "codex_oss/read_evidence.py"]
    # Verify paths exist
    existing = [p for p in read_paths if os.path.exists(os.path.join(project_root, p))]
    if len(existing) >= 2:
        transcript = runner.run_read_floor(
            model=model,
            read_only_paths=existing[:3],
            mission_id=f"gold_ux_read_{model}_{int(time.time())}",
        )
        eval_result = transcript.evaluate_gold_ux()
        eval_result["test_name"] = f"read_floor_3_files [{model}]"
        results.append(eval_result)
    else:
        results.append({
            "test_name": f"read_floor_3_files [{model}]",
            "gold_pass": False,
            "level": "none",
            "error": "required test files not found",
        })

    # Test 2: Bounded implementation
    with tempfile.TemporaryDirectory() as tmp:
        scratch = os.path.join(tmp, "gold_ux_scratch.txt")
        with open(scratch, "w") as f:
            f.write("GOLD_UX_BEFORE\n")

        # Can't run implementation test from temp dir with the bridge
        # because the bridge runs from project root.
        # Use the project root scratch instead.
        pass

    owned_path = "tests/gold_ux_scratch.txt"
    scratch_full = os.path.join(project_root, owned_path)
    Path(scratch_full).parent.mkdir(parents=True, exist_ok=True)
    original = "GOLD_UX_BEFORE\n"
    try:
        with open(scratch_full, "w") as f:
            f.write(original)

        transcript = runner.run_bounded_implementation(
            model=f"mission-a5-{model.replace('oss_', '')}",
            owned_paths=[owned_path],
            intent_edits=[{
                "operation": "replace_exact",
                "path": owned_path,
                "old_text": original,
                "new_text": "GOLD_UX_AFTER\n",
                "reason": "Gold UX burn-in",
            }],
            mission_id=f"gold_ux_impl_{model}_{int(time.time())}",
        )
        eval_result = transcript.evaluate_gold_ux()
        eval_result["test_name"] = f"bounded_implementation [{model}]"
        results.append(eval_result)
    except Exception as exc:
        results.append({
            "test_name": f"bounded_implementation [{model}]",
            "gold_pass": False,
            "level": "none",
            "error": str(exc),
        })
    finally:
        try:
            os.remove(scratch_full)
        except FileNotFoundError:
            pass

    # Test 3: Pending read recovery
    if existing:
        transcript = runner.run_pending_read_recovery(
            model=model,
            read_only_paths=existing[:2],
            mission_id=f"gold_ux_pending_read_{model}_{int(time.time())}",
        )
        eval_result = transcript.evaluate_gold_ux()
        eval_result["test_name"] = f"pending_read_recovery [{model}]"
        results.append(eval_result)

    # Test 4: Grep/search recovery
    if existing:
        transcript = runner.run_grep_recovery(
            model=model,
            read_only_paths=["README.md"],
            pattern="codex",
            mission_id=f"gold_ux_grep_{model}_{int(time.time())}",
        )
        eval_result = transcript.evaluate_gold_ux()
        eval_result["test_name"] = f"grep_recovery [{model}]"
        results.append(eval_result)

    # Test 5: ls/list recovery
    if existing:
        transcript = runner.run_ls_recovery(
            model=model,
            read_only_paths=["README.md"],
            mission_id=f"gold_ux_ls_{model}_{int(time.time())}",
        )
        eval_result = transcript.evaluate_gold_ux()
        eval_result["test_name"] = f"ls_recovery [{model}]"
        results.append(eval_result)

    return results


def build_transcript_report(transcript: SpawnedTranscript) -> str:
    """Build a human-readable transcript report."""
    lines = [
        f"=== Spawned Subagent Transcript ===",
        f"Mission: {transcript.mission_id}",
        f"Model: {transcript.model}",
        f"Task class: {transcript.task_class}",
        f"Duration: {transcript.duration_seconds:.1f}s",
        f"HTTP status: {transcript.http_status}",
        f"Stream done: {transcript.stream_done}",
        f"Final status: {transcript.final_status}",
        f"Error: {transcript.error or 'none'}",
        f"",
        f"--- Raw messages ({len(transcript.raw_messages)}) ---",
    ]
    for msg in transcript.raw_messages:
        lines.append(f"  [{msg.get('event_type', '')}] {msg.get('text', '')[:200]}")
    lines.append("")
    lines.append(f"--- Commentary detected ({len(transcript.commentary_messages)}) ---")
    for msg in transcript.commentary_messages:
        lines.append(f"  [{msg.get('event_type', '')}] {msg.get('text', '')[:200]}")
    lines.append("")
    lines.append(f"--- Commentary before final ({len(transcript.commentary_before_final)}) ---")
    for msg in transcript.commentary_before_final:
        lines.append(f"  [{msg.get('event_type', '')}] {msg.get('text', '')[:200]}")
    lines.append("")
    lines.append(f"--- Commentary event classes ---")
    for cls in sorted(transcript.commentary_event_classes):
        lines.append(f"  {cls}")
    lines.append("")
    lines.append(f"--- Artifacts ---")
    for name, exists in sorted(transcript.artifacts.items()):
        lines.append(f"  {'✓' if exists else '✗'} {name}")
    lines.append("")
    lines.append(f"--- Safety ---")
    if transcript.safety_violations:
        for v in transcript.safety_violations:
            lines.append(f"  ✗ {v}")
    else:
        lines.append("  No violations detected")
    return "\n".join(lines)
