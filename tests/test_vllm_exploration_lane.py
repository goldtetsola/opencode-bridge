#!/usr/bin/env python3
"""vLLM exploration lane contract tests."""

from __future__ import annotations

import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("ALLOW_MISSING_OPENCODE_KEY", "1")

from bridge import ProxyApp


def read(rel_path: str) -> str:
    with open(os.path.join(ROOT, rel_path), "r", encoding="utf-8") as handle:
        return handle.read()


def assert_readme_keeps_vllm_inside_runtime_contract():
    text = read("README.md")
    required = [
        "Optional vLLM backend exploration",
        "not a replacement for the OSS Agent Runtime",
        "mission-a2/mission-a3 alias",
        "JSON action loop with tools=[] upstream",
        "ValidatedReportV1",
        "Raw direct vLLM behavior is measured only as a baseline",
    ]
    for needle in required:
        assert needle in text, f"README.md missing {needle!r}"


def assert_config_exposes_direct_vllm_only_as_research_lane():
    text = read("config.toml.example")
    assert "[model_providers.vllm_direct]" in text, "vLLM direct provider example missing"
    assert 'wire_api = "responses"' in text, "vLLM direct provider should use Responses"
    assert "not to bypass MissionV1" in text, "vLLM direct caveat missing"
    assert 'model_provider = "oss_runtime"' in text, "runtime provider should remain the product path"


def assert_runtime_env_example_is_generic_and_safe():
    text = read("examples/vllm-runtime.env.example")
    assert "UPSTREAM_BASE=http://127.0.0.1:8000/v1" in text
    assert "UPSTREAM_API_KEY=dummy" in text
    assert "OPENCODE_GO_API_KEY" not in text
    assert "MODEL_MAP_JSON=" in text
    assert "tools=[] upstream" in text
    assert "sk-" not in text


def assert_bridge_accepts_generic_upstream_api_key():
    old_env = os.environ.copy()
    try:
        os.environ.clear()
        os.environ.update({
            "UPSTREAM_API_KEY": "dummy-vllm-key",
            "UPSTREAM_BASE": "http://127.0.0.1:8000/v1",
            "GPT_MODEL_STRATEGY": "error",
            "ALLOW_MISSING_OPENCODE_KEY": "0",
        })
        app = ProxyApp()
        assert app.upstream_key == "dummy-vllm-key", app.upstream_key
        assert app.upstream_chat_url == "http://127.0.0.1:8000/v1/chat/completions"
    finally:
        os.environ.clear()
        os.environ.update(old_env)


def assert_default_model_map_stays_opencode_named():
    source = read("bridge.py")
    tree = ast.parse(source)
    maps = [
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id == "DEFAULT_MODEL_MAP"
    ]
    assert maps, "DEFAULT_MODEL_MAP not found"
    keys = {key.value for key in maps[0].keys if isinstance(key, ast.Constant)}
    assert "ocg-kimi-k2.6" in keys
    assert "mission-a3-kimi" not in keys, "Mission aliases should stay in managed runtime, not raw model map"


def main() -> int:
    assert_readme_keeps_vllm_inside_runtime_contract()
    assert_config_exposes_direct_vllm_only_as_research_lane()
    assert_runtime_env_example_is_generic_and_safe()
    assert_bridge_accepts_generic_upstream_api_key()
    assert_default_model_map_stays_opencode_named()
    print("PASS: vLLM exploration lane suite")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
