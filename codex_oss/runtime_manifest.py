"""RuntimeSourceManifestV1 — hashes the runtime module tree at import time.

Provides freshness checks by comparing the hash at startup against current disk state.
The module-level constant STARTUP_TREE_SHA256 is computed once when this module is imported.
"""

from __future__ import annotations

import glob
import hashlib
import os
from typing import Any

JSON = dict[str, Any]

RUNTIME_SOURCE_PATTERNS: list[str] = [
    "bridge.py",
    "bin/codex-oss",
    "codex_oss/**/*.py",
]

EXCLUDED_PATTERNS: list[str] = [
    "__pycache__",
    "*.pyc",
]


def _file_sha256(path: str) -> str:
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return ""


def _collect_runtime_files(project_root: str) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    seen = set()
    for pattern in RUNTIME_SOURCE_PATTERNS:
        for match in glob.glob(os.path.join(project_root, pattern), recursive=True):
            if not os.path.isfile(match):
                continue
            rel = os.path.relpath(match, project_root)
            if any(rel.startswith(ex) or rel.endswith(ex.replace("*", "")) for ex in EXCLUDED_PATTERNS):
                continue
            if rel in seen:
                continue
            seen.add(rel)
            files.append({
                "path": rel,
                "sha256": _file_sha256(match),
            })
    files.sort(key=lambda f: f["path"])
    return files


def compute_tree_sha256(project_root: str) -> str:
    """Compute a single SHA-256 hash of the entire runtime module tree."""
    files = _collect_runtime_files(project_root)
    h = hashlib.sha256()
    for f in files:
        h.update(f["path"].encode("utf-8"))
        h.update(f["sha256"].encode("utf-8"))
    return h.hexdigest()


def runtime_identity(project_root: str) -> JSON:
    """Build a RuntimeIdentityV1 object comparing startup hash vs current disk."""
    from codex_oss.managed_bridge import RUNTIME_MODEL_ALIASES

    current_tree_sha = compute_tree_sha256(project_root)
    files = _collect_runtime_files(project_root)

    startup_sha = STARTUP_TREE_SHA256
    fresh = current_tree_sha == startup_sha
    changed = [] if fresh else [
        f["path"] for f in files if _file_sha256(os.path.join(project_root, f["path"])) != f.get("_startup_sha", "")
    ]

    # Annotate files with their startup sha for change detection
    for f in files:
        f["_startup_sha"] = f["sha256"]
        if not fresh:
            current_sha = _file_sha256(os.path.join(project_root, f["path"]))
            if current_sha == f["sha256"]:
                del f["_startup_sha"]
            else:
                f["sha256"] = current_sha

    return {
        "runtime_identity_version": "1.0",
        "pid": os.getpid(),
        "project_root": os.path.abspath(project_root),
        "cwd": os.getcwd(),
        "runtime_source_tree": {
            "startup_sha256": startup_sha,
            "current_sha256": current_tree_sha,
            "fresh": fresh,
            "changed_files": changed[:20] if not fresh else [],
            "total_files": len(files),
        },
        "model_aliases": dict(RUNTIME_MODEL_ALIASES) if isinstance(RUNTIME_MODEL_ALIASES, dict) else {},
        "freshness": {
            "live_tests_allowed": fresh,
            "restart_required": not fresh,
        },
    }


# ── Computed at module import time ──
_PROJECT_ROOT = os.getcwd()
STARTUP_TREE_SHA256 = compute_tree_sha256(_PROJECT_ROOT)
STARTUP_FILES = _collect_runtime_files(_PROJECT_ROOT)
STARTUP_MANIFEST: JSON = {
    "manifest_version": "runtime_source_manifest.v1",
    "project_root": os.path.abspath(_PROJECT_ROOT),
    "computed_at": None,  # filled at startup by bridge
    "startup_tree_sha256": STARTUP_TREE_SHA256,
    "files": [
        {"path": f["path"], "sha256": f["sha256"]}
        for f in STARTUP_FILES
    ],
}
