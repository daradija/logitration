"""Tests of the TYPES in the arguments of the predicates and of the `[program]` mark (inspired by Clorm). No network, no real model:
the translator is a stub that returns what a model could return (a badly formed fact) and, to say it in the client's language, a
stub phrase. Also writes tests/out/medicion_tipos.txt: how many badly formed facts the engine refuses without types, with the
`[R: ram disk]` values it already had, and with types. Run: python tests/test_types.py"""
import contextlib
import io
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))
sys.dont_write_bytecode = True

from logitration import Idea, IdeaError, LogicAgent, Rejection, cli, load                    # noqa: E402
from logitration import programmer as P                                                      # noqa: E402
from logitration.lint import lint                                                            # noqa: E402
from logitration.phrases import PhraseError, Type                                            # noqa: E402
from logitration.translators import Translator, TranslatorError                              # noqa: E402

EX = ROOT / "examples"
R = {"ok": 0, "fail": 0}


def check(cond, name, detail=""):
    R["ok" if cond else "fail"] += 1
    print(f"  [{'ok  ' if cond else 'FAIL'}] {name}" + ("" if cond else f"   -> {str(detail)[:400]}"))


def section(t):
    print("== " + t)


def run(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main(argv)
    return code, out.getvalue()


def idea_file(text, name="t.logi"):
    d = Path(tempfile.mkdtemp(prefix="logi_types_"))
    (d / name).write_text(text)
    return d / name


TYPED = "capacity(N:name, R:ram|disk, G:int>=0) = {N} has {G} GB of {R}"
PLAIN = "capacity(N, R, G) = {N} has {G} GB of {R}"
VALUES = "capacity(N, R, G) = {N} has {G} GB of {R}      [R: ram disk]"
BASE = (EX / "cluster.logi").read_text()
assert TYPED in BASE


def variant(line):
    return idea_file(BASE.replace(TYPED, line))


# ============================================================================ 1. the types
section("1. the types")
check(Type("int>=0").check(3) is None and Type("int>=0").check(-5) and Type("int>=0").check("3") and Type("int>=0").check(True), "int>=0: 3 yes; -5, '3', True no")
check(Type("int>0<=100").lo == 1 and Type("int>0<=100").hi == 100 and Type("int>0<=100").check(101), "int>0<=100 means 1..100")
check(Type("ram|disk").check("ram") is None and Type("ram|disk").check("gpu") and Type("ram|disk").check(3), "ram|disk: ram yes; gpu and 3 no")
check(Type("name").check("n2") is None and Type("name").check("N2") and Type("name").check(7), "name: n2 yes; N2 and 7 no")
check(Type("any").check(7) is None and Type("any").check("x") is None, "any: everything")
for bad in ("float", "int>=", "Ram|disk", "int>5<2"):
    try:
        Type(bad)
        check(False, f"type {bad!r} is refused")
    except PhraseError:
        check(True, f"type {bad!r} is refused")
check(not Type("ram|disk").compatible(Type("int")) and Type("ram|disk").compatible(Type("name")) and Type("ram|disk").compatible(Type("disk|gpu"))
      and not Type("ram|disk").compatible(Type("gpu|tpu")) and not Type("int>=5").compatible(Type("int<2")), "types that no value shares are told apart")

# ============================================================================ 2. the file
section("2. the file: typed signatures, `type` lines")
a = load(variant(TYPED))
ph = a.vocab.by_sig[("capacity", 3)]
check(sorted(ph.types) == [0, 1, 2] and ph.types[2].describe() == "a whole number, 0 or more", "typed signature: the phrase holds its three types")
check("capacity(N, R, G)" in a.idea.word_lines and a.idea.words["capacity(N, R, G)"]["types"]["R"] == "ram|disk", "the key stays capacity(N, R, G); the types go aside")
named = idea_file(BASE.replace(TYPED, "type resource = ram | disk\ntype gb = int>=0\ncapacity(N:name, R:resource, G:gb) = {N} has {G} GB of {R}"))
an = load(named)
check(an.vocab.by_sig[("capacity", 3)].types[1].values == ["ram", "disk"] and an.vocab.by_sig[("capacity", 3)].types[2].lo == 0, "`type resource = ram|disk` is a name to reuse")
for line, why in (("capacity(N:nmae, R, G) = {N} has {G} GB of {R}", "unknown type"), ("type a = b\ntype a = c\ncapacity(N, R, G) = {N} has {G} GB of {R}", "two `type a`"),
                  ("type a = a\ncapacity(N:a, R, G) = {N} has {G} GB of {R}", "defined by itself")):
    try:
        load(idea_file(BASE.replace(TYPED, line)))
        check(False, f"{why}: refused")
    except IdeaError as e:
        check(" t.logi:" in " " + str(e) and why.split()[0] in str(e) or why in str(e), f"{why}: refused with its line", e)
check(len(a.lint) and not [x for x in a.lint if x[0] in ("error", "warning")], "the typed cluster has no lint errors or warnings")

# ============================================================================ 3. badly formed facts, counted
section("3. the eight badly formed facts of the Clorm study, with and without types")
CASES = [("arity", 'capacity(n2, ram)', "a missing argument"), ("text", 'capacity(n2, ram, "sixty four")', "GB written as text"),
         ("resource", "capacity(n2, gpu, 80)", "a resource that does not exist"), ("negative", "capacity(n2, ram, -5)", "a negative amount"),
         ("variable", "capacity(N2, ram, 64)", "a variable instead of a node"), ("decimal", "capacity(n2, ram, 64.5)", "a decimal"),
         ("typo", "capasity(n2, ram, 64)", "a predicate misspelled"), ("order", "capacity(ram, n2, 64)", "arguments in another order")]
GOOD = "capacity(n2, ram, 64)"
table = {}
for label, line in (("without types, without values", PLAIN), ("with `[R: ram disk]` (what the library already had)", VALUES), ("with types", TYPED)):
    k = load(variant(line)).knowledge
    refused, why = [], {}
    for code, text, _ in CASES:
        try:
            k.fact(text)
        except Rejection as e:
            refused.append(code)
            why[code] = f"{e.code}: {e.detail}"
    try:
        k.fact(GOOD)
        good = True
    except Rejection:
        good = False
    table[label] = (refused, why, good)
    print(f"     {label}: refuses {len(refused)}/8; the well formed one is {'accepted' if good else 'REFUSED'}")
check(len(table["without types, without values"][0]) == 5 and len(table["with `[R: ram disk]` (what the library already had)"][0]) == 7
      and len(table["with types"][0]) == 8, "5/8 without anything, 7/8 with [R: ram disk], 8/8 with types", {k: len(v[0]) for k, v in table.items()})
check(all(v[2] for v in table.values()), "the well formed fact is accepted in the three cases")
why = table["with types"][1]
check("G must be a whole number, 0 or more" in why["negative"] and "G must be a whole number" in why["text"] and "G must be a whole number" in why["decimal"]
      and "N must be a name" in why["variable"] and "R must be one of ram, disk" in why["resource"] and all(why[c].startswith("invalid_type") for c in ("negative", "text", "decimal", "variable", "resource", "order")),
      "each refusal says WHICH argument and what it must be", why)
check(why["arity"].startswith("unknown_fact") and why["typo"].startswith("unknown_fact"), "arity and typo: the vocabulary already refused them (unknown_fact)")
out = ["# medicion_tipos: facts mal formados del estudio de Clorm (logitration/clorm/out/05_mal_formados.txt) frente a Knowledge.fact()",
       f"# bien formado (control): {GOOD}"]
for label, (refused, why_, good) in table.items():
    out.append(f"== {label}: rechaza {len(refused)}/8; el control {'se acepta' if good else 'SE RECHAZA'}")
    for code, text, what in CASES:
        out.append(f"   {code:9} {text:36} {'RECHAZA ' + why_[code] if code in why_ else 'ACEPTA (entra al motor)'}")
(HERE / "out" / "medicion_tipos.txt").write_text("\n".join(out) + "\n")

# ============================================================================ 4. questions, observations, the client's language
section("4. questions, observations and the client's language")
k = a.knowledge
for text, ok in (("capacity(n2, ram, X)", True), ("capacity(n2, gpu, X)", False), ("capacity(N, R, -3)", False), ("runnable(j1, N)", True)):
    try:
        k.goal(text)
        check(ok, f"question {text}: accepted" if ok else f"question {text}: should be refused")
    except Rejection as e:
        check(not ok and e.code == "invalid_type", f"question {text}: refused ({e.code})" if not ok else f"question {text}: wrongly refused", e)
facts, unread = a.facts_from_text("n1 has 16 GB of ram\nn9 has 64 GB of gpu\ncapacity(n3, ram, -2).\nn4 is a node")
check(len(facts) == 2 and len(unread) == 2, "an observation with a wrong value: the good lines stay, the others are reported as unread", (facts, unread))


class Stub(Translator):
    """what a model could return for 'n2 has sixty four GB of ram' (a text where a number goes), and its phrasing in Spanish"""
    def __init__(self, phrase):
        self._phrase = phrase

    def interpret(self, text, context):
        return {"intent": "tell", "facts": ['capacity(n2, ram, "sixty four")'] if "sixty" in text else ["capacity(n2, gpu, 80)"] if "gpu" in text else ["capacity(n2, ram, 64)"],
                "events": [], "remove": [], "question": None, "doubts": []}

    def phrase(self, material):
        return self._phrase(material)


def say_with(phrase, text):
    ag = LogicAgent(rules=a.idea.prolog, phrases=a.idea.words, translator=Stub(phrase), name="t")
    return ag.say(text, session="s")


r = say_with(lambda m: "La RAM tiene que ser un número entero, de 0 o más." if "G must be" in m["rejected"] and m["client_message"] else None, "n2 tiene sixty GB de ram")
check(r == "La RAM tiene que ser un número entero, de 0 o más.", "a refused type is said by the translator in the client's language", r)
r = say_with(lambda m: None, "n2 tiene sixty GB de ram")
check(r.startswith("I could not use that:") and "G must be a whole number" in r, "the simulated translator has no language: English, with the argument and the type", r)


def boom(m):
    raise TranslatorError("down")
r = say_with(boom, "n2 tiene sixty GB de ram")
check(r.startswith("I could not use that:"), "if the translator cannot phrase it, English", r)
r = say_with(lambda m: "NO" if "R must be" not in m["rejected"] else "Esa clase de recurso no existe", "n2 gpu")
check(r == "Esa clase de recurso no existe", "a value outside an enumeration is a type refusal too", r)
r = say_with(lambda m: "NO", "ok fact")
check("could not use" not in r and "NO" != r, "a well formed fact does not go through the phrasing of refusals", r)

# ============================================================================ 5. lint: types
section("5. lint: types that disagree")
def lint_of(text):
    return lint(Idea(idea_file(text)))
def msgs(items, level="error"):
    return [m for lv, _, m in items if lv == level]
L = lint_of(BASE + "\ncapacity(n2, gpu, 80).\n")
check(any("R is gpu" in m and "one of ram, disk" in m for m in msgs(L)), "a fact written in the file with a value that is not of the type is an error", msgs(L))
L = lint_of(BASE + "\ncapacity(n2, ram, -5).\n")
check(any("G is -5" in m for m in msgs(L)), "a negative amount written in the file", msgs(L))
L = lint_of(BASE + "\nodd(J) :- capacity(_, R, G), demand(J, G, R).\n")
check(any("one variable stands for" in m for m in msgs(L)), "one variable that is a resource in one place and a number in another", msgs(L))
L = lint_of(BASE + "\nok(J) :- capacity(_, R, _), demand(J, R, _).\n")
check(not any("one variable" in m for m in msgs(L)), "the same variable in two places of the same type is fine", msgs(L))
L = lint_of(BASE.replace(TYPED, "capacity(N:name, R:ram|disk, G:int>=0) = {N} has {G} GB of {R}      [R: ram disk gpu]"))
check(any("two lists disagree" in m for m in msgs(L)), "`R:ram|disk` and `[R: ram disk gpu]` that disagree", msgs(L))
L = lint_of(BASE.replace(TYPED, PLAIN))
check(not msgs(L) and not msgs(L, "warning"), "without types nothing changes", L)

# ============================================================================ 6. [program]
section("6. [program]: facts that a program computes")
M = EX / "memoria.logi"
mi = Idea(M)
check(mi.program_marks == {"mem_free(N, MB)": (15, ["vm_stat"])} and [d["name"] for d in mi.programs] == ["mem_free"] and mi.programs[0]["via"] == ["vm_stat"],
      "`[program via vm_stat]` is the declaration of the observe script (no second line)", (mi.program_marks, mi.programs))
check(mi.programs[0]["types"] == {"N": "name", "MB": "int>=0"}, "the script knows the types of what it must return")
user_lines = [l for l in M.read_text().splitlines() if l.strip() and not l.lstrip().startswith("#")]
check(len(user_lines) <= 30, f"memoria.logi: {len(user_lines)} lines of the user (at most 30)", len(user_lines))
code, text = run(["lint", str(M)])
check(code == 0 and "computed by a program: mem_free/2 via vm_stat (built, v1)" in text and "written in the file (facts): need/2" in text, "lint says who provides each fact", text)
code, text = run(["spec", str(M)])
check(code == 0 and "WHO PROVIDES EACH FACT" in text and "computed by a program: mem_free/2" in text and "[program]" in text and "MB: a whole number, 0 or more" in text, "logi spec: who provides each fact, the mark and the types in the prompt", text[:300])
code, text = run(["check", str(M), "--program"])
check(code == 0 and "2/2 turns answered as written" in text, "logi check memoria.logi --program: 2/2 (the memory read by the anchored program on pretend machines)", text)
ag = load(M, program=True)
check(not ag.program_notes, "the anchored mem_free validates and loads without notes", ag.program_notes)
r = ag.say("where can j2 run?", session="m")
check("j2 is runnable on n2" in r and "n3" not in r, "j2 (60000 MB) fits only n2 (64000 MB free); n3 has 32000", r)
w = P.idea_world(mi, "vm_stat")
res = w.run("n2", "vm_stat")
check(res.rc == 0 and "page size of 16384 bytes" in res.out and "Pages free:" in res.out, "the pretend machine prints vm_stat as macOS does")
check(P.load_function(open(EX / "programs" / "mem_free.py").read(), P.build_tasks(mi, ag)[0])(P.Door(w, ["vm_stat"]), "n1") == [("n1", "16000")], "mem_free(n1) = 16000 MB", "")
# a program that returns something that is not of the type is refused when its rows enter the world
bad = load(M, observe=lambda: "n1 has -5 MB free\nn2 has 64000 MB free")
r = bad.say("where can j1 run?", session="b")
check("j1 is runnable on n2" in r and "n1" not in r.replace("on n2", ""), "a negative memory read by a program does not enter the rules", r)
# mark and `observe ... via` together
two = (M.read_text().replace("[program via vm_stat]", "[program via vm_stat]").replace("rehearse n1", "observe mem_free(N, MB) via vm_stat\nrehearse n1"))
try:
    Idea(idea_file(two, "memoria.logi"))
    check(False, "the command said twice is refused")
except IdeaError as e:
    check("say it once" in str(e), "the command said twice (mark and observe line) is refused", e)
one = M.read_text().replace("[program via vm_stat]", "[program]").replace("rehearse n1", "observe mem_free(N, MB) via vm_stat\nrehearse n1")
oi = Idea(idea_file(one, "memoria.logi"))
check(len(oi.programs) == 1 and oi.programs[0]["via"] == ["vm_stat"] and oi.programs[0]["marked"], "`[program]` + `observe ... via` under -- program: one declaration")
try:
    Idea(idea_file(M.read_text().replace("[program via vm_stat]", "[program]"), "memoria.logi"))
    check(False, "[program] without a command is refused")
except IdeaError as e:
    check("say which command" in str(e) and "[program via vm_stat]" in str(e), "[program] with no command anywhere is refused, with the way to say it", e)
un = M.read_text().replace("[program via vm_stat]", "").replace("-- program", "-- program\nobserve mem_free(N, MB) via vm_stat")
L = lint(Idea(idea_file(un, "memoria.logi")))
check(any("does not say so" in m and "[program]" in m for m in msgs(L, "note")), "an observe script whose phrase is not marked: lint asks for the mark", L)
check(Idea(EX / "modelos.logi").program_marks == {"installed(M, X)": (19, [])} and not [m for m in msgs(lint(Idea(EX / "modelos.logi")), "note") if "does not say so" in m], "modelos.logi: `[set; program]` + `observe installed via ollama`")

print(f"\nchecks: {R['ok'] + R['fail']}   failures: {R['fail']}")
sys.exit(1 if R["fail"] else 0)
