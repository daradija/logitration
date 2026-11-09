"""Tests of the 07-11 redesign (no network, no LLM): the three layers (facts / rules / meta-interpreters), several named
meta-interpreters, the agent that LOOKS at the world (observe:), a variable in a parameter, max/min, and the agent that
teaches how to ask. Run: python tests/test_design.py"""
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))
sys.dont_write_bytecode = True

from logitration import Idea, IdeaError, load                      # noqa: E402
from logitration import engine as E, terms as T                    # noqa: E402
from logitration.idea import check as replay                       # noqa: E402
from logitration.lint import lint                                  # noqa: E402
from logitration import cli                                        # noqa: E402

EX, FIX = ROOT / "examples", HERE / "fixtures"
R = {"ok": 0, "fail": 0}


def check(cond, name, detail=""):
    R["ok" if cond else "fail"] += 1
    print(f"  [{'ok  ' if cond else 'FAIL'}] {name}" + ("" if cond else f"   -> {str(detail)[:400]}"))


def section(t):
    print("== " + t)


def tmp(text, name="idea.logi"):
    d = Path(tempfile.mkdtemp(prefix="logi_design_"))
    (d / name).write_text(text)
    return d / name


def solve(pl, q):
    g = E.deref(list(E.read_clauses(q + "."))[0][0])
    out = []
    for _ in pl.solve_closed(g):
        out.append(T.text(E.copy_term_(g)))
    return out


# ============================================================================ 1. the engine: aggregate_all
section("1. engine: aggregate_all (max/min/count/sum/bag/set)")
pl = E.Prolog()
pl.consult_text("c(n1,ram,16). c(n2,ram,64). c(n3,ram,256). c(n1,disk,100). c(n3,disk,2000).")
check(any("max(256,n3)" in x for x in solve(pl, "aggregate_all(max(C,N), c(N,ram,C), M)")), "max(E, Witness): the node with the most ram")
check(any("min(16,n1)" in x for x in solve(pl, "aggregate_all(min(C,N), c(N,ram,C), M)")), "min(E, Witness): the node with the least ram")
check(any(x.endswith(",2000)") for x in solve(pl, "aggregate_all(max(C), c(_,disk,C), M)")), "max(E): the biggest disk")
check(any(x.endswith(",16)") for x in solve(pl, "aggregate_all(min(C), c(_,ram,C), M)")), "min(E): the smallest ram")
check(any(x.endswith(",336)") for x in solve(pl, "aggregate_all(sum(C), c(_,ram,C), S)")), "sum(E)")
check(any(x.endswith(",3)") for x in solve(pl, "aggregate_all(count, c(_,ram,_), S)")), "count")
check(any(x.endswith(",0)") for x in solve(pl, "aggregate_all(count, c(_,gpu,_), S)")), "count of nothing is 0")
check(solve(pl, "aggregate_all(max(C), c(_,gpu,C), M)") == [], "max of nothing fails (there is no biggest)")
check(any(x.endswith("[disk,ram])") for x in solve(pl, "aggregate_all(set(R), c(_,R,_), L)")), "set(T): sorted, no repeats")
check(any(x.endswith("[ram,disk])") for x in solve(pl, "aggregate_all(bag(R), c(n1,R,_), L)")), "bag(T): in order")
try:
    solve(pl, "aggregate_all(median(C), c(_,ram,C), M)")
    check(False, "an unknown aggregate spec is an error")
except E.PrologError:
    check(True, "an unknown aggregate spec is an error")
pl.consult_text("big(R, N) :- aggregate_all(max(C, M), c(M, R, C), max(_, N)). small(R, N) :- aggregate_all(min(C, M), c(M, R, C), min(_, N)).")
check(any("big(ram,n3)" in x for x in solve(pl, "big(ram, N)")) and any("small(disk,n1)" in x for x in solve(pl, "small(disk, N)")),
      "inside a rule, with a bound resource")

# ============================================================================ 2. the three layers in the file
section("2. the file: facts / rules / meta-interpreters (-- check, -- execute, -- complete)")
cl = Idea(EX / "cluster.logi")
check(cl.raw["facts"] and cl.raw["rules"] and cl.raw["meta"] and not any(l.strip() for _, l in cl.raw["logic"]),
      "cluster.logi has the three layers and no old `== logic`")
check(cl.interpreters == {"check": ["explain/2"], "execute": ["act/2", "can_change/1"], "complete": ["ask/2", "missing/1"]},
      "each meta-interpreter says what the file wrote for it", cl.interpreters)
