"""Tests of the idea file and the `logi` command (standard library only; no network, no LLM):

  1. the idea file: the three examples parse; errors say file:line
  2. `logi check`: every example answers its own conversation as written; a wrong expectation is reported
  3. the meta-interpreter: missing/ask, can_change (and its default), act + device (and a failing device), explain
  4. `logi try` with the input on stdin: conversation and :why :facts :rules :example :reload :help; Spanish aliases
  5. `logi serve` in a subprocess + a local client (LogicAgent.connect and `logi talk`); `--device` with a real program
Run: python tests/test_ideas.py      (exit code 1 if any check fails)
"""
import contextlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

from logitration import Idea, IdeaError, LogicAgent, load      # noqa: E402
from logitration import cli                                    # noqa: E402
from logitration.idea import check as replay                   # noqa: E402

EX = ROOT / "examples"
IDEAS = {"circuit": EX / "circuit.logi", "traffic_light": EX / "traffic_light.logi", "shop": EX / "shop.logi",
         "cluster": EX.parent / "tests" / "fixtures" / "cluster_v1.logi",          # the older layout (== logic, one == meta-interpreter)
         "cluster3": EX / "cluster.logi"}                                         # the three layers + observe
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
RESULTS = {"ok": 0, "fail": 0}
LOG = []


def check(cond, what, detail=""):
    RESULTS["ok" if cond else "fail"] += 1
    line = f"  [{'ok  ' if cond else 'FAIL'}] {what}" + ("" if cond or not detail else f"   -> {str(detail)[:400]}")
    LOG.append(line)
    print(line, flush=True)


def section(t):
    LOG.append("== " + t)
    print("== " + t, flush=True)


def tmp_idea(text):
    d = Path(tempfile.mkdtemp(prefix="logi_"))
    p = d / "idea.logi"
    p.write_text(text)
    return p


def logi(*args, stdin="", timeout=60):
    r = subprocess.run([sys.executable, "-m", "logitration", *args], input=stdin, capture_output=True, text=True,
                       env=ENV, timeout=timeout, cwd=str(ROOT))
    return r.returncode, r.stdout, r.stderr


def try_inproc(path, lines, **kw):
    """run `logi try` in this process with `lines` as stdin; returns what it printed"""
    a = type("A", (), {"idea": str(path), "translator": "simulated", "model": None, "device": kw.get("device")})
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.cmd_try(a, stdin=io.StringIO("".join(x + "\n" for x in lines)))
    return buf.getvalue()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def user_lines(p):
    return sum(1 for l in Path(p).read_text().splitlines() if l.strip() and not l.lstrip().startswith("#"))


# ============================================================================ 1. the idea file
section("1. the idea file")
for name, p in IDEAS.items():
    idea = Idea(p)
    total = len(p.read_text().splitlines())
    check(total < (80 if name == "cluster3" else 60) and idea.words and idea.conversation and idea.prolog.strip(),
          f"{name}: {total} lines in the file ({user_lines(p)} not comments), {len(idea.words)} words, "
          f"{len(idea.conversation)} client turns, meta: {', '.join(idea.meta_defined) or 'default'}")
c = Idea(IDEAS["circuit"])
check(c.name == "circuit" and c.about == "electrical circuits", "name: and about: are read")
check(c.words["state(X, V)"].get("changes") and c.words["state(X, V)"]["values"]["V"] == ["ok", "up", "down", "blown"],
      "a word line with [changes; V: ...] becomes a changing phrase with allowed values")
