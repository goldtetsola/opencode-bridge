#!/usr/bin/env python3
"""codex-oss installer — generate config, agents, rules, and AGENTS.md block."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from .doctor import run_doctor, find_project_root

OPENCODE_PROVIDER_BLOCK = '''[model_providers.opencode_bridge]
name = "OpenCode Bridge"
base_url = "http://127.0.0.1:4000/v1"
wire_api = "responses"
request_max_retries = 2
stream_max_retries = 2
stream_idle_timeout_ms = 300000

[model_providers.opencode_bridge.auth]
command = "echo"
args = ["sk-local-codex-bridge"]
timeout_ms = 1000
'''

OSS_RUNTIME_PROVIDER_BLOCK = '''[model_providers.oss_runtime]
name = "OSS Agent Runtime"
base_url = "http://127.0.0.1:4000/v1"
wire_api = "responses"
request_max_retries = 1
stream_max_retries = 1
stream_idle_timeout_ms = 1800000

[model_providers.oss_runtime.auth]
command = "echo"
args = ["sk-local-codex-bridge"]
timeout_ms = 1000
'''

PROVIDER_BLOCK = OPENCODE_PROVIDER_BLOCK.rstrip() + "\n\n" + OSS_RUNTIME_PROVIDER_BLOCK

AGENTS_MD_BLOCK = '''
<!-- codex-oss:start -->
## OSS delegation

Use OSS agents for bounded low/medium-risk work only. Use runtime-controlled OSS agents for normal A2/A3 read-only investigation; raw OSS agents are experimental baselines.

When spawning OSS agents, always use fork_turns: "none":
- Full-history forks inherit GPT-5.5 model/reasoning, which conflicts with OSS agent overrides.
- OSS agents use different model providers and must not inherit the parent session.

### Handoff template

Spawn <oss_agent> with fork_turns: "none".

Include a machine-readable handoff block before prose. The bridge validates this before trusting task routing:

OSS_HANDOFF_JSON:
{"schema_version":1,"role":"<worker role>","goal":"<concrete sub-task>","task_type":"scout|review|docs_support|bounded_write|implementation","owned_paths":[],"read_only_paths":["<files or dirs>"],"forbidden_actions":["<paths or operations>"],"verification_steps":["<checks>"],"deliverable_fields":["files inspected","confidence","caveats","escalation recommendation"],"completion_rule":"stop after the requested deliverable","escalation_rule":"stop if scope or critical-path risk appears"}

After the JSON block, add any human-readable context needed for the worker. For reusable or high-stakes handoffs, validate the draft first with `codex-oss validate-handoff /path/to/handoff.md`.

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

### Portable command discipline
- Read the repo instructions before running commands.
- Prefer portable search/list commands: `rg`, `rg --files`, and POSIX-compatible `ls`.
- Do not use GNU-only/macOS-incompatible flags such as `ls --tree`.
- Do not assume private helper tools are installed.
- If a command is blocked or unsupported, retry once with the suggested replacement and mention the blocked command in the report.
- Do not paste full file contents or raw tool output into the final answer; summarize and cite paths/lines.
- The requested output format is mandatory. If you cannot satisfy it, return LOW confidence with caveats instead of dumping evidence.
- For A2/A3 read-only investigation, prefer MissionV1 through the bridge runtime over broad shell access.
<!-- codex-oss:end -->
'''

COMMAND_DISCIPLINE = '''COMMAND DISCIPLINE:
- Read repo instructions before running commands.
- Prefer portable commands: `rg`, `rg --files`, and POSIX-compatible `ls`.
- Do not use GNU-only/macOS-incompatible flags such as `ls --tree`.
- Do not assume private helper tools are installed.
- If a command is blocked or unsupported, retry once with the suggested replacement before giving up.
- Do not paste full file contents or raw tool output into the final answer; summarize and cite paths/lines.
- The requested output format is mandatory. If you cannot satisfy it, return LOW confidence with caveats instead of dumping evidence.
- For A2/A3 read-only investigation, prefer MissionV1/managed-runtime handoffs when available.
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
''' + COMMAND_DISCIPLINE + '''"""
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
''' + COMMAND_DISCIPLINE + '''"""
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
''' + COMMAND_DISCIPLINE + '''"""
'''

AGENT_RUNTIME_KIMI = '''name = "oss_kimi_investigator"
description = "Runtime-controlled OSS investigator. USE ME WHEN: A2/A3 read-only repo exploration, implementation-site discovery, dependency mapping, evidence-backed reports. DO NOT USE FOR: writes, critical-path final decisions, or prose-only handoffs. Requires MissionV1."