check(cl.observe == "python3 observe_cluster.py" and cl.observe_line, "`observe:` is read, with its line")
check(set(cl.part_of.values()) == {"check", "execute", "complete"}, "every meta line knows its subsection")
old = Idea(FIX / "cluster_v1.logi")
check(old.raw["logic"] and not old.raw["facts"] and not old.part_of and old.meta_defined, "the older layout (== logic, one == meta-interpreter) still loads")
for f in ("circuit_v1", "cluster_v1"):
    a = load(FIX / f"{f}.logi")
    res = replay(a)
    check(all(not m for _, _, _, m, _ in res), f"older layout {f}: its conversation answers as written ({len(res)} turns)")
# the same agent in both layouts gives the same answers (circuit)
new, oldc = load(EX / "circuit.logi"), load(FIX / "circuit_v1.logi")
same = all(str(new.say(c, session="s")) == str(oldc.say(c, session="s")) for c, _, _ in new.idea.conversation)
check(same, "circuit in the new layout and in the old one: the same words in every reply")
for bad, frag in (("== 1. conversation\nclient: x\n== 2. facts\n-- check\n", "only goes inside the meta-interpreters section"),
                  ("== 4. meta-interpreters\n-- guess\n", "unknown meta-interpreter '-- guess'"),
                  ("== 2. facts\nlamp(X) = {X} is a lamp\nobserve: a b\nobserve: c d\n", "two 'observe:' lines")):
    try:
        Idea(tmp(bad))
        check(False, f"error expected: {frag}")
    except IdeaError as e:
        check(frag in str(e) and "idea.logi:" in str(e), f"bad file says where: {str(e)[:100]}")
misplaced = (EX / "cluster.logi").read_text().replace("-- check\n", "-- execute\n", 1)
items = lint(Idea(tmp(misplaced)))
check(any(l == "warning" and "explain/2 is read by the `check` meta-interpreter" in m for l, _, m in items),
      "lint: a meta line under the subsection of another meta-interpreter is a warning", items)
check(not any(l in ("error", "warning") for l, _, _ in lint(cl)), "lint: cluster.logi has no errors or warnings", lint(cl))
check(any("looks at the world" in m and "--observe" in m for _, _, m in lint(cl)), "lint says that observe runs only with --observe")

# ============================================================================ 3. the agent looks at the world
section("3. observe: the agent looks at the cluster; the client only asks")
agent = load(EX / "cluster.logi", observe=True)
r = agent.say("where can j1 run?", session="a")
check("j1 is runnable on n3" in r and r.state == "completed", "no description from the client: it looked, and answered", r)
check(r.data["result"]["found"] == ["runnable(j1,n3)"], "variable in a parameter: the answer is the list of nodes (data)", r.data)
r = agent.say("where can j2 run?", session="a")
check(r.data["result"]["found"] == ["runnable(j2,n1)", "runnable(j2,n2)", "runnable(j2,n3)"] and "n1" in r and "n2" in r and "n3" in r,
      "a job that fits several nodes: all of them", r)
r = agent.say("which node has the most ram?", session="a")
check(r.data["result"]["found"] == ["most(n3,ram)"], "max over the facts: most ram", r.data)
r = agent.say("which node has the most disk?", session="a")
check(r.data["result"]["found"] == ["most(n3,disk)"], "max over the facts: most disk", r.data)
r = agent.say("is j1 runnable on n2?", session="a")
check(r.startswith("No"), "yes/no still works", r)
r = agent.say("get j1 running on n3", session="a")
check("launch j1 n3" in r and r.state == "completed", "a concrete instance: execute sends the command", r)
off = load(EX / "cluster.logi")
r = off.say("where can j1 run?", session="b")
check("Tell me your situation first" in r, "without observe=True the file's observe: does not run (the agent knows nothing)", r)
check(off.observe is None and off.idea.observe, "...and it says it is declared but off")
rc = cli.main(["check", str(EX / "cluster.logi")])
check(rc == 1, "logi check without --observe fails (nothing was observed)")
check(cli.main(["check", str(EX / "cluster.logi"), "--observe"]) == 0, "logi check --observe: the conversation of the file passes")

# the gap: j3 has no ram figure
r = agent.say("where can j3 run?", session="c")
check(r.state == "input-required" and "How many GB of ram does j3 need to run?" in r and "To answer that I need to know" in r,
      "complete: the question is about j3 and j3 lacks its ram, so it asks", r)
r2 = agent.say("where can j1 run?", session="d")
check("j1 is runnable" in r2 and "need to know" not in r2, "a gap about another job does not stop this question", r2)
r2 = agent.say("which node has the most ram?", session="d")
check("n3 has the most ram" in r2 and "need to know" not in r2, "a question that names no job is not stopped by j3's gap", r2)
r = agent.say("j3 needs 4 GB of ram", session="c")
check("This is what I understood" in r and "j3 needs 4 GB of ram" in r and "n1 is a node" not in r,
      "the confirmation lists what the client said, not what the agent saw", r)
