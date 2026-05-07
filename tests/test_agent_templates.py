#!/usr/bin/env python3
"""Public agent template hygiene tests."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from codex_oss.installer import _ensure_config

AGENT_FILES = [
    "agents/oss-deepseek-investigator.toml",
    "agents/oss-deepseek-pro.toml",
    "agents/oss-flash-context.toml",
    "agents/oss-flash-support.toml",
    "agents/oss-kimi-investigator.toml",
    "agents/oss-kimi-rapid.toml",
    "orchestration/agents/oss-deepseek-investigator.toml",
    "orchestration/agents/oss-deepseek-pro.toml",
    "orchestration/agents/oss-flash-context.toml",
    "orchestration/agents/oss-flash-support.toml",
    "orchestration/agents/oss-kimi-investigator.toml",
    "orchestration/agents/oss-kimi-rapid.toml",
]

RUNTIME_AGENT_FILES = [
    "agents/oss-deepseek-investigator.toml",
    "agents/oss-flash-context.toml",
    "agents/oss-kimi-investigator.toml",
    "orchestration/agents/oss-deepseek-investigator.toml",
    "orchestration/agents/oss-flash-context.toml",
    "orchestration/agents/oss-kimi-investigator.toml",
]

RAW_AGENT_FILES = [path for path in AGENT_FILES if path not in RUNTIME_AGENT_FILES]


def read(rel_path: str) -> str:
    with open(os.path.join(ROOT, rel_path), "r", encoding="utf-8") as handle:
        return handle.read()


def assert_agent_command_discipline():
    required = [
        "Read repo instructions before running commands.",
        "Prefer portable commands",
        "rg --files",
        "Do not use GNU-only/macOS-incompatible flags",
        "Do not assume private helper tools are installed.",
        "If a command is blocked or unsupported, retry once",
        "Do not paste full file contents or raw tool output",
        "The requested output format is mandatory.",
    ]
    forbidden = [
        "This environment uses the `rtk` tool wrapper",
        "Always prefix with `rtk `",
        "Raw commands will be blocked.",
    ]
    for rel_path in RAW_AGENT_FILES:
        text = read(rel_path)
        for needle in required:
            assert needle in text, f"{rel_path} missing {needle!r}"
        for needle in forbidden:
            assert needle not in text, f"{rel_path} contains public-repo-hostile text {needle!r}"


def assert_runtime_agents_are_mission_controlled():
    for rel_path in RUNTIME_AGENT_FILES:
        text = read(rel_path)
        assert 'model_provider = "opencode_bridge"' in text, rel_path
        assert "mission-a" in text, rel_path
        assert "MissionV1" in text, rel_path
        assert "<OSS_HANDOFF_JSON>" not in text, f"{rel_path} must not shadow mission entrypoint tags"
        assert "</OSS_HANDOFF_JSON>" not in text, f"{rel_path} must not shadow mission entrypoint tags"
        assert "Do not use shell commands directly." in text, rel_path
        assert "The runtime owns tools, evidence, validation, and report structure." in text, rel_path


def assert_installer_templates_match_policy():
    text = read("codex_oss/installer.py")
    assert "COMMAND_DISCIPLINE" in text
    assert "Do not assume private helper tools are installed." in text
    assert "ls --tree" in text
    assert "Always prefix with `rtk `" not in text
    assert "opencode_bridge" in text
    assert "mission-a3-kimi" in text
    assert "You must receive exactly one <OSS_HANDOFF_JSON>" not in text


def assert_installer_appends_only_missing_runtime_provider():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = root / ".codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(
            '[model_providers.opencode_bridge]\n'
            'name = "OpenCode Bridge"\n'
            'base_url = "http://127.0.0.1:4000/v1"\n'
        )
        assert _ensure_config(root, force=False) == 0
        text = config.read_text()
        assert text.count("[model_providers.opencode_bridge]") == 1, text
        assert text.count("[model_providers.oss_runtime]") == 1, text


def main() -> int:
    assert_agent_command_discipline()
    assert_runtime_agents_are_mission_controlled()
    assert_installer_templates_match_policy()
    assert_installer_appends_only_missing_runtime_provider()
    print("PASS: agent template hygiene suite")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
