#!/usr/bin/env python3
"""Transport self-test: verify stream=true always gets terminal SSE event."""

import json
import os
import time
import urllib.request
import socketserver
import http.server
import threading
import subprocess
import sys

BRIDGE_PORT = 4002
FAKE_UPSTREAM_PORT = 9002

FINALIZER_RESPONSE = json.dumps({
    "id": "chatcmpl-fake",
    "object": "chat.completion",
    "created": int(time.time()),
    "model": "kimi-k2.6",
    "choices": [{
        "index": 0,
        "message": {"role": "assistant", "content": "Finalizer: the file was read successfully."},
        "finish_reason": "stop"
    }],
    "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}
}).encode()

STALLING = False  # global toggle


class FakeUpstream(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        if STALLING:
            time.sleep(60)  # hang
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(FINALIZER_RESPONSE)))
        self.end_headers()
        self.wfile.write(FINALIZER_RESPONSE)

    def log_message(self, *args):
        pass


class ReusableTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_fake_upstream():
    server = ReusableTCPServer(("127.0.0.1", FAKE_UPSTREAM_PORT), FakeUpstream)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


def sse_stream_contains_terminal(url: str, body: dict, auth: str):
    """Return True if SSE stream contains response.completed or response.failed before close."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {auth}")
    req.add_header("Content-Type", "application/json")
    resp = urllib.request.urlopen(req, timeout=30)
    raw = b""
    try:
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            raw += chunk
            text = raw.decode("utf-8", errors="replace")
            if "response.completed" in text or "response.failed" in text:
                return True
    except Exception:
        pass
    text = raw.decode("utf-8", errors="replace")
    return "response.completed" in text or "response.failed" in text


def json_response_is_terminal(url: str, body: dict, auth: str):
    """Return True if JSON response has status=completed or error."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {auth}")
    req.add_header("Content-Type", "application/json")
    resp = urllib.request.urlopen(req, timeout=30)
    d = json.loads(resp.read())
    return d.get("status") == "completed" or d.get("error") is not None


def test_stream_deterministic_write():
    """stream=true write continuation must produce SSE with response.completed."""
    body = {
        "model": "ocg-deepseek-v4-pro",
        "stream": True,
        "previous_response_id": "resp_prev",
        "input": [{"type": "function_call_output", "call_id": "call_w", "output": "ok"}],
    }
    url = f"http://127.0.0.1:{BRIDGE_PORT}/v1/responses"
    ok = sse_stream_contains_terminal(url, body, "sk-local-codex-bridge")
    assert ok, "stream=true write continuation must include response.completed"
    print("  PASS: stream=true write continuation returns terminal SSE")


def test_nonstream_deterministic_write():
    """stream=false write continuation must return JSON 200 with completed."""
    body = {
        "model": "ocg-deepseek-v4-pro",
        "stream": False,
        "previous_response_id": "resp_prev2",
        "input": [{"type": "function_call_output", "call_id": "call_w2", "output": "ok"}],
    }
    url = f"http://127.0.0.1:{BRIDGE_PORT}/v1/responses"
    ok = json_response_is_terminal(url, body, "sk-local-codex-bridge")
    assert ok, "stream=false write continuation must return completed"
    print("  PASS: stream=false write continuation returns JSON completed")


def test_stream_read_finalizer():
    """stream=true read continuation must produce SSE with response.completed."""
    body = {
        "model": "ocg-kimi-k2.6",
        "stream": True,
        "previous_response_id": "resp_read_prev",
        "input": [{"type": "function_call_output", "call_id": "call_r", "output": "file contents"}],
    }
    url = f"http://127.0.0.1:{BRIDGE_PORT}/v1/responses"
    ok = sse_stream_contains_terminal(url, body, "sk-local-codex-bridge")
    assert ok, "stream=true read finalizer must include response.completed"
    print("  PASS: stream=true read finalizer returns terminal SSE")


def test_stream_degraded_on_stall():
    """Even with stalled upstream, stream=true must produce terminal SSE."""
    global STALLING
    STALLING = True
    try:
        body = {
            "model": "ocg-kimi-k2.6",
            "stream": True,
            "previous_response_id": "resp_stall",
            "input": [{"type": "function_call_output", "call_id": "call_s", "output": "data"}],
        }
        url = f"http://127.0.0.1:{BRIDGE_PORT}/v1/responses"
        ok = sse_stream_contains_terminal(url, body, "sk-local-codex-bridge")
        assert ok, "stalled upstream read continuation must still return terminal SSE"
        print("  PASS: stalled read continuation returns terminal SSE (degraded)")
    finally:
        STALLING = False


def test_health_identity():
    """Health endpoint must show version, pid, and transport_contract."""
    req = urllib.request.Request(f"http://127.0.0.1:{BRIDGE_PORT}/health")
    req.add_header("Authorization", "Bearer sk-local-codex-bridge")
    d = json.loads(urllib.request.urlopen(req, timeout=5).read())
    version = d.get("bridge_version")
    assert version and tuple(map(int, version.split("."))) >= (9, 0), f"Expected bridge >=9.0, got {version}"
    assert d.get("pid"), "Health must include pid"
    tc = d.get("transport_contract", {})
    assert tc.get("stream_terminal_guarantee"), "Transport contract must show stream guarantee"
    print(f"  PASS: health v={d['bridge_version']}, pid={d['pid']}, transport={tc}")


def main():
    global STALLING
    STALLING = False

    print("v9 transport self-tests")
    print("=======================")

    os.environ["OPENCODE_GO_API_KEY"] = "sk-test"
    os.environ["LITELLM_MASTER_KEY"] = "sk-local-codex-bridge"
    os.environ["ALLOW_MISSING_OPENCODE_KEY"] = "1"
    os.environ["PROXY_PORT"] = str(BRIDGE_PORT)
    os.environ["UPSTREAM_BASE"] = f"http://127.0.0.1:{FAKE_UPSTREAM_PORT}/v1"
    os.environ["GPT_MODEL_STRATEGY"] = "oss"
    os.environ["WRITE_RESULT_MODE"] = "deterministic"
    os.environ["CONTINUATION_TOOLS"] = "none"
    os.environ["CONTINUATION_DEADLINE_SECONDS"] = "10"
    os.environ["CONTINUATION_FALLBACK_MODELS"] = "deepseek-v4-flash"

    upstream = start_fake_upstream()
    time.sleep(0.5)

    bridge_proc = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(__file__), "..", "bridge.py")],
        env={**os.environ},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    # Wait for bridge
    for _ in range(15):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{BRIDGE_PORT}/health")
            req.add_header("Authorization", "Bearer sk-local-codex-bridge")
            with urllib.request.urlopen(req, timeout=2) as resp:
                if json.loads(resp.read()).get("ok"):
                    break
        except Exception:
            time.sleep(0.5)
    else:
        bridge_proc.terminate()
        raise RuntimeError("Bridge failed to start")

    try:
        test_health_identity()
        test_stream_deterministic_write()
        test_nonstream_deterministic_write()
        test_stream_read_finalizer()
        test_stream_degraded_on_stall()

        print()
        print("All v9 transport self-tests passed")
    finally:
        bridge_proc.terminate()
        bridge_proc.wait()
        upstream.shutdown()
        upstream.server_close()


if __name__ == "__main__":
    main()