check(c.words["state(X, V)"]["say"][1] == "raise {X}{V=up}", "templates are split on |")
check("lit(L) :- lamp(L)" in c.prolog and "= {X} is" not in c.prolog, "rules go to Prolog, word lines do not")
check(Idea(EX / "circuit").path.name == "circuit.logi", "the .logi suffix can be left out")
for bad, frag in (("== logics\n", "unknown section"),
                  ("== logic\nlamp(X) = {X} is a lamp [often]\n", "unknown option"),
                  ("== conversation\nhello\n== logic\nlamp(X) = {X} is a lamp\n", "client:"),
                  ("== logic\nlamp(X) :- true.\n", "no word lines"),
                  ("title: x\n", "only 'name:' and 'about:'")):
    try:
        Idea(tmp_idea(bad))
        check(False, f"error expected: {frag}")
    except IdeaError as e:
        check(frag in str(e) and "idea.logi" in str(e), f"bad file -> {str(e)[:90]}")
try:
    load(tmp_idea("== logic\nlamp(X) = {X} is a lamp\nlit(L) = {L} is lit\nlit(L) :- lamp(L\n"))
    check(False, "Prolog error expected")
except IdeaError as e:
    check("idea.logi:4" in str(e) and "missing final period" in str(e), f"a Prolog error in the logic -> {str(e)[:90]}")

# ============================================================================ 2. logi check
section("2. logi check: the conversation is the test")
for name, p in IDEAS.items():
    rc, out, err = logi("check", str(p), "--observe")
    check(rc == 0 and "FAIL" not in out, f"logi check {p.name}: {out.strip().splitlines()[-1] if out.strip() else err[:200]}", out + err)
wrong = IDEAS["circuit"].read_text().replace("agent:  Now: nothing is lit.", "agent:  Now: l1 is lit.")
rc, out, _ = logi("check", str(tmp_idea(wrong)))
check(rc == 1 and "FAIL line 13" in out and "✗ Now: l1 is lit." in out and "| Loaded:" in out,
      "a wrong expectation fails, with its line, the missing text and what the agent said", out)
rc, out, _ = logi("comprobar", str(IDEAS["shop"]))
check(rc == 0, "Spanish alias: logi comprobar")
rc, out, err = logi("check", "nowhere.logi")
check(rc == 2 and "cannot read" in err, "a missing file: exit 2 and a clear error")

# ============================================================================ 3. the meta-interpreter
section("3. the meta-interpreter")
ag = load(IDEAS["circuit"])
r = ag.say("mains is a source. l1 is a lamp. sw is a switch. l1 gets current from mains if sw is up")
check(r.state == "input-required" and "Is sw up or down?" in r, "missing/1 + ask/2: the question is the user's words", r)
ag.say("sw is down")
ag.say("yes")
r = ag.say("make l1 lit")
check("Done: sw is up (command: relay sw up)" in r and r.data["result"]["holds"], "make: the way is found, act/2 gives the command", r)
r = ag.say("make l1 lit")
check(r.startswith("Already true"), "make on something already true does nothing", r)
r = ag.say("why is l1 lit?")
check("mains is the mains supply" in r and "mains is a source" not in r, "explain/2 replaces the proof of source(mains)", r)
r = ag.say("then lower sw")
r = ag.say("make l1 lit")
check("relay sw up" in r, "make again after an event", r)

# can_change blocks; the default allows every changing fact
shop = load(IDEAS["shop"])
for t in ("o1 is an order of lamp. o1 is paid", "yes"):
    shop.say(t)
r = shop.say("get o1 ready to ship")
check("not allowed to change: lamp is in stock" in r and "o1 is packed" in r, "can_change/1: the agent refuses what is not its own", r)
check(shop.say("then lamp is sold out").startswith("Added"), "the `not:` form is understood as a negated event")
# the cluster example: a node too small is explained with the numbers; only a runnable node may be launched; act sends the command
calls_c = []
cl = load(IDEAS["cluster"], device=lambda cmd: calls_c.append(cmd) or "ok")
for t in ("n2 is a node. n2 has 64 GB of ram. n2 has 500 GB of disk. n3 is a node. n3 has 256 GB of ram. n3 has 2000 GB of disk",
          "j1 is a job. j1 needs 120 GB of ram. j1 needs 300 GB of disk", "yes"):
    cl.say(t)
