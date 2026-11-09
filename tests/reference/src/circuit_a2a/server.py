"""A2A server of the electrical agent (HTTP, stdlib). Listens on 127.0.0.1 only, port 8923 (A2A_PORT to change it).

  GET  /.well-known/agent-card.json   the agent card
  POST /                              JSON-RPC 2.0: message/send, tasks/get, tasks/cancel
  GET  /log?n=20                      last lines of the A2A log (read-only)
Threads: one request per thread (conversations wait for the LLM without blocking the others), but EVERYTHING that touches Prolog runs in the main
thread, one at a time (core.run_in_core), because the per-question time cap is a signal (SIGALRM) and only works there.
Usage: python -m circuit_a2a server [--translator {simulated,claude-cli,anthropic-api}] [--model NAME] [--port N] [--no-test]
With no translator given it asks (terminal) or auto-detects (no terminal): see wizard.py. Precedence: flag > env > assistant.
Variables: A2A_PORT (8923), A2A_TRANSLATOR, A2A_MODEL, A2A_CLI_USE_SUBSCRIPTION, A2A_LOG (path).
"""
import argparse
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from circuit_a2a import wizard
from circuit_a2a.agent import Agent, agent_card
from circuit_a2a.core import LIMITS, Rejection, run_in_core, serve_queue

PORT = int(os.environ.get("A2A_PORT", "8923"))
HOST = "127.0.0.1"
URL = f"http://{HOST}:{PORT}/"
AGENT = None


def rpc_error(id_, code, message, data=None):
    e = {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}
    if data:
        e["error"]["data"] = data
    return e


class Handler(BaseHTTPRequestHandler):
    timeout = 10
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def reply(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        route = self.path.split("?")[0]
        if route in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
            return self.reply(200, agent_card(URL))
        if route == "/log":
            n = 20
            if "n=" in self.path:
                try:
                    n = max(1, min(200, int(self.path.split("n=")[1].split("&")[0])))
                except ValueError:
                    pass
            return self.reply(200, AGENT.tail(n))
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = -1
        if n < 0 or n > LIMITS["bytes_per_message"]:
            self.reply(413, rpc_error(None, -32600, "too_large", {"code": "too_large",
                       "detail": f"more than {LIMITS['bytes_per_message']} bytes"}))
            self.close_connection = True
            return
        raw = self.rfile.read(n)
        try:
            req = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return self.reply(200, rpc_error(None, -32700, "invalid_json", {"code": "invalid_json"}))
        if not isinstance(req, dict) or req.get("jsonrpc") != "2.0" or not isinstance(req.get("method"), str):
            return self.reply(200, rpc_error(req.get("id") if isinstance(req, dict) else None, -32600, "invalid JSON-RPC request"))
        id_, method, params = req.get("id"), req["method"], req.get("params")
        try:
            if method == "message/send":
                if AGENT.is_conversation(params):
                    res = AGENT.message(params, n)          # in this thread: waits for the LLM; Prolog goes to the main thread
                else:
                    res = run_in_core(lambda: AGENT.message(params, n))
            elif method in ("tasks/get", "tasks/cancel"):
                if not isinstance(params, dict) or not isinstance(params.get("id"), str):
                    raise Rejection("invalid_format", "params.id (task id) is required")
                res = AGENT.get_task(params["id"]) if method == "tasks/get" else AGENT.cancel(params["id"])
            else:
                return self.reply(200, rpc_error(id_, -32601, "method not supported: " + method[:40]))
        except Rejection as r:
            return self.reply(200, rpc_error(id_, -32602, r.code, r.to_json()))
        self.reply(200, {"jsonrpc": "2.0", "id": id_, "result": res})


def main(argv=None):
    global AGENT, PORT, URL
    ap = argparse.ArgumentParser(prog="circuit_a2a server", description="Start the electrical A2A agent (HTTP, 127.0.0.1 only).",
                                 epilog="With no translator flag the program asks which one to use (terminal) or picks the best detected one (no terminal).")
    ap.add_argument("--port", type=int, help="port to listen on (default 8923). Env: A2A_PORT")
    wizard.add_arguments(ap)
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        PORT = a.port if a.port is not None else int(os.environ.get("A2A_PORT", "8923"))
    except ValueError:
        raise SystemExit("error: A2A_PORT must be a number")
    URL = f"http://{HOST}:{PORT}/"
    with socket.socket() as probe:                      # fail fast, before asking questions or testing the LLM
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((HOST, PORT))
        except OSError as e:
            raise SystemExit(f"cannot listen on {HOST}:{PORT} ({e.strerror}). Is another server already running? Use another port: python -m circuit_a2a server --port {PORT + 7}")
    kind, model, strip = wizard.choose(a)
    AGENT = Agent(URL, log=os.environ.get("A2A_LOG") or None, translator=wizard.build(kind, model, strip))
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer.request_queue_size = 64
    try:
        srv = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        raise SystemExit(f"cannot listen on {HOST}:{PORT} ({e.strerror}). Is another server already running? Use another port: python -m circuit_a2a server --port {PORT + 7}")
    print(f"electrical A2A agent at {URL}  (card: {URL}.well-known/agent-card.json) · translator: {AGENT.translator.name}", flush=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        while True:
            serve_queue()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