r = agent.say("yes", session="c")
check("j3 is runnable on n3" in r, "after the yes, the question that was waiting is answered", r)

# the world is looked at again at every question
state = {"n": 0}
texts = ["n1 is a node\nn1 has 16 GB of ram\nn1 has 100 GB of disk\nj1 is a job\nj1 needs 4 GB of ram\nj1 needs 4 GB of disk\n",
         "n1 is a node\nn1 has 2 GB of ram\nn1 has 100 GB of disk\nj1 is a job\nj1 needs 4 GB of ram\nj1 needs 4 GB of disk\n"]


def look():
    state["n"] += 1
    return texts[min(state["n"] - 1, 1)]


ag = load(EX / "cluster.logi", observe=look)
a1 = ag.say("is j1 runnable on n1?", session="e")
a2 = ag.say("is j1 runnable on n1?", session="e")
check(a1.startswith("Yes") and a2.startswith("No") and state["n"] == 2, "the cluster changed between questions: the second answer follows it", (a1, a2))
check(a2.data["result"]["holds"] is False and "n1 has 2 GB of ram" not in a2, "(observed facts are replaced, not piled up)")

# what the observation prints is only facts of the vocabulary
junk = "n1 is a node\nn1 has 16 GB of ram\nrm -rf /\nfoo(bar).\nn1 has 16 GB of gpu\nram(n1, 5).\nj1 is a job\n"
ag = load(EX / "cluster.logi", observe=lambda: junk)
ag.say("which node has the most ram?", session="f")
facts, unread = ag.facts_from_text(junk)
check(len(facts) == 3 and [u[0] for u in unread] == ["rm -rf /", "foo(bar).", "n1 has 16 GB of gpu", "ram(n1, 5)."],
      "lines that are not facts of the phrases are ignored (and counted); a value outside [R: ram disk] is not a fact", (facts, unread))
facts, unread = ag.facts_from_text("capacity(n9, ram, 7).\n# a comment\n\nn9 is a node")
check([T.text(f) for f in facts] == ["capacity(n9,ram,7)", "node(n9)"], "Prolog form and sentence form are both read", facts)

def boom():
    raise RuntimeError("exit 2: no route to the cluster")


r = load(EX / "cluster.logi", observe=boom).say("where can j1 run?", session="g")
check(r.state == "input-required" and "could not look at the cluster of computers for language-model jobs" in r and "no route" in r,
      "the observation fails: it says so, it does not answer from nothing", r)
d = tmp((EX / "cluster.logi").read_text().replace("observe: python3 observe_cluster.py", "observe: python3 nowhere.py"))
r = load(d, observe=True).say("where can j1 run?", session="h")
check("could not look" in r, "a command that does not exist is a clean failure", r)
# the client's own facts survive the next look
ag = load(EX / "cluster.logi", observe=True)
for t in ("j9 is a job", "j9 needs 1 GB of ram", "j9 needs 1 GB of disk", "yes"):
    ag.say(t, session="i")
r = ag.say("where can j9 run?", session="i")
check("j9 is runnable on n1" in r, "what the client told stays in the world after a new look", r)
# the real, local machine: one node `here` with the true free disk and ram
import subprocess  # noqa: E402
out = subprocess.run([sys.executable, str(EX / "observe_cluster.py"), "--local"], capture_output=True, text=True).stdout
facts, unread = ag.facts_from_text(out)
check(any("here" in T.text(f) for f in facts) and not unread, "observe_cluster.py --local adds this machine as node `here`", unread)
ram_here = [l for l in out.splitlines() if l.startswith("here has") and "ram" in l]
check(ram_here and int(ram_here[0].split()[2]) > 0, "...with a real amount of ram", out[-200:])

# ============================================================================ 4. the agent teaches how to ask
section("4. teaching how to ask")
ag = load(EX / "cluster.logi", observe=True)
r = ag.say("what can I ask?", session="t")
ex = r.data.get("examples", [])
check(r.state == "input-required" and ex and ex[0] == "where can j1 run?", "“what can I ask?”: examples made from the phrases, with names it sees", r)
check("get j1 running on n3" in ex, "...and the actions it may do (only where the rules allow: n3, not n1)", ex)
ok = True
for e in ex:
    rr = ag.say(e, session="t2")
    ok = ok and "did not understand" not in rr and rr.state != "rejected"
check(ok, "every example it offers is understood (and answered) by the agent", ex)
r = ag.say("put j1 on the best node", session="t")
check("I did not understand that" in r and "You can ask me, for example" in r and "where can j1 run?" in r and r.data["examples"],
      "something it does not understand is not a dry refusal: it teaches how to ask", r)
