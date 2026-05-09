"""PatchRecipeV1 — anchor-based patch construction.

The runtime inspects target files, extracts anchor candidates
(function/class definitions, key lines), and the model selects
an anchor + content. The runtime builds the diff.

Recipe types:
  insert_after   — insert content after a matched anchor
  insert_before  — insert content before a matched anchor
  append_to_file — append content to end of file
  replace_exact  — replace matched anchor text with content
"""

from __future__ import annotations

import os
import re
from typing import Any

JSON = dict[str, Any]


def extract_anchors(path: str) -> list[JSON]:
    """Read a file and extract structural anchors for patching."""
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return []
    text = "".join(lines)
    anchors: list[JSON] = []

    # File boundary anchors
    anchors.append({
        "id": "anchor:file_start",
        "kind": "file_start",
        "line": 0,
        "text": "<start of file>",
    })
    anchors.append({
        "id": "anchor:file_end",
        "kind": "file_end",
        "line": len(lines),
        "text": "<end of file>",
    })

    # Function definitions
    for match in re.finditer(r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\([^)]*\)\s*:", text, re.MULTILINE):
        name = match.group(1)
        line_num = text[: match.start()].count("\n")
        anchors.append({
            "id": f"anchor:def_{name}",
            "kind": "function_definition",
            "line": line_num,
            "text": match.group(0).strip(),
            "detail": f"Function definition: {name}",
        })

    # Class definitions
    for match in re.finditer(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\s*[\(:]", text, re.MULTILINE):
        name = match.group(1)
        line_num = text[: match.start()].count("\n")
        anchors.append({
            "id": f"anchor:class_{name}",
            "kind": "class_definition",
            "line": line_num,
            "text": match.group(0).strip(),
            "detail": f"Class definition: {name}",
        })

    # Import blocks
    for match in re.finditer(r"(^import\s+\S+.*$|^from\s+\S+\s+import\s+.*$)", text, re.MULTILINE):
        line_num = text[: match.start()].count("\n")
        anchors.append({
            "id": f"anchor:import_{line_num}",
            "kind": "import",
            "line": line_num,
            "text": match.group(0).strip(),
            "detail": f"Import at line {line_num + 1}",
        })

    # Sort by line number
    anchors.sort(key=lambda a: int(a.get("line", 0)))
    return anchors


def build_patch_from_recipe(
    recipe: JSON,
    anchors: list[JSON],
    path: str,
) -> str | None:
    """Build a unified diff from a PatchRecipeV1 model response.

    Recipe format:
    {
      "recipe_type": "insert_after" | "insert_before" | "append_to_file" | "replace_exact",
      "selected_anchor_id": "anchor:def_describe",
      "content": "\\n\\ndef new_function():\\n    pass\\n",
      "constraints": {"max_added_lines": 8, "no_imports": true}
    }
    """
    recipe_type = str(recipe.get("recipe_type", "") or "")
    anchor_id = str(recipe.get("selected_anchor_id", "") or "")
    content = str(recipe.get("content", "") or "")

    if not content.strip():
        return None

    if not os.path.isfile(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return None

    if recipe_type == "append_to_file":
        return _build_append_patch(path, lines, content)

    anchor = next((a for a in anchors if a.get("id") == anchor_id), None)
    if anchor is None and recipe_type not in {"append_to_file"}:
        return None

    if recipe_type == "insert_after":
        return _build_insert_after(path, lines, anchor, content)
    if recipe_type == "insert_before":
        return _build_insert_before(path, lines, anchor, content)
    if recipe_type == "replace_exact":
        return _build_replace_exact(path, lines, anchor, content)

    return None


def _build_append_patch(path: str, lines: list[str], content: str) -> str:
    original = "".join(lines)
    if not original.endswith("\n"):
        content = "\n" + content
    modified = original.rstrip("\n") + content + "\n"
    return _unified_diff(path, original, modified)


def _build_insert_after(path: str, lines: list[str], anchor: JSON, content: str) -> str:
    line_no = int(anchor.get("line", 0))
    indent = _detect_indent(lines, line_no)
    content = _indent_content(content, indent)
    modified = list(lines)
    insert_at = _end_of_block(lines, line_no)
    content = content + "\n"
    modified.insert(insert_at + 1, content)
    return _unified_diff(path, "".join(lines), "".join(modified))


def _build_insert_before(path: str, lines: list[str], anchor: JSON, content: str) -> str:
    line_no = int(anchor.get("line", 0))
    indent = _detect_indent(lines, line_no)
    content = _indent_content(content, indent)
    modified = list(lines)
    content = content + "\n"
    modified.insert(line_no, content)
    return _unified_diff(path, "".join(lines), "".join(modified))


def _build_replace_exact(path: str, lines: list[str], anchor: JSON, content: str) -> str:
    line_no = int(anchor.get("line", 0))
    line_count = _block_line_count(lines, line_no)
    modified = list(lines)
    for i in range(line_no + line_count - 1, line_no - 1, -1):
        modified.pop(i)
    modified.insert(line_no, content + "\n")
    return _unified_diff(path, "".join(lines), "".join(modified))


def _detect_indent(lines: list[str], line_no: int) -> str:
    if line_no < len(lines):
        match = re.match(r"^(\s*)", lines[line_no])
        if match:
            return match.group(1)
    return ""


def _indent_content(content: str, indent: str) -> str:
    if not indent:
        return content
    return "\n".join(indent + line if line.strip() else line for line in content.split("\n"))


def _end_of_block(lines: list[str], line_no: int) -> int:
    if line_no >= len(lines):
        return line_no
    block_indent = _detect_indent(lines, line_no)
    if not block_indent:
        return line_no
    for i in range(line_no + 1, len(lines)):
        line = lines[i]
        if line.strip() and not line.startswith(block_indent + " ") and not line.startswith(block_indent + "\t"):
            if _detect_indent(lines, i) <= block_indent:
                return i - 1
    return len(lines) - 1


def _block_line_count(lines: list[str], line_no: int) -> int:
    return _end_of_block(lines, line_no) - line_no + 1


def _unified_diff(path: str, original: str, modified: str) -> str:
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".orig", delete=False) as of:
        of.write(original)
        orig_path = of.name
    with tempfile.NamedTemporaryFile(mode="w", suffix=".mod", delete=False) as mf:
        mf.write(modified)
        mod_path = mf.name

    try:
        proc = subprocess.run(
            ["diff", "-u", orig_path, mod_path],
            capture_output=True, text=True,
        )
        diff_output = proc.stdout or ""
        if not diff_output.strip():
            return ""
        diff_output = diff_output.replace(orig_path, path).replace(mod_path, path)
        return diff_output
    finally:
        try:
            os.unlink(orig_path)
        except OSError:
            pass
        try:
            os.unlink(mod_path)
        except OSError:
            pass
