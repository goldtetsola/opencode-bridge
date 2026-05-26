#!/usr/bin/env python3
"""Native-polish smoke helper tests."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codex_oss.installer import _ensure_agents
from codex_oss.native_polish import _check_implementer_installed, run_native_polish_smoke


def assert_missing_implementer_fails_helpfully():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        report = run_native_polish_smoke(str(root))
        assert report["ok"] is False, report
        assert report["checks"][0]["name"] == "implementer_agent_installed", report
        assert report["checks"][0]["status"] == "FAIL", report
        assert "codex-oss install --force" in report["checks"][0]["fix"], report


def assert_installed_implementer_passes_registry_check():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _ensure_agents(root, force=False)
        check = _check_implementer_installed(root)
        assert check["status"] == "PASS", check
        assert "mission-a5-deepseek" in check["message"], check


def main() -> int:
    assert_missing_implementer_fails_helpfully()
    assert_installed_implementer_passes_registry_check()
    print("PASS: native polish smoke helper suite")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
