#!/usr/bin/env python3
"""Live A4/A5 OSS model matrix.

Skipped by default. Run with LIVE_A4A5_MATRIX=1 and OPENCODE_GO_API_KEY set.
This wraps the focused frontier rungs in test_live_patch_burnin.py so the
Kimi/DeepSeek/Flash implementation lane can be re-tested with one command.
"""

from __future__ import annotations

import os
import subprocess
import sys


ROOT = os.path.dirname(os.path.dirname(__file__))


MATRIX = [
    ("kimi", "mission-a4-kimi", "mission-a5-kimi"),
    ("deepseek", "mission-a4-deepseek", "mission-a5-deepseek"),
    ("flash", "mission-a4-flash", "mission-a5-flash"),
]

FRONTIER_CASES = [
    ("multifile", "low-risk multi-file isolated A5"),
    ("test_only", "realistic unittest-only isolated A5"),
]

FULL_CASES = FRONTIER_CASES + [
    ("patch_recipe", "PatchRecipe anchor-slot A4"),
    ("replace_exact", "PatchIntent replace_exact isolated A5"),
    ("a6_critical", "critical-path isolated A6"),
]


def run_case(label: str, a4_model: str, a5_model: str, case_filter: str, port: int) -> tuple[bool, str]:
    env = {
        **os.environ,
        "LIVE_A4A5_BURNIN": "1",
        "LIVE_A4A5_CASE_FILTER": case_filter,
        "LIVE_A4A5_A4_MODEL": a4_model,
        "LIVE_A4A5_A5_MODEL": a5_model,
        "LIVE_A4A5_DEADLINE_SECONDS": os.getenv("LIVE_A4A5_DEADLINE_SECONDS", "240"),
        "LIVE_A4A5_UPSTREAM_TIMEOUT_SECONDS": os.getenv("LIVE_A4A5_UPSTREAM_TIMEOUT_SECONDS", "240"),
        "LIVE_A4A5_CLIENT_TIMEOUT": os.getenv("LIVE_A4A5_CLIENT_TIMEOUT", "300"),
        "LIVE_A4A5_BURNIN_PORT": str(port),
    }
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tests", "test_live_patch_burnin.py")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=int(os.getenv("LIVE_A4A5_MATRIX_CASE_TIMEOUT", "420")),
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, output


def main() -> None:
    if os.getenv("LIVE_A4A5_MATRIX") != "1":
        print("SKIP: set LIVE_A4A5_MATRIX=1 and OPENCODE_GO_API_KEY to run live A4/A5 model matrix")
        return
    if not os.getenv("OPENCODE_GO_API_KEY") and not os.getenv("UPSTREAM_API_KEY"):
        print("SKIP: OPENCODE_GO_API_KEY/UPSTREAM_API_KEY is not set")
        return

    failures: list[str] = []
    port = int(os.getenv("LIVE_A4A5_MATRIX_BASE_PORT", "4020"))
    cases = FULL_CASES if os.getenv("LIVE_A4A5_MATRIX_SCOPE", "frontier") == "full" else FRONTIER_CASES
    for model_label, a4_model, a5_model in MATRIX:
        for case_filter, case_label in cases:
            label = f"{model_label} {case_label}"
            ok, output = run_case(label, a4_model, a5_model, case_filter, port)
            port += 1
            if ok:
                print(f"  PASS: {label}")
            else:
                failures.append(f"{label}\n{output[-4000:]}")
                print(f"  FAIL: {label}")
                print(output[-4000:])

    if failures:
        print("FAIL: live A4/A5 model matrix")
        for failure in failures:
            print(f"\n---\n{failure}")
        raise SystemExit(1)
    print("PASS: live A4/A5 model matrix")


if __name__ == "__main__":
    main()