r = cl.say("why is j1 too big for n2?")
check("n2 has only 64 GB of ram and j1 needs 120" in r, "cluster: explain/2 says why j1 does not fit n2, with the numbers", r)
r = cl.say("get j1 running on n2")
check("cannot make it true by myself" in r and not calls_c, "cluster: can_change/1 refuses a node where the job does not fit; no command sent", r)
r = cl.say("get j1 running on n3")
check("Done" in r and calls_c == ["launch j1 n3"], "cluster: act/2 sends `launch j1 n3` to the device", (r, calls_c))
tl = load(IDEAS["traffic_light"])
for t in ("c1 is a crossing", "the light at c1 is amber", "yes"):
    tl.say(t)
r = tl.say("make c1 safe to cross")
check("lights c1 red" in r, "no can_change/1: by default the agent may change any changing fact", r)

# the device: called with the command; a failure stops and is told
calls = []
ag = load(IDEAS["traffic_light"], device=lambda cmd: calls.append(cmd) or "ok")
for t in ("c1 is a crossing", "the light at c1 is green", "yes"):
    ag.say(t)
r = ag.say("make c1 safe to cross")
check(calls == ["lights c1 red"] and "(device: lights c1 red -> ok)" in r, "device(command) is called when the agent acts", r)


def broken(cmd):
    raise RuntimeError("relay stuck")


ag = load(IDEAS["traffic_light"], device=broken)
for t in ("c1 is a crossing", "the light at c1 is green", "yes"):
    ag.say(t)
r = ag.say("make c1 safe to cross")
check(r.state == "failed" and "relay stuck" in r and "c1 is not safe to cross" in r and ag._conversation("main").world.last == 0,
      "a device failure: nothing is changed and the client is told", r)
r = ag.ask("make", "safe(c1)")
check(r["result"]["kind"] == "make", "make also works as a structured question (data part / ask())")
r = ag._ask_reply({"kind": "make", "goal": "safe(X)"}, "main")
check(r.state == "input-required" and "Say which one" in r, "make on a goal with a variable asks which one", r)
mini = load(tmp_idea("== logic\nlamp(X) = {X} is a lamp\nswitch(X, V) = the switch of {X} is {V} [changes; V: on off]\n"
                     "on(X) = {X} is on\non(X) :- lamp(X), switch(X, on).\n"))
for t in ("l1 is a lamp", "the switch of l1 is off", "yes"):
    mini.say(t)
check("Done: the switch of l1 is on" in mini.say("make l1 on"), "an idea with no meta-interpreter and no conversation works (all defaults)")
from logitration.translators import SYSTEM          # noqa: E402
check("make = the client wants the agent to" in SYSTEM and "timeline|make" in SYSTEM, "the LLM prompt knows the kind 'make'")

# ============================================================================ 4. logi try
section("4. logi try (input on stdin)")
out = try_inproc(IDEAS["circuit"], ["mains is a source. l1 is a lamp. sw is a switch. l1 gets current from mains if sw is up",
                                    "sw is down", "yes", "is l1 lit?", ":why", ":porque lit(l1)", ":facts", ":hechos", ":rules",
                                    ":example", ":nonsense", ":help", "make l1 lit", ":quit", "never read"])
check("\ncircuit: 8 words, meta-interpreters (check: explain/2; execute: act/2, can_change/1; complete: ask/2, missing/1)" in "\n" + out,
      "the banner says what was loaded (the three meta-interpreters)", out[:300])
check("agent> No: l1 is not lit." in out, "a question is answered", out)
check(out.count("It would need: sw is up") >= 2, ":why and :porque explain the last answer / a goal")
check("loaded:\n  mains is a source" in out, ":facts lists what the agent was told")
check(re.search(r"\n\s*\d+ lit\(L\) :- lamp\(L\)", out) is not None and "missing(state(S, _))" in out, ":rules shows the logic and the meta-interpreter with line numbers")
check("8/8 turns answered as written" in out, ":example replays the file's conversation")
check("unknown command :nonsense" in out and ":recargar" in out, "unknown commands get the help")
check("Done: sw is up" in out and "never read" not in out, ":quit leaves")
d = Path(tempfile.mkdtemp(prefix="logi_"))
p = d / "lamp.logi"
p.write_text("== logic\nlamp(X) = {X} is a lamp\nlit(L) = {L} is lit\nlit(L) :- lamp(L).\n")


