"""say: ONE conversational turn, clean output, so any shell (e.g. a Claude Code session) can play the owner.

  PYTHONPATH=src python3 -m circuit_a2a say --session ID "I have a source src, a wire w1 and a lamp l1."
  ... say --session ID "yes"            (same ID = same conversation; the server keeps the state)
  ... say --session ID --json "..."     (full agent reply: state, text, data)

Prints the agent's text on stdout and the task state on stderr (`[input-required]` = the agent expects your answer). Exit code 0 ok, 2 on error."""
import argparse
import json
import sys
import urllib.error
import uuid

from circuit_a2a.wire import DEFAULT_URL, send_text


def main(argv=None):
    ap = argparse.ArgumentParser(prog="say", description=__doc__.split("\n")[0])
    ap.add_argument("message", nargs="?", help="what the owner says (omit to read it from stdin)")
    ap.add_argument("--session", help="conversation id (contextId); omit to start a new one (it is printed on stderr)")
    ap.add_argument("--url", help=f"agent URL (default {DEFAULT_URL})")
    ap.add_argument("--port", type=int, help="shortcut for --url http://127.0.0.1:PORT/ (env A2A_PORT also works)")
    ap.add_argument("--json", action="store_true", help="print the whole reply as JSON")
    a = ap.parse_args(argv)
    a.url = a.url or (f"http://127.0.0.1:{a.port}/" if a.port else DEFAULT_URL)
    text = a.message if a.message is not None else sys.stdin.read().strip()
    session = a.session or "s-" + uuid.uuid4().hex[:8]
    try:
        state, txt, data, ms = send_text(a.url, session, text)
    except (RuntimeError, urllib.error.URLError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
    if a.json:
        print(json.dumps({"session": session, "state": state, "text": txt, "data": data}, ensure_ascii=False, indent=1))
    else:
        print(txt)
    print(f"[{state}] session={session} {ms / 1000:.1f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
