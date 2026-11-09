"""Test client of the electrical A2A agent. It behaves like another agent: reads the card, learns with `teach`,
defines circuits, asks questions and checks the answers against the original example (src/circuit/answers.txt and
diary.txt, read-only) and against its own independent simulator (no Prolog).

Usage:  PYTHONPATH=src python3 src/circuit_a2a/client.py [--url http://127.0.0.1:8923/] [--launch]      (--launch starts its own server on another port)
Writes sessions 01..05_*.txt, test_results.txt, timings.json and lamina_session.json to the output dir (A2A_OUT, default out/).
Exit code = number of failed checks.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent   # src/
CIRCUIT = ROOT / "circuit"
OUT = Path(os.environ.get("A2A_OUT") or HERE.parents[1] / "out")


# =============================================================================================== transport + transcript
class Recorder:
    def __init__(self):
        self.lines, self.failures, self.checks, self.timings, self.reg, self.per_session = [], [], 0, {}, {}, {}

    def open(self, title, note=""):
        self.lines = [f"# {title}", f"# {note}" if note else "", f"# test client · {time.strftime('%Y-%m-%d %H:%M:%S')}", ""]

    def write(self, s=""):
        self.lines.append(s)

    def save(self, name):
        (OUT / name).write_text("\n".join(self.lines) + "\n")


G = Recorder()


def cut(s, n):
    return s if len(s) <= n else s[:n] + f" …(+{len(s) - n} characters)"


class Client:
    def __init__(self, url):
        self.url = url
        self.id = 0

    def get(self, path):
        with urllib.request.urlopen(self.url.rstrip("/") + path, timeout=15) as r:
            return json.loads(r.read())

    def rpc(self, method, params, raw=None):
        self.id += 1
        body = raw if raw is not None else json.dumps({"jsonrpc": "2.0", "id": self.id, "method": method, "params": params}).encode()
        t0 = time.perf_counter()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                resp = json.loads(r.read())
        except urllib.error.HTTPError as e:
            resp = json.loads(e.read())
            resp["_http"] = e.code
        return resp, (time.perf_counter() - t0) * 1000

    def message(self, data, task=None, label=None, show=1500):
        """sends a data part; returns (task, artifact data, text). Transcribes request and reply."""
        m = {"role": "user", "messageId": f"m{self.id + 1}", "parts": [{"kind": "data", "data": data}]}
        if task:
            m["taskId"] = task
        resp, ms = self.rpc("message/send", {"message": m})
        G.write(f">>> {label or data.get('skill', '?')}" + (f"  (task {task})" if task else ""))
        G.write("    " + cut(json.dumps(data, ensure_ascii=False), 700))
        if "error" in resp:
            G.write(f"<<< JSON-RPC ERROR {resp['error']['code']} {resp['error']['message']}  [{ms:.1f} ms]")
            G.write("")
            return None, None, None
        t = resp["result"]
        st = t["status"]
        art = t["artifacts"][0]["parts"] if t["artifacts"] else []
        d = next((p["data"] for p in art if p["kind"] == "data"), None)
        text = next((p["text"] for p in art if p["kind"] == "text"), None)
        G.write(f"<<< {st['label'].upper()} ({st['state']})  task {t['id']}  [{ms:.1f} ms]  history: " +
                " → ".join(h["label"] for h in t["history"]))
        if d is not None:
            G.write("    data: " + cut(json.dumps(d, ensure_ascii=False), show))
        if text:
            for l in cut(text, 1400).splitlines():
                G.write("    text: " + l)
        if "message" in st:
            G.write("    message: " + cut(json.dumps(st["message"], ensure_ascii=False), 700))
        G.write("")
        G.reg.setdefault(label or data.get("skill", "?"), {"req": data, "label": st["label"], "state": st["state"], "ms": round(ms, 1), "text": text,
                                                            "data": d, "message": st.get("message"), "task": t["id"]})
        k = str(data.get("question") or data.get("skill") or "(taskId)")
        G.timings.setdefault(k, []).append(ms)
        return t, d, text


def check(cond, what, detail=""):
    G.checks += 1
    mark = "ok   " if cond else "FAIL "
    G.write(f"  [{mark}] {what}" + (f"  — {detail}" if (detail and not cond) else ""))
    if not cond:
        G.failures.append(what + (f": {detail}" if detail else ""))
        print("FAIL:", what, detail, file=sys.stderr)
    return cond


def reject(cli, data, code, what, **kw):
    """sends something bad and checks that the task is rejected with the expected code"""
    t, d, _ = cli.message(data, label="BAD: " + what, **kw)
    ok = t is not None and t["status"]["state"] == "rejected" and t["status"]["message"]["error"]["code"] == code
    check(ok, f"rejected with {code}: {what}",
          "" if t is None else f"state {t['status']['state']} {t['status'].get('message', {}).get('error')}")
    return t


# =============================================================================================== reference simulator
class Simulator:
    """A circuit in plain Python (independent of Prolog): parts, wires with a condition, and events."""

    def __init__(self, defn):
        self.type = {p["name"]: p["type"] for p in defn["parts"]}
        self.pos = {p["name"]: p.get("position") for p in defn["parts"] if p["type"] == "switch"}
        self.ok = {n: True for n, t in self.type.items() if t in ("lamp", "fuse")}
        self.wires = [(c["to"], c["from"], c.get("when")) for c in defn.get("wires", [])]

    def apply(self, action, part):
        if action == "raise":
            self.pos[part] = "up"
        elif action == "lower":
            self.pos[part] = "down"
        elif action == "blow":
            self.ok[part] = False
        elif action == "repair":
            self.ok[part] = True

    def holds(self, when):
        if when is None:
            return True
        k, p = re.match(r"(\w+)\((\w+)\)", when).groups()
        return self.ok[p] if k == "ok" else self.pos[p] == k

    def current(self, x, seen=()):
        if self.type[x] == "source":
            return True
        return any(d == x and self.holds(w) and o not in seen and self.current(o, seen + (x,)) for d, o, w in self.wires)

    def lit(self):
        return sorted(n for n, t in self.type.items() if t == "lamp" and self.ok[n] and self.current(n))

    def state_of(self, fact):
        """'up(s2)' -> what holds today for that part and aspect"""
        k, p = re.match(r"(\w+)\((\w+)\)", fact).groups()
        if k in ("up", "down"):
            return f"{self.pos[p]}({p})"
        return f"{'ok' if self.ok[p] else 'blown'}({p})"


# =============================================================================================== reading the original files
def definition_from_events_pl():
    """The diary circuit, as circuit.py recorded it (events.pl): parts, wires and events 37.."""
    evs = []
    for l in (CIRCUIT / "events.pl").read_text().splitlines():
        m = re.match(r"event\((\d+), (.*)\)\.$", l)
        if m:
            evs.append((int(m.group(1)), m.group(2)))
    parts, wires, events, pos = [], [], [], {}
    setup = max(n for n, t in evs if t.startswith("connects"))
    for n, t in evs:
        if n > setup:
            k, p = re.match(r"(\w+)\((\w+)\)", t).groups()
            events.append({"action": {"up": "raise", "down": "lower", "blown": "blow", "ok": "repair"}[k], "part": p})
        elif t.startswith("part"):
            name, type_ = re.match(r"part\((\w+), (\w+)\)", t).groups()
            parts.append({"name": name, "type": type_})
        elif t.startswith("connects"):
            d, o, c = re.match(r"connects\((\w+), (\w+), (.*)\)$", t).groups()
            wires.append({"to": d, "from": o, **({} if c == "true" else {"when": c})})
        elif re.match(r"(up|down)\(", t):
            k, p = re.match(r"(\w+)\((\w+)\)", t).groups()
            pos[p] = k
    for p in parts:
        if p["type"] == "switch":
            p["position"] = pos[p["name"]]
    return {"parts": parts, "wires": wires}, events, setup


def read_answers():
    """answers.txt -> list of (question, goal, output)"""
    out = []
    for block in (CIRCUIT / "answers.txt").read_text().strip().split("\n\n"):
        ls = block.splitlines()
        out.append((ls[0][3:], ls[1].strip(), "\n".join(ls[2:])))
    return out


def read_diary():
    """diary.txt -> {event: [lines]}"""
    out, n = {}, None
    for l in (CIRCUIT / "diary.txt").read_text().splitlines():
        m = re.match(r"== After '(.*)' \(event (\d+)\)", l)
        if m:
            n = int(m.group(2))
            out[n] = []
        elif l.strip() and n is not None:
            out[n].append(l)
    return out


def exhaustive_cross_check(cli, name, sim_def, events, setup, show_lines=True):
    """For EVERY step (setup..last): lit and, for each lamp, why (n-1->n) and why_not against the simulator."""
    sim = Simulator(sim_def)
    lamps = [p["name"] for p in sim_def["parts"] if p["type"] == "lamp"]
    prev_lit, bad = None, 0
    steps = [(setup, None)] + [(setup + i + 1, s) for i, s in enumerate(events)]
    for n, s in steps:
        if s:
            sim.apply(s["action"], s["part"])
        _, d, _ = cli.message({"skill": "ask", "circuit": name, "question": "lit", "step": n},
                              label=f"lit @{n}")
        ok = d is not None and d["lit"] == sim.lit()
        bad += not ok
        check(ok, f"{name} @{n}: lit = simulator {sim.lit()}", f"agent {d and d['lit']}")
        for l in lamps:
            if l not in sim.lit():
                _, dn, _ = cli.message({"skill": "ask", "circuit": name, "question": "why_not", "part": l, "step": n},
                                       label=f"why_not {l} @{n}", show=700)
                good = dn is not None and not dn["lit"] and dn["fails"] is not None and len(dn["missing"]) > 0
                if good:
                    # what each part "left" must be what the simulator holds today, and what would be needed must NOT hold today
                    for f in dn["missing"]:
                        if f["left_by"]:
                            good &= sim.state_of(f["would_need"]) == f["left_by"]["fact"] and f["left_by"]["fact"] != f["would_need"]
                check(good, f"{name} @{n}: why_not {l} consistent with the simulated state", json.dumps(dn, ensure_ascii=False)[:200])
        if prev_lit is not None:
            for l in lamps:
                before, after = l in prev_lit[1], l in sim.lit()
                _, dc, _ = cli.message({"skill": "ask", "circuit": name, "question": "why", "part": l,
                                        "from_step": prev_lit[0], "to_step": n}, label=f"why {l} {prev_lit[0]}→{n}", show=500)
                expected = "no_change" if before == after else ("turned_on" if after else "turned_off")
                ok = dc is not None and dc["change"] == expected
                if ok and expected != "no_change":
                    ok = any(e["event"] == n for e in dc["events"])   # each step is a single event: it is the cause
                check(ok, f"{name} {l} {prev_lit[0]}→{n}: {expected}", f"agent {dc and dc['change']}")
        prev_lit = (n, sim.lit())
    return sim, bad


# =============================================================================================== sessions
def session_1_learn(cli):
    G.open("Session 1 · an agent arrives knowing nothing: reads the card and learns with `teach`",
           "It checks that what was learned works: the client replays the agent's examples and sees that the reply contains what was expected.")
    card = cli.get("/.well-known/agent-card.json")
    G.write(f">>> GET /.well-known/agent-card.json\n<<< name={card['name']} version={card['version']} url={card['url']}")
    G.write("    skills: " + ", ".join(s["id"] for s in card["skills"]))
    G.write("    capabilities: " + json.dumps(card["capabilities"]))
    G.write("    limits: " + json.dumps(card["limits"]))
    G.write("")
    check({s["id"] for s in card["skills"]} == {"converse", "define_circuit", "ask", "explain", "teach"}, "the card announces the 5 skills (converse + 4 JSON)")
    check(card["capabilities"]["streaming"] is False and card["capabilities"]["pushNotifications"] is False, "no streaming and no push")
    _, ind, _ = cli.message({"skill": "teach"}, label="teach (index)")
    _, fmt, _ = cli.message({"skill": "teach", "topic": "format"}, label="teach · format", show=1800)
    # the client learns the keys of each template and uses them to compose its messages
    templ = fmt["templates"]
    keys = {k: set(v) for k, v in templ.items()}
    check("skill" in templ["define_circuit"] and "parts" in keys["define_circuit"], "define template learned")
    cli.templates = keys
    _, pr, _ = cli.message({"skill": "teach", "topic": "questions"}, label="teach · questions", show=900)
    check(set(pr["questions"]) == {"lit", "supporting", "how", "why", "why_not", "timeline"}, "question catalogue learned (6 with timeline)")
    _, er, _ = cli.message({"skill": "teach", "topic": "errors"}, label="teach · errors", show=300)
    _, lim, _ = cli.message({"skill": "teach", "topic": "limits"}, label="teach · limits")
    _, exs, _ = cli.message({"skill": "teach", "topic": "examples"}, label="teach · examples", show=400)
    G.write("--- the learner replays each example and compares with what is expected ---")
    for ex in exs["examples"]:
        for i, step in enumerate(ex["steps"]):
            msg = step["message"]
            check(set(msg) <= keys[msg["skill"]], f"{ex['name']} step {i}: the message only uses keys of the template")
            t, d, _ = cli.message(msg, label=f"example {ex['name']} #{i}", show=500)
            ok = t is not None and t["status"]["state"] == "completed" and all(d.get(k) == v for k, v in step["expected"].items())
            check(ok, f"example {ex['name']} step {i}: the reply contains what is expected")
    # a typical learner error: uses 'lamp' instead of 'part'
    reject(cli, {"skill": "ask", "circuit": "minimal", "question": "why_not", "lamp": "l1"}, "unknown_field",
           "field 'lamp' (it is called 'part') — the error teaches")
    G.save("01_learn_format.txt")


def session_2_diary(cli):
    G.open("Session 2 · the diary circuit (src/circuit): same questions, same answers",
           "The circuit is transcribed from events.pl (what circuit.py recorded): 17 parts, 12 wires, 6 events. It is checked against answers.txt, diary.txt and its own simulator.")
    defn, events, setup = definition_from_events_pl()
    _, d, _ = cli.message({"skill": "define_circuit", "circuit": "diary", **defn, "events": events}, label="define diary (setup + the 6 events at once)", show=500)
    check(d is not None and d["step"] == 42 and d["assembly_until"] == 36 and d["lit"] == ["l1"], "diary: 42 events (36 of setup + 6 events) and at the end only l1 is lit", str(d))
    resp = read_answers()
    diary = read_diary()
    G.write("--- check against answers.txt (output of the original Prolog with SWI-Prolog) ---")
    # 1. lit(36), how(36,l2), supporting(36,l2)
    for question, goal, output in resp:
        m = re.match(r"lit\((\d+), Ls\)", goal)
        if m:
            _, d, _ = cli.message({"skill": "ask", "circuit": "diary", "question": "lit", "step": int(m.group(1))}, label=question)
            check("[" + ",".join(d["lit"]) + "]" == output, f"answers.txt · {goal}", f"{d['lit']} vs {output}")
            continue
        m = re.match(r"how\((\d+), (\w+), A\)", goal)
        if m:
            _, d, _ = cli.message({"skill": "ask", "circuit": "diary", "question": "how", "part": m.group(2), "step": int(m.group(1))},
                                  label=question, show=600)
            check(d["prolog_tree"] == output, f"answers.txt · {goal}: tree identical character by character", d["prolog_tree"][:100] + " vs " + output[:100])
            continue
        m = re.match(r"supporting\((\d+), (\w+), Es\)", goal)
        if m:
            _, d, _ = cli.message({"skill": "ask", "circuit": "diary", "question": "supporting", "part": m.group(2), "step": int(m.group(1))},
                                  label=question, show=600)
            lines = "\n".join(f"{e['event']}-{e['fact']}" for e in d["supporting"])
            check(lines == output, f"answers.txt · {goal}: same 9 supports", lines + " vs " + output)
            continue
        m = re.match(r"forall\(why\((\d+), (\d+), (\w+), R\)", goal)
        if m:
            n1, n2, l = int(m.group(1)), int(m.group(2)), m.group(3)
            _, d, _ = cli.message({"skill": "ask", "circuit": "diary", "question": "why", "part": l, "from_step": n1, "to_step": n2}, label=question, show=500)
            mine = f"{d['change']}([" + ",".join(f"{e['event']}-{e['fact']}" for e in d["events"]) + "])"
            check(mine == output, f"answers.txt · why({n1},{n2},{l})", mine + " vs " + output)
    G.write("")
    G.write("--- check against diary.txt: the 6 events are applied one by one and compared text by text ---")
    _, d, _ = cli.message({"skill": "define_circuit", "circuit": "diary_step", **defn}, label="define diary_step (setup only: another session, another interpreter)", show=300)
    check(d["step"] == 36 and d["lit"] == ["l2"], "diary_step: after setup, step 36, only l2")
    prev = setup
    sim = Simulator({**defn})
    # state before the first event
    texts = diary[setup]
    exp = [l for l in texts if l.startswith("Why is l1 unlit?")][0].split("? ", 1)[1]
    _, _, sentence = cli.message({"skill": "ask", "circuit": "diary_step", "question": "why_not", "part": "l1", "step": setup}, label="Why is l1 unlit?")
    check(sentence == exp, "diary @36: text of «Why is l1 unlit?» identical", f"{sentence!r} vs {exp!r}")
    _, _, sentence = cli.message({"skill": "ask", "circuit": "diary_step", "question": "supporting", "part": "l2", "step": setup}, label="What holds up l2?")
    exp = [l for l in texts if l.startswith("What holds up l2?")][0].split("? ", 1)[1]
    check(sentence == exp, "diary @36: text of «What holds up l2?» identical", f"{sentence!r} vs {exp!r}")
    for i, s in enumerate(events):
        _, d, _ = cli.message({"skill": "define_circuit", "circuit": "diary_step", "events": [s]}, label=f"event {s['action']} {s['part']}", show=300)
        n = setup + i + 1
        check(d["step"] == n, f"diary: event {i + 1} is step {n}", str(d["step"]))
        for l in diary[n]:
            m = re.match(r"Which lamps are lit\? My objects: .*The logic: (.*)\.$", l)
            if m:
                exp_lamps = re.findall(r"'(\w+)'", m.group(1))
                _, d, _ = cli.message({"skill": "ask", "circuit": "diary_step", "question": "lit", "step": n}, label=f"lit @{n}", show=200)
                check(d["lit"] == exp_lamps, f"diary @{n}: lit {exp_lamps}", str(d["lit"]))
            m = re.match(r"Why did (\w+) change\? (.*)$", l)
            if m:
                _, d, sentence = cli.message({"skill": "ask", "circuit": "diary_step", "question": "why", "part": m.group(1), "from_step": n - 1, "to_step": n},
                                             label=f"why {m.group(1)} {n - 1}→{n}", show=300)
                check(sentence == m.group(2), f"diary @{n}: text of the change of {m.group(1)} identical", f"{sentence!r} vs {m.group(2)!r}")
    G.write("")
    G.write("--- exhaustive cross-check against the simulator (all steps, all lamps) ---")
    _, bad = exhaustive_cross_check(cli, "diary", defn, events, setup)
    G.write("")
    G.write("--- explain: the indented proof tree of l2 at setup, and the reason for a switch-off ---")
    cli.message({"skill": "explain", "circuit": "diary", "question": "how", "part": "l2", "step": setup, "format": "indented"},
                label="explain how l2 @36", show=200)
    cli.message({"skill": "explain", "circuit": "diary", "question": "why", "part": "l1", "from_step": 37, "to_step": 38}, label="explain why l1 37→38", show=200)
    G.save("02_diary.txt")


def session_3_hallway(cli):
    G.open("Session 3 · the course's «hallway»: missing data, events, reset, cancellation",
           "The hallway: source, f1 (fuse), s1 (switch), c1, c2, l1, l2. It is sent WITHOUT the position of s1 to see the «needs input» state.")
    defn = {"parts": [{"name": "source", "type": "source"}, {"name": "f1", "type": "fuse"}, {"name": "s1", "type": "switch"},
                      {"name": "c1", "type": "wire"}, {"name": "c2", "type": "wire"}, {"name": "l1", "type": "lamp"}, {"name": "l2", "type": "lamp"}],
            "wires": [{"to": "c1", "from": "source"}, {"to": "c2", "from": "c1", "when": "ok(f1)"},
                      {"to": "l1", "from": "c2"}, {"to": "l2", "from": "c2", "when": "up(s1)"}]}
    t, d, _ = cli.message({"skill": "define_circuit", "circuit": "hallway", **defn}, label="define hallway (without the position of s1)")
    check(t["status"]["state"] == "input-required" and d is None, "«needs input» state (input-required) when the position of s1 is missing")
    tid = t["id"]
    t2, d, _ = cli.message({"positions": {"s1": "down"}}, task=tid, label="answers in the SAME task: s1 down")
    check(t2["id"] == tid and t2["status"]["state"] == "completed" and d["step"] == 15 and d["lit"] == ["l1"],
          "same task completed: step 15, only l1 is lit", str(d))
    check([h["state"] for h in t2["history"]] == ["submitted", "working", "input-required", "working", "completed"], "state history of the task with a pause",
          str([h["state"] for h in t2["history"]]))
    _, d, f = cli.message({"skill": "ask", "circuit": "hallway", "question": "why_not", "part": "l2"}, label="why is l2 NOT lit?")
    check(d["fails"] == "has_current(l2)" and d["missing"] == [{"would_need": "up(s1)", "left_by": {"event": 5, "fact": "down(s1)"}}],
          "l2: would need up(s1), but event 5 left down(s1) (what the course says)", str(d["missing"]))
    cli.message({"skill": "explain", "circuit": "hallway", "question": "how", "part": "l1"}, label="explain how l1 is lit", show=200)
    _, d, f = cli.message({"skill": "define_circuit", "circuit": "hallway", "events": [{"action": "raise", "part": "s1"}]}, label="raise s1", show=300)
    check(d["step"] == 16 and d["lit"] == ["l1", "l2"], "after raising s1: event 16, l1 and l2 are lit")
    _, d, f = cli.message({"skill": "ask", "circuit": "hallway", "question": "why", "part": "l2", "from_step": 15, "to_step": 16}, label="what changed in l2?")
    check(d["change"] == "turned_on" and d["events"] == [{"event": 16, "fact": "up(s1)"}] and f == "l2 turned on because of event 16 (up(s1)).",
          "l2 turned on because of event 16 (up(s1))  — the course's sentence")
    cli.message({"skill": "define_circuit", "circuit": "hallway", "events": [{"action": "blow", "part": "f1"}]}, label="blow f1", show=200)
    _, d, f = cli.message({"skill": "ask", "circuit": "hallway", "question": "why", "part": "l2", "from_step": 16, "to_step": 17}, label="what changed in l2 (16→17)?")
    check(d["change"] == "turned_off" and d["events"] == [{"event": 17, "fact": "blown(f1)"}], "l2 turned off because of event 17 (blown(f1))")
    _, d, f = cli.message({"skill": "ask", "circuit": "hallway", "question": "why_not", "part": "l1"}, label="why is l1 NOT lit?")
    check(d["missing"] == [{"would_need": "ok(f1)", "left_by": {"event": 17, "fact": "blown(f1)"}}], "l1: would need ok(f1), but event 17 left blown(f1)")
    cli.message({"skill": "ask", "circuit": "hallway", "question": "why_not", "part": "l1", "step": 15}, label="why_not l1 @15 (already lit)", show=300)
    resp, ms = cli.rpc("tasks/get", {"id": tid})
    check(resp["result"]["id"] == tid and resp["result"]["status"]["state"] == "completed" and len(resp["result"]["history"]) == 5, "tasks/get returns the task with its history of 5 states")
    G.write(f">>> tasks/get {tid}\n<<< {resp['result']['status']['label']} · history {[h['label'] for h in resp['result']['history']]}  [{ms:.1f} ms]\n")
    G.write("--- separate sessions: cross-check with the simulator and reset ---")
    events = [{"action": "raise", "part": "s1"}, {"action": "blow", "part": "f1"}]
    defn_ok = {"parts": [dict(p, **({"position": "down"} if p["type"] == "switch" else {})) for p in defn["parts"]], "wires": defn["wires"]}
    exhaustive_cross_check(cli, "hallway", defn_ok, events, 15)
    _, before, _ = cli.message({"skill": "ask", "circuit": "hallway", "question": "how", "part": "l2", "step": 16}, label="how l2 @16 (before the reset)", show=200)
    _, rd, _ = cli.message({"skill": "define_circuit", "circuit": "hallway", "action": "reset"}, label="reset the session")
    _, after, _ = cli.message({"skill": "ask", "circuit": "hallway", "question": "how", "part": "l2", "step": 16}, label="how l2 @16 (after the reset)", show=200)
    check(rd["reset"] and rd["step"] == 17 and before["prolog_tree"] == after["prolog_tree"], "reset rebuilds the identical session (same 17 events, same tree)")
    cli.message({"skill": "define_circuit", "circuit": "hallway", "action": "describe"}, label="describe", show=300)
    cli.message({"skill": "define_circuit", "circuit": "hallway", "action": "cancel"}, label="cancel the circuit", show=200)
    reject(cli, {"skill": "ask", "circuit": "hallway", "question": "lit"}, "unknown_circuit", "asking the circuit that is already canceled")
    # cancel a task that is waiting for data
    t, _, _ = cli.message({"skill": "define_circuit", "circuit": "other", "parts": [{"name": "s9", "type": "switch"}]}, label="define with a switch without position")
    resp, ms = cli.rpc("tasks/cancel", {"id": t["id"]})
    check(resp["result"]["status"]["state"] == "canceled", "tasks/cancel on a task that is waiting for data → canceled")
    G.write(f">>> tasks/cancel {t['id']}\n<<< {resp['result']['status']['label']}  [{ms:.1f} ms]\n")
    G.save("03_hallway.txt")


def session_4_invented(cli):
    G.open("Session 4 · an invented circuit: two switches IN SERIES and a BRANCH",
           "Source → fuse f1 → c1; series branch: c1 –a→ c2 –b→ c3 → l1; branch: c1 → c4 –(a down)→ l2; socket e1 hanging from c1. "
           "It exists in no file of the example: the only thing that validates the answers is the independent simulator and reasoning by hand.")
    defn = {"parts": [{"name": "source", "type": "source"}, {"name": "f1", "type": "fuse"},
                      {"name": "a", "type": "switch", "position": "up"}, {"name": "b", "type": "switch", "position": "down"},
                      {"name": "c1", "type": "wire"}, {"name": "c2", "type": "wire"}, {"name": "c3", "type": "wire"}, {"name": "c4", "type": "wire"},
                      {"name": "e1", "type": "socket"}, {"name": "l1", "type": "lamp"}, {"name": "l2", "type": "lamp"}],
            "wires": [{"to": "c1", "from": "source", "when": "ok(f1)"},
                      {"to": "c2", "from": "c1", "when": "up(a)"}, {"to": "c3", "from": "c2", "when": "up(b)"}, {"to": "l1", "from": "c3"},
                      {"to": "c4", "from": "c1"}, {"to": "l2", "from": "c4", "when": "down(a)"}, {"to": "e1", "from": "c1"}]}
    events = [{"action": "raise", "part": "b"}, {"action": "lower", "part": "a"}, {"action": "lower", "part": "b"},
              {"action": "raise", "part": "a"}, {"action": "raise", "part": "b"}, {"action": "blow", "part": "f1"}, {"action": "repair", "part": "f1"},
              {"action": "blow", "part": "l1"}]
    _, d, _ = cli.message({"skill": "define_circuit", "circuit": "series_branch", **defn}, label="define series_branch", show=500)
    # reasoning by hand: a up, b down -> l1 unlit (b up is missing), l2 unlit (a down is missing)
    check(d["lit"] == [] and d["step"] == 23, "setup: 23 events and no lamp is lit (a up, b down)", str(d))
    _, d, f = cli.message({"skill": "ask", "circuit": "series_branch", "question": "why_not", "part": "l1"}, label="why is l1 NOT lit?")
    check(d["missing"][0]["would_need"] == "up(b)" and d["missing"][0]["left_by"]["fact"] == "down(b)" and len(d["missing"]) == 1, "l1: would need up(b) (the only cut of the series)", str(d["missing"]))
    _, d, f = cli.message({"skill": "ask", "circuit": "series_branch", "question": "why_not", "part": "l2"}, label="why is l2 NOT lit?")
    check(d["missing"][0]["would_need"] == "down(a)" and d["missing"][0]["left_by"]["fact"] == "up(a)", "l2: would need down(a), but a is up", str(d["missing"]))
    setup = d["step"]
    for s in events:
        cli.message({"skill": "define_circuit", "circuit": "series_branch", "events": [s]}, label=f"{s['action']} {s['part']}", show=250)
    G.write("--- exhaustive cross-check against the simulator ---")
    sim, bad = exhaustive_cross_check(cli, "series_branch", defn, events, setup)
    G.write("--- explain: the proof of l1 when everything fits (step after raising b and a up) ---")
    _, d, f = cli.message({"skill": "explain", "circuit": "series_branch", "question": "how", "part": "l1", "step": setup + 5}, label="explain how l1 is lit @setup+5", show=100)
    check(d is not None and "indented" in d and any("up(a)" in l for l in d["indented"]) and any("up(b)" in l for l in d["indented"]),
          "the tree of l1 goes through the two switches in series")
    cli.message({"skill": "explain", "circuit": "series_branch", "question": "why", "part": "l2", "from_step": setup + 1, "to_step": setup + 2}, label="explain why l2", show=100)
    cli.message({"skill": "ask", "circuit": "series_branch", "question": "supporting", "part": "l1", "step": setup + 5}, label="supporting l1", show=700)
    # The same client, two live circuits, not mixed up
    _, d, _ = cli.message({"skill": "ask", "circuit": "diary", "question": "lit", "step": 36}, label="(the 'diary' session is still alive and separate) lit @36")
    check(d is not None and d["lit"] == ["l2"], "sessions are not mixed up: the diary still says l2")
    G.save("04_series_branch.txt")


def session_5_bad(cli):
    G.open("Session 5 · bad inputs: everything must be rejected with a clear code, without reaching Prolog",
           "Includes `halt.`-style injection in names, questions, conditions and free text; sizes; loops; non-existent parts.")
    base = {"skill": "define_circuit", "circuit": "bad"}
    ok_p = [{"name": "source", "type": "source"}, {"name": "c1", "type": "wire"}, {"name": "l1", "type": "lamp"}]
    ok_c = [{"to": "c1", "from": "source"}, {"to": "l1", "from": "c1"}]
    reject(cli, {**base, "parts": ok_p + [{"name": "source", "type": "wire"}], "wires": ok_c}, "duplicate_part", "duplicate part")
    reject(cli, {**base, "parts": ok_p + [{"name": "x", "type": "reactor"}], "wires": ok_c}, "unknown_type", "unknown part type")
    reject(cli, {**base, "parts": ok_p, "wires": ok_c + [{"to": "l1", "from": "c9"}]}, "unknown_part", "wire from a non-existent part (c9)")
    reject(cli, {**base, "parts": ok_p, "wires": ok_c + [{"to": "c1", "from": "l1"}]}, "wire_cycle", "wire loop c1↔l1")
    reject(cli, {**base, "parts": ok_p, "wires": [{"to": "c1", "from": "c1"}]}, "wire_cycle", "wire from a part to itself")
    reject(cli, {**base, "parts": ok_p, "wires": [{"to": "l1", "from": "c1", "when": "up(c1)"}]}, "invalid_condition", "up() on something that is not a switch")
    reject(cli, {**base, "parts": ok_p, "wires": [{"to": "l1", "from": "c1", "when": "ok(c1)"}]}, "invalid_condition", "ok() on a wire")
    reject(cli, {**base, "parts": ok_p, "wires": [{"to": "l1", "from": "c1", "when": "up(x)), halt, (a"}]}, "invalid_condition", "INJECTION in the condition")
    reject(cli, {**base, "parts": ok_p + [{"name": "halt.", "type": "wire"}], "wires": ok_c}, "invalid_name", "INJECTION `halt.` as a part name")
    reject(cli, {**base, "parts": ok_p + [{"name": "x), shell('ls'", "type": "wire"}], "wires": ok_c}, "invalid_name", "INJECTION shell(...) in a name")
    reject(cli, {**base, "parts": ok_p + [{"name": "Lamp1", "type": "lamp"}], "wires": ok_c}, "invalid_name", "name with a capital letter (in Prolog it would be a variable)")
    reject(cli, {"skill": "define_circuit", "circuit": "halt.", "parts": ok_p}, "invalid_name", "INJECTION `halt.` as a circuit name")
    reject(cli, {**base, "parts": ok_p, "wires": ok_c, "command": "halt."}, "unknown_field", "extra field 'command'")
    reject(cli, {**base, "parts": [{"name": "s", "type": "switch", "position": "middle"}], "wires": []}, "invalid_position", "position 'middle'")
    reject(cli, {**base, "parts": ok_p, "wires": ok_c, "events": [{"action": "raise", "part": "l1"}]}, "invalid_action", "raising something that is not a switch")
    reject(cli, {**base, "parts": ok_p, "wires": ok_c, "events": [{"action": "explode", "part": "l1"}]}, "invalid_action", "action 'explode'")
    reject(cli, {**base, "parts": ok_p, "wires": ok_c, "events": [{"action": "blow", "part": "zz"}]}, "unknown_part", "blowing a non-existent part")
    reject(cli, {**base, "parts": [{"name": f"p{i}", "type": "wire"} for i in range(61)]}, "too_large", "61 parts (cap 60)")
    reject(cli, {**base, "parts": ok_p, "wires": ok_c, "events": [{"action": "blow", "part": "l1"}] * 201}, "too_large", "201 events in one message (cap 200)")
    reject(cli, {"skill": "run", "command": "ls /"}, "unknown_skill", "non-existent skill `run`")
    reject(cli, {"skill": "ask", "circuit": "bad", "question": "run", "command": "ls /"}, "unknown_field", "question 'run' with a 'command' field (the field is rejected before the question)")
    # with a valid circuit
    cli.message({"skill": "define_circuit", "circuit": "good", "parts": ok_p, "wires": ok_c}, label="define 'good' (valid)", show=250)
    reject(cli, {"skill": "ask", "circuit": "good", "question": "run"}, "question_not_in_catalogue", "question not in the catalogue `run`")
    reject(cli, {"skill": "ask", "circuit": "good", "question": "halt."}, "question_not_in_catalogue", "INJECTION `halt.` as a question name")
    reject(cli, {"skill": "ask", "circuit": "good", "question": "why_not", "part": "l1), halt, (x"}, "invalid_name", "INJECTION in the argument 'part'")
    reject(cli, {"skill": "ask", "circuit": "good", "question": "why_not", "part": "l9"}, "unknown_part", "non-existent part l9")
    reject(cli, {"skill": "ask", "circuit": "good", "question": "why_not", "part": "c1"}, "not_a_lamp", "asking why a wire is not lit")
    reject(cli, {"skill": "ask", "circuit": "good", "question": "lit", "step": 99}, "invalid_step", "non-existent step 99")
    reject(cli, {"skill": "ask", "circuit": "good", "question": "lit", "step": "halt."}, "invalid_step", "step = text `halt.`")
    reject(cli, {"skill": "ask", "circuit": "good", "question": "lit", "step": True}, "invalid_step", "step = true (boolean)")
    reject(cli, {"skill": "ask", "circuit": "good", "question": "why", "part": "l1", "from_step": 5, "to_step": 3}, "invalid_step", "from_step after to_step")
    reject(cli, {"skill": "ask", "circuit": "good", "question": "how", "part": "l1", "from_step": 1}, "unknown_field", "field 'from_step' that 'how' does not accept")
    reject(cli, {"skill": "explain", "circuit": "good", "question": "how", "part": "l1", "format": "html"}, "invalid_format", "format 'html'")
    reject(cli, {"skill": "teach", "topic": "../../etc/passwd"}, "invalid_format", "topic = file path")
    reject(cli, {"skill": "define_circuit", "circuit": "good", "parts": ok_p, "wires": ok_c}, "circuit_exists", "redefining without replace:true")
    G.write("--- outside the message schema ---")
    # free text instead of a data part
    resp, ms = cli.rpc("message/send", {"message": {"role": "user", "parts": [{"kind": "text", "text": "halt."}]}})
    t = resp["result"]
    check(t["status"]["state"] in ("input-required", "completed") and not t["artifacts"], "free text `halt.` in a text part → it is now natural language (skill converse): treated as text, not executed")
    G.write(">>> parts:[{kind:text, text:'halt.'}]\n<<< (converse) " + t["status"]["state"] + ": not executed, the text never reaches Prolog\n")
    resp, ms = cli.rpc("message/send", {"message": {"role": "user", "parts": [{"kind": "data", "data": {"skill": "ask"}}, {"kind": "text", "text": "ls /"}]}})
    check(resp["result"]["status"]["state"] == "rejected", "two parts (data + text) → rejected")
    resp, ms = cli.rpc("message/send", {"message": {"role": "user", "parts": [{"kind": "data", "data": {"skill": "ask"}}], "taskId": "t-nonexistent"}})
    check(resp["result"]["status"]["message"]["error"]["code"] == "unknown_task", "made-up taskId → unknown_task")
    resp, ms = cli.rpc(None, None, raw=b"{this is not json")
    check(resp["error"]["code"] == -32700, "body that is not JSON → JSON-RPC error -32700")
    G.write(">>> body '{this is not json'\n<<< JSON-RPC error -32700 invalid_json\n")
    resp, ms = cli.rpc("system/run", {"cmd": "ls"})
    check(resp["error"]["code"] == -32601, "unknown JSON-RPC method → -32601")
    G.write(">>> method system/run\n<<< JSON-RPC error -32601\n")
    big = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"padding": "x" * 70000}}).encode()
    resp, ms = cli.rpc(None, None, raw=big)
    check(resp.get("_http") == 413 or "error" in resp, "70 KB message (> 64 KiB) → 413 too_large")
    G.write(f">>> body of {len(big)} bytes\n<<< HTTP {resp.get('_http')} {resp.get('error', {}).get('message')}\n")
    G.write("--- the A2A log records all this ---")
    log = cli.get("/log?n=5")
    for b in log:
        G.write("    " + json.dumps(b, ensure_ascii=False)[:230])
    check(len(log) == 5 and all("ms" in b for b in log if "state" in b) and any("state" in b for b in log), "the log has task, state, duration (the task lines; the translator also leaves its own)")
    G.save("05_bad_inputs.txt")


def time_cap_test():
    """The time cap is tested in-process (on the server it would be 5 s): with 0.2 ms the question must fail and the session reset."""
    from circuit_a2a import agent as ag
    from circuit_a2a import core
    a = ag.Agent("x", log="/dev/null")
    defn, events, setup = definition_from_events_pl()
    a.message({"message": {"parts": [{"kind": "data", "data": {"skill": "define_circuit", "circuit": "d", **defn}}]}})
    old = core.LIMITS["seconds_per_question"]
    core.LIMITS["seconds_per_question"] = 0.0002
    t = a.message({"message": {"parts": [{"kind": "data", "data": {"skill": "ask", "circuit": "d", "question": "how", "part": "l2"}}]}})
    core.LIMITS["seconds_per_question"] = old
    t2 = a.message({"message": {"parts": [{"kind": "data", "data": {"skill": "ask", "circuit": "d", "question": "lit"}}]}})
    return t["status"]["state"], t["status"].get("message", {}).get("error", {}).get("code"), t2["status"]["state"]


def cleanup(cli):
    """the client can be repeated against the same server: cancels the circuits it uses (if they exist)"""
    for n in ("minimal", "with_switch", "with_fuse", "diary", "diary_step", "hallway", "series_branch", "bad", "good", "other"):
        cli.message({"skill": "define_circuit", "circuit": n, "action": "cancel"}, label="cleanup")
    G.reg.clear()
    G.timings.clear()


# =============================================================================================== main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8923/")
    ap.add_argument("--launch", action="store_true", help="starts its own server (port 8924) with a separate log")
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    proc = None
    if a.launch:
        env = dict(os.environ, A2A_PORT="8924", A2A_TRANSLATOR="simulated", A2A_LOG=str(OUT / "client_json_log.jsonl"),
                   PYTHONPATH=str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""))
        proc = subprocess.Popen([sys.executable, str(HERE / "server.py")], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        a.url = "http://127.0.0.1:8924/"
        for _ in range(100):                      # wait until the card answers (the server tests its translator first)
            try:
                urllib.request.urlopen(a.url + ".well-known/agent-card.json", timeout=2).read()
                break
            except OSError:
                time.sleep(0.2)
    cli = Client(a.url)
    try:
        cleanup(cli)
        for f in (session_1_learn, session_2_diary, session_3_hallway, session_4_invented, session_5_bad):
            n0, f0 = G.checks, len(G.failures)
            t0 = time.perf_counter()
            f(cli)
            G.per_session[f.__name__] = {"checks": G.checks - n0, "failures": len(G.failures) - f0, "s": round(time.perf_counter() - t0, 2)}
            print(f"{f.__name__}: {G.checks - n0} checks, {len(G.failures) - f0} failures, {time.perf_counter() - t0:.2f} s")
        state, code, state2 = time_cap_test()
        print(f"time cap (in-process): {state}/{code}; afterwards, the reset session answers: {state2}")
        if state != "failed" or code != "timeout" or state2 != "completed":
            G.failures.append("time cap")
            G.checks += 1
        else:
            G.checks += 1
    finally:
        if proc:
            proc.terminate()
    res = {k: {"n": len(v), "mean_ms": round(sum(v) / len(v), 2), "max_ms": round(max(v), 2)} for k, v in sorted(G.timings.items(), key=lambda kv: str(kv[0]))}
    keys = ["define hallway (without the position of s1)", "answers in the SAME task: s1 down", "why is l2 NOT lit?", "explain how l1 is lit",
            "what changed in l2?", "why is l1 NOT lit?", "BAD: INJECTION `halt.` as a part name"]
    (OUT / "lamina_session.json").write_text(json.dumps({"steps": [dict(G.reg[k], label=k) for k in keys if k in G.reg], "sessions": G.per_session,
                                                         "checks": G.checks, "failures": len(G.failures), "url": a.url}, ensure_ascii=False, indent=1))
    (OUT / "timings.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    text = [f"checks: {G.checks}", f"failures: {len(G.failures)}"] + [f"  - {x}" for x in G.failures]
    text += ["", "timings per request type (HTTP round trip from the client, ms):"]
    text += [f"  {k:22s} n={v['n']:3d}  mean {v['mean_ms']:7.2f}  max {v['max_ms']:7.2f}" for k, v in res.items()]
    (OUT / "test_results.txt").write_text("\n".join(text) + "\n")
    print("\n".join(text))
    return len(G.failures)


if __name__ == "__main__":
    sys.exit(min(main(), 100))
