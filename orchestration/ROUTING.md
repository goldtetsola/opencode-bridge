# OSS Model Routing — Reference & Troubleshooting

## Architecture

```
Parent session:  GPT-5.5 (native openai provider)  ← orchestrator
                                                      reads AGENTS.md, spawns subagents
OSS subagents:   opencode_bridge provider            ← only used in agent TOMLs
                   → bridge.py (v6)
                   → OpenCode Go
                   → DeepSeek V4 Pro / Kimi K2.6 / Flash
```

**Bridge is a subagent-only provider.** Do NOT set `model_provider = "opencode_bridge"` as your session-wide provider. Codex will route GPT-5.5 orchestrator requests through the bridge, which cannot serve GPT models. The bridge v6 detects and rejects GPT requests immediately (`GPT_MODEL_STRATEGY=error`).

## How routing works

Codex's orchestrator (GPT-5.5) matches tasks to agents by reading `.codex/agents/*.toml` and applying the rules in `AGENTS.md`. OSS agents use `fork_turns: "none"` to avoid inheriting the parent GPT-5.5 model, which conflicts with custom provider overrides.

```
You: "Add a test for parseHeader() in src/parser.ts"
         │
         ▼
   GPT-5.5 reads AGENTS.md routing rules
         │
         │  Safety check: auth/recovery/schema? → No
         │  Scope: single file, tests exist → Bounded implementation
         │
         ▼
  GPT-5.5 spawns oss_deepseek_implementer with fork_turns: "none"
        │  Handoff: MissionV1 A4/A5 task, risk tier, allowed/forbidden paths, verification policy
        ▼
  Runtime owns patch/apply/verify/status; DeepSeek provides patch intent and rationale
         │
         ▼
   GPT-5.5 validates result. Accepts. Done.
```

## Decision flowchart

```
Task received by GPT-5.5
    │
    ├─ Safety check: touches auth/recovery/schema/CI/migrations/cross-module?
    │       → YES: Handle with GPT-5.5/5.4. STOP.
    │       → NO: continue
    │
    ├─ Scoping: what kind of work?
    │
    ├─ Exploration? (find X, map Y, search Z, trace imports)
    │       → oss_kimi_investigator (runtime-controlled read-only, fork_turns: "none")
    │
    ├─ Docs/mechanical? (changelog, summaries, formatting, test inventory)
    │       → oss_flash_context (runtime-controlled read-only, fork_turns: "none", cheapest)
    │
    ├─ Bounded implementation? (single file/module, tests exist, clear spec)
    │       → oss_deepseek_implementer (MissionV1 A4/A5, fork_turns: "none")
    │       → Accept only if runtime artifacts show patch/apply/verify/status and GPT review agrees
    │
    └─ Ambiguous? (unclear scope, unknown blast radius)
            → oss_kimi_investigator first (runtime-controlled scout, fork_turns: "none")
            → Based on scout: DeepSeek or GPT-5.4
```

## Agent capability matrix

| Agent | Model | Reasoning | Write? | Best for | Do NOT use for |
|---|---|---|---|---|---|
| `oss_kimi_investigator` | mission-a3-kimi (runtime) | medium | read-only | Repo nav, review, scouting, first-pass analysis | Auth, cross-module, critical paths |
| `oss_deepseek_investigator` | mission-a3-deepseek (runtime) | high | read-only | Deeper investigation, multi-file analysis | Auth, critical paths |
| `oss_flash_context` | mission-a2-flash (runtime) | medium | read-only | Docs, changelog, summaries, formatting | Correctness-critical work |
| `oss_deepseek_implementer` | mission-a5-deepseek (runtime) | high | runtime-controlled | Bounded implementation via MissionV1 A4/A5; runtime owns patch/apply/verify/status | Critical paths without certification |
| `oss_deepseek_pro` | deepseek-v4-pro (raw) | high | workspace-write | Bounded low-risk implementation, debugging, and patch drafting | Cross-module (>2 files), critical paths |
| `oss_kimi_rapid` | kimi-k2.6 (raw) | medium | read-only | Manual repo nav baseline | Auth, critical paths |
| `oss_flash_support` | deepseek-v4-flash (raw) | medium | read-only | Manual docs/support baseline | Correctness-critical work |

**Runtime-controlled agents** use MissionV1 contracts with governed tool access, evidence tracking, patch validation, and audit artifacts. This is the recommended path for all OSS work, including implementation.

**Raw experimental agents** use prompt-only discipline without the full runtime control plane. `oss_deepseek_pro` may do bounded low-risk edits inside explicit owned paths; other raw agents remain read-only support/scout lanes.

All agents require `model_provider = "opencode_bridge"` and `fork_turns: "none"`.

## Fork mode

### Why `fork_turns: "none"` is required

Codex's subagent spawn defaults to full-history fork mode. Full-history fork means the child agent inherits the parent's:
- Agent type (GPT-5.5)
- Model
- Reasoning effort

