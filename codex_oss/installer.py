#!/usr/bin/env python3
"""codex-oss installer — generate config, agents, rules, and AGENTS.md block."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from .doctor import run_doctor, find_project_root

PROVIDER_BLOCK = '''[model_providers.opencode_bridge]
name = "OpenCode Bridge"
base_url = "http://127.0.0.1:4000/v1"
env_key = "LITELLM_MASTER_KEY"
wire_api = "responses"
request_max_retries = 2
stream_max_retries = 2
stream_idle_timeout_ms = 300000
'''

AGENTS_MD_BLOCK = '''
<!-- codex-oss:start -->
## OSS delegation

Use OSS agents for bounded low/medium-risk work only.

When spawning OSS agents, always use fork_turns: "none":
- Full-history forks inherit GPT-5.5 model/reasoning, which conflicts with OSS agent overrides.
- OSS agents use different model providers and must not inherit the parent session.

### Handoff template

Spawn <oss_agent> with fork_turns: "none".

Task: <concrete sub-task>
Risk tier: <low / medium / high>
Allowed paths: <files or directories>
Forbidden: <paths or modules>
Verification: <what to check after completion>
Return: files inspected, files changed, confidence, caveats, escalation recommendation.

### Critical paths (never route to OSS)
- Authentication, authorization, session management
- Recovery paths, error recovery, state repair
- Schema authority, database migrations
- CI gates, build pipelines, deployment
- Cross-module invariants (>2 modules)
- Any path where failure = data loss or security breach

### Never
- Never use recursive codex exec from inside a Codex session.
- Never set model_provider = "opencode_bridge" as the parent session provider.
- Never delegate critical paths to OSS agents.
<!-- codex-oss:end -->
'''

RECURSIVE_RULE = '''# codex-oss: generated rule — do not edit manually
prefix_rule(
    pattern = ["codex", "exec"],
    decision = "forbidden",
    justification = "Recursive codex exec is blocked. Use native SpawnAgent or run codex exec from external terminal."
)

prefix_rule(
    pattern = ["npx", "codex", "exec"],
    decision = "forbidden",
    justification = "Nested Codex execution is blocked. Use native SpawnAgent or run from external terminal."
)
'''

AGENT_DEEPSEEK = '''name = "oss_deepseek_pro"
description = "OSS bounded implementation worker. USE ME WHEN: single file change, tests exist, clear scope. DO NOT USE FOR: auth, schema, recovery, cross-module changes, or critical paths. Must spawn with fork_turns: none."

model_provider = "opencode_bridge"
model = "ocg-deepseek-v4-pro"
model_reasoning_effort = "high"
sandbox_mode = "workspace-write"

developer_instructions = """
You are a bounded implementation worker under GPT-5.5 orchestration.

CAPABILITIES:
- Write and edit code in single files or tightly coupled modules (max 2 files)
- Debug with existing test coverage
- Write tests following existing patterns

BOUNDARIES — stop immediately if you touch these:
- auth, authorization, session management
- recovery paths, error recovery/rollback
- schema authority, database migrations
- CI configuration, build pipelines
- cross-module contracts (>2 files)

OUTPUT FORMAT:
1. Confidence: HIGH / MEDIUM / LOW
2. Files inspected
3. Files changed
4. Verification performed
5. Failure-mode caveats
6. Review recommendation (GPT-5.4 or GPT-5.5 needed)

RULES:
- Prefer one tool call per turn. Do not make parallel tool calls.
- Make the smallest defensible change.
- Use the available file/search/shell tools. Do not assume a specific tool prefix.
- If uncertain, escalate — do not guess.
"""
'''

AGENT_KIMI = '''name = "oss_kimi_rapid"
description = "OSS scout/review worker. USE ME WHEN: repo navigation, finding callers, mapping dependencies, code review, first-pass analysis. DO NOT USE FOR: critical-path implementation, auth/schema/recovery changes. Must spawn with fork_turns: none."

model_provider = "opencode_bridge"
model = "ocg-kimi-k2.6"
model_reasoning_effort = "medium"
sandbox_mode = "read-only"

developer_instructions = """
You are a fast repo-navigation worker under GPT-5.5 orchestration.

CAPABILITIES:
- Navigate and search the codebase
- Find callers, imports, references
- Map dependencies and trace code paths
- Review diffs and analyze patterns
- Estimate scope/blast-radius of changes

BOUNDARIES:
- Stay read-only unless explicitly assigned docs-only edits.
- Do not inspect or modify: auth, authorization, schema authority, persistence, recovery, migrations, CI gates, or cross-module contracts unless assigned by GPT-5.5.
- If critical paths appear, stop and escalate.

OUTPUT FORMAT:
1. Confidence: HIGH / MEDIUM / LOW
2. Files inspected + exact references
3. Key findings
4. Recommendation (next step)
5. Failure-mode caveats
6. Escalation recommendation

RULES:
- Prefer one tool call per turn. Do not make parallel tool calls.
- Use the available file/search/shell tools. Do not assume a specific tool prefix.
- When scouting for a downstream task, structure findings so the next worker can use them.
"""
'''

AGENT_FLASH = '''name = "oss_flash_support"
description = "OSS documentation and support worker. USE ME WHEN: docs, changelog, summaries, test inventory, formatting, mechanical low-risk tasks. DO NOT USE FOR: correctness-critical work, production logic, or any task where a mistake would cause bugs. Must spawn with fork_turns: none."

model_provider = "opencode_bridge"
model = "ocg-deepseek-v4-flash"
model_reasoning_effort = "medium"
sandbox_mode = "read-only"

developer_instructions = """
You are a low-cost documentation and support worker.