check("I do not run instructions" in r, "...and still says it does not run instructions or code", r)
r = ag.say("help", session="t")
check("I look at the cluster" in r and "where can j1 run?" in r, "help of an agent that looks at the world: ask, do not tell", r)
c = load(EX / "circuit.logi")
r = c.say("what can I ask?", session="u")
check("You can tell me" in r and "You can ask me, for example" in r and "lit" in r, "an agent that is told things: tell and ask are both taught", r)
r = c.say("blah blah", session="u")
check("did not understand" in r and "You can ask me, for example" in r, "circuit: not understood -> examples too", r)
tl = load(EX / "traffic_light.logi")
r = tl.say("what can I ask?", session="v")
check("You can ask me, for example" in r and "safe to cross" in r, "traffic_light: one question, one example", r)

# ============================================================================ 5. a variable in the parameter, in any file
section("5. variable in a parameter / max-min in a small file of its own")
small = """name: shelf
about: a shelf
== 1. conversation
client: where can b1 go?
agent: b1 is fits on s1
== 2. facts
shelf(S) = {S} is a shelf
box(B) = {B} is a box
space(S, N) = {S} has room for {N} boxes
size(B, N) = {B} is {N} boxes wide
== 3. rules
fits(B, S) = {B} is fits on {S} | where can {B} go{S=?}
roomiest(S) = {S} is the roomiest shelf
fits(B, S) :- box(B), shelf(S), size(B, W), space(S, N), W =< N.
roomiest(S) :- aggregate_all(max(N, X), space(X, N), max(_, S)).
"""
a = load(tmp(small, "shelf.logi"))
for t in ("s1 is a shelf", "s2 is a shelf", "b1 is a box", "s1 has room for 2 boxes", "s2 has room for 9 boxes", "b1 is 3 boxes wide", "yes"):
    r = a.say(t, session="w")
r = a.say("where can b1 go?", session="w")
check(r.data["result"]["found"] == ["fits(b1,s2)"], "a variable in the second argument, from a template of {S=?}", r)
r = a.say("which shelf is the roomiest shelf?", session="w")
check(r.data["result"]["found"] == ["roomiest(s2)"], "aggregate_all(max(N, X), ...) in the rules of a new file", r)

# ============================================================================ 6. the three interpreters are real objects; try shows them; explain on a partly bound goal
section("6. named meta-interpreters, :reglas, explain on a goal with variables")
from logitration.interpreters import Check, Complete, Execute      # noqa: E402
conv_ = load(EX / "cluster.logi", observe=True)._conversation("n")
check(isinstance(conv_.check, Check) and isinstance(conv_.execute, Execute) and isinstance(conv_.complete, Complete),
      "a conversation has its three meta-interpreters by name: check, execute, complete")
check(all(hasattr(conv_.check, m) for m in ("answer", "summary")) and hasattr(conv_.execute, "make") and
      all(hasattr(conv_.complete, m) for m in ("observe", "examples", "teach", "gaps_for", "ask_gaps")),
      "...and each one has its own functions")
r_ = subprocess.run([sys.executable, "-m", "logitration", "try", str(EX / "cluster.logi"), "--observe"], input=":reglas\n:quit\n", capture_output=True, text=True,
                    cwd=str(ROOT), env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"})
check(r_.returncode == 0 and "== facts" in r_.stdout and "== rules" in r_.stdout and "-- check" in r_.stdout and "-- execute" in r_.stdout
      and "-- complete" in r_.stdout, ":reglas shows facts, rules and the meta-interpreters with their subsections", r_.stdout[-600:])
check("meta-interpreters (check: explain/2; execute: act/2, can_change/1; complete: ask/2, missing/1)" in r_.stdout and "looking at the world with" in r_.stdout,
      "the banner of try names the three meta-interpreters and the observation", r_.stdout[:400])
ag = load(EX / "cluster.logi", observe=True)
r = ag.say("why is j1 too big for n2?", session="x")
check("n2 has only 64 GB of ram and j1 needs 120" in r, "explain/2 is used even when the goal still has a variable (short(j1, n2, _))", r)
big = "\n".join(f"n{i} is a node" for i in range(1, 450))
try:
    ag.facts_from_text(big)
    check(False, "an observation of more than 400 facts is refused")
except Exception as e:
    check("too_large" in str(getattr(e, "code", e)) or "more than" in str(e), "an observation of more than 400 facts is refused", e)
d = tmp((EX / "cluster.logi").read_text().replace("runnable(J, N) = {J} is runnable on {N} | where can {J} run{N=?}", "runnable(J, N) = where can {J} run{N=?} | {J} is runnable on {N}"))
try:
    load(d, observe=True)
    check(False, "the first template cannot fix a value ({N=?}): error expected")
except IdeaError as e:
    check("first template" in str(e), "the first template is how the agent speaks: it cannot hold {N=?}", e)

print(f"\nchecks: {R['ok'] + R['fail']}   failures: {R['fail']}")
sys.exit(1 if R["fail"] else 0)