class Reloading(io.StringIO):
    """stdin that edits the file just before :reload is read (as a user editing in another window)"""

    def readline(self):
        line = super().readline()
        if line.startswith(":reload"):
            p.write_text("== logic\nlamp(X) = {X} is a lamp\nbroken(X) = {X} is broken\nlit(L) = {L} is lit\n"
                         "lit(L) :- lamp(L), \\+ broken(L).\nbroken(l1).\n")
        return line


a = type("A", (), {"idea": str(p), "translator": "simulated", "model": None, "device": None})
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    cli.cmd_try(a, stdin=Reloading("l1 is a lamp\nyes\nis l1 lit?\n:reload\nis l1 lit?\n"))
out = buf.getvalue()
check("Yes: l1 is lit." in out and "replayed 3 line(s)" in out and "No: l1 is not lit." in out,
      ":reload re-reads the file and replays what was typed", out)
rc, out, _ = logi("probar", str(IDEAS["traffic_light"]), stdin="c1 is a crossing\nthe light at c1 is green\nyes\nmake c1 safe to cross\n")
check(rc == 0 and "Done: the light at c1 is red (command: lights c1 red)" in out, "python -m logitration probar (alias), real process", out)

# ============================================================================ 5. logi serve + clients, --device
section("5. logi serve (subprocess) and a local client")
port = free_port()
srv = subprocess.Popen([sys.executable, "-m", "logitration", "serve", str(IDEAS["circuit"]), "--port", str(port)],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=ENV, cwd=str(ROOT))
url = f"http://127.0.0.1:{port}/"
try:
    for _ in range(100):
        try:
            card = json.loads(urllib.request.urlopen(url + ".well-known/agent-card.json", timeout=1).read())
            break
        except OSError:
            time.sleep(0.1)
    else:
        card = {}
    check(card.get("name") == "circuit" and "electrical circuits" in card.get("description", ""), "the card is served with the idea's name and about")
    remote = LogicAgent.connect(url)
    r = remote.say("mains is a source. l1 is a lamp. sw is a switch. l1 gets current from mains if sw is up", session="t1")
    check(r.state == "input-required" and "Is sw up or down?" in r, "over A2A: the meta-interpreter asks", r)
    remote.say("sw is down", session="t1")
    remote.say("yes", session="t1")
    r = remote.say("make l1 lit", session="t1")
    check("relay sw up" in r and r.state == "completed", "over A2A: make acts", r)
    rc, out, _ = logi("talk", url, "why", "is", "l1", "lit?", "--session", "t1")
    check(rc == 0 and "l1 is lit, because:" in out and "[completed]" in out, "logi talk URL message: the same conversation from the command line", out)
    rc, out, _ = logi("hablar", url, "is", "l1", "lit?", "--session", "other")
    check("Tell me your situation first" in out, "another session starts empty (alias hablar)", out)
finally:
    srv.terminate()
    srv.wait(timeout=10)

section("5b. --device runs a real program with the command")
d = Path(tempfile.mkdtemp(prefix="logi_"))
log = d / "relay.log"
prog = d / "relay"
prog.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\necho switched\n")
prog.chmod(0o755)
rc, out, _ = logi("try", str(IDEAS["circuit"]), "--device", str(prog),
                  stdin="mains is a source. l1 is a lamp. sw is a switch. l1 gets current from mains if sw is up\nsw is down\nyes\nmake l1 lit\n")
check(log.exists() and log.read_text().strip() == "relay sw up" and "(device: relay sw up -> switched)" in out,
      "the device program received 'relay sw up' and its output is shown", out)
