"""RuntimeToolV1 execution layer. Bridge-owned tools, no Codex tool loop involvement."""

from __future__ import annotations

import os
import fnmatch
import subprocess
import hashlib
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple

JSON = Dict[str, Any]

PROJECT_ROOT = os.getcwd()

# Forbidden paths (always blocked, even under allowed roots)
_DENY_PATTERNS = {
    ".env", "*.env", "secrets", ".git", "node_modules",
    ".codex-oss/env", ".codex-oss/state", ".codex-oss/logs",
}

# Secret patterns to redact
_SECRET_PATTERNS = [
    "sk-", "OPENCODE_GO_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
    "DATABASE_URL", "AUTH_TOKEN", "-----BEGIN",
]


@dataclass
class ToolResult:
    tool: str
    args: dict
    exit_code: int
    stdout: str
    stderr: str
    sha256: str = ""
    complete: bool = True
    chars_total: int = 0
    redactions_applied: bool = False
    risk_flags: List[str] = field(default_factory=list)


def resolve_path(raw: str, allowed_roots: list, allowed_paths: list) -> Tuple[Optional[str], Optional[str]]:
    """Resolve and validate a path. Returns (normalized_path, error)."""
    path = raw.strip().strip("'\"")
    if not path:
        return None, "empty path"
    if not allowed_roots and not allowed_paths:
        return None, "no allowed roots or paths supplied"

    # Block .. escapes
    if ".." in path.split(os.sep):
        return None, f"path escape blocked: {raw}"

    # Resolve absolute paths
    if path.startswith("/"):
        real = os.path.realpath(path)
        cwd = os.path.realpath(PROJECT_ROOT)
        if not real.startswith(cwd + os.sep) and real != cwd:
            return None, f"absolute path outside project root: {raw}"
        path = os.path.relpath(real, PROJECT_ROOT)

    # Block deny patterns on the requested path.
    deny_error = _deny_match(path)
    if deny_error:
        return None, deny_error

    # Check symlinks
    try:
        full = os.path.join(PROJECT_ROOT, path)
        real = os.path.realpath(full)
        cwd = os.path.realpath(PROJECT_ROOT)
        if not real.startswith(cwd + os.sep) and real != cwd:
            return None, f"symlink resolves outside project root: {path}"
        real_rel = os.path.relpath(real, PROJECT_ROOT)
        deny_error = _deny_match(real_rel)
        if deny_error:
            return None, deny_error
    except OSError:
        return None, f"path resolution failed: {path}"

    # Check exact paths first
    for allowed in allowed_paths:
        if path == allowed or path == allowed.rstrip("/"):
            return path, None

    # Check roots
    for root in allowed_roots:
        root_clean = root.rstrip("/") + "/"
        path_check = path if path.endswith("/") else path + "/"
        if path_check.startswith(root_clean) or path == root.rstrip("/"):
            return path, None

    return None, f"path not in allowed roots/paths: {path}"


def _deny_match(path: str) -> Optional[str]:
    normalized = path.strip().strip("'\"").strip(os.sep)
    for deny in _DENY_PATTERNS:
        deny_norm = deny.rstrip("/")
        if deny.startswith("*"):
            if normalized.endswith(deny[1:]) or normalized.endswith(deny[1:].rstrip("/") + os.sep):
                return f"deny pattern matched: {deny}"
        elif normalized == deny_norm or normalized.startswith(deny_norm + os.sep):
            return f"deny pattern matched: {deny}"
    return None


def _minimal_env() -> dict:
    allowed = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM")
    return {key: value for key, value in os.environ.items() if key in allowed}