CAPABILITIES:
- Write and update docs, READMEs, comments
- Generate changelog entries
- Create test inventories
- Summarize changes and PRs
- Format code (indentation, imports — no logic changes)
- Mechanical search-and-replace (no logic changes)

BOUNDARIES:
- Do not edit logic. If your change could change runtime behavior, stop.
- Stay read-only unless explicitly assigned docs-only edits.
- Do not make correctness-critical decisions.
- Do not touch auth, recovery, schema, migrations, or CI configuration.
- If asked to implement a feature, escalate.

OUTPUT FORMAT:
1. Confidence: HIGH / MEDIUM / LOW
2. Files inspected
3. Files changed (if any)
4. Failure-mode caveats
5. Whether GPT-5.4 review is needed

RULES:
- Read-only by default.
- Keep output concise and evidence-backed.
- Use the available file/search/shell tools. Do not assume a specific tool prefix.
"""
'''

GITIGNORE_ENTRIES = '''
# codex-oss runtime — generated by codex-oss install
.codex-oss/state/
.codex-oss/logs/
.codex-oss/run/
.codex-oss/env/*.env
'''


def install(project_root: Optional[Path] = None, force: bool = False) -> int:
    root = project_root or find_project_root()
    print(f"Installing OSS bridge config into {root}")
    print()

    errors = 0

    # 1. Generate provider block in .codex/config.toml
    errors += _ensure_config(root, force)

    # 2. Generate agent TOMLs
    errors += _ensure_agents(root, force)

    # 3. Generate AGENTS.md block
    errors += _ensure_agreements(root, force)

    # 4. Generate recursive-codex rule
    errors += _ensure_rules(root, force)

    # 5. Create runtime dirs (.codex-oss/)
    errors += _ensure_runtime_dirs(root)

    # 6. Update .gitignore
    errors += _ensure_gitignore(root)

    # 7. Run doctor
    print()
    print("Running doctor checks...")
    print()
    report = run_doctor(root)
    report.print()

    if errors:
        print(f"\n{errors} file(s) had issues during install.")
        return 1

    print("\nInstall complete. Start the bridge with: codex-oss start")
    return 0


def _ensure_config(root: Path, force: bool) -> int:
    config_path = root / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    existing = config_path.read_text() if config_path.exists() else ""

    if PROVIDER_BLOCK.strip() in existing:
        print("  .codex/config.toml — provider block already present")
        return 0

    if existing and not force:
        # Append to existing config
        content = existing.rstrip() + "\n\n" + PROVIDER_BLOCK
        config_path.write_text(content)
        print("  .codex/config.toml — appended provider block")
    else:
        config_path.write_text(PROVIDER_BLOCK)
        print("  .codex/config.toml — created with provider block")

    return 0


def _ensure_agents(root: Path, force: bool) -> int:
    agents_dir = root / ".codex" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    agents = {
        "oss-deepseek-pro.toml": AGENT_DEEPSEEK,
        "oss-kimi-rapid.toml": AGENT_KIMI,
        "oss-flash-support.toml": AGENT_FLASH,
    }

    errors = 0
    for filename, content in agents.items():
        path = agents_dir / filename
        if path.exists() and not force:
            print(f"  .codex/agents/{filename} — already exists (use --force to overwrite)")
            continue
        path.write_text(content)
        print(f"  .codex/agents/{filename} — installed")

    return errors


def _ensure_agreements(root: Path, force: bool) -> int:
    agreements_path = root / "AGENTS.md"
    existing = agreements_path.read_text() if agreements_path.exists() else ""

    if "codex-oss:start" in existing:
        print("  AGENTS.md — OSS delegation block already present")
        return 0

    if existing:
        content = existing.rstrip() + "\n" + AGENTS_MD_BLOCK
    else:
        content = AGENTS_MD_BLOCK

    agreements_path.write_text(content)
    print("  AGENTS.md — added OSS delegation block")
    return 0


def _ensure_rules(root: Path, force: bool) -> int:
    rules_dir = root / ".codex" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)

    rule_path = rules_dir / "no-recursive-codex.rules"
    if rule_path.exists() and not force:
        print("  .codex/rules/no-recursive-codex.rules — already exists")
        return 0

    rule_path.write_text(RECURSIVE_RULE)
    print("  .codex/rules/no-recursive-codex.rules — installed")
    return 0


def _ensure_runtime_dirs(root: Path) -> int:
    for subdir in ["state", "logs", "run", "env"]:
        path = root / ".codex-oss" / subdir
        path.mkdir(parents=True, exist_ok=True)
    print("  .codex-oss/ — runtime directories created")
    return 0


def _ensure_gitignore(root: Path) -> int:
    gitignore = root / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""

    if ".codex-oss/state/" in existing:
        print("  .gitignore — codex-oss entries already present")
        return 0

    content = existing.rstrip() + GITIGNORE_ENTRIES
    gitignore.write_text(content)
    print("  .gitignore — added codex-oss runtime entries")
    return 0
