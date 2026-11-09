"""owner_cli: an interactive chat where YOU play the owner of the circuit and talk to the agent in plain English.

  PYTHONPATH=src python3 -m circuit_a2a owner_cli [--url http://127.0.0.1:8923/]

Type a line and press Enter. Commands: /quit (leave), /reset (start a new conversation), /help (ask the agent for help)."""
import argparse
import urllib.error
import uuid

from circuit_a2a.wire import DEFAULT_URL, fetch_card, send_text


def main(argv=None):
    ap = argparse.ArgumentParser(prog="owner_cli", description=__doc__.split("\n")[0])
    ap.add_argument("--url", help=f"agent URL (default {DEFAULT_URL})")
    ap.add_argument("--port", type=int, help="shortcut for --url http://127.0.0.1:PORT/ (env A2A_PORT also works)")
    a = ap.parse_args(argv)
    a.url = a.url or (f"http://127.0.0.1:{a.port}/" if a.port else DEFAULT_URL)
    try:
        card = fetch_card(a.url)
    except (urllib.error.URLError, OSError) as e:
        raise SystemExit(f"cannot reach an agent at {a.url}: {e}\nStart one with: PYTHONPATH=src python3 -m circuit_a2a server")
    print(f"Connected to agent '{card['name']}' v{card['version']} at {a.url}\nDescribe your circuit in plain English. /quit to leave, /reset for a new conversation.\n")
    session = "cli-" + uuid.uuid4().hex[:8]
    while True:
        try:
            line = input("owner> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line == "/quit":
            break
        if line == "/reset":
            session = "cli-" + uuid.uuid4().hex[:8]
            print("(new conversation)\n")
            continue
        try:
            state, txt, _, ms = send_text(a.url, session, line)
        except (RuntimeError, urllib.error.URLError, OSError) as e:
            print(f"error: {e}\n")
            continue
        print(f"\nagent [{state}, {ms / 1000:.1f}s]> {txt}\n")


if __name__ == "__main__":
    main()
