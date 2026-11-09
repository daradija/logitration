# Copyright (c) 2026 David Ragel
# SPDX-License-Identifier: BUSL-1.1
# Licensed under the Business Source License 1.1; see LICENSE.
"""A2A over HTTP with the standard library: the agent card, JSON-RPC 2.0 (message/send, tasks/get, tasks/cancel)
and the small client that LogicAgent.connect() uses.

  GET  /.well-known/agent-card.json      the card (built from your phrases)
  POST /                                 JSON-RPC 2.0
A message carries ONE part: kind=text (conversation, same contextId = same conversation) or kind=data with
{"ask": {"kind": ..., "goal": ..., "step": ...}} (a structured question, no language model involved).
Not implemented: streaming, push notifications, authentication, TLS. It listens on 127.0.0.1 unless told otherwise.
"""
import json
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 65536
PROTOCOL = "0.3.0"


def card(agent, url):
    ex_fact = next((p.render(p.argnames and [a.lower() for a in p.argnames]) for p in agent.vocab.facts()), "")
    main = agent.knowledge.main
    return {
        "name": agent.name, "description": agent.description, "version": "1", "protocolVersion": PROTOCOL,
        "url": url, "preferredTransport": "JSONRPC",
        "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": False},
        "defaultInputModes": ["text/plain", "application/json"], "defaultOutputModes": ["text/plain", "application/json"],
        "securitySchemes": {}, "security": [],
        "skills": [
            {"id": "converse", "name": f"Talk about {agent.domain}",
             "description": "Describe your situation in natural language; the agent repeats what it understood and loads it after your yes. "
                            "Then ask: is it true, which ones, why, why not, how it changed. Use the same contextId to continue. "
                            "State 'input-required' means the agent is asking you something.",
             "tags": ["natural-language", "why", "why-not"],
             "examples": [ex_fact, f"why {main.render([a.lower() for a in main.argnames])}?"],
             "inputModes": ["text/plain"], "outputModes": ["text/plain", "application/json"]},
            {"id": "ask", "name": "Structured question",
             "description": "data part {\"ask\": {\"kind\": \"is|which|why|why_not|change|timeline\", \"goal\": \"pred(args)\", \"step\": k}} "
                            "on a conversation whose description is loaded. Goals: " + ", ".join(p.pattern() for p in agent.vocab),
             "tags": ["json"], "inputModes": ["application/json"], "outputModes": ["application/json"]},
        ],
    }


class Tasks:
    def __init__(self, limit=500):
        self.d, self.limit, self.lock = {}, limit, threading.Lock()

    def make(self, context_id, reply, task_id=None):
        tid = task_id or "t-" + uuid.uuid4().hex[:10]
        parts = [{"kind": "text", "text": reply.text}]
        if reply.data:
            parts.append({"kind": "data", "data": reply.data})
        task = {"kind": "task", "id": tid, "contextId": context_id,
                "status": {"state": reply.state, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                           "message": {"role": "agent", "kind": "message", "messageId": "m-" + uuid.uuid4().hex[:10], "parts": parts}}}
        with self.lock:
            self.d[tid] = task
            while len(self.d) > self.limit:
                self.d.pop(next(iter(self.d)))
        return task

    def get(self, tid):
        with self.lock:
            return self.d.get(tid)


def serve(agent, host="127.0.0.1", port=8923, block=True):
    """start the HTTP server; returns (server, url). block=True serves until Ctrl-C."""
    url = f"http://{host}:{port}/"
    tasks = Tasks()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"
        timeout = 30

        def log_message(self, *a):
            pass

        def send(self, status, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.split("?")[0] in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
                return self.send(200, card(agent, url))
            self.send(404, {"error": "not found"})

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                self.close_connection = True
                return self.send(413, _err(None, -32600, "too_large"))
            try:
                req = json.loads(self.rfile.read(n))
            except (ValueError, UnicodeDecodeError):
                return self.send(200, _err(None, -32700, "invalid_json"))
            if not isinstance(req, dict) or req.get("jsonrpc") != "2.0" or not isinstance(req.get("method"), str):
                return self.send(200, _err(req.get("id") if isinstance(req, dict) else None, -32600, "invalid JSON-RPC request"))
            rid, method, params = req.get("id"), req["method"], req.get("params") or {}
            if method == "message/send":
                msg = params.get("message") if isinstance(params, dict) else None
                parts = msg.get("parts") if isinstance(msg, dict) else None
                if not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict):
                    return self.send(200, _err(rid, -32602, "invalid_params", "the message must have exactly one part"))
                ctx = msg.get("contextId") if isinstance(msg.get("contextId"), str) else "c-" + uuid.uuid4().hex[:8]
                ctx = ctx[:64]
                p = parts[0]
                if p.get("kind") == "text" and isinstance(p.get("text"), str):
                    reply = agent.say(p["text"], session=ctx)
                elif p.get("kind") == "data" and isinstance(p.get("data"), dict) and isinstance(p["data"].get("ask"), dict):
                    reply = agent._ask_reply(p["data"]["ask"], ctx)
                else:
                    return self.send(200, _err(rid, -32602, "invalid_params", "use one part kind=text, or kind=data with {ask: {...}}"))
                tid = msg.get("taskId") if isinstance(msg.get("taskId"), str) and tasks.get(msg["taskId"]) else None
                return self.send(200, {"jsonrpc": "2.0", "id": rid, "result": tasks.make(ctx, reply, tid)})
            if method in ("tasks/get", "tasks/cancel"):
                t = tasks.get(params.get("id")) if isinstance(params, dict) else None
                if not t:
                    return self.send(200, _err(rid, -32001, "task not found"))
                if method == "tasks/cancel" and t["status"]["state"] == "input-required":
                    t["status"]["state"] = "canceled"
                return self.send(200, {"jsonrpc": "2.0", "id": rid, "result": t})
            self.send(200, _err(rid, -32601, "method not found"))

    ThreadingHTTPServer.daemon_threads = True
    srv = ThreadingHTTPServer((host, port), Handler)
    if not block:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, url
    print(f"{agent.name}: A2A agent at {url} (card: {url}.well-known/agent-card.json) · translator {agent.translator.name}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return srv, url


def _err(rid, code, message, detail=None):
    e = {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}
    if detail:
        e["error"]["data"] = {"detail": detail}
    return e


# ---------------------------------------------------------------- client
def fetch_card(url, timeout=15):
    with urllib.request.urlopen(url.rstrip("/") + "/.well-known/agent-card.json", timeout=timeout) as r:
        return json.loads(r.read())


def send(url, session, part, timeout=300):
    """one message/send; returns the task dict (raises RuntimeError on a JSON-RPC error)"""
    msg = {"role": "user", "kind": "message", "messageId": "m-" + uuid.uuid4().hex[:10], "contextId": session, "parts": [part]}
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"message": msg}}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read())
    if "error" in resp:
        raise RuntimeError("A2A error: " + json.dumps(resp["error"], ensure_ascii=False))
    return resp["result"]


def reply_parts(task):
    """(text, data, state) of a task"""
    parts = task["status"].get("message", {}).get("parts", [])
    text = "\n".join(p["text"] for p in parts if p.get("kind") == "text")
    data = next((p["data"] for p in parts if p.get("kind") == "data"), {})
    return text, data, task["status"]["state"]