bad = d / "stuck"
bad.write_text("#!/bin/sh\necho stuck >&2\nexit 3\n")
bad.chmod(0o755)
rc, out, _ = logi("try", str(IDEAS["circuit"]), "--device", str(bad),
                  stdin="mains is a source. l1 is a lamp. sw is a switch. l1 gets current from mains if sw is up\nsw is down\nyes\nmake l1 lit\n")
check("device failed on 'relay sw up': exit 3: stuck" in out and "Now: l1 is not lit." in out, "a failing device program: exit code and message reach the client", out)

# ============================================================================ 6. what is missing is said (cluster.logi, one piece removed)
section("6. what the file or the client leaves unsaid is said, not answered blindly")
from logitration.lint import lint                               # noqa: E402

CL = IDEAS["cluster"].read_text()
FACTS = ["n1 is a node. n1 has 16 GB of ram. n1 has 100 GB of disk", "n2 is a node. n2 has 64 GB of ram. n2 has 500 GB of disk",
         "n3 is a node. n3 has 256 GB of ram. n3 has 2000 GB of disk", "j1 is a job. j1 needs 120 GB of ram. j1 needs 300 GB of disk",
         "j2 is a job. j2 needs 8 GB of ram. j2 needs 20 GB of disk", "j3 is a job. j3 needs 1500 GB of disk"]


def talk(text, turns, **kw):
    agent = load(tmp_idea(text), **kw)
    return agent, [agent.say(t) for t in turns]


def cluster_edit(old, new=""):
    assert old in CL, old
    return CL.replace(old, new, 1)


lints = lambda text: lint(Idea(tmp_idea(text)))
found = lambda items, level, frag: any(l == level and frag in m for l, _, m in items)

# case 1: a fact other nodes have is missing for n2
_, r = talk(CL, [FACTS[0], FACTS[1].replace(" n2 has 64 GB of ram.", ""), *FACTS[2:], "j3 needs 4 GB of ram", "yes", "is j1 runnable on n2?"])
check("missing: n2 has ? GB of ram (I have it for n1, n3)" in r[-1].text.replace("To decide this I am ", "missing: ").replace("missing: missing", "missing"),
      "case 1: no ram for n2 -> the answer says what it lacks", r[-1])
check(r[-1].data["result"]["missing_info"][0]["known_for"] == ["n1", "n3"], "case 1: the lack goes in the data too (missing_info)")
# the same question with everything told: no warning
_, r = talk(CL, [*FACTS, "j3 needs 4 GB of ram", "yes", "is j1 runnable on n2?"])
check(r[-1].text == "No: j1 is not runnable on n2." and "missing_info" not in r[-1].data["result"], "case 1 control: all told -> a plain answer", r[-1])
# case 6: no missing/ask: j3 has no ram and the answer says so
_, r = talk(cluster_edit("missing(need_ram(J, _)) :- job(J), \\+ need_ram(J, _).\n").replace("ask(need_ram(J, _), ['How many GB of ram does', J, 'need to run?']).\n", ""),
            [*FACTS, "yes", "is j3 runnable on n1?"])
check("j3 needs ? GB of ram (I have it for j1, j2)" in r[-1].text, "case 6: no missing/ask -> it loads, and the answer says what it lacks", r[-1])
# an edge list is not a property of every thing: no false alarm in the circuit
_, r = talk(IDEAS["circuit"].read_text(), ["mains is a source. l1 is a lamp. l2 is a lamp. sw is a switch. l1 gets current from mains", "sw is up", "yes", "is l1 lit?", "is l2 lit?"])
check(all("missing" not in x.text for x in r[-2:]), "no false alarm for something few things have (feeds)", [x.text for x in r[-2:]])