OSS agents MUST override all three (different provider, different model, different reasoning). Codex currently rejects combining full-history fork with these overrides ([issue #20077](https://github.com/openai/codex/issues/20077)).

The fix: spawn OSS agents with `fork_turns: "none"`.

### What you give up

With `fork_turns: "none"`, the subagent does NOT automatically inherit:
- Earlier conversation turns
- Prior decisions and constraints
- Logs/output from the parent session

This is actually desirable for OSS workers — it prevents context pollution and keeps handoffs clean.

### When full-history fork is OK

Only for same-model/same-role continuation agents where no model, provider, agent type, or reasoning override is required. Example: spawning another GPT-5.5 reviewer with the same context.

## Tool compatibility

OSS models running through Codex get Codex's tool environment (bash, read, write, grep) but do NOT automatically know:
- RTK tool prefixes (`rtk read` vs raw `cat`)
- Codex-specific policy hooks
- Sandbox restrictions

This can cause tool call failures when OSS models use raw commands that Codex's sandbox blocks. This is a known limitation — the OSS model doesn't receive the same system prompt as GPT models.

## Customizing for your project

### Safety check list
Edit the safety check in `AGENTS.md` to match your project's critical surfaces.

```yaml
critical_paths:
  - src/auth/
  - src/database/
  - src/config/security
  - .github/workflows/
  - packages/shared-contracts/
```

To find your critical paths, ask: "What code, if changed incorrectly, would cause data loss, a security breach, or a production outage?"

### Relaxing rules
If your project has simple or no auth/database/CI, remove those from the safety list.

### Adding new agents
Create a new `.toml` in `.codex/agents/` following the existing format. Ensure:
- `model_provider = "opencode_bridge"`
- `model = "mission-a<tier>-<model>"` for runtime agents, especially MissionV1 A4/A5 implementation agents
- `model_reasoning_effort = "high" | "medium" | "low"`
- Spawn with `fork_turns: "none"`

## Handoff pattern examples

### Example 1: Exploration → Implementation

```
Spawn oss_kimi_rapid with fork_turns: "none".

OSS_HANDOFF_JSON:
{"schema_version":1,"role":"Read-only repo scout","goal":"Find all callers of validateUser() and describe the error-handling pattern","task_type":"scout","owned_paths":[],"read_only_paths":["src","tests"],"forbidden_actions":["edit files","inspect secrets","touch auth/session internals"],"verification_steps":["Search for validateUser references"],"deliverable_fields":["file paths","line numbers","pattern description","confidence","caveats"],"completion_rule":"stop after the read-only report","escalation_rule":"stop if critical auth/session logic is required"}

Human context:
Risk tier: low. Keep the result ready for a bounded implementation handoff.
```

→ Scout result. If bounded:
```
Spawn oss_deepseek_implementer with fork_turns: "none".

<OSS_HANDOFF_JSON>
{ "...": "MissionV1 A4/A5 produced by codex-oss mission compile" }
</OSS_HANDOFF_JSON>

Human context:
Risk tier: medium because this touches an auth-adjacent file; runtime owns patch construction, apply, verification, rollback, and final status. The model owns narrative, patch intent, and rationale only. GPT review remains required before acceptance.
```

### Example 2: Ambiguous → Scout → Escalate

```
Spawn oss_kimi_rapid with fork_turns: "none".

OSS_HANDOFF_JSON:
{"schema_version":1,"role":"Read-only architecture scout","goal":"Map all files involved in the payment flow and identify test coverage for each","task_type":"scout","owned_paths":[],"read_only_paths":["src","tests"],"forbidden_actions":["edit files","run live payment actions","print secrets"],"verification_steps":["Search for payment-related entry points","Search for matching tests"],"deliverable_fields":["file list","test coverage per file","recommendation","confidence","caveats"],"completion_rule":"stop after the read-only map","escalation_rule":"stop if payment authority changes are required"}

Human context:
Risk tier: medium. This is exploration only; GPT-5.5 decides any follow-up.
```

→ Scout returns: 7 files across 3 modules, no shared test suite. Recommendation: escalate.
→ GPT-5.5: escalates to GPT-5.4 (cross-module + billing critical path).

## Troubleshooting

### "Full-history forked agents inherit..." error
→ The orchestrator used full-history fork for an OSS agent. Must use `fork_turns: "none"`.

### "My task went to the wrong agent"
→ The agent `description` field might be too vague. Include both "use me when" AND "do NOT use me when".

### "The OSS agent used the wrong tool/command"
→ Tool compatibility issue. OSS models don't receive Codex's RTK system prompt. Document known tool mappings or test each agent's tool preferences.

### "DeepSeek/Kimi timed out on a complex task"
→ The bridge has SSE heartbeat support. If timeouts persist, increase `UPSTREAM_TIMEOUT_SECONDS` and `SSE_UPSTREAM_HEARTBEAT_SECONDS`.

### "401 Unauthorized on subagent spawn"
→ Codex session auth issue. Try: `codex logout && codex login`. Test from persistent Codex Desktop rather than `codex exec`. Consider `cli_auth_credentials_store = "file"` in `~/.codex/config.toml`.

### "Operation not permitted" on subagent spawn
→ Recursive `codex exec` is blocked by Codex sandbox. Use native `SpawnAgent` with `fork_turns: "none"`, or run external `codex exec` from a separate terminal.
