#!/usr/bin/env python3
"""v10 managed autonomy integration test."""

import json, os, time, urllib.request, socketserver, http.server, threading, subprocess, sys

BRIDGE_PORT = 4004
FAKE_UPSTREAM_PORT = 9004

TURN = 0


class FakeUpstream(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        global TURN
        TURN += 1
        if TURN >= 4:  # After 3 reads, produce final answer
            resp = json.dumps({"id":"f","object":"chat.completion","created":int(time.time()),
              "model":"kimi","choices":[{"index":0,"message":{"role":"assistant",
              "content":"Report: 3 files inspected. All clear."},"finish_reason":"stop"}],
              "usage":{"prompt_tokens":5,"completion_tokens":10,"total_tokens":15}}).encode()
        else:
            resp = json.dumps({"id":"f","object":"chat.completion","created":int(time.time()),
              "model":"kimi","choices":[{"index":0,"message":{"role":"assistant","content":None,
              "tool_calls":[{"id":f"c{TURN}","type":"function",
              "function":{"name":"rtk_read","arguments":"{}"}}]},"finish_reason":"tool_calls"}],
              "usage":{"prompt_tokens":5,"completion_tokens":5,"total_tokens":10}}).encode()
        self.send_response(200); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(resp))); self.end_headers(); self.wfile.write(resp)
    def log_message(self,*a): pass


def json_call(url, body, auth="sk-local-codex-bridge"):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {auth}"); req.add_header("Content-Type","application/json")
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def main():
    global TURN; TURN = 0
    print("v10 managed autonomy test")

    os.environ.update({"OPENCODE_GO_API_KEY":"sk-test","LITELLM_MASTER_KEY":"sk-local-codex-bridge",
      "ALLOW_MISSING_OPENCODE_KEY":"1","PROXY_PORT":str(BRIDGE_PORT),
      "UPSTREAM_BASE":f"http://127.0.0.1:{FAKE_UPSTREAM_PORT}/v1","GPT_MODEL_STRATEGY":"oss",
      "CONTINUATION_TOOLS":"read_only","CONTINUATION_DEADLINE_SECONDS":"10"})

    s = socketserver.ThreadingTCPServer(("127.0.0.1",FAKE_UPSTREAM_PORT),FakeUpstream)
    threading.Thread(target=s.serve_forever,daemon=True).start(); time.sleep(0.5)
    bridge_proc = subprocess.Popen([sys.executable,os.path.join(os.path.dirname(__file__),"..","bridge.py")],
      env={**os.environ},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

    for _ in range(15):
        try:
            req=urllib.request.Request(f"http://127.0.0.1:{BRIDGE_PORT}/health")
            req.add_header("Authorization","Bearer sk-local-codex-bridge")
            if json.loads(urllib.request.urlopen(req,timeout=2).read()).get("ok"): break
        except: time.sleep(0.5)
    else: bridge_proc.terminate(); raise RuntimeError("Bridge failed to start")

    try:
        url = f"http://127.0.0.1:{BRIDGE_PORT}/v1/responses"

        # Turn 0: Fresh request -> gets tool call c1
        d0 = json_call(url, {"model":"ocg-kimi-k2.6","stream":False,
          "input":[{"role":"system","content":"TASK TYPE: scout. Read-only repo inspection."},
                   {"role":"user","content":"Find all files"}],
          "tools":[{"type":"function","name":"rtk_read","parameters":{}}]})
        resp_id = d0["id"]
        calls = [o for o in d0.get("output",[]) if o.get("type")=="function_call"]
        assert calls, "Expected tool call on turn 0"; cid0 = calls[0]["call_id"]
        print(f"  Turn 0: tool call {cid0}")

        # Turn 1: Continuation -> should CONTINUE (budget 6), get tool call c2
        d1 = json_call(url, {"model":"ocg-kimi-k2.6","stream":False,
          "previous_response_id":resp_id,
          "input":[{"type":"function_call_output","call_id":cid0,"output":"file1 content"}]})
        calls1 = [o for o in d1.get("output",[]) if o.get("type")=="function_call"]
        assert calls1, f"Expected another tool call on turn 1 (budget available), got {d1.get('output',[])}"
        cid1 = calls1[0]["call_id"]
        print(f"  Turn 1: continued, got tool call {cid1} — PASS")

        # Turn 2: Continuation -> should CONTINUE, get tool call c3
        d2 = json_call(url, {"model":"ocg-kimi-k2.6","stream":False,
          "previous_response_id":resp_id,
          "input":[{"type":"function_call_output","call_id":cid1,"output":"file2 content"}]})
        calls2 = [o for o in d2.get("output",[]) if o.get("type")=="function_call"]
        assert calls2, f"Expected another tool call on turn 2 (budget available), got {d2.get('output',[])}"
        cid2 = calls2[0]["call_id"]
        print(f"  Turn 2: continued, got tool call {cid2} — PASS")

        # Turn 3: Continuation -> should FINALIZE (upstream sends text)
        d3 = json_call(url, {"model":"ocg-kimi-k2.6","stream":False,
          "previous_response_id":resp_id,
          "input":[{"type":"function_call_output","call_id":cid2,"output":"file3 content"}]})
        msgs = [o for o in d3.get("output",[]) if o.get("type")=="message"]
        assert msgs, f"Expected final report on turn 3+"
        print(f"  Turn 3: finalized — '{msgs[0]['content'][0]['text'][:60]}' — PASS")

        # Count: 4 upstream turns (T0 tool, T1 tool, T2 tool, T3 final)
        assert TURN >= 3, f"Expected at least 3 upstream turns"
        print(f"\n  PASS: {TURN} total upstream turns under managed autonomy")
    finally:
        bridge_proc.terminate(); bridge_proc.wait(); s.shutdown()


if __name__ == "__main__":
    main()
