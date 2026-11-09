"""Tiny A2A HTTP helpers shared by the owner commands (urllib only)."""
import json
import os
import time
import urllib.request

# same port as the server: A2A_PORT, default 8923
DEFAULT_URL = f"http://127.0.0.1:{os.environ.get('A2A_PORT', '8923')}/"


def fetch_card(url):
    return json.load(urllib.request.urlopen(url.rstrip("/") + "/.well-known/agent-card.json", timeout=15))


def rpc(url, method, params, timeout=300):
    """JSON-RPC 2.0 call. Returns (response dict, milliseconds)."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read())
    return resp, (time.perf_counter() - t0) * 1000


def text_of(task):
    """(text, data) of a task: the agent's text parts joined, and its structured data part."""
    st = task["status"]
    parts = (st.get("message") or {}).get("parts", [])
    txt = "\n".join(p["text"] for p in parts if p["kind"] == "text")
    if not txt:
        for a in task.get("artifacts", []):
            txt = "\n".join(p["text"] for p in a["parts"] if p["kind"] == "text")
    data = {}
    for a in task.get("artifacts", []):
        for p in a["parts"]:
            if p["kind"] == "data":
                data = p["data"]
    for p in parts:
        if p["kind"] == "data":
            data = p["data"]
    return txt, data


def send_text(url, session, text, n=[0]):
    """One conversational turn: message/send with contextId=session. Returns (state, text, data, ms) or raises RuntimeError on a JSON-RPC error."""
    n[0] += 1
    msg = {"role": "user", "messageId": f"m{int(time.time() * 1000)}-{n[0]}", "contextId": session, "parts": [{"kind": "text", "text": text}]}
    resp, ms = rpc(url, "message/send", {"message": msg})
    if "error" in resp:
        raise RuntimeError("JSON-RPC error: " + json.dumps(resp["error"], ensure_ascii=False))
    t = resp["result"]
    txt, data = text_of(t)
    return t["status"]["state"], txt, data, ms
