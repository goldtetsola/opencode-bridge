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

    # up — foreground supervisor
    up = sub.add_parser("up", help="Start bridge with foreground supervisor (keep terminal open)")
    up.add_argument("--port", type=int, default=4000)

    # run — start bridge, run command, cleanup
    run = sub.add_parser("run", help="Start bridge, run command, stop bridge")
    run.add_argument("--port", type=int, default=4000)
    run.add_argument("cmd", nargs=argparse.REMAINDER, help="Command to run while bridge is alive")

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

    elif args.command == "up":
        sys.exit(_supervise(args.port))

    elif args.command == "run":
        sys.exit(_run_with_bridge(args.port, args.cmd))

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

    # Use project-local state/log paths when running from a project directory
    project_root = env.get("CODEX_OSS_PROJECT", os.getcwd())
    state_dir = os.path.join(project_root, ".codex-oss", "state")
    os.makedirs(state_dir, exist_ok=True)
    env["PROXY_STATE_DB"] = env.get("PROXY_STATE_DB", os.path.join(state_dir, "proxy.sqlite3"))

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


# ── Supervisor ──

def _supervise(port: int) -> int:
    """Foreground supervisor: start bridge, monitor health, stream logs, handle Ctrl+C."""
    import os, signal, time, threading, subprocess, urllib.request, json, hashlib

    package_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(package_dir)
    bridge_path = os.path.join(repo_root, "bridge.py")
    if not os.path.exists(bridge_path):
        print(f"ERROR: bridge.py not found at {bridge_path}")
        return 1

    env = os.environ.copy()
    env["PROXY_PORT"] = str(port)
    env["CODEX_OSS_SUPERVISOR_MODE"] = "foreground"

    if not env.get("OPENCODE_GO_API_KEY"):
        env_file = os.path.join(os.getcwd(), ".codex-oss", "env", "opencode-go.env")
        if os.path.exists(env_file):
            with open(env_file) as f:
                for line in f:
                    if line.startswith("OPENCODE_GO_API_KEY="):
                        env["OPENCODE_GO_API_KEY"] = line.strip().split("=", 1)[1]
                        break

    if not env.get("OPENCODE_GO_API_KEY"):
        print("ERROR: OPENCODE_GO_API_KEY not set")
        print("  Set it via: export OPENCODE_GO_API_KEY=sk-...")
        print("  Or create .codex-oss/env/opencode-go.env")
        return 1

    log_dir = os.path.join(os.getcwd(), ".codex-oss", "logs")
    os.makedirs(log_dir, exist_ok=True)
    out_log = open(os.path.join(log_dir, "bridge.log"), "a")
    err_log = open(os.path.join(log_dir, "bridge.err.log"), "a")

    def _get_hash():
        try:
            with open(bridge_path, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()[:12]
        except Exception:
            return "unknown"

    source_hash = _get_hash()
    print(f"  OpenCode Go key: loaded")
    print(f"  bridge.py source hash: {source_hash}")
    print(f"  Starting bridge on port {port}...")

    proc = subprocess.Popen(
        [sys.executable, bridge_path],
        env=env, stdout=out_log, stderr=err_log,
        start_new_session=True,
    )

    pid_file = os.path.join(os.getcwd(), ".codex-oss", "run", "bridge.pid")
    os.makedirs(os.path.dirname(pid_file), exist_ok=True)
    with open(pid_file, "w") as f:
        f.write(str(proc.pid))

    # Supervisory JSON
    supervisor_info = {
        "mode": "foreground", "pid": proc.pid, "port": port,
        "source_hash": source_hash, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "project_root": os.getcwd(),
    }
    supervisor_file = os.path.join(os.getcwd(), ".codex-oss", "run", "supervisor.json")
    with open(supervisor_file, "w") as f:
        json.dump(supervisor_info, f)

    # Wait for health
    print(f"  Waiting for health...")
    for i in range(30):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
            key = env.get("LITELLM_MASTER_KEY", "sk-local-codex-bridge")
            req.add_header("Authorization", f"Bearer {key}")
            d = json.loads(urllib.request.urlopen(req, timeout=2).read())
            if d.get("ok"):
                print(f"  Provider listening: http://127.0.0.1:{port}/v1")
                print(f"  State DB: {d.get('state_db', 'unknown')}")
                print()
                print("  Ready for Codex.")
                print("  Keep this terminal open. Open Codex Desktop/CLI in another window.")
                print()
                break
        except Exception:
            time.sleep(0.5)
    else:
        print("  WARN: Bridge did not respond to health check within 15s")
        print("  Check .codex-oss/logs/bridge.err.log")

    # Handle Ctrl+C gracefully
    def _shutdown(sig=None, frame=None):
        print("\n  Shutting down bridge...")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        os.remove(pid_file) if os.path.exists(pid_file) else None
        os.remove(supervisor_file) if os.path.exists(supervisor_file) else None
        print("  Bridge stopped.")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Log tailer — stream last few lines of error log
    def _tail():
        try:
            while proc.poll() is None:
                time.sleep(3)
                # Print any new error log content
                if os.path.exists(os.path.join(log_dir, "bridge.err.log")):
                    with open(os.path.join(log_dir, "bridge.err.log")) as f:
                        lines = f.readlines()
                        if lines:
                            last = lines[-1].strip()
                            if "error" in last.lower() or "fatal" in last.lower():
                                print(f"  [bridge] {last[:120]}")
        except Exception:
            pass
    threading.Thread(target=_tail, daemon=True).start()

    # Wait for bridge process
    proc.wait()
    print("  Bridge process exited unexpectedly.")
    os.remove(pid_file) if os.path.exists(pid_file) else None
    return 1


def _run_with_bridge(port: int, cmd: list) -> int:
    """Start bridge, run command, stop bridge when done."""
    import os, time, subprocess, urllib.request, json

    if not cmd:
        print("Usage: codex-oss run -- <command>")
        print("Example: codex-oss run -- codex")
        return 1

    # Start bridge
    env = os.environ.copy()
    env["PROXY_PORT"] = str(port)

    package_dir = os.path.dirname(os.path.abspath(__file__))
    bridge_path = os.path.join(os.path.dirname(package_dir), "bridge.py")
    if not os.path.exists(bridge_path):
        print(f"ERROR: bridge.py not found at {bridge_path}")
        return 1

    log_dir = os.path.join(os.getcwd(), ".codex-oss", "logs")
    os.makedirs(log_dir, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, bridge_path], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    # Wait for health
    for _ in range(30):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
            key = env.get("LITELLM_MASTER_KEY", "sk-local-codex-bridge")
            req.add_header("Authorization", f"Bearer {key}")
            if json.loads(urllib.request.urlopen(req, timeout=2).read()).get("ok"):
                print(f"Bridge started on port {port}")
                break
        except Exception:
            time.sleep(0.5)
    else:
        proc.terminate()
        print("Bridge failed to start")
        return 1

    # Run user command (strip leading '--' if present)
    user_cmd = cmd[1:] if cmd and cmd[0] == "--" else cmd
    result = subprocess.run(user_cmd, env={**os.environ, "LITELLM_MASTER_KEY": "sk-local-codex-bridge"})
    rc = result.returncode

    # Stop bridge
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    print(f"Bridge stopped (command exited with {rc})")
    return rc


if __name__ == "__main__":
    main()
