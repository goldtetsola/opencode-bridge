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

# 4. Start the bridge
OPENCODE_GO_API_KEY=sk-... python3 ~/bridge/bin/codex-oss start --mode production

# 5. Verify everything
python3 ~/bridge/bin/codex-oss doctor
# Expected: 22 passed, 0 warnings, 0 failed
```

That's it. `codex-oss install` generates all config — `.codex/config.toml`, agent TOMLs, `AGENTS.md` routing rules, and recursive-codex-exec blocking. `codex-oss doctor` checks 22 invariants and tells you exactly what to fix.

## What `codex-oss` does

| Command | What it does |
|---|---|
| `install` | Generates provider config, 3 agent TOMLs, AGENTS.md delegation contract, recursive-codex blocking rules, runtime directories, and gitignore entries. |
| `doctor` | Checks 22 invariants: config correctness, agent configuration, AGENTS.md compliance, recursive-codex exec blocking, bridge health, GPT leakage, OSS inference, state DB persistence. PASS/WARN/FAIL with fix instructions. Supports `--json` for automation. |
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
    │     bridge.py   ← this repo (v6)
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
| `UPSTREAM_STREAM` | `1` | Use stream=true for upstream Chat Completions (v5+ live streaming) |
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
| `CONTINUATION_TOOLS` | `none` | Tools for continuation turns (v8: `none` = no tools, force finalization) |
| `CONTINUATION_MODEL` | `kimi-k2.6` | Model for read-result finalizer |
| `CONTINUATION_DEADLINE_SECONDS` | `45` | Deadline for finalizer model calls |
| `WRITE_RESULT_MODE` | `deterministic` | Write results: `deterministic` = no model call |
| `MAX_TOOL_OUTPUT_CHARS` | `20000` | Compact tool outputs larger than this |
| `UPSTREAM_FIRST_BYTE_TIMEOUT_SECONDS` | `30` | Timeout for first byte from upstream |
| `UPSTREAM_IDLE_TIMEOUT_SECONDS` | `30` | Timeout for upstream idle during processing |
| `DEGRADED_COMPLETION_ON_TIMEOUT` | `1` | Return degraded report on timeout (v8: always returns terminal response) |
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

## Bridge v8+: OSS subagent runtime

OSS agents are no longer autonomous multi-turn agents — they're bounded transactions with deterministic finalization, managed autonomy, and context-pack mode for prep tasks.

- **Writes**: No model call needed after a successful write. The bridge generates the report directly.
- **Reads**: A no-tool finalizer call with a short deadline.
- **Managed autonomy** (v10): Scouts get per-task-class budgets (scout:6, prep_report:8). The bridge tracks turns, injects evidence ledgers, and lets the agent continue until budget exhausted or task complete.
- **Context-pack mode** (v11): When `READ-ONLY PATHS` are explicit (prep/report tasks with known sources), the bridge gathers all files + git commands internally and sends one no-tools synthesis call. No duplicate reads, no budget waste, one model turn.
- **Duplicate suppression** (v11): In managed autonomy, if the model re-requests an already-read file, the bridge returns `[ALREADY READ]` with remaining paths instead of re-executing.
- **Report validation**: Startup sentences and sub-50-char outputs are rejected. Missing required fields are logged.

Every path guarantees a terminal response (`response.completed` or `response.failed`) before closing the SSE stream.

### v8+ environment variables

| Variable | Default | Description |
|---|---|---|
| `OSS_NATIVE_MAX_TOOL_EXCHANGES` | `1` | Max tool calls per OSS subagent turn |
| `CONTINUATION_TOOLS` | `none` | Tools for continuation turns (`none` = no tools) |
| `CONTINUATION_MODEL` | `kimi-k2.6` | Model for read finalizer |
| `CONTINUATION_FALLBACK_MODELS` | `deepseek-v4-flash` | Fallback finalizer models |
| `CONTINUATION_DEADLINE_SECONDS` | `45` | Deadline for finalizer calls |
| `WRITE_RESULT_MODE` | `deterministic` | Write handling: `deterministic` = no model call |
| `MAX_TOOL_OUTPUT_CHARS` | `20000` | Compact outputs larger than this |
| `FORCE_SINGLE_TOOL_INSTRUCTIONS` | `1` | Enforce single-tool-per-turn (prevents parallel-call repair failures) |
| `DEGRADED_COMPLETION_ON_TIMEOUT` | `1` | Return degraded report on timeout |

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

Three pre-built agent files are provided in `orchestration/agents/` (recommended) and `agents/` (minimal):

| Agent TOML | Model | Reasoning | Sandbox | Use case |
|---|---|---|---|---|
| `oss-deepseek-pro.toml` | deepseek-v4-pro | high | workspace-write | Bounded impl, debugging, analysis |
| `oss-kimi-rapid.toml` | kimi-k2.6 | medium | read-only | Repo navigation, scouting, review |
| `oss-flash-support.toml` | deepseek-v4-flash | medium | read-only | Docs, summaries, changelog, mechanical |

### Creating your own agent

You can create agents for any model OpenCode Go supports:

1. **Pick a model ID**. Run `curl https://opencode.ai/zen/go/v1/models -H "Authorization: Bearer $OPENCODE_GO_API_KEY"` to see the full catalog. Use the model name with an `ocg-` prefix (e.g. `qwen3.6-plus` → `ocg-qwen3.6-plus`).

2. **Create a `.toml` file** in your project's `.codex/agents/`:

```toml
name = "oss_my_worker"
description = "What this agent does. USE ME WHEN: <criteria>. DO NOT USE FOR: <boundaries>."

model_provider = "opencode_bridge"       # always this
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

**First step for any issue:** run `codex-oss doctor`. It checks 22 invariants and tells you exactly what to fix.

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