model_provider = "opencode_bridge"
model = "mission-a3-kimi"
model_reasoning_effort = "medium"
sandbox_mode = "read-only"

developer_instructions = """
You are a runtime-controlled OSS investigator.

You must receive exactly one MissionV1 handoff block labeled OSS_HANDOFF_JSON with schema_version "oss_agent_mission.v1".
If MissionV1 is missing or invalid, return INVALID_MISSION and ask the parent to provide one.

Do not use shell commands directly.
Do not return raw file contents.
The runtime owns tools, evidence, validation, and report structure.
Treat your result as evidence for GPT-5.5, not final authority.
"""
'''

AGENT_RUNTIME_DEEPSEEK = '''name = "oss_deepseek_investigator"
description = "Runtime-controlled DeepSeek investigator. USE ME WHEN: reasoning-heavy A3 read-only investigation with explicit MissionV1, uncertainty tracking, and evidence-backed reports. DO NOT USE FOR: writes or critical-path final authority."

model_provider = "opencode_bridge"
model = "mission-a3-deepseek"
model_reasoning_effort = "high"
sandbox_mode = "read-only"

developer_instructions = """
You are a runtime-controlled OSS investigation reasoning engine.

You must receive exactly one MissionV1 handoff block labeled OSS_HANDOFF_JSON with schema_version "oss_agent_mission.v1".
If MissionV1 is missing or invalid, return INVALID_MISSION and ask the parent to provide one.

Do not use shell commands directly.
Do not return raw file contents.
The runtime owns tools, evidence, validation, and report structure.
Treat your result as evidence for GPT-5.5, not final authority.
"""
'''

AGENT_RUNTIME_FLASH = '''name = "oss_flash_context"
description = "Runtime-controlled cheap OSS context/report worker. USE ME WHEN: low-risk A2/A3 read-only context gathering, docs inventories, and evidence-backed summaries with explicit MissionV1. DO NOT USE FOR: correctness-critical work or writes."

model_provider = "opencode_bridge"
model = "mission-a2-flash"
model_reasoning_effort = "medium"
sandbox_mode = "read-only"

developer_instructions = """
You are a runtime-controlled OSS context worker.

You must receive exactly one MissionV1 handoff block labeled OSS_HANDOFF_JSON with schema_version "oss_agent_mission.v1".
If MissionV1 is missing or invalid, return INVALID_MISSION and ask the parent to provide one.

Do not use shell commands directly.
Do not return raw file contents.
The runtime owns tools, evidence, validation, and report structure.
Treat your result as evidence for GPT-5.5, not final authority.
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
    report = run_doctor(root, offline=True)
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

    provider_blocks = []
    if "[model_providers.opencode_bridge]" not in existing or force:
        provider_blocks.append(OPENCODE_PROVIDER_BLOCK.strip())
    if "[model_providers.oss_runtime]" not in existing or force:
        provider_blocks.append(OSS_RUNTIME_PROVIDER_BLOCK.strip())

    if not provider_blocks:
        print("  .codex/config.toml — provider blocks already present")
        return 0

    if existing and not force:
        content = existing.rstrip() + "\n\n" + "\n\n".join(provider_blocks) + "\n"
        config_path.write_text(content)
        print("  .codex/config.toml — appended missing provider block(s)")
    else:
        config_path.write_text("\n\n".join(provider_blocks) + "\n")
        print("  .codex/config.toml — created with provider block")

    return 0


def _ensure_agents(root: Path, force: bool) -> int:
    agents_dir = root / ".codex" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    agents = {
        "oss-kimi-investigator.toml": AGENT_RUNTIME_KIMI,
        "oss-deepseek-investigator.toml": AGENT_RUNTIME_DEEPSEEK,
        "oss-flash-context.toml": AGENT_RUNTIME_FLASH,
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