# case 5: a name nobody told the agent about
_, r = talk(CL, [*FACTS, "j3 needs 4 GB of ram", "yes", "is j1 runnable on n9?", "get j9 running on n3", "is j1 runnable on n3?"])
check(r[-3].text.startswith("I do not know n9: it is not in what you have told me") and r[-3].state == "input-required" and "No:" not in r[-3].text,
      "case 5: n9 is not answered No: it says it does not know it", r[-3])
check(r[-2].text.startswith("I do not know j9"), "case 5: the same for an order about j9", r[-2])
check(r[-1].text == "Yes: j1 is runnable on n3.", "case 5 control: a known name is answered", r[-1])

# case 9: an order with a part not said
text9 = cluster_edit("runnable(J, N)  =", "running(J)      = {J} is running\nrunnable(J, N)  =").replace("== 3.", "running(J) :- runs(J, _).\n\n== 3.")
_, r = talk(text9, [*FACTS, "j3 needs 4 GB of ram", "yes", "get j1 running"])
check(r[-1].state == "input-required" and "I cannot do that yet" in r[-1].text and "j1 is running on ?" in r[-1].text, "case 9: no Python trace: it says what is missing", r[-1])

# case 3: a rule that calls itself ends as a message, never as a crash
loop = cluster_edit("runnable(J, N) :- job(J)", "runnable(J, N) :- runnable(J, N).\nrunnable(J, N) :- job(J)")
t0 = time.time()
_, r = talk(loop, [*FACTS[:1], FACTS[3], "j1 needs 4 GB of ram", "yes", "is j1 runnable on n1?"], limits={"seconds_per_question": 1})
check(("took more than 1 s" in r[-1].text or "recursed too deeply" in r[-1].text) and time.time() - t0 < 30, "case 3: a rule in a loop -> message after the time limit, no crash", r[-1])

# case 8: syntax errors say the line
bad = cluster_edit("act(runs(J, N), [launch, J, N]).", "act(runs(J, N), [launch, J, N])")
try:
    load(tmp_idea(bad))
    check(False, "case 8: error expected")
except IdeaError as e:
    check("idea.logi:50:" in str(e) and "end of line 49" in str(e) and "explain(short_ram" in str(e), "case 8: the error says the line, the line itself and the missing dot", e)
for text_, frag in (("== logic\nlamp(X) = {X} is a lamp\nlit(L) = {L} is lit\n\nlit(L) :- lamp(L), 'x.\n", "unterminated"),
                    ("== logic\nlamp(X) = {X} is a lamp\nlit(L) = {L} is lit\nlit(L) :- lamp(L) $$$ .\n", "idea.logi:4"),
                    ("== logic\nlamp(X) = {X} is a lamp\nlit(L) = {L} is lit\nlit(L) :- lamp(L)\nlit(L) :- lamp(L).\n", "idea.logi:5")):
    try:
        load(tmp_idea(text_))
        check(False, f"syntax error expected: {frag}")
    except IdeaError as e:
        check(frag in str(e), f"syntax error -> {str(e).splitlines()[0][:100]}")

