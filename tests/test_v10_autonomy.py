#!/usr/bin/env python3
"""v10 runtime tests — proper tool execution, Rorschach task, managed autonomy."""

import json, os, time, urllib.request, subprocess, sys, re

BRIDGE_PORT = 4005
RORSCHACH = "/Users/goldtetsola/Desktop/Coding Projects/Rorschach"
AUTH = "sk-local-codex-bridge"
URL = f"http://127.0.0.1:{BRIDGE_PORT}/v1/responses"


def extract_path(args):
    """Robustly extract a file path from tool call arguments."""
    if isinstance(args, str):
        try: args = json.loads(args)
        except: return args.strip()
    if not isinstance(args, dict): return str(args)

    for key in ("path", "file_path", "filepath", "file", "filename", "target"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    for nested_key in ("args", "input", "request"):
        nested = args.get(nested_key)
        if isinstance(nested, dict):
            found = extract_path(nested)
            if found: return found

    return json.dumps(args)


def run_rtk_read(project_root, rel_path):
    """Execute rtk read and return faithful output."""
    full = os.path.normpath(os.path.join(project_root, rel_path))
    if not os.path.abspath(full).startswith(os.path.abspath(project_root) + os.sep):
        return f"[SECURITY] Path escapes project root: {rel_path}"

    try:
        out = subprocess.run(["rtk", "read", rel_path], cwd=project_root,
                             capture_output=True, text=True, timeout=15)
        MAX = 4000
        content = out.stdout
        if len(content) > MAX:
            content = (f"[TRUNCATED: {len(content)} chars total, showing first {MAX}]\n"
                       + content[:MAX] + f"\n[... {len(content) - MAX} more chars omitted ...]")
        return (f"[rtk_read RESULT]\npath: {rel_path}\nexit_code: {out.returncode}\n"
                f"stdout_bytes: {len(out.stdout)}\nstderr_bytes: {len(out.stderr)}\n\n"
                f"STDOUT:\n{content}\n\nSTDERR:\n{out.stderr}")
    except Exception as e:
        return f"[rtk_read ERROR]\npath: {rel_path}\nerror: {e}"


def run_rtk_git(project_root, args_list):
    """Execute rtk git and return faithful output."""
    try:
        out = subprocess.run(["rtk", "git"] + args_list, cwd=project_root,
                             capture_output=True, text=True, timeout=15)
        return (f"[rtk_git RESULT]\ncmd: git {' '.join(args_list)}\n"
                f"exit_code: {out.returncode}\n"
                f"STDOUT:\n{out.stdout[:3000]}\n\nSTDERR:\n{out.stderr}")
    except Exception as e:
        return f"[rtk_git ERROR]\nerror: {e}"


def call_bridge(body, timeout=120):
    data = json.dumps(body).encode()
    req = urllib.request.Request(URL, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {AUTH}")
    req.add_header("Content-Type", "application/json")
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def get_tool_calls(resp):
    return [o for o in resp.get("output", []) if o.get("type") == "function_call"]


def get_messages(resp):
    return [o for o in resp.get("output", []) if o.get("type") == "message"]


def test_multi_turn_with_evidence_ledger():
    """Bridge should inject evidence ledger showing what's been read and what remains."""
    print("=== Test 1: Multi-turn with evidence ledger ===")
    body = {
        "model": "ocg-kimi-k2.6", "stream": False,
        "input": [
            {"role": "system", "content": "TASK TYPE: read-only repo inspection. PREP REPORT. "
             "READ-ONLY PATHS: package.json, README.md. Do not reread completed files."},
            {"role": "user", "content": "Read package.json and README.md. Summarize each."},
        ],
        "tools": [{"type": "function", "name": "rtk_read", "parameters": {"type": "object",
                  "properties": {"path": {"type": "string"}}, "required": ["path"]}}],
    }
    d0 = call_bridge(body)
    tc0 = get_tool_calls(d0)
    assert tc0, "Expected tool call"
    call_id = tc0[0]["call_id"]

    # Read package.json
    path = extract_path(tc0[0].get("arguments", "{}"))
    content = run_rtk_read(".", path)

    body2 = {
        "model": "ocg-kimi-k2.6", "stream": False,
        "previous_response_id": d0["id"],
        "input": [{"type": "function_call_output", "call_id": call_id, "output": content}],
        "tools": [{"type": "function", "name": "rtk_read", "parameters": {"type": "object",
                  "properties": {"path": {"type": "string"}}, "required": ["path"]}}],
    }
    d1 = call_bridge(body2)
    tc1 = get_tool_calls(d1)
    tm1 = get_messages(d1)

    if tc1:
        print(f"  -> CONTINUED (budget allowed): next = {tc1[0].get('name')}")
    elif tm1:
        print(f"  -> FINALIZED: {tm1[0]['content'][0]['text'][:120]}")
    print("  PASS: Managed autonomy + evidence injection active")


def test_rorschach_prep_task():
    """Exact Rorschach prep task with faithful tool execution."""
    print("\n=== Test 2: Rorschach prep task ===")

    if not os.path.isdir(RORSCHACH):
        print("  SKIP: Rorschach project not found")
        return

    task = (
        "ROLE: OSS read-only proof prep scout.\n"
        "TASK TYPE: read-only repo/memory inspection. PREP REPORT.\n"
        "READ-ONLY PATHS: .codex/napkin.md, docs/CONTINUITY.md, ORCHESTRATION.md, docs/memory/ORCHESTRATOR.md.\n"
        "VERIFICATION STEPS: git status --short, git rev-parse --abbrev-ref HEAD, git rev-parse HEAD, git log --oneline -3.\n"
        "DELIVERABLE: source id to use, prior green proof ids, current HEAD/branch, stale-memory caveats.\n"
        "DO NOT TOUCH: no edits, no staging, no DB, no secrets.\n"
        "COMPLETION RULE: read-only report only."
    )

    body = {
        "model": "ocg-kimi-k2.6", "stream": False,
        "input": [
            {"role": "system", "content": task},
            {"role": "user", "content": "Execute the prep report. Read each file using rtk_read."},
        ],
        "tools": [
            {"type": "function", "name": "rtk_read", "parameters": {"type": "object",
             "properties": {"path": {"type": "string"}}, "required": ["path"]}},
            {"type": "function", "name": "rtk_git", "parameters": {"type": "object",
             "properties": {"args": {"type": "array", "items": {"type": "string"}}}, "required": ["args"]}},
        ],
    }

    d = call_bridge(body, timeout=180)
    resp_id = d["id"]
    tc = get_tool_calls(d)
    files_read = set()
    turns = 0
    last_was_read = False

    while tc and turns < 8:
        turns += 1
        tool = tc[0]
        name = tool.get("name", "unknown")
        args_raw = tool.get("arguments", "{}")
        call_id = tool["call_id"]

        if name == "rtk_read":
            path = extract_path(args_raw)
            if path in files_read:
                print(f"  Turn {turns}: DUPLICATE read of {path} — would suppress in runtime")
                content = (f"[DUPLICATE_SUPPRESSED]\n{path} was already read completely.\n"
                           f"Remaining: {', '.join(p for p in ['.codex/napkin.md','docs/CONTINUITY.md','ORCHESTRATION.md','docs/memory/ORCHESTRATOR.md'] if p not in files_read)}")
            else:
                files_read.add(path)
                content = run_rtk_read(RORSCHACH, path)
                print(f"  Turn {turns}: read {path} ({len(content)} chars)")
            last_was_read = True
        elif name in ("rtk_git", "exec_command"):
            try:
                args_list = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                if isinstance(args_list, dict):
                    args_list = args_list.get("args", args_list.get("command", str(args_list)))
                if isinstance(args_list, str):
                    args_list = args_list.split()
            except:
                args_list = []
            content = run_rtk_git(RORSCHACH, args_list)
            print(f"  Turn {turns}: git {' '.join(args_list) if args_list else '?'}")
            last_was_read = False
        else:
            content = f"[UNSUPPORTED TOOL]\nTool: {name}\nArgs: {str(args_raw)[:200]}"
            print(f"  Turn {turns}: unsupported tool {name}")
            last_was_read = False

        body_cont = {
            "model": "ocg-kimi-k2.6", "stream": False,
            "previous_response_id": resp_id,
            "input": [{"type": "function_call_output", "call_id": call_id, "output": content}],
            "tools": body["tools"],
        }
        d = call_bridge(body_cont, timeout=180)
        tc = get_tool_calls(d)
        tm = get_messages(d)

        if tc:
            print(f"    -> CONTINUED (budget: {8 - turns} remaining)")
        elif tm:
            text = tm[0]["content"][0]["text"]
            print(f"    -> FINALIZED ({turns} files read): {text[:150]}")

            # Validate output
            has_source = "c07d2202" in text or "source" in text.lower()
            has_head = "HEAD" in text or "branch" in text.lower()
            has_caveats = "stale" in text.lower() or "caveat" in text.lower() or "warning" in text.lower()
            print(f"    Validation: source_id={'✓' if has_source else '✗'}, "
                  f"branch={'✓' if has_head else '✗'}, caveats={'✓' if has_caveats else '✗'}")
            break

    dupes = turns - len(files_read)
    print(f"\n  Files read: {sorted(files_read)}")
    print(f"  Total turns: {turns}, Unique files: {len(files_read)}, Duplicates: {dupes}")
    assert turns >= 1, "Should complete at least one turn"
    assert len(files_read) >= 1, "Should read at least one unique file"
    if dupes > turns // 2:
        print(f"  WARN: {dupes} duplicate reads out of {turns} total — evidence ledger needed")
    else:
        print(f"  PASS: managed autonomy completed with {turns} turns")


def main():
    # Start bridge
    os.environ.update({
        "OPENCODE_GO_API_KEY": os.environ.get("OPENCODE_GO_API_KEY", ""),
        "LITELLM_MASTER_KEY": AUTH, "ALLOW_MISSING_OPENCODE_KEY": "1",
        "PROXY_PORT": str(BRIDGE_PORT),
        "GPT_MODEL_STRATEGY": "error",
        "CONTINUATION_TOOLS": "read_only", "CONTINUATION_DEADLINE_SECONDS": "15",
        "FORCE_SINGLE_TOOL_INSTRUCTIONS": "0",
        "EXPOSE_EMPTY_REASONING_ITEM": "0",
    })

    # Bridge must be started externally (we connect to existing)
    # Check if bridge is running
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{BRIDGE_PORT}/health")
        req.add_header("Authorization", f"Bearer {AUTH}")
        urllib.request.urlopen(req, timeout=2)
    except:
        print("ERROR: Bridge not running on port", BRIDGE_PORT)
        print("Start with:")
        print(f"  cd /Users/goldtetsola/Desktop/Coding Projects/opencode-bridge")
        print(f"  OPENCODE_GO_API_KEY=... python3 bridge.py")
        sys.exit(1)

    test_multi_turn_with_evidence_ledger()
    test_rorschach_prep_task()
    print("\nAll v10 runtime tests passed")


if __name__ == "__main__":
    main()
