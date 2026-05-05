# OSS Model Routing — Reference & Troubleshooting

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
   GPT-5.5 spawns oss_deepseek_pro with fork_turns: "none"
         │  Handoff: task, risk tier, allowed/forbidden paths, output format
         ▼
   DeepSeek writes test, returns confidence: HIGH
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
    │       → oss_kimi_rapid (read-only, fork_turns: "none")
    │
    ├─ Docs/mechanical? (changelog, summaries, formatting, test inventory)
    │       → oss_flash_support (read-only, fork_turns: "none", cheapest)
    │
    ├─ Bounded implementation? (single file/module, tests exist, clear spec)
    │       → oss_deepseek_pro (workspace-write, fork_turns: "none")
    │       → Check confidence:
    │           HIGH → accept
    │           MEDIUM → accept with GPT review flag
    │           LOW → escalate to GPT-5.4
    │
    └─ Ambiguous? (unclear scope, unknown blast radius)
            → oss_kimi_rapid first (scout, fork_turns: "none")
            → Based on scout: DeepSeek or GPT-5.4
```

## Agent capability matrix

| Agent | Model | Reasoning | Write? | Req/5hr | Best for | Do NOT use for |
|---|---|---|---|---|---|---|
| `oss_kimi_rapid` | kimi-k2.6 | medium | read-only | 1,100 | Repo nav, review, scouting, first-pass analysis | Auth, cross-module, critical paths, final production code |
| `oss_deepseek_pro` | deepseek-v4-pro | high | workspace-write | 3,400 | Bounded impl, debugging, feature work, analysis | Cross-module (>2 files), untested code, critical paths |
| `oss_flash_support` | deepseek-v4-flash | medium | read-only | 31,000 | Docs, changelog, summaries, test inventory, formatting | ANY correctness-critical work |

All three require:
- `model_provider = "opencode_bridge"` (routes through the bridge)
- `fork_turns: "none"` when spawned by the orchestrator
- Explicit handoff (task, scope, forbidden paths, output format)

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
- `model = "ocg-<model-id>"`
- `model_reasoning_effort = "high" | "medium" | "low"`
- Spawn with `fork_turns: "none"`

## Handoff pattern examples

### Example 1: Exploration → Implementation

```
Spawn oss_kimi_rapid with fork_turns: "none".

Task: Find all callers of validateUser() and describe the error-handling pattern.
Risk tier: low
Allowed scope: read-only repo navigation
Forbidden: no edits, no auth/recovery/schema files
Return: file paths, line numbers, pattern description, confidence, caveats.
```

→ Scout result. If bounded:
```
Spawn oss_deepseek_pro with fork_turns: "none".

Task: Add EmailValidationError handling to auth-router.ts following the existing pattern found by scout.
Risk tier: medium (touches auth path, bounded change)
Allowed files: src/auth-router.ts, src/auth/__tests__/auth-router.test.ts
Forbidden: src/auth/token.js, src/auth/session.js
Return: files changed, verification, confidence, GPT-5.4 review recommendation.
```

### Example 2: Ambiguous → Scout → Escalate

```
Spawn oss_kimi_rapid with fork_turns: "none".

Task: Map all files involved in the payment flow. Identify test coverage for each.
Risk tier: medium
Allowed scope: read-only
Forbidden: no edits
Return: file list with line counts, test coverage per file, recommendation.
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
→ Bridge v3+ has SSE heartbeat. If using v2, upgrade. If timeouts persist, increase `UPSTREAM_TIMEOUT_SECONDS` and `SSE_UPSTREAM_HEARTBEAT_SECONDS`.

### "401 Unauthorized on subagent spawn"
→ Codex session auth issue. Try: `codex logout && codex login`. Test from persistent Codex Desktop rather than `codex exec`. Consider `cli_auth_credentials_store = "file"` in `~/.codex/config.toml`.

### "Operation not permitted" on subagent spawn
→ Recursive `codex exec` is blocked by Codex sandbox. Use native `SpawnAgent` with `fork_turns: "none"`, or run external `codex exec` from a separate terminal.
