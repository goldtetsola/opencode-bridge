#!/usr/bin/env python3
"""Tests for the Codex app-server progress probe helper."""

from codex_oss.app_server_probe import app_server_config_args


def assert_app_server_config_args_define_probe_provider():
    args = app_server_config_args(43211)
    joined = "\n".join(args)
    assert 'model_provider="desktop_pre_final_text_probe"' in joined
    assert 'model="desktop-pre-final-text-probe"' in joined
    assert 'model_providers.desktop_pre_final_text_probe.base_url="http://127.0.0.1:43211/v1"' in joined
    assert 'model_providers.desktop_pre_final_text_probe.wire_api="responses"' in joined
    assert 'model_providers.desktop_pre_final_text_probe.auth.command="echo"' in joined


def main():
    tests = [assert_app_server_config_args_define_probe_provider]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"Results: {len(tests) - failed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
