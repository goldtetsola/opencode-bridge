#!/usr/bin/env python3
"""codex-oss CLI — one-command setup for OpenCode Bridge."""

from __future__ import annotations

import argparse
import sys

from .doctor import run_doctor
from .installer import install


def main():
    parser = argparse.ArgumentParser(
        prog="codex-oss",
        description="One-command setup and diagnostics for OpenCode Bridge OSS subagents.",
    )
    sub = parser.add_subparsers(dest="command", help="Commands")

    # doctor
    d = sub.add_parser("doctor", help="Check setup for correctness")
    d.add_argument("--fix", action="store_true", help="Auto-repair issues")
    d.add_argument("--json", action="store_true", help="Machine-readable output")
    d.add_argument("--project", type=str, help="Project root path", default=None)

    # install
    i = sub.add_parser("install", help="Install OSS bridge config into current project")
    i.add_argument("--force", action="store_true", help="Overwrite existing files")
    i.add_argument("--project", type=str, help="Project root path", default=None)

    # start
    s = sub.add_parser("start", help="Start the bridge proxy")
    s.add_argument("--port", type=int, default=4000, help="Port (default: 4000)")
    s.add_argument("--mode", choices=["production", "compat-test", "openai-passthrough"],
                   default="production", help="Bridge mode")

    # stop
    sub.add_parser("stop", help="Stop the bridge proxy")

    # status
    sub.add_parser("status", help="Show bridge health")

    args = parser.parse_args()

    if args.command == "doctor":
        from pathlib import Path
        root = Path(args.project) if args.project else None
        report = run_doctor(root, fix=args.fix)
        report.print(json_output=args.json)
        sys.exit(0 if report.healthy else 1)

    elif args.command == "install":
        from pathlib import Path
        root = Path(args.project) if args.project else None
        sys.exit(install(root, force=args.force))

    elif args.command == "start":
        _start_bridge(args.port, args.mode)

    elif args.command == "stop":
        _stop_bridge(args.port if hasattr(args, 'port') else 4000)

    elif args.command == "status":
        _bridge_status()

    else:
        parser.print_help()
        sys.exit(1)


def _start_bridge(port: int, mode: str):
    import os
    import subprocess
    import time

    # Check if already running
    import urllib.request
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
        key = os.getenv("LITELLM_MASTER_KEY", "sk-local-codex-bridge")
        req.add_header("Authorization", f"Bearer {key}")
        urllib.request.urlopen(req, timeout=2)
        print(f"Bridge already running on port {port}")
        return
    except Exception:
        pass

    # Find bridge.py relative to this package
    package_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(package_dir)
    bridge_path = os.path.join(repo_root, "bridge.py")

    if not os.path.exists(bridge_path):
        print(f"ERROR: bridge.py not found at {bridge_path}")
        sys.exit(1)

    # Build env
    env = os.environ.copy()
    env["PROXY_PORT"] = str(port)

    gpt_strategies = {
        "production": "error",
        "compat-test": "oss",
        "openai-passthrough": "openai",
    }
    env["GPT_MODEL_STRATEGY"] = gpt_strategies[mode]

    # Validate key is present
    if not env.get("OPENCODE_GO_API_KEY"):
        # Try env file first
        env_file = os.path.join(repo_root, ".codex-oss", "env", "opencode-go.env")
        if os.path.exists(env_file):
            with open(env_file) as f:
                for line in f:
                    if line.startswith("OPENCODE_GO_API_KEY="):
                        env["OPENCODE_GO_API_KEY"] = line.strip().split("=", 1)[1]
                        break

    if not env.get("OPENCODE_GO_API_KEY"):
        print("ERROR: OPENCODE_GO_API_KEY is not set.")
        print("  Set it in the environment or create .codex-oss/env/opencode-go.env")
        sys.exit(1)

    # Launch bridge in background
    log_dir = os.path.join(repo_root, ".codex-oss", "logs")
    os.makedirs(log_dir, exist_ok=True)
    out_log = open(os.path.join(log_dir, "bridge.log"), "a")
    err_log = open(os.path.join(log_dir, "bridge.err.log"), "a")

    proc = subprocess.Popen(
        [sys.executable, bridge_path],
        env=env, stdout=out_log, stderr=err_log,
        start_new_session=True
    )

    pid_file = os.path.join(repo_root, ".codex-oss", "run", "bridge.pid")
    os.makedirs(os.path.dirname(pid_file), exist_ok=True)
    with open(pid_file, "w") as f:
        f.write(str(proc.pid))

    time.sleep(1)
    print(f"Bridge started on port {port} (mode: {mode}, PID: {proc.pid})")
    print(f"Logs: .codex-oss/logs/bridge.log")


def _stop_bridge(port: int = 4000):
    import os
    import signal

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pid_file = os.path.join(repo_root, ".codex-oss", "run", "bridge.pid")

    if os.path.exists(pid_file):
        with open(pid_file) as f:
            pid = int(f.read().strip())
        try:
            os.kill(pid, signal.SIGTERM)
            os.remove(pid_file)
            print(f"Bridge stopped (PID: {pid})")
        except ProcessLookupError:
            os.remove(pid_file)
            print("Bridge was not running (stale PID file removed)")
    else:
        # Fallback: kill by port
        import subprocess
        result = subprocess.run(["lsof", "-i", f":{port}", "-t"],
                                capture_output=True, text=True)
        if result.stdout.strip():
            for pid_str in result.stdout.strip().split("\n"):
                try:
                    os.kill(int(pid_str), signal.SIGTERM)
                    print(f"Bridge stopped (PID: {pid_str})")
                except ProcessLookupError:
                    pass
        else:
            print(f"No bridge found on port {port}")


def _bridge_status():
    import os
    import urllib.request
    import json

    port = int(os.getenv("PROXY_PORT", "4000"))
    key = os.getenv("LITELLM_MASTER_KEY", "sk-local-codex-bridge")

    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
        req.add_header("Authorization", f"Bearer {key}")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            print(json.dumps(data, indent=2))
    except Exception as e:
        print(f"Bridge not running: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
