"""Tests of the `-- program` meta-interpreter (no network, no real model): the specification built from the file, the validator, the
rehearsal world, the retry loop, anchoring, `logi spec`, `logi program` and the anchored scripts running as the agent's observe and
device. The model is a RECORDED one: it answers with what Sonnet really answered (tests/fixtures/program_recorded/, taken from
tests/out/modelos_program*). Run: python tests/test_program.py"""
import io
import json
import shutil
import sys
import tempfile
import contextlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))
sys.dont_write_bytecode = True

from logitration import Idea, IdeaError, load, cli                    # noqa: E402
from logitration import programmer as P                                # noqa: E402
from logitration.rehearsal import Door, FakeWorld, WorldError          # noqa: E402

EX, REC = ROOT / "examples", HERE / "fixtures" / "program_recorded"
R = {"ok": 0, "fail": 0}


def check(cond, name, detail=""):
    R["ok" if cond else "fail"] += 1
    print(f"  [{'ok  ' if cond else 'FAIL'}] {name}" + ("" if cond else f"   -> {str(detail)[:400]}"))


def section(t):
    print("== " + t)


def work():
    """a fresh folder with modelos.logi and NO programs/"""
    d = Path(tempfile.mkdtemp(prefix="logi_program_"))
    shutil.copy(EX / "modelos.logi", d / "modelos.logi")
    return d / "modelos.logi"


class Recorded:
    """the model, recorded: answers from a list, remembers what it was asked"""
    def __init__(self, *answers):
        self.answers, self.asked = list(answers), []

    def __call__(self, system, prompt):
        self.asked.append(prompt)
        return self.answers[len(self.asked) - 1], {"model": "recorded", "usd": 0.01}


def rec(name):
    return (REC / name).read_text()


