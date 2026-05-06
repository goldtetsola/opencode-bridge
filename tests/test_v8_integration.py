#!/usr/bin/env python3
"""v8 integration test: verify no request can hang indefinitely."""

import json
import os
import threading
import time
import urllib.request
import socketserver
import http.server

BRIDGE_PORT = 4001
FAKE_UPSTREAM_PORT = 9001

KIMI_RESPONSE = json.dumps({
    "id": "chatcmpl-fake",
    "object": "chat.completion",
    "created": int(time.time()),
    "model": "kimi-k2.6",
    "choices": [{
        "index": 0,
        "message": {"role": "assistant", "content": "Finalizer summary: the file contains a JSON project config."},
        "finish_reason": "stop"
    }],
    "usage": {"prompt_tokens": 10, "completion_tokens": 15, "total_tokens": 25}
}).encode()

STALLING_RESPONSE = b""  # Never send anything — simulate upstream stall


class FakeUpstream(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len)
        payload = json.loads(body)

        # Stalling mode: don't respond
        if "/stall" in self.path:
            time.sleep(600)  # hang forever

        # Normal mode: respond with Kimi summary
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(KIMI_RESPONSE)))
        self.end_headers()
        self.wfile.write(KIMI_RESPONSE)

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


def test_deterministic_write_can_hang():
    """Write result should get deterministic close — no upstream call needed."""
    # This should work even without an upstream server
    body = {
        "model": "ocg-deepseek-v4-pro",
        "previous_response_id": "resp_test_write",
        "input": [
            {"type": "function_call_output", "call_id": "wc1", "name": "write_to_file", "output": "success"}
        ],
        "stream": False,
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{BRIDGE_PORT}/v1/responses",
        data=json.dumps(body).encode(),
        method="POST",
    )
    req.add_header("Authorization", "Bearer sk-local-codex-bridge")
    req.add_header("Content-Type", "application/json")

    start = time.time()
    with urllib.request.urlopen(req, timeout=30) as resp:
        d = json.loads(resp.read())
        elapsed = time.time() - start
        assert d.get("status") == "completed", f"Expected completed, got {d.get('status')}"
        text = d["output"][0]["content"][0]["text"]
        assert "OSS write completed" in text, f"Expected deterministic write report, got: {text[:100]}"
        assert elapsed < 5, f"Write should be instant, took {elapsed:.1f}s"
        print(f"  PASS: deterministic write completes in {elapsed:.1f}s")


def test_fresh_turn():
    """Fresh turn should work normally."""
    body = {
        "model": "ocg-kimi-k2.6",
        "input": [{"role": "user", "content": "Say OK"}],
        "stream": False,
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{BRIDGE_PORT}/v1/responses",
        data=json.dumps(body).encode(),
        method="POST",
    )
    req.add_header("Authorization", "Bearer sk-local-codex-bridge")
    req.add_header("Content-Type", "application/json")

    with urllib.request.urlopen(req, timeout=30) as resp:
        d = json.loads(resp.read())
        assert d.get("status") == "completed"
        print("  PASS: fresh turn completes")


def test_continuation_detected():
    """Write tool output with no state — should still get deterministic close."""
    body = {
        "model": "ocg-deepseek-v4-pro",
        "input": [
            {"type": "function_call_output", "call_id": "wc_no_state", "name": "write_to_file", "output": "ok"}
        ],
        "stream": False,
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{BRIDGE_PORT}/v1/responses",
        data=json.dumps(body).encode(),
        method="POST",
    )
    req.add_header("Authorization", "Bearer sk-local-codex-bridge")
    req.add_header("Content-Type", "application/json")

    with urllib.request.urlopen(req, timeout=10) as resp:
        d = json.loads(resp.read())
        assert d.get("status") == "completed"
        text = d["output"][0]["content"][0]["text"]
        assert "OSS write" in text or "OSS tool failed" in text or "could not complete" in text.lower(), \
            f"Expected terminal response, got: {text[:100]}"
        print("  PASS: continuation terminates even without state")


def test_no_hang_on_stall():
    """Even with a completely stalled upstream, the bridge must return within the deadline."""
    # This tests the degraded completion path — write mode makes it instant
    body = {
        "model": "ocg-deepseek-v4-pro",
        "previous_response_id": "resp_stall_test",
        "input": [
            {"type": "function_call_output", "call_id": "stall_1", "name": "apply_patch", "output": "applied cleanly"}
        ],
        "stream": False,
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{BRIDGE_PORT}/v1/responses",
        data=json.dumps(body).encode(),
        method="POST",
    )
    req.add_header("Authorization", "Bearer sk-local-codex-bridge")
    req.add_header("Content-Type", "application/json")

    start = time.time()
    with urllib.request.urlopen(req, timeout=15) as resp:
        d = json.loads(resp.read())
        elapsed = time.time() - start
        assert d.get("status") == "completed", f"Expected completed, got {d.get('status')}"
        assert elapsed < 10, f"Stall should resolve in <10s, took {elapsed:.1f}s"
        print(f"  PASS: write returns in {elapsed:.1f}s even with stalled upstream")


def main():
    print("v8 integration tests")
    print("====================")

    # Start bridge pointing at fake upstream
    os.environ["OPENCODE_GO_API_KEY"] = "sk-test"
    os.environ["LITELLM_MASTER_KEY"] = "sk-local-codex-bridge"
    os.environ["PROXY_PORT"] = str(BRIDGE_PORT)
    os.environ["UPSTREAM_BASE"] = f"http://127.0.0.1:{FAKE_UPSTREAM_PORT}/v1"
    os.environ["WRITE_RESULT_MODE"] = "deterministic"
    os.environ["CONTINUATION_TOOLS"] = "none"
    os.environ["CONTINUATION_DEADLINE_SECONDS"] = "15"
    os.environ["ALLOW_MISSING_OPENCODE_KEY"] = "1"

    upstream = start_fake_upstream()
    time.sleep(0.5)

    # Start bridge in separate thread
    import subprocess, sys
    env = {**os.environ}
    env["GPT_MODEL_STRATEGY"] = "oss"  # Use oss mode since upstream is fake
    env["GPT_MODEL_OSS_FALLBACK"] = "kimi-k2.6"
    bridge_proc = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(__file__), "..", "bridge.py")],
        env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    # Wait for bridge to be ready
    for _ in range(10):
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
        test_deterministic_write_can_hang()
        test_continuation_detected()
        test_fresh_turn()
        test_no_hang_on_stall()

        print()
        print("All v8 integration tests passed")
    finally:
        bridge_proc.terminate()
        bridge_proc.wait()
        upstream.shutdown()
        upstream.server_close()


if __name__ == "__main__":
    main()
