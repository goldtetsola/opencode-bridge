#!/usr/bin/env python3
"""Smoke-test repo-local Codex OSS install artifacts in a temp project."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile


ROOT = os.path.dirname(os.path.dirname(__file__))


def main() -> None:
    project = tempfile.mkdtemp(prefix="oss_codex_install_")
    try:
        proc = subprocess.run(
            [os.path.join(ROOT, "bin", "codex-oss"), "install", "--project", project],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        expected = [
            ".codex/config.toml",
            ".codex/agents/oss-kimi-investigator.toml",
            ".codex/agents/oss-deepseek-investigator.toml",
            ".codex/agents/oss-flash-context.toml",
            "AGENTS.md",
        ]
        missing = [path for path in expected if not os.path.exists(os.path.join(project, path))]
        assert not missing, missing
        print("PASS: codex-oss install smoke")
    finally:
        shutil.rmtree(project)


if __name__ == "__main__":
    main()