def exec_read(path: str, max_bytes: int = 200000, timeout: int = 20) -> ToolResult:
    """Read an allowed path using RTK when available, else a native fallback."""
    try:
        proc = subprocess.run(
            ["rtk", "read", path],
            cwd=PROJECT_ROOT, env=_minimal_env(), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ToolResult(tool="rtk_read", args={"path": path},
                          exit_code=-1, stdout="", stderr="timeout",
                          complete=False)
    except FileNotFoundError:
        return _native_read(path, max_bytes=max_bytes)

    stdout = proc.stdout[:max_bytes] if proc.stdout else ""
    stderr = proc.stderr[:20000] if proc.stderr else ""
    sha = hashlib.sha256(stdout.encode()).hexdigest()[:12]

    # Scan for secrets
    redacted = False
    for pattern in _SECRET_PATTERNS:
        if pattern in stdout or pattern.lower() in stdout.lower():
            stdout = _redact(stdout)
            redacted = True
            break

    chars_total = len(proc.stdout or "")
    complete = chars_total <= max_bytes

    return ToolResult(tool="rtk_read", args={"path": path},
                      exit_code=proc.returncode or 0, stdout=stdout, stderr=stderr,
                      sha256=sha, complete=complete, chars_total=chars_total,
                      redactions_applied=redacted)


def exec_grep(
    pattern: str,
    path: str,
    timeout: int = 20,
    case_sensitive: Optional[bool] = None,
    include_glob: Optional[str] = None,
    max_results: int = 100,
    context_lines: int = 0,
) -> ToolResult:
    """Search an allowed path using RTK when available, else a native fallback."""
    if case_sensitive is not None or include_glob or max_results != 100 or context_lines:
        return _native_grep(
            pattern,
            path,
            case_sensitive=True if case_sensitive is None else bool(case_sensitive),
            include_glob=include_glob,
            max_results=max_results,
            context_lines=context_lines,
        )
    try:
        proc = subprocess.run(
            ["rtk", "grep", pattern, path],
            cwd=PROJECT_ROOT, env=_minimal_env(), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ToolResult(tool="rtk_grep", args={"pattern": pattern, "path": path},
                          exit_code=-1, stdout="", stderr="timeout", complete=False)
    except FileNotFoundError:
        return _native_grep(pattern, path)

    stdout = proc.stdout[:100000] if proc.stdout else ""
    exit_code = proc.returncode or 0
    if stdout.lower().startswith("0 matches for "):
        exit_code = 1
    sha = hashlib.sha256(stdout.encode()).hexdigest()[:12]
    matches = stdout.count("\n") if stdout else 0

    return ToolResult(tool="rtk_grep", args={"pattern": pattern, "path": path},
                      exit_code=exit_code, stdout=stdout,
                      stderr=proc.stderr[:5000] if proc.stderr else "",
                      sha256=sha, complete=True,
                      chars_total=len(proc.stdout or ""),
                      risk_flags=[] if exit_code in (0, 1) else ["grep_tool_error"])


def exec_ls(path: str, timeout: int = 10) -> ToolResult:
    """List an allowed path using RTK when available, else a native fallback."""
    try:
        proc = subprocess.run(
            ["rtk", "ls", path],
            cwd=PROJECT_ROOT, env=_minimal_env(), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ToolResult(tool="rtk_ls", args={"path": path},
                          exit_code=-1, stdout="", stderr="ls failed", complete=False)
    except FileNotFoundError:
        return _native_ls(path)

    return ToolResult(tool="rtk_ls", args={"path": path},
                      exit_code=proc.returncode or 0,
                      stdout=proc.stdout[:50000] if proc.stdout else "",
                      stderr=proc.stderr[:5000] if proc.stderr else "",
                      complete=True)


def exec_git_status(timeout: int = 10) -> ToolResult:
    """Run bounded git status using RTK when available, else git directly."""
    try:
        proc = subprocess.run(
            ["rtk", "git", "status", "--short"],
            cwd=PROJECT_ROOT, env=_minimal_env(), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ToolResult(tool="rtk_git_status", args={},
                          exit_code=-1, stdout="", stderr="git failed", complete=False)
    except FileNotFoundError:
        return _native_git("rtk_git_status", ["status", "--short"], {})

    return ToolResult(tool="rtk_git_status", args={},
                      exit_code=proc.returncode or 0,
                      stdout=proc.stdout[:20000] if proc.stdout else "",
                      complete=True)


def exec_git_log(limit: int = 5, timeout: int = 10) -> ToolResult:
    """Run bounded git log using RTK when available, else git directly."""
    limit = min(max(1, limit), 20)
    try:
        proc = subprocess.run(
            ["rtk", "git", "log", "--oneline", f"-n{limit}"],
            cwd=PROJECT_ROOT, env=_minimal_env(), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ToolResult(tool="rtk_git_log", args={"limit": limit},
                          exit_code=-1, stdout="", stderr="git failed", complete=False)
    except FileNotFoundError:
        return _native_git("rtk_git_log", ["log", "--oneline", f"-n{limit}"], {"limit": limit})

    return ToolResult(tool="rtk_git_log", args={"limit": limit},
                      exit_code=proc.returncode or 0,
                      stdout=proc.stdout[:20000] if proc.stdout else "",
                      complete=True)


def exec_git_show_stat(rev: str = "HEAD", timeout: int = 10) -> ToolResult:
    """Run stat-only git show using RTK when available, else git directly."""
    try:
        proc = subprocess.run(
            ["rtk", "git", "show", "--stat", rev],
            cwd=PROJECT_ROOT, env=_minimal_env(), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ToolResult(tool="rtk_git_show_stat", args={"rev": rev},
                          exit_code=-1, stdout="", stderr="git failed", complete=False)
    except FileNotFoundError:
        return _native_git("rtk_git_show_stat", ["show", "--stat", rev], {"rev": rev})

    return ToolResult(tool="rtk_git_show_stat", args={"rev": rev},
                      exit_code=proc.returncode or 0,
                      stdout=proc.stdout[:20000] if proc.stdout else "",
                      complete=True)


def exec_git_diff_stat(path: str = "", timeout: int = 10) -> ToolResult:
    """Run stat-only git diff using RTK when available, else git directly."""
    cmd = ["rtk", "git", "diff", "--stat"]
    if path:
        cmd.append(path)
    try:
        proc = subprocess.run(cmd, cwd=PROJECT_ROOT, env=_minimal_env(), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ToolResult(tool="rtk_git_diff_stat", args={"path": path},
                          exit_code=-1, stdout="", stderr="git failed", complete=False)
    except FileNotFoundError:
        args = ["diff", "--stat"] + ([path] if path else [])
        return _native_git("rtk_git_diff_stat", args, {"path": path})

    return ToolResult(tool="rtk_git_diff_stat", args={"path": path},
                      exit_code=proc.returncode or 0,
                      stdout=proc.stdout[:20000] if proc.stdout else "",
                      complete=True)


TOOL_EXECUTORS = {
    "rtk_read": exec_read,
    "rtk_grep": exec_grep,
    "rtk_ls": exec_ls,
    "rtk_git_status": exec_git_status,
    "rtk_git_log": exec_git_log,
    "rtk_git_show_stat": exec_git_show_stat,
    "rtk_git_diff_stat": exec_git_diff_stat,
}


def _redact(text: str) -> str:
    lines = text.split("\n")
    out = []
    for line in lines:
        for pattern in _SECRET_PATTERNS:
            if pattern in line:
                out.append(f"[REDACTED: {pattern[:20]}...]")
                break
        else:
            out.append(line)
    return "\n".join(out)


def _native_read(path: str, max_bytes: int = 200000) -> ToolResult:
    full = os.path.join(PROJECT_ROOT, path)
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as handle:
            raw = handle.read()
    except OSError as exc:
        return ToolResult(tool="rtk_read", args={"path": path}, exit_code=-1,
                          stdout="", stderr=str(exc), complete=False)
    stdout = raw[:max_bytes]
    redacted = False
    for pattern in _SECRET_PATTERNS:
        if pattern in stdout or pattern.lower() in stdout.lower():
            stdout = _redact(stdout)
            redacted = True
            break
    return ToolResult(tool="rtk_read", args={"path": path}, exit_code=0,
                      stdout=stdout, stderr="", sha256=hashlib.sha256(stdout.encode()).hexdigest()[:12],
                      complete=len(raw) <= max_bytes, chars_total=len(raw),
                      redactions_applied=redacted)


def _native_grep(
    pattern: str,
    path: str,
    case_sensitive: bool = True,
    include_glob: Optional[str] = None,
    max_results: int = 100,
    context_lines: int = 0,
) -> ToolResult:
    full = os.path.join(PROJECT_ROOT, path)
    matches = []
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        regex = re.compile(pattern, flags)
    except re.error:
        regex = re.compile(re.escape(pattern), flags)
    try:
        paths = _iter_text_paths(full)
        for file_path in paths:
            rel = os.path.relpath(file_path, PROJECT_ROOT)
            if include_glob and not fnmatch.fnmatch(rel, include_glob) and not fnmatch.fnmatch(os.path.basename(rel), include_glob):
                continue
            try:
                with open(file_path, "r", encoding="utf-8", errors="replace") as handle:
                    lines = list(handle)
                    for line_no, line in enumerate(lines, 1):
                        if regex.search(line):
                            if context_lines:
                                start = max(1, line_no - context_lines)
                                end = min(len(lines), line_no + context_lines)
                                for ctx_no in range(start, end + 1):
                                    prefix = ":" if ctx_no == line_no else "-"
                                    matches.append(f"{rel}{prefix}{ctx_no}:{lines[ctx_no - 1].rstrip()}")
                            else:
                                matches.append(f"{rel}:{line_no}:{line.rstrip()}")
                            if len(matches) >= max_results:
                                raise StopIteration
            except OSError:
                continue
    except StopIteration:
        pass
    except OSError as exc:
        return ToolResult(tool="rtk_grep", args={"pattern": pattern, "path": path},
                          exit_code=-1, stdout="", stderr=str(exc), complete=False)

    if matches:
        stdout = "\n".join(matches) + "\n"
        exit_code = 0
    else:
        stdout = f"0 matches for '{pattern}'\n"
        exit_code = 1
    args = {"pattern": pattern, "path": path}
    if case_sensitive is not True:
        args["case_sensitive"] = case_sensitive
    if include_glob:
        args["include_glob"] = include_glob
    if max_results != 100:
        args["max_results"] = max_results
    if context_lines:
        args["context_lines"] = context_lines
    return ToolResult(tool="rtk_grep", args=args,
                      exit_code=exit_code, stdout=stdout, stderr="",
                      sha256=hashlib.sha256(stdout.encode()).hexdigest()[:12],
                      complete=True, chars_total=len(stdout))


def _native_ls(path: str) -> ToolResult:
    full = os.path.join(PROJECT_ROOT, path)
    try:
        names = sorted(os.listdir(full))
    except OSError as exc:
        return ToolResult(tool="rtk_ls", args={"path": path}, exit_code=-1,
                          stdout="", stderr=str(exc), complete=False)
    stdout = "\n".join(names) + ("\n" if names else "")
    return ToolResult(tool="rtk_ls", args={"path": path}, exit_code=0,
                      stdout=stdout, stderr="", complete=True,
                      chars_total=len(stdout))


def _native_git(tool: str, git_args: list, result_args: dict) -> ToolResult:
    try:
        proc = subprocess.run(["git"] + git_args, cwd=PROJECT_ROOT, env=_minimal_env(),
                              capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return ToolResult(tool=tool, args=result_args, exit_code=-1,
                          stdout="", stderr=str(exc), complete=False)
    stdout = proc.stdout[:20000] if proc.stdout else ""
    return ToolResult(tool=tool, args=result_args, exit_code=proc.returncode or 0,
                      stdout=stdout, stderr=proc.stderr[:5000] if proc.stderr else "",
                      sha256=hashlib.sha256(stdout.encode()).hexdigest()[:12],
                      complete=True, chars_total=len(proc.stdout or ""))


def _iter_text_paths(full: str) -> list:
    if os.path.isfile(full):
        return [full]
    out = []
    for root, dirnames, filenames in os.walk(full):
        dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__", "node_modules"}]
        for filename in filenames:
            if fnmatch.fnmatch(filename, "*.pyc") or filename == ".DS_Store":
                continue
            out.append(os.path.join(root, filename))
    return out