# lint: cases 4 and 5 and the meta line that does nothing
items = lints(cluster_edit("short_ram(J, N) = {N} lacks the ram of {J}\n"))
check(found(items, "warning", "explain/2 is about short_ram/2, which has no phrase"), "case 4: lint says explain is about a predicate with no phrase", items)
items = lints(CL + "retry(runs(J, N)).\n")
check(found(items, "warning", "retry/1 has facts but no phrase and nothing calls it"), "lint: a meta line that nothing reads", items)
items = lints(cluster_edit("short_ram(J, N) :- need_ram(J, R), ram(N, M), M < R.", "short_ram(J, N) :- need_ram(J, R), memory(N, M), M < R."))
check(found(items, "error", "memory/2 is called (in a rule of short_ram/2)"), "lint: a call to something undefined", items)
items = lints(cluster_edit("short_disk(J, N) :- need_disk(J, D), disk(N, K), K < D.", "short_disk(J, N) :- need_disk(J, D), K = 1, K < D."))
check(found(items, "warning", "the phrase of disk/2 is never used"), "lint: a phrase no rule reads", items)
items = lints(cluster_edit("short_ram(J, N) :- need_ram(J, R), ram(N, M), M < R.", "short_ram(J, N) :- need_ram(J), ram(N, M), M < R."))
check(found(items, "error", "need_ram/1 is called") and "only need_ram/2 exists" in str(items), "lint: wrong number of arguments", items)
items = lints(cluster_edit("act(runs(J, N), [launch, J, N]).", "act(runs(J, N), [launch, J, N], now)."))
check(found(items, "warning", "act/3 is not part of the meta-interpreter (act/2 is)"), "lint: act/3 instead of act/2", items)
# what lint cannot know (cases 2 and 7): nothing is invented
items2 = lints(cluster_edit("short_disk(J, N) :- need_disk(J, D), disk(N, K), K < D.\n"))
check(not [i for i in items2 if i[0] == "error"] and {m.split(" is never")[0] for l, _, m in items2 if l == "warning"} ==
      {"the phrase of disk/2", "the phrase of need_disk/2"} and any("short_disk/2" in m for l, _, m in items2 if l == "info"),
      "case 2: the forgotten rule is not found, only its symptom (disk and need_disk are read by nobody now)", items2)
items7 = lints(cluster_edit("can_change(runs(J, N)) :- runnable(J, N).\n"))
check(not [i for i in items7 if i[0] in ("error", "warning")], "case 7: a too-permissive rule is not invented", items7)
for name, p in IDEAS.items():
    items = lint(Idea(p))
    check(not [i for i in items if i[0] == "error"] and (name == "circuit" or not [i for i in items if i[0] == "warning"]),
          f"lint of the example {name}: no errors" + ("" if name != "circuit" else " (one real warning: the fuse phrase)"), items)

# the command
rc, out, err = logi("lint", str(IDEAS["circuit"]))
check(rc == 0 and "warning" in out and "fuse/1" in out and "note" in out and "info    the client states" in out and out.strip().endswith("note(s)"), "logi lint: lists warnings, notes and what the client states", out)
f = tmp_idea(bad)
rc, out, err = logi("lint", str(f))
check(rc == 2 and "idea.logi:50" in err, "logi lint on a file with a syntax error: the line, exit 2", err)
f = tmp_idea(CL + "retry(runs(J, N)).\n")
rc, out, err = logi("lint", str(f))
check(rc == 0 and "retry/1" in out, "logi lint: warnings do not fail (exit 0)", out)
f = tmp_idea(cluster_edit("short_ram(J, N) :- need_ram(J, R), ram(N, M), M < R.", "short_ram(J, N) :- need_ram(J, R), memory(N, M), M < R."))
rc, out, err = logi("lint", str(f))
check(rc == 1 and "error" in out, "logi lint: an error -> exit 1", out)
rc, out, err = logi("try", str(f), stdin=":quit\n")
check("error: idea.logi:" in out and "memory/2 is called" in out, "logi try says the lint warnings when it loads the file", out)
rc, out, err = logi("revisar", str(IDEAS["circuit"]))
check(rc == 0 and "fuse/1" in out, "Spanish alias: revisar")
out = try_inproc(IDEAS["circuit"], [":lint", ":quit"])
check("fuse/1" in out and "lint circuit.logi" in out, ":lint inside logi try", out)
rc, out, err = logi("check", str(IDEAS["circuit"]))
check(rc == 0 and "warning: circuit.logi:30" in out, "logi check says the lint warnings before the replay", out)

print(f"checks: {RESULTS['ok']}   failures: {RESULTS['fail']}")
(HERE / "out").mkdir(exist_ok=True)
(HERE / "out" / "test_ideas_results.txt").write_text("\n".join(LOG) + f"\nchecks: {RESULTS['ok']}   failures: {RESULTS['fail']}\n")
sys.exit(1 if RESULTS["fail"] else 0)
