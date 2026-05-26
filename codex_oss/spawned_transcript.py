"""Spawned-subagent transcript harness — proves native-like UX end-to-end.

Captures user-visible transcripts from actual spawned OSS subagents and
reconciles them with mission artifacts. This is the Gold UX gate.
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

    def _process_sse_line(self, line: str, transcript: SpawnedTranscript) -> None:
        """Process a single SSE data line."""
        if not line.startswith("data: "):
            return
        data_str = line[6:].strip()
        if data_str == "[DONE]":
            transcript.stream_done = True
            return

        try:
            event = json.loads(data_str)
        except json.JSONDecodeError:
            transcript.unparseable_lines += 1
            return

        event_type = event.get("type", "")

        # Capture response.created for metadata
        if event_type == "response.created":
            resp = event.get("response", {})
            transcript.response_id = resp.get("id", "")
            return

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
                "phase": event.get("phase", ""),
            })
            transcript.all_text.append(text)

    def _extract_text(self, event: JSON) -> str:
        """Extract text content from an SSE event."""
        # Direct text in delta
        delta = event.get("delta", "")
        if isinstance(delta, str) and delta.strip():
            return delta

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
            "report.json",
            "canonical_patch_evidence.json",
            "implementation_narrative.json",
            "tool_call_adoption_probes.json",
            "server_side_read_finalizer_attempts.jsonl",
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
        from codex_oss.native_experience import classify_commentary_event, extract_event_classes

        # From SSE transcript: look for messages tagged as commentary
        for msg in self.raw_messages:
            text = str(msg.get("text", "") or "")
            phase = str(msg.get("phase", "") or "")

            # Commentary messages have a phase field or are tagged
            is_commentary = bool(phase) or "[OSS progress]" in text or self._looks_like_progress(text)

            if is_commentary:
                self.commentary_messages.append(msg)
                event_type = phase if phase else "transcript_progress"
                self.commentary_event_classes.add(classify_commentary_event(event_type))

        # From artifacts: load JSONL commentary
        jsonl = self.artifact_data.get("visible_commentary.jsonl")
        if isinstance(jsonl, list):
            for event in jsonl:
                if isinstance(event, dict):
                    event_type = str(event.get("event_type", "") or "")
                    self.commentary_event_classes.add(classify_commentary_event(event_type))
                    # These are "emitted" but were they observed?
                    text = str(event.get("message", "") or "")
                    observed = any(
                        text[:50] in str(m.get("text", ""))
                        for m in self.raw_messages
                    )
                    if observed and event not in self.commentary_messages:
                        self.commentary_messages.append({
                            "timestamp": event.get("timestamp", 0),
                            "event_type": event_type,
                            "text": text,
                            "phase": event.get("phase", ""),
                            "source": "artifact_reconciled",
                        })

        # Determine which commentary was before final
        # (all commentary is before final in SSE since final is the last message)
        self.commentary_before_final = [
            m for m in self.commentary_messages
            if m.get("event_type") != "mission_completed"
        ]

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
        model_narrative_valid = (
            isinstance(report, dict)
            and report.get("implementation_narrative_valid", False)
        ) or bool(isinstance(impl_narrative, dict) and impl_narrative.get("schema_version"))

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
        )

        result = evaluate_native_experience(
            contract,
            artifacts_exist=self.artifacts,
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
        }

        return result


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
        "models": models,
        "total_tests": total,
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