def run(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main(argv)
    return code, out.getvalue()


path = work()
idea = Idea(path)
agent = load(path)
tasks = P.build_tasks(idea, agent)
obs, act = tasks
world = P.idea_world(idea, "ollama")

# ============================================================================ 1. the specification, from what the user wrote
section("1. the specification is built from the file")
check([t.kind for t in tasks] == ["observe", "act"] and [t.name for t in tasks] == ["installed", "install"], "two programs: observe installed, act install", [t.name for t in tasks])
check(obs.args == ["M", "X"] and act.args == ["M", "X"], "the arguments come from the phrase installed(M, X) (and the act/2 command [install, M, X])", (obs.args, act.args))
check(obs.says == ["{X} is installed on {M}"], "the definition sentence is the user's phrase", obs.says)
check(obs.consts["M"][:1] == ["m1"] and "llama3" in obs.consts["X"], "the names the cases use come from the conversation", obs.consts)
check("mistral" in obs.consts["X"] and "m2" in obs.consts["M"], "...and from the pretend world", obs.consts)
h = P.header(obs)
check(h.startswith("def installed(world, M=None, X=None):") and "world.run(machine, command" in h and "Commands you may use: ollama" in h, "the header is the signature, the door and the commands allowed", h[:200])
check(P.header(act).startswith("def install(world, M, X):") and "Raise RuntimeError" in P.header(act), "an act script has no defaults and must raise when the command fails")
p0, p1, p2 = P.prompt(obs, 0), P.prompt(obs, 1), P.prompt(obs, 2)
check("SIGNATURE" not in p0 and "SIGNATURE" in p1 and "SIGNATURE" in p2, "level 0 is the name only; 1 adds the signature; 2 more")
check("client: is llama3 on m1?" in p2 and "client: is llama3 on m1?" not in p1 and "«{X} is installed on {M}»" in p2, "level 2 adds the user's words and the conversation examples")
p3 = P.prompt(obs, 2, [("def installed(): pass", "the signature must be ...")])
check("A previous attempt of yours" in p3 and "It was rejected: the signature must be" in p3, "a retry carries the previous script and its error")
cases = P.case_list(obs, world)
check(len(cases) == 10 and cases[0][0].startswith("installed(M=None, X=None)"), "the cases: everything, each name as a question, and the pairs", len(cases))
check(any("must RAISE" in c[0] for c in P.case_list(act, world)), "an act script is also tried on something that cannot be done (it must raise)")

# ============================================================================ 2. the validator
section("2. validator (AST)")
good, tag_bug = rec("installed_2.txt"), rec("installed_1.txt")
check(P.validate(good, obs) is None and P.validate(rec("install_1.txt"), act) is None, "the recorded scripts pass")
check("import of `os`" in P.validate("import os\ndef installed(world, M=None, X=None):\n    return []", obs), "import of os refused")
check("import of `subprocess`" in P.validate("from subprocess import run\ndef installed(world, M=None, X=None):\n    return []", obs), "from subprocess import refused")
check("not allowed" in P.validate("def installed(world, M=None, X=None):\n    return eval('1')", obs), "eval refused")
check("not allowed" in P.validate("def installed(world, M=None, X=None):\n    return open('/etc/passwd')", obs), "open refused")
check("attribute" in P.validate("def installed(world, M=None, X=None):\n    return world.__class__", obs), "dunder attribute refused")
check("world.shell does not exist" in P.validate("def installed(world, M=None, X=None):\n    return world.shell('ls')", obs), "the door has only run and machines")
check("command `rm` is not declared" in P.validate("def installed(world, M=None, X=None):\n    world.run('m1', 'rm', 'x')\n    return []", obs), "a command that was not declared via is refused")
check("signature must be" in P.validate("def installed(world, M, X):\n    return []", obs) or "defaults to None" in P.validate("def installed(world, M, X):\n    return []", obs), "wrong signature refused")
check("signature must be" in P.validate("def installed(world, A=None, B=None):\n    return []", obs), "wrong argument names refused")
check("there is no `def installed" in P.validate("def other(world, M=None, X=None):\n    return []", obs), "the function must exist")
check("syntax error" in P.validate("def installed(world M=None):", obs), "syntax error reported with its line")
check("top-level code" in P.validate("print(1)\ndef installed(world, M=None, X=None):\n    return []", obs), "code that runs on import refused")
check("class" in P.validate("class A: pass\ndef installed(world, M=None, X=None):\n    return []", obs).lower(), "classes refused")
check(P.strip_code("```python\ndef f(): pass\n```") == "def f(): pass", "markdown fences are stripped")

# ============================================================================ 3. the rehearsal world
section("3. rehearsal world and cases")
ok, rep, res = P.rehearse(good, obs, world)
check(ok and rep.startswith("10/10"), "the recorded second attempt passes every case", rep)
ok, rep, res = P.rehearse(tag_bug, obs, world)
check(not ok and "mistral:latest" in rep and rep.startswith("4/10"), "the recorded first attempt fails: it reports `mistral:latest` where the world says mistral", rep)
ok, rep, _ = P.rehearse("def install(world, M, X):\n    world.run(M, 'ollama', 'pull', X)\n    return 'ok'", act, world)
check(not ok and "hid it" in rep, "a script that hides a failed command is caught by the case that cannot be done", rep)
ok, rep, _ = P.rehearse("def installed(world, M=None, X=None):\n    while True:\n        pass", obs, world, seconds=1)
check(not ok and "Timeout" in rep, "an endless loop becomes a failure, not a hang", rep)
ok, rep, _ = P.rehearse("def installed(world, M=None, X=None):\n    return 'm1 has mistral'", obs, world)
check(not ok and "list of tuples" in rep, "wrong shape of the result is said", rep)
fw = FakeWorld({"m1": {"ollama": ["mistral"]}})
out = fw.run("m1", "ollama", "list").out
check(out.splitlines()[0].startswith("NAME") and "mistral:latest" in out, "the pretend ollama prints a table like the real one, with :latest")
check(fw.run("m1", "ollama", "pull", "nosuch").rc == 1 and fw.run("m1", "ollama", "pull", "llama3").rc == 0 and "llama3" in fw.items("m1", "ollama"), "pull: unknown model fails; a known one is added")
check(fw.run("m1", "ollama", "rm", "llama3").rc == 0 and fw.run("m1", "ollama", "rm", "llama3").rc == 1, "rm: removes, then says not found")
check(fw.run("m1", "zzz").rc == 127, "unknown command: rc 127")
try:
    fw.run("m9", "ollama", "list")
    check(False, "unknown machine raises")
except WorldError as e:
    check("unknown machine" in str(e), "unknown machine raises", e)
d = Door(fw, ["ollama"])
try:
    d.run("m1", "brew", "list")
    check(False, "the door refuses a command not declared")
except PermissionError as e:
    check("not declared" in str(e), "the door refuses a command not declared", e)
check(not hasattr(d, "state") and not hasattr(d, "items"), "the door does not show the oracle")

# ============================================================================ 4. the loop: a coding agent
section("4. the retry loop (recorded model)")
llm = Recorded(tag_bug, good)
out = P.program_one(obs, llm, world, level=2)
check(out.accepted and out.calls == 2 and not out.attempts[0]["accepted"], "first attempt fails the rehearsal, the second is accepted", [a["accepted"] for a in out.attempts])
check("mistral:latest" in llm.asked[1] and "It was rejected: it ran but the rehearsal failed" in llm.asked[1], "the second prompt carries the first script and the error of the rehearsal", llm.asked[1][-500:])
llm = Recorded("import os\ndef installed(world, M=None, X=None):\n    return []", good)
out = P.program_one(obs, llm, world)
check(out.accepted and out.attempts[0]["validation"].startswith("line 1: import of `os`") and out.attempts[0]["rehearsal"] is None, "a script the validator refuses is not even tried; the reason goes to the next prompt", out.attempts[0])
llm = Recorded("def installed(world, M=None, X=None):\n    return []", "def installed(world, M=None, X=None):\n    return []", "def installed(world, M=None, X=None):\n    return []")
out = P.program_one(obs, llm, world)
check(not out.accepted and out.calls == 3 and "NOT programmed after 3" in out.informe and "attempt 3" in out.informe, "three failures: the informe says what was wanted and what each attempt did", out.informe[:300])
b = P.Budget(1)
try:
    P.program_one(obs, Recorded(tag_bug, good), world, budget=b)
    check(False, "the budget of calls stops the loop")
except P.BudgetExhausted:
    check(b.used == 1, "the budget of calls stops the loop", b.used)

# ============================================================================ 5. anchoring and running the anchored scripts
section("5. anchoring; the scripts as observe and device")
out = P.program_one(obs, Recorded(good), world)
meta = P.anchor(idea, obs, out.src, out, model="recorded")
d = path.parent / "programs"
check((d / "installed.py").exists() and (d / "installed.json").exists() and meta["version"] == 1, "anchored: programs/installed.py + .json, version 1")
j = json.loads((d / "installed.json").read_text())
check("spec" in j and "reason" in j and j["says"] == obs.says and "modelos.logi:31" in j["reason"], "the metadata keeps the specification and the reason", list(j))
meta2 = P.anchor(idea, obs, out.src + "\n# again", out, model="recorded")
check(meta2["version"] == 2 and (d / "installed.v1.py").exists(), "anchoring again is version 2 and keeps v1")
P.anchor(idea, obs, out.src, out, model="recorded")
out_a = P.program_one(act, Recorded(rec("install_1.txt")), world)
P.anchor(idea, act, out_a.src, out_a, model="recorded")
ag = load(path, program=True)
check(ag.program_notes == [], "the agent loads the anchored programs without notes", ag.program_notes)
r = ag.say("which models are on m1?", session="s")
check("mistral is on m1" in r and "qwen2 is on m1" in r, "the anchored observe script is what the agent looks with", r)
r = ag.say("get llama3 installed on m1", session="s")
check("Done: llama3 is installed on m1" in r and "device: install m1 llama3" in r, "the anchored act script is the device of act/2", r)
code, text = run(["check", str(path), "--program"])
check(code == 0 and "4/4 turns answered as written" in text, "logi check --program: the conversation of the file passes with the anchored scripts", text[-300:])
ag2 = load(path, program=True)
r1 = ag2.say("is llama3 on m1?", session="t")
check("not on m1" in r1, "every agent starts from the rehearsal world as written in the file (the pull of the other session was on its copy)", r1)
(d / "installed.py").write_text((d / "installed.py").read_text() + "\n# edited by hand\n")
ag3 = load(path, program=True)
check(any("changed since it was anchored" in n for n in ag3.program_notes), "a script edited after anchoring is refused (hash), with the way out", ag3.program_notes)
(d / "installed.py").write_text((d / "installed.py").read_text().replace("# edited by hand", "").rstrip() + "\n")
bad_src = "# a\n# b\n# c\nimport os\ndef installed(world, M=None, X=None):\n    return []\n"
(d / "installed.py").write_text(bad_src)
j = json.loads((d / "installed.json").read_text())
import hashlib
j["sha256"] = hashlib.sha256(bad_src.split("\n", 3)[3].rstrip().encode()).hexdigest()
(d / "installed.json").write_text(json.dumps(j))
ag4 = load(path, program=True)
check(any("no longer validates" in n for n in ag4.program_notes), "a script that no longer validates is never run", ag4.program_notes)
ag5 = load(path)
check(ag5.observe is None, "without --program nothing of programs/ runs")

# ============================================================================ 6. the commands: logi spec, logi program, lint
section("6. logi spec / logi program / lint")
p6 = work()
code, text = run(["spec", str(p6)])
check(code == 0 and "PROGRAM installed  (observe)" in text and "PROGRAM install  (act)" in text and "def installed(world, M=None, X=None):" in text, "logi spec prints both specifications", text[:200])
check(not (p6.parent / "programs").exists(), "logi spec writes nothing and calls no model")
code, text = run(["spec", str(p6), "--level", "0"])
check("SIGNATURE" not in text and code == 0, "logi spec --level 0 shows the prompt without the signature")
code, text = run(["especificar", str(p6)])
check(code == 0 and "PROGRAM installed" in text, "Spanish alias: especificar")
check(any("not built" in x[2] for x in load(p6).lint if x[0] == "note"), "lint notes the programs that are not built yet")
import types
llm = Recorded(tag_bug, good, rec("install_1.txt"))
ns = types.SimpleNamespace(idea=str(p6), model="recorded", level=2, attempts=3, calls=12, force=False, trace=str(p6.parent / "tr"), llm=llm)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    code = cli.cmd_program(ns)
text = buf.getvalue()
check(code == 0 and "attempt 1: validator ok; rehearsal 4/10 cases" in text and "attempt 2: validator ok; rehearsal 10/10 cases -> ACCEPTED" in text and text.count("anchored programs/") == 2, "logi program: attempt, error, attempt, anchored, for each script", text)
tr = p6.parent / "tr" / "installed"
check(all((tr / f).exists() for f in ("spec.txt", "attempt1_1_prompt.txt", "attempt1_2_response.txt", "attempt1_3_validation.txt", "attempt1_4_rehearsal.txt", "attempt2_4_rehearsal.txt", "summary.json")), "the trace keeps spec, prompt, response, validation and rehearsal of every attempt", sorted(x.name for x in tr.iterdir()))
check(json.loads((tr / "summary.json").read_text())["calls"] == 2, "the summary counts the calls")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    code = cli.cmd_program(ns)
check("already anchored" in buf.getvalue() and len(llm.asked) == 3, "logi program does not build again what is anchored (no new call)", buf.getvalue())
ns.force, ns.llm = True, Recorded(good, rec("install_1.txt"))
with contextlib.redirect_stdout(io.StringIO()):
    cli.cmd_program(ns)
check(json.loads((p6.parent / "programs" / "installed.json").read_text())["version"] == 2, "--force anchors a new version")
p7 = work()
ns2 = types.SimpleNamespace(idea=str(p7), model="recorded", level=2, attempts=2, calls=12, force=False, trace=None, llm=Recorded("def installed(): pass", "def installed(): pass", "def install(world, M, X):\n    return 1", "x"))
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    code = cli.cmd_program(ns2)
check(code == 1 and "was NOT programmed" in buf.getvalue() and not (p7.parent / "programs" / "installed.py").exists(), "when it cannot, it says what is missing and anchors nothing; exit 1", buf.getvalue()[-500:])
ns3 = types.SimpleNamespace(idea=str(work()), model="recorded", level=2, attempts=3, calls=1, force=False, trace=None, llm=Recorded(tag_bug, good))
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    code = cli.cmd_program(ns3)
check(code == 1 and "stopped" in buf.getvalue(), "--calls caps the model calls in all", buf.getvalue()[-300:])

# ============================================================================ 7. the file: syntax of `-- program`
section("7. the `-- program` lines")
base = (EX / "modelos.logi").read_text()
def bad(repl, frag):
    p = work()
    p.write_text(base.replace(*repl))
    try:
        Idea(p)
        check(False, f"error expected: {repl[1][:30]}")
    except IdeaError as e:
        check(frag in str(e), f"{repl[1][:40]!r} -> {frag}", e)
bad(("observe installed(M, X)    via ollama", "observe installed(M, X)"), "say which command")
bad(("observe installed(M, X)    via ollama", "observe nothing(M, X) via ollama"), "has no word line")
bad(("act install                via ollama", "act borrow via ollama"), "no act(Fact, [borrow")
bad(("act install                via ollama", "frobnicate x"), "under `-- program` write")
bad(("rehearse m2: qwen2", "rehearse m2 qwen2"), "write  rehearse m1")
bad(("-- program", "-- programs"), "unknown meta-interpreter")
i2 = Idea(work())
check([d["via"] for d in i2.programs] == [["ollama"], ["ollama"]] and sorted(i2.rehearse) == ["m1", "m2"], "comments after a line are ignored; via and rehearse are read")
p8 = work()
p8.write_text(base.replace("rehearse m1: mistral qwen2                      # pretend machines to try them on\nrehearse m2: qwen2\n", ""))
check(any(x[0] == "warning" and "no `rehearse" in x[2] for x in load(p8).lint), "lint warns when there is nothing to try the scripts on")

# ============================================================================ 8. the `set` fact (installed(M, X): several at once; make adds it)
section("8. a `set` fact")
a8 = load(work(), observe=lambda: "mistral is installed on m1\nqwen2 is installed on m1")
r = a8.say("which models are on m1?", session="z")
check("mistral is on m1" in r and "qwen2 is on m1" in r, "two facts with the same first argument are both true (a plain `changes` fact would keep one)", r)
r = a8.say("get llama3 installed on m1", session="z")
check("Done: llama3 is installed on m1" in r, "make adds a member of the set (command: install m1 llama3)", r)

print(f"\nchecks: {R['ok'] + R['fail']}   failures: {R['fail']}")
sys.exit(1 if R["fail"] else 0)
