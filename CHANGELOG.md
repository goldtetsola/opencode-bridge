# Changelog

## v13 - May 2026

### Runtime-backed OSS subagent UX

Runtime missions now stream safe, native-style progress commentary while they run.

- **Visible commentary streaming**: managed MissionV1 requests start SSE before the runtime loop begins. Commentary messages stream with `phase="commentary"` and final reports stream with `phase="final_answer"`.
- **Richer middle narration**: A2/A3 missions now emit model action requests, model-declared action rationale, tool start, tool result summaries, coverage updates, report validation, and model-call failure events.
- **Visible artifacts**: every current runtime mission writes `visible_commentary.jsonl` and `summary.md`; final reports link both.
- **Safe by design**: commentary exposes public progress, not hidden chain-of-thought, provider reasoning content, full file dumps, secrets, or system/developer prompts.
- **Docs refresh**: README rewritten around user outcomes, setup, MissionV1 usage, visible progress, safety boundaries, artifacts, and troubleshooting.

## v12 — May 2026

### Acceptance model
Generic validation pipeline replaces per-task patches. Every output goes through:

```
handoff schema → scope gate → evidence gate → synthesis gate → deliverable gate → verification gate → acceptance status
```

- **Report contract validation**: Transport, evidence, synthesis, and task status tracked separately. Deterministic fallback returns `PARTIAL`, not fake `PASS`. Thin reports missing required fields rejected.
- **Patch/docs write acceptance**: `bounded_write_exact` only for `exact_content`. Docs/patch tasks route to `bounded_write_patch`. Patch tasks require changed owned paths in git status. No-change or out-of-scope writes fail.
- **Verification ledger**: Verification is observed evidence, not prose. Report claiming "verification passed" invalid unless bridge observed it. Verification failure produces `FAIL`.
- **Evidence coverage gate**: Requested paths/search terms must be in evidence pack. No-match grep preserved as evidence. Confident `PASS` reports rejected if coverage incomplete.
- **Protocol conformance suite**: `tests/test_protocol_conformance.py` covers malformed handoffs, exact-write routing, no-match evidence, incomplete coverage, patch acceptance, verification claims, and scope boundaries.

### OSS subagent runtime
OSS agents are now bounded tool transactions with execution modes, not open-ended autonomous workers.

- **Execution modes**: `no_tool_exact`, `context_pack_report`, `managed_autonomy`, `bounded_write_exact`, `bounded_write_patch`, `escalate` — auto-selected from structured handoff or prose
- **Context-pack mode**: Gathers all `READ-ONLY PATHS` + git commands internally, sends one no-tools synthesis call. Command-aware: `grep X in Y` steps get grep output, not full files
- **Bounded exact writes**: Bridge writes file directly, reads back, returns deterministic PASS/FAIL. No model call needed
- **Intent rejection**: "I will", "Running...", "Starting..." rejected as non-terminal. Internal retry once, then deterministic report from gathered evidence
- **Timeout recovery**: Request-level deadline prevents serial timeout stacking. Deterministic PARTIAL report within deadline
- **Subagent fork detection**: Auto-aliases GPT-5.5 subagent forks to OSS — no `fork_turns` config needed

### Structured handoff
Machine-readable `OSS_HANDOFF_JSON` with required fields (role, goal, task_type, owned_paths, read_only_paths, forbidden_actions, verification_steps, deliverable_fields). Invalid handoffs fail closed before executing.

```bash
codex-oss validate-handoff /path/to/handoff.md
```

### Daemon supervisor
Cross-platform `codex-oss up --daemon` starts bridge as supervised daemon. Terminal doesn't need to stay open. Two-process setup: supervisor owns bridge child, restarts on crash.

```bash
codex-oss up --daemon    # start and detach
codex-oss run -- codex   # start bridge, run Codex CLI, cleanup
codex-oss doctor         # default health/supervisor/source checks
```

### OSS Agent Runtime contract hardening
A2/A3 managed investigations now enter a dedicated runtime adapter instead of living inside the HTTP handler. Structured `oss_agent_mission.v1` handoffs run inside the bridge with JSON-only actions, `tools=[]` upstream, explicit runtime-owned RTK tools, evidence-ledger validation, critical-path read checks, source-hash health identity, and terminal structured reports on runtime failures.

### Doctor
`codex-oss doctor` now separates offline/default/live-model checks. Default doctor checks bridge health and source identity without burning an OSS model call; `--offline` checks local files/config only; `--live-model` runs the optional inference smoke.

---

## v11 — May 2026

### Context-pack mode
When `READ-ONLY PATHS` are explicit, the bridge gathers all files + git commands internally and sends one no-tools synthesis call. Eliminates duplicate reads, multi-turn drift, and parallel-call repair issues.

### Managed autonomy
Scouts get per-task-class budgets (scout:6, prep_report:8). The bridge tracks turns, injects evidence ledgers, and lets the agent continue until budget exhausted or task complete.

### Duplicate suppression
In managed autonomy mode, if the model re-requests an already-read file, the bridge returns `[ALREADY READ]` with remaining paths instead of re-executing.

### Evidence ledger
Bridge injects progress context into continuation turns: already read files, remaining required, budget remaining.

### Task-class budgets
Per-task-class tool budgets: scout:6, prep_report:8, bounded_write:3, docs_support:4, proof_critical:0.

---

## v10 — May 2026

### ResponseEmitter
Single transport abstraction guarantees terminal SSE for every request. `stream=true` always receives `response.completed` or `response.failed` before `[DONE]`. No silent disconnects.

### Transport self-tests
5 tests verify stream contract: deterministic write, non-stream write, read finalizer, stalled upstream degradation, health identity.

---

## v9 — May 2026

### Live upstream streaming
`stream=true` against OpenCode Go. Translates Chat Completions chunks to Responses SSE deltas in real time.

### GPT model handling
Bridge detects GPT-5.5/5.4 requests. `GPT_MODEL_STRATEGY=error` rejects top-level misuse with diagnostic message. `GPT_MODEL_STRATEGY=oss` aliases to OSS for compatibility testing.

---

## v8 — May 2026

### Managed autonomy (initial)
Replaced global `OSS_NATIVE_MAX_TOOL_EXCHANGES=1` with per-task-class budgets.

### Evidence ledger (initial)
Bridge injects progress context from conversation history.

---

## v7 — May 2026

### Bridge hardening
- Concurrency semaphores: global + per-model caps prevent rate-limit death spirals
- Circuit breaker: marks models degraded after sustained capacity errors, auto-routes to fallback
- Orphan recovery: returns 200 with recovery message instead of 400 error when state is lost
- Fatal missing key: bridge refuses to start in production mode without `OPENCODE_GO_API_KEY`
- Health enrichment: `/health` exposes model health, concurrency config, process identity

---

## v6 — May 2026

### GPT model handling (initial)
Bridge detects and routes GPT-model requests. Fails fast with diagnostic message instead of silently forwarding to OpenCode Go.

### SSE heartbeat
`response.created` sent immediately. Heartbeat comments prevent Codex timeout on complex queries.

---

## v5 — May 2026

### Live upstream streaming (initial)
First implementation of `stream=true` against OpenCode Go with real-time SSE translation.

---

## v3–v4 — May 2026

### Early bridge iterations
SSE streaming, heartbeat keepalive, tool format conversion, reasoning preservation, conversation state repair.
