# OpenCode Bridge

Use [OpenCode Go](https://opencode.ai/docs/go/) OSS models (DeepSeek V4 Pro, Kimi K2.6, DeepSeek V4 Flash) as **native [Codex](https://developers.openai.com/codex) subagents** — with full tool-loop support, multi-turn conversation, and reasoning preservation.

Codex speaks the OpenAI Responses API. OpenCode Go exposes Chat Completions. This bridge sits in the middle, translating between them so Codex can spawn DeepSeek and Kimi workers the same way it spawns GPT workers.

## Quick start

### Zero-config: just tell Codex

Copy this entire prompt into Codex and it will clone, configure, and start everything automatically:

```
Clone https://github.com/goldtetsola/opencode-bridge into a `bridge/` directory at the root of this project.

Then:
1. Copy bridge/opencode-go.env.example to bridge/opencode-go.env.
2. Ask me for my OpenCode Go API key, write it into bridge/opencode-go.env as OPENCODE_GO_API_KEY=sk-...
3. Copy bridge/agents/*.toml into .codex/agents/
4. Merge bridge/config.toml.example into .codex/config.toml (keep existing settings, just add the new provider and profile blocks)
5. Start the proxy: `cd bridge && ./bin/start-proxy` (background it with `&`)
6. Verify: `curl http://127.0.0.1:4000/health -H "Authorization: Bearer sk-local-codex-bridge"`
7. Test with: `codex exec --sandbox read-only -c model_provider=opencode_bridge -m ocg-deepseek-v4-pro 'Say: hello'`
```

### Manual setup

### 1. Prerequisites

- [OpenCode Go](https://opencode.ai) with API key
- [Codex CLI](https://developers.openai.com/codex/cli) or Codex Desktop
- Python 3.13+ (stdlib only, no pip packages needed)
- (Optional) [Proton Pass](https://proton.me/pass) CLI for vault-based credential management

### 2. Set your OpenCode Go key

Copy the example env file and add your key:

```bash
cp opencode-go.env.example opencode-go.env
```

Edit `opencode-go.env`:

```env
OPENCODE_GO_API_KEY=sk-...
```

Three credential methods are supported (pick one):

1. **Plain key in the env file** (recommended) — just paste your key
2. **Proton Pass vault reference** — if you use `pass-cli`, use `pass://vault/item/OPENCODE_GO_API_KEY`
3. **Shell environment** — export `OPENCODE_GO_API_KEY` before starting the proxy

### 3. Start the proxy

```bash
bin/start-proxy
```

This launches a local HTTP server on port 4000. You'll see:

```
Responses->Chat proxy listening on http://127.0.0.1:4000/v1
```

Verify it with:

```bash
curl http://127.0.0.1:4000/health -H "Authorization: Bearer sk-local-codex-bridge"
```

### 4. Configure Codex

Copy the provider config into your project's `.codex/config.toml`:

```toml
[model_providers.opencode_bridge]
name = "OpenCode Go Responses Proxy"
base_url = "http://127.0.0.1:4000/v1"
env_key = "LITELLM_MASTER_KEY"
wire_api = "responses"
request_max_retries = 2
stream_max_retries = 2
stream_idle_timeout_ms = 300000
```

Or merge `config.toml.example` from this repo into your existing config.

Set the proxy key in your environment:

```bash
export LITELLM_MASTER_KEY="sk-local-codex-bridge"
```

### 5. Copy the agent TOMLs

Copy the agent TOML files into your project's `.codex/agents/` directory:

- `agents/oss-deepseek-pro.toml` — DeepSeek V4 Pro
- `agents/oss-kimi-rapid.toml` — Kimi K2.6
- `agents/oss-flash-support.toml` — DeepSeek V4 Flash

### 6. Test it

```bash
codex exec --sandbox read-only \
  -c model_provider=opencode_bridge \
  -m ocg-deepseek-v4-pro \
  'Say: hello'
```

## Architecture

```
Codex Desktop / CLI
    │
    │  Responses API (SSE streaming)
    ▼
bridge.py   ← this repo
    │
    │  Chat Completions API
    ▼
api.opencode.ai/zen/go/v1
    │
    ▼
DeepSeek V4 Pro / Kimi K2.6 / DeepSeek V4 Flash
```

The bridge handles:

- **Protocol translation**: Responses API ↔ Chat Completions (request format, tool definitions, output items)
- **SSE streaming**: Proper OpenAI Responses SSE event sequence (response.created, output_item.added, content_part.added, output_text.delta, output_item.done, response.completed)
- **Tool type filtering**: Strips hosted tools (image_generation, web_search, code_interpreter), MCP namespaces, and app/connector tools that OSS providers reject
- **Tool format conversion**: Responses flat format → Chat Completions nested `function` wrapper, with name sanitization for strict providers
- **Reasoning preservation**: DeepSeek V4 Pro requires `reasoning_content` to be replayed across multi-turn tool calls. The proxy stores and injects it correctly
- **Conversation state**: Tracks response history in SQLite so tool round-trips survive proxy restarts. Matches orphan `function_call_output` items to cached `function_call` items by `call_id`
- **Context preservation**: Repairs conversation history so earlier completed assistant→tool exchanges are preserved (not truncated), while incomplete tails are dropped
- **Retry + fallback**: Retries transient upstream errors with exponential backoff. Falls back to alternate models on capacity errors
- **Developer role mapping**: Maps Codex's `developer` role to `system` for providers that reject it (DeepSeek, Kimi)

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `OPENCODE_GO_API_KEY` | (required) | Your OpenCode Go API key |
| `PROXY_API_KEY` | `LITELLM_MASTER_KEY` value | Key Codex sends to authenticate with the proxy |
| `LITELLM_MASTER_KEY` | `sk-local-codex-bridge` | Auth key (shared name for Codex config compatibility) |
| `PROXY_PORT` | `4000` | Port the proxy listens on |
| `PROXY_STATE_DB` | `/tmp/opencode_responses_proxy_state.sqlite3` | SQLite file for conversation state |
| `FORCE_SINGLE_TOOL_INSTRUCTIONS` | `0` | Set to `1` to inject a guard discouraging parallel tool calls |
| `FALLBACK_MODEL_MAP_JSON` | deepseek→kimi/flash fallback | JSON map of model→fallback chain |
| `UPSTREAM_TIMEOUT_SECONDS` | `240` | Timeout for upstream API calls |
| `UPSTREAM_RETRIES` | `2` | Number of retries on transient errors |
| `MODEL_MAP_JSON` | (built-in) | Override model name mapping |
| `PROXY_LOG_PATH` | (stderr) | Path for structured JSON log output |
| `SSE_CHUNK_SIZE` | `256` | Characters per SSE text delta chunk |
| `EXPOSE_EMPTY_REASONING_ITEM` | `1` | Include empty reasoning item in output for Codex compatibility |
| `STRIP_TOOLS` | `0` | Set to `1` to strip ALL tools (force text-only responses) |

## Supported models

| Codex model ID | Upstream model | Best for |
|---|---|---|
| `ocg-deepseek-v4-pro` | deepseek-v4-pro | Bounded implementation, debugging, reasoning-heavy analysis |
| `ocg-kimi-k2.6` | kimi-k2.6 | Fast repo navigation, code-structure, review, drafts |
| `ocg-deepseek-v4-flash` | deepseek-v4-flash | Docs, summaries, mechanical low-risk tasks |
| `ocg-kimi-k2.5` | kimi-k2.5 | (untested) |
| `ocg-qwen3.6-plus` | qwen3.6-plus | (untested) |
| `ocg-glm-5.1` | glm-5.1 | (untested) |
| `ocg-minimax-m2.7` | minimax-m2.7 | (untested) |

Also accepts OpenCode-style `opencode-go/<model>` model IDs.

## Model-task matrix

What model to use for what kind of work, based on real usage:

| Model | Best for | Real example | Rate limit |
|---|---|---|---|
| DeepSeek V4 Flash | Docs, summaries, mechanical edits, test inventories | "Write a changelog entry for the last 3 commits" | 31K req/5hr — effectively unlimited |
| DeepSeek V4 Pro | Bounded implementation, debugging, feature work | "Add a test for canonicalizeGroundingValue following existing patterns" | 3.4K req/5hr |
| Kimi K2.6 | Repo exploration, code review, drafts, fast navigation | "Find every place that calls getJobLifecycle and summarize the call patterns" | 1.1K req/5hr |
| GPT-5.4 | Implementation where blast radius matters, cross-module changes | "Refactor the publish-bundle hydration to use the new artifact reader" | Usage-based |
| GPT-5.5 | Architecture, final review, critical paths | "Review this recovery path change for safety" | Usage-based |

## Orchestration pattern

Here's how the models fit together in a Codex session:

```
┌─────────────────────────────────────┐
│         GPT-5.5 (orchestrator)      │
│    Architecture, final review,       │
│    recovery/auth/schema decisions    │
└──────────┬──────────┬───────────────┘
           │          │
    ┌──────▼───┐  ┌──▼──────────────┐
    │ GPT-5.4  │  │ OSS via bridge  │
    │ Bounded  │  │ (OpenCode Go)   │
    │ impl │    │ │                 │
    │ review   │  │ ┌─────────────┐ │
    └──────────┘  │ │ DeepSeek    │ │
                  │ │ V4 Pro      │ │
                  │ │ impl, debug │ │
                  │ └─────────────┘ │
                  │ ┌─────────────┐ │
                  │ │ Kimi K2.6   │ │
                  │ │ explore,    │ │
                  │ │ review      │ │
                  │ └─────────────┘ │
                  │ ┌─────────────┐ │
                  │ │ DS V4 Flash │ │
                  │ │ docs,       │ │
                  │ │ summaries   │ │
                  │ └─────────────┘ │
                  └─────────────────┘

GPT-5.5: orchestrator + final review (GPT credits)
GPT-5.4: bounded implementation (GPT credits)
OSS models: everything else ($10/month flat)
```

Three pre-built agent files are provided in `agents/`: copy them into your project's `.codex/agents/`.

## Agent TOMLs

Three pre-built agent files are provided in `agents/`: copy them into your project's `.codex/agents/`.

| Agent TOML | Model | Reasoning | Sandbox | Use case |
|---|---|---|---|---|
| `oss-deepseek-pro.toml` | deepseek-v4-pro | high | workspace-write | Implementation, debugging, analysis |
| `oss-kimi-rapid.toml` | kimi-k2.6 | high | workspace-write | Fast navigation, review, drafts |
| `oss-flash-support.toml` | deepseek-v4-flash | medium | read-only | Docs, summaries, mechanical |

### Creating your own agent

You can create agents for any model OpenCode Go supports. The only requirements are:

1. **Pick a model ID**. Run `curl https://opencode.ai/zen/go/v1/models -H "Authorization: Bearer $OPENCODE_GO_API_KEY"` to see the full catalog. Use the model name with an `ocg-` prefix (e.g. `qwen3.6-plus` → `ocg-qwen3.6-plus`).

2. **Create a `.toml` file** in your project's `.codex/agents/`:

```toml
name = "oss_my_worker"
description = "Description of what this agent does."

model_provider = "opencode_bridge"   # always this — routes through the bridge
model = "ocg-<model-id>"               # e.g. ocg-qwen3.6-plus
model_reasoning_effort = "high"        # high / medium / low
sandbox_mode = "workspace-write"       # or "read-only"

developer_instructions = """
Your custom instructions here.
Rules, scope, output format, escalation criteria.
"""
```

3. **Set the right reasoning effort**:

| Effort | When to use | Example models |
|---|---|---|
| `high` | Implementation, debugging, analysis | deepseek-v4-pro, kimi-k2.6 |
| `medium` | Docs, summaries, mechanical tasks | deepseek-v4-flash |
| `low` | Trivial text generation | Any fast model |

4. **Choose the right sandbox mode**:

| Mode | Permissions | Best for |
|---|---|---|
| `workspace-write` | Can read and edit project files | Implementation, debugging, refactoring |
| `read-only` | Can read files and run safe commands | Exploration, review, docs, analysis |

5. **Write developer instructions that include**:
   - What the agent should and should not do
   - Escalation rules (when to defer to GPT)
   - Output format (confidence marker, files inspected, verification, caveats)

6. **Use it**: Codex will pick up any `.toml` file in `.codex/agents/`. Spawn it with `codex exec` or let the orchestrator route to it naturally.

### Tested models

| Agent | Model | Reasoning | Sandbox | Status |
|---|---|---|---|---|
| `oss-deepseek-pro.toml` | deepseek-v4-pro | high | workspace-write | Working |
| `oss-kimi-rapid.toml` | kimi-k2.6 | high | workspace-write | Working |
| `oss-flash-support.toml` | deepseek-v4-flash | medium | read-only | Working |
| `oss-qwen3.6-plus` (custom) | qwen3.6-plus | high | workspace-write | Untested |
| `oss-glm-5.1` (custom) | glm-5.1 | high | workspace-write | Untested |
| `oss-minimax-m2.7` (custom) | minimax-m2.7 | high | workspace-write | Untested |

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

## Model routing guidance

```
Lane A — GPT-5.5
  Orchestration, architecture, final acceptance, critical review

Lane B — GPT-5.4
  Trusted bounded implementation and review

Lane C — GPT-5.4-mini
  Cheap read-heavy exploration and support

Lane D — OSS native subagents (through this bridge)
  oss-deepseek-pro: bounded implementation, debugging, analysis
  oss-kimi-rapid: fast navigation, review, drafts
  oss-flash-support: docs, summaries, mechanical

Lane E — OSS external workers (fallback)
  Direct opencode run when proxy is unavailable
```

## Limitations

- **Not production-grade**: This is a local development tool. It uses a single-threaded Python HTTP server (though concurrent via ThreadingHTTPServer), in-memory state, and no authentication beyond a shared key.
- **Single machine only**: Bind to localhost. Do not expose publicly.
- **DeepSeek thinking mode costs tokens**: DeepSeek V4 Pro's reasoning_content is preserved internally but counts against your OpenCode Go usage. Expect ~300-400K tokens for multi-turn coding tasks.
- **No streaming from upstream**: The proxy requests `stream: false` from OpenCode Go and fakes SSE deltas after receiving the complete response. True token-by-token streaming from the upstream would reduce perceived latency.
- **Tool type restrictions**: Hosted tools (web search, code interpreter, image generation), MCP namespaces, and app/connector tools are stripped. Only function-style tools reach the model. Use the slim OSS profile to avoid sending them in the first place.

## Self-test

```bash
python3 bridge.py --self-test
```

Expected output:

```
self-test passed
```

## Troubleshooting

**"Error from provider: unknown variant `developer`"**
→ Update the proxy. The latest version maps `developer` → `system` for providers that reject it.

**"Messages with role 'tool' must be a response to a preceding message with 'tool_calls'"**
→ This is the bridge's conversation repair at work. It means the proxy received orphan function_call_output items it couldn't match to a stored conversation. Restart the proxy and retry from a fresh conversation.

**"We're currently experiencing high demand"**
→ OpenCode Go rate limiting. Reduce concurrency (use 1-2 OSS subagents at a time), or switch to a different model via the fallback map.

**Codex says "unknown provider for model"**
→ Verify the proxy is running (`curl http://127.0.0.1:4000/health`). Check that your `.codex/config.toml` has the `opencode_bridge` provider block. Ensure `LITELLM_MASTER_KEY` is set.

## License

Apache 2.0 — see LICENSE file.
