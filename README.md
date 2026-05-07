# OpenCode Bridge

Use [OpenCode Go](https://opencode.ai/docs/go/) OSS models (DeepSeek V4 Pro, Kimi K2.6, DeepSeek V4 Flash) as **native [Codex](https://developers.openai.com/codex) subagents** — with full tool-loop support, multi-turn conversation, reasoning preservation, and orchestration routing.

Codex speaks the OpenAI Responses API. OpenCode Go exposes Chat Completions. This bridge sits in the middle, translating between them so Codex can spawn DeepSeek and Kimi workers the same way it spawns GPT workers.

## Critical: Architecture warning

**Do NOT set `model_provider = "opencode_bridge"` as your top-level Codex provider.** Codex will route GPT-5.5 orchestrator requests through the bridge, which cannot serve GPT models (OpenCode Go rejects them). This causes timeouts on any operation requiring orchestration (reads, writes, multi-turn tool loops).

**Correct architecture:**

```
Parent session: GPT-5.5 (native openai provider)
OSS subagents only: opencode_bridge provider
```

The bridge is a **subagent-only provider**. Use `model_provider = "opencode_bridge"` in agent TOMLs only. Your `.codex/config.toml` should NOT set a top-level `model_provider` to `opencode_bridge`.

For direct `codex exec` testing without subagents, use v6 compatibility mode: start the bridge with `GPT_MODEL_STRATEGY=oss` to alias GPT requests to OSS models. This is for bridge testing only — not the recommended production setup.

## Quick start

```bash
# 1. Clone the bridge
git clone https://github.com/goldtetsola/opencode-bridge.git ~/bridge

# 2. Set your OpenCode Go key
echo 'OPENCODE_GO_API_KEY=sk-...' > ~/bridge/.codex-oss/env/opencode-go.env

# 3. cd into your Codex project and install
cd ~/your-codex-project
python3 ~/bridge/bin/codex-oss install

# 4. Start the bridge under a supervisor
OPENCODE_GO_API_KEY=sk-... python3 ~/bridge/bin/codex-oss up --daemon

# 5. Verify everything
python3 ~/bridge/bin/codex-oss doctor
# Add --live-model only when you intentionally want to spend one OSS model call
```

That's it. `codex-oss install` generates all config — `.codex/config.toml`, agent TOMLs, `AGENTS.md` routing rules, and recursive-codex-exec blocking. `codex-oss doctor` checks setup, bridge health, supervision, source identity, and state persistence, then tells you exactly what to fix.

## What `codex-oss` does

| Command | What it does |
|---|---|
| `install` | Generates provider config, 3 agent TOMLs, AGENTS.md delegation contract, recursive-codex blocking rules, runtime directories, and gitignore entries. |
| `doctor` | Checks config correctness, agent configuration, AGENTS.md compliance, structured handoff contract, recursive-codex exec blocking, bridge health, durable supervision, source hash identity, GPT leakage, and state DB persistence. PASS/WARN/FAIL with fix instructions. Supports `--offline`, `--json`, and `--live-model` for optional inference smoke. |
| `start` | Launches the bridge with mode selection (`production`/`compat-test`/`openai`). Refuses to start in production mode without a valid key. Sets project-local state paths. |
| `stop` | Graceful shutdown via PID file or port. |
| `status` | Queries the bridge health endpoint — shows version, mode, model health, and concurrency config. |

## Architecture

```
Codex Desktop / CLI
    │
    │  Parent session: GPT-5.5 (native openai provider)
    │  OSS subagents only: opencode_bridge provider
    │
    ├─ GPT-5.5 orchestrator (native)
    │     │
    │     ├─ GPT-5.4 worker (native)
    │     └─ OSS subagent spawn
    │           │
    │           │  Responses API (SSE streaming, live upstream)
    │           ▼
    │     bridge.py   ← this repo
    │           │
    │           │  Chat Completions API (stream=true)
    │           ▼
    │     api.opencode.ai/zen/go/v1
    │           │
    │           ▼
    │     DeepSeek V4 Pro / Kimi K2.6 / Flash
```

**Bridge is a subagent-only provider.** Do NOT set `model_provider = "opencode_bridge"` as your session-wide provider. The bridge rejects GPT-5.5 requests (or aliases them to OSS in compatibility mode, which is for testing only).

The bridge handles:

- **Protocol translation**: Responses API ↔ Chat Completions (request format, tool definitions, output items)
- **SSE streaming with heartbeat**: Sends `response.created` immediately, then heartbeat comments during upstream processing. Prevents Codex timeouts on complex queries with long reasoning.
- **Tool type filtering**: Strips hosted tools (image_generation, web_search, code_interpreter), MCP namespaces, and app/connector tools that OSS providers reject
- **Tool format conversion**: Responses flat format → Chat Completions nested `function` wrapper, with name sanitization for strict providers
- **Reasoning preservation**: DeepSeek V4 Pro requires `reasoning_content` to be replayed across multi-turn tool calls. The proxy stores and injects it correctly
- **Conversation state**: Tracks response history in SQLite so tool round-trips survive proxy restarts. Matches orphan `function_call_output` items to cached `function_call` items by `call_id`
- **Context preservation**: Repairs conversation history so earlier completed assistant→tool exchanges are preserved (not truncated), while incomplete tails are dropped
- **Retry + fallback**: Retries transient upstream errors with exponential backoff. Falls back to alternate models on capacity errors
- **Developer role mapping**: Maps Codex's `developer` role to `system` for providers that reject it (DeepSeek, Kimi)
- **GPT model handling** (v6): Detects and rejects GPT-5.5/5.4 requests hitting the bridge by mistake. Configurable via `GPT_MODEL_STRATEGY` — `error` (immediate rejection, default), `oss` (alias to OSS for compatibility testing), or `openai` (API passthrough)
- **Live upstream streaming** (v5+): Uses `stream=true` against OpenCode Go and translates Chat Completions chunks to Responses SSE deltas in real time

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `OPENCODE_GO_API_KEY` | (required) | Your OpenCode Go API key |
| `UPSTREAM_API_KEY` | falls back to `OPENCODE_GO_API_KEY` | Generic upstream API key for non-OpenCode backends such as vLLM. Use this for backend experiments; keep `OPENCODE_GO_API_KEY` for the normal OpenCode Go path. |
| `PROXY_API_KEY` | `LITELLM_MASTER_KEY` value | Key Codex sends to authenticate with the proxy |
| `LITELLM_MASTER_KEY` | `sk-local-codex-bridge` | Auth key (shared name for Codex config compatibility). Leave empty for no auth on localhost. |
| `PROXY_PORT` | `4000` | Port the proxy listens on |
| `PROXY_STATE_DB` | `/tmp/opencode_responses_proxy_state.sqlite3` | SQLite file for conversation state |
| `FORCE_SINGLE_TOOL_INSTRUCTIONS` | `0` | Set to `1` to inject a guard discouraging parallel tool calls |
| `FALLBACK_MODEL_MAP_JSON` | deepseek→kimi/flash fallback | JSON map of model→fallback chain |
| `UPSTREAM_TIMEOUT_SECONDS` | `240` | Timeout for upstream API calls |
| `UPSTREAM_RETRIES` | `2` | Number of retries on transient errors |
| `MODEL_MAP_JSON` | (built-in) | Override model name mapping |
| `PROXY_LOG_PATH` | (stderr) | Path for structured JSON log output |
| `SSE_CHUNK_SIZE` | `256` | Characters per SSE text delta chunk |
| `SSE_UPSTREAM_HEARTBEAT_SECONDS` | `5` | Seconds between heartbeat comments while waiting for upstream |
| `UPSTREAM_STREAM` | `1` | Use stream=true for upstream Chat Completions (live streaming) |
| `GPT_MODEL_STRATEGY` | `error` | How to handle GPT-model requests: `error` (reject immediately), `oss` (alias to OSS model for testing), `openai` (passthrough to OpenAI API — requires `OPENAI_API_KEY`) |
| `GPT_MODEL_OSS_FALLBACK` | `deepseek-v4-pro` | OSS model to use when `GPT_MODEL_STRATEGY=oss` |
| `OPENAI_API_KEY` | (not set) | Required only for `GPT_MODEL_STRATEGY=openai` |
| `MAX_GLOBAL_UPSTREAM_CONCURRENCY` | `2` | Cap concurrent upstream requests globally |
| `MODEL_CONCURRENCY_JSON` | deepseek/kimi 1, flash 2 | Per-model concurrency caps |
| `CIRCUIT_BREAKER_ERRORS` | `2` | Errors before marking a model degraded |
| `CIRCUIT_BREAKER_COOLDOWN` | `300` | Seconds before auto-recovering a degraded model |
| `OSS_MAX_TOOL_TURNS` | `6` | Max tool turns before OSS agent is stopped |
| `ALLOW_MISSING_OPENCODE_KEY` | `0` | Set to `1` to bypass fatal key check |
| `OSS_NATIVE_MAX_TOOL_EXCHANGES` | `1` | Max tool calls per OSS subagent turn |
| `CONTINUATION_TOOLS` | `none` | Tools for continuation turns (`none` = force finalization) |
| `CONTINUATION_MODEL` | `kimi-k2.6` | Model for read-result finalizer |
| `CONTINUATION_DEADLINE_SECONDS` | `60` | Deadline for finalizer model calls |
| `WRITE_RESULT_MODE` | `deterministic` | Write results: `deterministic` = no model call |
| `MAX_TOOL_OUTPUT_CHARS` | `20000` | Compact tool outputs larger than this |
| `UPSTREAM_FIRST_BYTE_TIMEOUT_SECONDS` | `30` | Timeout for first byte from upstream |
| `UPSTREAM_IDLE_TIMEOUT_SECONDS` | `30` | Timeout for upstream idle during processing |
| `DEGRADED_COMPLETION_ON_TIMEOUT` | `1` | Return degraded report on timeout |
| `EXPOSE_EMPTY_REASONING_ITEM` | `1` | Include empty reasoning item in output for Codex compatibility |
| `STRIP_TOOLS` | `0` | Set to `1` to strip ALL tools (force text-only responses) |

## Supported models

| Codex model ID | Upstream model | Best for |
|---|---|---|
| `ocg-deepseek-v4-pro` | deepseek-v4-pro | Bounded implementation, debugging, reasoning-heavy analysis |
| `ocg-kimi-k2.6` | kimi-k2.6 | Fast repo navigation, scouting, review |
| `ocg-deepseek-v4-flash` | deepseek-v4-flash | Docs, summaries, mechanical low-risk tasks |
| `ocg-kimi-k2.5` | kimi-k2.5 | (untested) |
| `ocg-qwen3.6-plus` | qwen3.6-plus | (untested) |
| `ocg-glm-5.1` | glm-5.1 | (untested) |
| `ocg-minimax-m2.7` | minimax-m2.7 | (untested) |

Also accepts OpenCode-style `opencode-go/<model>` model IDs.

## Optional vLLM backend exploration

vLLM is worth testing as a backend, but it is not a replacement for the OSS Agent Runtime. The useful near-term lane is:

```text
Codex runtime-backed agent
→ mission-a2/mission-a3 alias
→ OSS Agent Runtime
→ JSON action loop with tools=[] upstream
→ vLLM-served reasoning model
→ ValidatedReportV1
```

This keeps path policy, evidence ledgers, report validation, status-text rejection, and GPT-5.5 final judgment in the bridge runtime. vLLM may improve privacy, provider independence, and local/open-weight model testing, but it does not by itself prevent raw file dumps, unsupported final claims, bad command choices, or missing evidence refs.

To try vLLM behind the runtime, start a vLLM server separately and launch the bridge with `examples/vllm-runtime.env.example`, replacing the served model names in `MODEL_MAP_JSON`.

```bash
# Example vLLM command; choose flags for your model/parser.
vllm serve Qwen/Qwen3.6-27B \
  --port 8000 \
  --served-model-name Qwen/Qwen3.6-27B \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder

set -a
. examples/vllm-runtime.env.example
set +a
python3 bridge.py
```

For direct Codex-to-vLLM research, `config.toml.example` includes a commented `vllm_direct` provider using `wire_api = "responses"`. Treat that as an R&D comparison lane only. Serious OSS investigation should still enter through `oss_runtime` and MissionV1.

Evaluation gates for vLLM:

1. No-tool exact output succeeds.
2. Responses terminal contract succeeds.
3. MissionV1 runtime alias returns `COMPLETE`, `PARTIAL`, `ESCALATE`, or `FAILED`.
4. Upstream receives `tools=[]` for A2/A3 runtime missions.
5. Raw direct vLLM behavior is measured only as a baseline, not accepted as the product path.

## Explicit MissionV1 delegation

If the active Codex session cannot invoke repo-local runtime-backed agent TOMLs by name, use the CLI delegation surface. This is the reliable fallback for “spawn an OSS investigator” because it still enters the bridge through MissionV1 and the managed runtime.

Create a mission:

```bash
bin/codex-oss mission template \
  --mission-id mission_readme_scan \
  --objective "Inspect README runtime-agent guidance and report caveats." \
  --allowed-path README.md \
  > /tmp/mission_readme_scan.json
```

Run it through the local bridge:

```bash
bin/codex-oss mission run /tmp/mission_readme_scan.json --model mission-a3-kimi
```

The command posts a `/v1/responses` request to the local bridge using `model = "mission-a3-kimi"` by default. The bridge then maps the runtime alias to the OSS reasoning model, uses `tools=[]` upstream, executes only runtime-owned tools, validates the report, and prints the final report text. If the bridge is not running, start it first with `bin/codex-oss up` or `bin/codex-oss start`.

## Bridge: OSS subagent runtime

OSS agents are bounded transactions with deterministic finalization, managed autonomy, and context-pack mode for prep tasks. The bridge can reject or alias accidental GPT-family traffic according to `GPT_MODEL_STRATEGY`, but correct Codex orchestration still requires OSS agents to be spawned with `fork_turns: "none"` so the child does not inherit the parent GPT model/provider context.

### Execution modes

The bridge selects the right mode based on the task handoff:

- **`invalid_handoff`** — malformed `OSS_HANDOFF_JSON` blocks fail closed before delegated work is trusted.
- **`no_tool_exact`** — exact output tasks (guardrail tests, control probes). No model decisions needed.
- **`context_pack_report`** — read/report tasks with explicit `READ-ONLY PATHS` + `DELIVERABLE`. Bridge gathers all files internally, sends one no-tools synthesis call, and validates that the final text includes the requested structured deliverable fields. Command-aware: `grep X in Y` steps get grep output, not full files.
- **`managed_autonomy`** — discovery tasks where the model chooses what to inspect. Budget-capped, duplicate-suppressed, evidence-ledger-injected.
- **`A2/A3 MissionV1`** — schema-first managed investigation using `<OSS_HANDOFF_JSON>` with `schema_version: "oss_agent_mission.v1"`. The loop runs inside the bridge runtime with JSON actions, `tools=[]` upstream, runtime-owned RTK tools, explicit path scope, evidence refs, and terminal structured reports.
- **`bounded_write_exact`** — writes to explicit `OWNED PATHS` only when `exact_content` is present. Bridge writes the file directly, reads it back, and returns PASS only when observed content matches the declared exact content. No model call needed.
- **`bounded_write_patch`** — model-assisted edits to explicit `OWNED PATHS` when no `exact_content` is present, including `docs_support` tasks. Permission fields define scope; they do not by themselves select exact-write mode.

### Guarantees

- **Exact writes**: Deterministic — bridge writes declared exact content, reads back, and reports PASS only on exact match. No model self-report dependency.
- **Patch/docs writes**: Scope-bounded — owned paths constrain the edit lane. The bridge checks changed owned paths, rejects no-change or out-of-scope writes, and gives the agent a bounded verification turn before the orchestrator treats the patch as accepted.
- **Reads/reports**: Context-pack or managed autonomy with deadline control. Grep-directed searches include only grep output instead of full files. Transport success and task success are separate: if synthesis is unavailable or required deliverable fields are missing, the bridge returns `PARTIAL` with evidence and caveats instead of pretending the delegated task passed.
- **Evidence coverage**: Requested read paths and search terms must be represented in the evidence pack as read, searched, errored, or rejected. A confident report with incomplete evidence coverage is rejected or downgraded to `PARTIAL`.
- **Verification ledger**: Verification is an observed tool/result, not a prose claim. Reports that request or claim verification are rejected or downgraded unless the bridge observed the verification turn.
- **Intent rejection**: "I will", "Running...", "Starting..." rejected as non-terminal. Internal retry once, then deterministic report from gathered evidence.
- **Timeout recovery**: Request-level deadline prevents serial timeout stacking. Deterministic PARTIAL report within deadline instead of client disconnect.
- **Terminal guarantee**: Every path emits `response.completed` or `response.failed` before closing the SSE stream.

### Structured handoff contract

Prefer a machine-readable block before human prose. The bridge validates this block and uses it instead of guessing from accumulated conversation history:

```text
OSS_HANDOFF_JSON:
{"schema_version":1,"role":"Read-only repo scout","goal":"Find exact evidence for a blocker","task_type":"scout","owned_paths":[],"read_only_paths":["scripts","docs"],"forbidden_actions":["edit files","print secrets"],"verification_steps":["Search for blocker_name"],"deliverable_fields":["confidence","evidence","caveats"],"completion_rule":"stop after the report","escalation_rule":"stop if a critical path appears"}
```

If the JSON is malformed or missing required fields, the bridge returns `FAIL` instead of guessing a mode from prose. You can validate a handoff before spawning an OSS subagent:

For managed A2/A3 investigation, use the stricter v1 mission schema:

```text
<OSS_HANDOFF_JSON>
{
  "schema_version": "oss_agent_mission.v1",
  "mission_id": "mission_example",
  "tier": "A3",
  "mode": "managed_investigation",
  "objective": "Find evidence for a read-only repo question.",
  "risk_tier": "low",
  "write_allowed": false,
  "allowed_roots": ["docs/"],
  "allowed_paths": [],
  "tool_budget": 10,
  "time_budget_seconds": 90,
  "allowed_tool_classes": ["read", "search", "list"],
  "stop_conditions": ["valid_report", "budget_exhausted", "deadline_reached"],
  "report_schema": "managed_investigation_report.v1",
  "required_outputs": ["files_inspected", "commands_run", "findings", "uncertainties", "confidence", "caveats", "escalation_recommendation"]
}
</OSS_HANDOFF_JSON>
```

A2/A3 missions fail closed without explicit `allowed_roots` or `allowed_paths`, `allowed_tool_classes`, and `required_outputs`. Critical-path reads require `critical_path_read_allowed: true`, a `critical_path_reason`, and exact paths or narrow roots.

```bash
python3 ~/bridge/bin/codex-oss validate-handoff /path/to/handoff.md
```

### Protocol conformance

Run the protocol conformance suite before trusting a bridge release:

```bash
python3 tests/test_protocol_conformance.py
```

It covers malformed structured handoffs, exact-write routing, no-match search evidence, incomplete evidence coverage, bounded patch acceptance, verification claims, verification output failure detection, and scope-boundary checks.

### OSS coding gauntlet

Run the deterministic coding gauntlet before promoting A2/A3 behavior. It drives the internal runtime loop against a fixture repo and checks agent-facing behavior rather than only protocol mechanics:

```bash
python3 tests/test_oss_gauntlet.py
```

The gauntlet covers implementation-site discovery, file and command evidence refs, duplicate-read suppression, critical-risk escalation before model calls, status-text repair, budget exhaustion, broad-scope denial, unsupported false confidence, and zero-match search evidence. Live OSS provider burn-in is intentionally separate from this deterministic suite and should only be run with a supervised sidecar plus explicit live-mode setup.

The suite generates its fixture under `tmp/oss-agent-runtime-gauntlet/`, which is ignored. It does not require a local RTK binary; the runtime-owned `rtk_*` tools have native fallbacks for public-repo testing while preserving the same model-facing tool names.

### Runtime environment variables

| Variable | Default | Description |
|---|---|---|
| `OSS_NATIVE_MAX_TOOL_EXCHANGES` | `1` | Max tool calls per OSS subagent turn |
| `CONTINUATION_TOOLS` | `none` | Tools for continuation turns (`none` = no tools) |
| `CONTINUATION_MODEL` | `kimi-k2.6` | Model for read finalizer |
| `CONTINUATION_FALLBACK_MODELS` | `deepseek-v4-flash` | Fallback finalizer models |
| `CONTINUATION_DEADLINE_SECONDS` | `60` | Deadline for finalizer calls |
| `WRITE_RESULT_MODE` | `deterministic` | Write handling: `deterministic` = no model call |
| `MAX_TOOL_OUTPUT_CHARS` | `20000` | Compact outputs larger than this |
| `FORCE_SINGLE_TOOL_INSTRUCTIONS` | `1` | Enforce single-tool-per-turn (prevents parallel-call repair failures) |
| `DEGRADED_COMPLETION_ON_TIMEOUT` | `1` | Return degraded report on timeout |
| `REQUEST_DEADLINE_SECONDS` | `90` | Hard deadline for entire request |
| `CONTEXT_PACK_MAX_CHARS` | `24000` | Max chars in context-pack source bundle |
| `GPT_MODEL_STRATEGY` | `error` | GPT handling: `error` (reject top-level misuse, auto-alias subagent forks), `oss` (alias all), `openai` (passthrough) |

## Model-task matrix

| Model | Best for | Real example | Rate limit |
|---|---|---|---|
| DeepSeek V4 Flash | Docs, summaries, mechanical edits, test inventories | "Write a changelog entry for the last 3 commits" | 31K req/5hr |
| DeepSeek V4 Pro | Bounded implementation, debugging, feature work | "Add a test for the validateToken function following existing patterns" | 3.4K req/5hr |
| Kimi K2.6 | Repo exploration, scouting, code review, fast navigation | "Find every place that calls formatName and summarize the call patterns" | 1.1K req/5hr |
| GPT-5.4 | Implementation where blast radius matters, cross-module changes | "Refactor the publish-bundle hydration to use the new artifact reader" | Usage-based |
| GPT-5.5 | Architecture, final review, critical paths | "Review this recovery path change for safety" | Usage-based |

## Orchestration

### How routing works

Codex's orchestrator (GPT-5.5) reads `AGENTS.md` and agent `description` fields from `.codex/agents/` to decide which worker handles each task:

```
You: "Find all callers of formatName"
         │
         ▼
   GPT-5.5 reads AGENTS.md routing rules
         │
         │  Safety check: auth? No
         │  Task type: exploration → Kimi
         │
         ▼
   GPT-5.5 spawns oss_kimi_rapid (fork_turns: "none")
         │  Handoff: task scope, allowed paths, output format
         ▼
   Kimi returns file paths + line numbers + confidence
         │
         ▼
   GPT-5.5 synthesizes result. Done.
```

### Safety boundaries

OSS agents have explicit "DO NOT USE FOR" descriptions and developer instructions that prevent them from touching critical paths. If the orchestrator routes an auth/schema/recovery task to an OSS agent, the agent should refuse.

Critical paths that must stay on GPT-5.5/5.4:
- Authentication, authorization, session management
- Recovery paths, error recovery, state repair
- Schema authority, database migrations
- CI gates, build pipelines, deployment
- Cross-module invariants (>2 modules affected)
- Any path where failure = data loss or security breach

### Direct OSS subagent caveat

Direct Codex OSS subagents still receive a broad Codex tool environment. They may choose non-portable shell commands or dump raw tool output unless the installed agent templates and handoff explicitly forbid that behavior. The managed A2/A3 MissionV1 runtime is the stricter path: it disables provider-native tools, executes only runtime-owned read/search/list/safe-git tools, validates evidence refs, and rejects status text or unsupported reports.

For direct subagent use, `codex-oss install` writes agent instructions that require portable commands, one retry after blocked commands, no full-file output dumps, and mandatory output-format compliance. Treat direct OSS subagent reports as evidence, not final authority.

### Fork mode

OSS subagents must be spawned with `fork_turns: "none"`. Full-history forks inherit the parent GPT-5.5 model and reasoning effort, which conflicts with the model/provider overrides OSS agents need. This is a known Codex limitation ([issue #20077](https://github.com/openai/codex/issues/20077)).

The AGENTS.md handoff template includes this requirement. See `orchestration/ROUTING.md` for details.

### Orchestration files

| File | Purpose |
|---|---|
| `orchestration/AGENTS.md` | Routing rules for GPT-5.5. Merge into your project's AGENTS.md. |
| `orchestration/ROUTING.md` | Reference: decision flowchart, capability matrix, handoff examples, troubleshooting. |
| `orchestration/agents/*.toml` | Recommended agent TOMLs with explicit routing descriptions and safety boundaries. |

## Agent TOMLs

Runtime-controlled investigator agents and raw experimental agents are provided in `orchestration/agents/` (recommended) and `agents/` (minimal). Use runtime-controlled agents for normal A2/A3 read-only investigation:

| Agent TOML | Model | Reasoning | Sandbox | Use case |
|---|---|---|---|---|
| `oss-kimi-investigator.toml` | mission-a3-kimi | medium | read-only | Runtime-controlled repo navigation, scouting, review |
| `oss-deepseek-investigator.toml` | mission-a3-deepseek | high | read-only | Runtime-controlled reasoning-heavy investigation |
| `oss-flash-context.toml` | mission-a2-flash | medium | read-only | Runtime-controlled cheap context/report work |
| `oss-deepseek-pro.toml` | deepseek-v4-pro | high | workspace-write | Raw experimental bounded impl/debugging baseline |
| `oss-kimi-rapid.toml` | kimi-k2.6 | medium | read-only | Raw experimental repo navigation baseline |
| `oss-flash-support.toml` | deepseek-v4-flash | medium | read-only | Raw experimental docs/support baseline |

### Creating your own agent

You can create agents for any model OpenCode Go supports:

1. **Pick a model ID**. Run `curl https://opencode.ai/zen/go/v1/models -H "Authorization: Bearer $OPENCODE_GO_API_KEY"` to see the full catalog. Use the model name with an `ocg-` prefix (e.g. `qwen3.6-plus` → `ocg-qwen3.6-plus`).

2. **Create a `.toml` file** in your project's `.codex/agents/`:

```toml
name = "oss_my_worker"
description = "What this agent does. USE ME WHEN: <criteria>. DO NOT USE FOR: <boundaries>."

model_provider = "oss_runtime"           # runtime-controlled A2/A3 agents
model = "mission-a3-kimi"                # mission-a3-kimi / mission-a3-deepseek / mission-a2-flash
model_reasoning_effort = "medium"
sandbox_mode = "read-only"
```

For raw experimental agents only:

```toml
model_provider = "opencode_bridge"
model = "ocg-<model-id>"                 # e.g. ocg-qwen3.6-plus
model_reasoning_effort = "high"          # high / medium / low
sandbox_mode = "workspace-write"         # or "read-only"

developer_instructions = """
Your instructions. Rules, scope, output format, escalation criteria.
Include: confidence marker (HIGH/MEDIUM/LOW), files inspected, caveats.
"""
```

3. **Set the right reasoning effort**:

| Effort | When to use | Example models |
|---|---|---|
| `high` | Implementation, debugging, analysis | deepseek-v4-pro |
| `medium` | Navigation, docs, summaries, mechanical | kimi-k2.6, deepseek-v4-flash |
| `low` | Trivial text generation | Any fast model |

4. **Choose the right sandbox mode**:

| Mode | Permissions | Best for |
|---|---|---|
| `workspace-write` | Read and edit project files | Implementation, debugging, refactoring |
| `read-only` | Read files, run safe commands | Exploration, review, docs, analysis |

5. **Write good descriptions**. The `description` field is Codex's routing signal. Include both "use me when" AND "do NOT use for" criteria. Example:

```
"Bounded implementation worker. USE ME WHEN: single file change, tests exist, requirements clear. DO NOT USE FOR: auth, schema, recovery, cross-module changes."
```

6. **Register in AGENTS.md**. Add your agent to the routing rules so the orchestrator knows when to delegate to it.

7. **Use `fork_turns: "none"`**. OSS agents use different models/providers than GPT-5.5, so they must not inherit the parent session via full-history fork.

### Tested models

| Agent | Model | Reasoning | Sandbox | Status |
|---|---|---|---|---|
| `oss-deepseek-pro.toml` | deepseek-v4-pro | high | workspace-write | Working |
| `oss-kimi-rapid.toml` | kimi-k2.6 | medium | read-only | Working |
| `oss-flash-support.toml` | deepseek-v4-flash | medium | read-only | Working |
| `oss-qwen3.6-plus` (custom) | qwen3.6-plus | — | — | Untested |
| `oss-glm-5.1` (custom) | glm-5.1 | — | — | Untested |
| `oss-minimax-m2.7` (custom) | minimax-m2.7 | — | — | Untested |

Untested models may need adjustments — some providers are stricter about tool schemas (shape failures) or message format requirements (relational failures). The bridge strips unsupported tool types and maps `developer` → `system`, but provider-specific quirks may still surface. If you test an untested model, open an issue with your findings.

## External OSS workers (fallback)

The `bin/` directory includes four wrapper scripts for running OSS models as external workers (via `opencode run` directly, without the proxy):

| Script | Default model | Purpose |
|---|---|---|
| `oss-scout` | kimi-k2.6 | Read-only repo exploration, file mapping, summaries |
| `oss-review` | kimi-k2.6 | First-pass review, missing-test detection |
| `oss-docs` | deepseek-v4-flash | Docs, changelog, low-stakes text |
| `oss-patch` | deepseek-v4-pro | Isolated patch drafts in separate worktree |

Use these when the proxy is down, rate-limited, or you need an isolated worktree for write tasks.

## Model routing lanes

```
Lane A — GPT-5.5
  Orchestration, architecture, final acceptance, critical review

Lane B — GPT-5.4
  Trusted bounded implementation and review

Lane C — GPT-5.4-mini
  Cheap read-heavy exploration and support

Lane D — OSS native subagents (through this bridge)
  oss-deepseek-pro: bounded implementation, debugging, analysis
  oss-kimi-rapid: repo navigation, scouting, review
  oss-flash-support: docs, summaries, mechanical

Lane E — OSS external workers (fallback)
  Direct opencode run when proxy is unavailable
```

## Limitations

- **Not production-grade**: This is a local development tool. It uses a single-threaded Python HTTP server (though concurrent via ThreadingHTTPServer) and no authentication beyond a shared key (configurable; disable entirely for localhost).
- **Single machine only**: Bind to localhost. Do not expose publicly.
- **Subagent-only provider**: The bridge is NOT a session-wide Codex provider. GPT-5.5 must remain native. Only OSS agents in `.codex/agents/` should route through the bridge.
- **DeepSeek thinking mode costs tokens**: DeepSeek V4 Pro's reasoning_content is preserved internally but counts against your OpenCode Go usage. Expect ~300-400K tokens for multi-turn coding tasks.
- **True upstream streaming** (v5+): The bridge uses `stream=true` against OpenCode Go and translates chunks live. This reduces latency compared to v3's fake SSE but depends on OpenCode Go's streaming behavior.
- **Subagent spawning**: OSS agents must use `fork_turns: "none"` (full-history forks conflict with model/provider overrides). The orchestrator needs to include explicit task context in handoffs since the child doesn't inherit parent conversation history.

## Self-test

```bash
python3 bridge.py --self-test
```

Expected output:

```
self-test passed
```

## Troubleshooting

**First step for any issue:** run `codex-oss doctor`. It checks setup, bridge health, durable supervision, source identity, and state persistence without spending a model call. Use `codex-oss doctor --offline` for local config only and `codex-oss doctor --live-model` when you intentionally want an OSS inference smoke.

Common issues the doctor catches:

| Problem | Doctor check | What it means |
|---|---|---|
| GPT requests timing out | `bridge.gpt_rejection` FAIL | Bridge set as session-wide provider. Remove `model_provider = "opencode_bridge"` from config. |
| OSS agents can't spawn | `agents.*.provider` FAIL | Agent TOML missing `model_provider = "opencode_bridge"`. Run `codex-oss install --force`. |
| Bridge won't start | Fatal at launch | `OPENCODE_GO_API_KEY` not set in production mode. Set key or `ALLOW_MISSING_OPENCODE_KEY=1`. |
| State lost after reboot | `bridge.state_db` WARN | State DB in `/tmp`. `codex-oss start` now uses `.codex-oss/state/` by default. |
| Recursive codex exec | `rules.recursive_block` WARN | No blocking rule installed. Run `codex-oss install`. |
| Full-history fork error | `agreements.fork_turns` WARN | AGENTS.md doesn't specify `fork_turns: none`. Run `codex-oss install`. |

For advanced debugging, use the JSON output:

```bash
codex-oss doctor --json
```

## License

Apache 2.0 — see LICENSE file.
