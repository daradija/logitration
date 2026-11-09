# Copyright (c) 2026 David Ragel
# SPDX-License-Identifier: BUSL-1.1
# Licensed under the Business Source License 1.1; see LICENSE.
"""The `-- program` meta-interpreter: when the agent lacks a SCRIPT (a way to look at the world, a way to change it), it writes the
specification of the script from what you already wrote, asks a language model for the Python, checks it, tries it in a
rehearsal world and, if it fails, tries again with the error: a coding agent in a loop, with a specification you can read first.

    -- program
    observe installed(M, X)  via ollama      # a script that LOOKS: the rows of installed(M, X); M, X given or free
    act install              via ollama      # the script behind  act(installed(M, X), [install, M, X])
    rehearse m1: mistral qwen2               # the pretend world the scripts are tried in

    logi spec    IDEA     the specifications it would hand to the model (no model, nothing written)
    logi program IDEA     build them: spec -> prompt -> model -> validate -> try -> (error -> again, 3 calls) -> anchor in programs/

The pieces, in the order they happen:  Task (the specification, from the file) -> header() + prompt() -> a model (LLM: callable
(system, prompt) -> (text, meta)) -> validate() (AST: imports, calls, the signature) -> rehearse() (cases taken from your
conversation, run against FakeWorld) -> program_one() (the loop) -> anchor() (programs/NAME.py + NAME.json) -> Programs (the
anchored scripts, as the `observe:` of the agent and its device).
"""
import ast
import hashlib
import json
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from logitration import engine as E
from logitration import terms as T
from logitration.rehearsal import Door, FakeWorld, WorldError

IMPORTS = ("re", "json")                               # the only modules a script may import
BANNED_NAMES = {"eval", "exec", "compile", "open", "__import__", "input", "getattr", "setattr", "delattr", "globals", "locals",
                "vars", "exit", "quit", "breakpoint", "memoryview", "type", "object", "super", "classmethod", "staticmethod"}
DOOR = ("run", "machines")
MAX_SOURCE = 6000
SAFE = "len str int float bool list tuple set dict frozenset sorted min max sum any all enumerate range zip map filter isinstance abs round " \
       "reversed repr ord chr divmod Exception RuntimeError ValueError KeyError IndexError TypeError AttributeError PermissionError StopIteration".split()
LEVELS = {0: "name", 1: "name + signature", 2: "name + signature + your words + conversation examples"}
SYSTEM = ("You write ONE small Python script for an agent that looks after machines. Answer with the code only: no explanations, no "
          "markdown fences. Python 3.9, standard library; the script may import only re and json.")
RE_DECL = re.compile(r"^(observe|act|rehearse)\s+(.*?)\s*$")
RE_OBS = re.compile(r"^([a-z][a-z0-9_]*)\s*\(\s*([A-Z]\w*(?:\s*,\s*[A-Z]\w*)*)\s*\)\s*(?:via\s+(.+))?$")
RE_ACT = re.compile(r"^([a-z][a-z0-9_]*)\s*(?:via\s+(.+))?$")
RE_REH = re.compile(r"^(\w+)(?:\s+(\w+))?\s*:\s*(.*)$")
INVENTORY = {"ollama": "nosuchmodel-xyz", "brew": "nosuchformula-xyz"}


class ProgramError(ValueError):
    pass


class BudgetExhausted(RuntimeError):
    pass


# ====================================================================== the specification (from the idea file)
class Task:
    """One script the agent lacks. kind: observe | act. args: the argument names, in order. says: how the file says it
    (templates). via: the commands the script may run on a machine. consts: {arg name: values seen in the conversation}.
    examples: [(client line, [agent lines])] of the conversation. The oracle (what the rehearsal world says is true) is
    rows(world) -> set of tuples (observe) | effect(world, args) -> bool (act); bad: argument tuples that must raise."""

    def __init__(self, kind, name, args, says, via, line=0, fact=None, consts=None, examples=(), rows=None, effect=None, bad=(), file="", twice=True, pairs=None, types=None):
        self.kind, self.name, self.args, self.says, self.via = kind, name, list(args), list(says), list(via)
        self.line, self.fact, self.file = line, fact, file
        self.types = dict(types or {})                      # {argument name: type spec} of the phrase: what the answer must look like
        self.marked = False                                  # the phrase says `[program]`
        self.consts = {a: list(v) for a, v in (consts or {}).items()}
        self.examples = list(examples)
        self.rows, self.effect, self.bad = rows, effect, [tuple(b) for b in bad]
        self.pairs = [tuple(p) for p in pairs] if pairs else None      # act: the argument tuples to try (default: the names seen, crossed)
        self.twice = twice                                  # an act script is safe to run twice (a removal is not: the second time there is nothing to remove)

    @property
    def sentence(self):
        return self.says[0] if self.says else f"{self.name}({', '.join(self.args)})"

    def call(self, vals):
        return f"{self.name}({', '.join(f'{a}={v!r}' for a, v in zip(self.args, vals))})"


def _sig_words(idea, name, arity):
    for key, w in idea.words.items():
        m = re.match(r"^([a-z]\w*)\s*(?:\((.*)\))?$", key)
        if m and m.group(1) == name and (len(m.group(2).split(",")) if m.group(2) else 0) == arity:
            return [a.strip() for a in (m.group(2) or "").split(",") if a.strip()], w
    return None, None


def parse_declarations(idea):
    """the lines under `-- program`: [(line number, text)] -> (decls, rehearse). A decl is a dict kind/name/args/via/line/fact."""
    decls, reh = [], {}
    for n, text in idea.program_lines:
        text = re.sub(r"\s+#.*$", "", text.strip())                 # a comment after the line
        m = RE_DECL.match(text)
        where = f"{idea.path.name}:{n}"
        if not m:
            raise ProgramError(f"{where}: under `-- program` write: observe pred(Args) via cmd | act name via cmd | rehearse machine: items")
        kind, rest = m.groups()
        if kind == "rehearse":
            r = RE_REH.match(rest)
            if not r:
                raise ProgramError(f"{where}: write  rehearse m1: item item  (or  rehearse m1 brew: item item)")
            reh.setdefault(r.group(1), {})[r.group(2) or "*"] = r.group(3).split()
            continue
        if kind == "observe":
            o = RE_OBS.match(rest)
            if not o:
                raise ProgramError(f"{where}: write  observe pred(M, X) via command")
            name, args, via = o.groups()
            args = [a.strip() for a in args.split(",")]
            names, w = _sig_words(idea, name, len(args))
            if w is None:
                raise ProgramError(f"{where}: observe {name}/{len(args)} has no word line in the facts (say what it means: {name}({', '.join(args)}) = {{{args[0]}}} ...)")
            decls.append(dict(kind="observe", name=name, args=names, via=(via or "").replace(",", " ").split(), line=n, fact=name, says=w["say"],
                              types=w.get("types") or {}))
        else:
            a = RE_ACT.match(rest)
            if not a:
                raise ProgramError(f"{where}: write  act name via command   (name = the first word of a command in act/2)")
            name, via = a.groups()
            found = _act_clause(idea, name)
            if found is None:
                raise ProgramError(f"{where}: `act {name}`: no act(Fact, [{name}, ...]) in the execute meta-interpreter says what {name} does")
            fact, argvars = found
            fargs = [E.deref(x) for x in T.args(fact)]
            names, w = _sig_words(idea, T.signature(fact)[0], len(fargs))
            if w is None:
                raise ProgramError(f"{where}: act {name}: the fact {T.signature(fact)[0]}/{len(fargs)} has no word line")
            order = []
            for v in argvars:
                if not any(v is x for x in fargs):
                    raise ProgramError(f"{where}: act {name}: each word of the command after `{name}` must be an argument of the fact")
                order.append(names[[i for i, x in enumerate(fargs) if x is v][0]])
            decls.append(dict(kind="act", name=name, args=order, via=(via or "").replace(",", " ").split(), line=n,
                              fact=T.signature(fact)[0], says=w["say"], phrase_args=names))
    for key, (n, via) in idea.program_marks.items():                  # [program via CMD] in the phrase: the script that computes this fact
        name, _, rest = key.partition("(")
        names = [a.strip() for a in rest.rstrip(") ").split(",") if a.strip()]
        d = next((d for d in decls if d["kind"] == "observe" and d["name"] == name and len(d["args"]) == len(names)), None)
        if d is not None:
            if via:
                raise ProgramError(f"{idea.path.name}:{n}: {name} says its command twice (`[program via ...]` here and `observe {name}(...) via` "
                                   f"at line {d['line']}): say it once")
            d["marked"] = n
            continue
        _, w = _sig_words(idea, name, len(names))
        decls.append(dict(kind="observe", name=name, args=names, via=via, line=n, fact=name, says=w["say"], types=w.get("types") or {}, marked=n))
    for d in decls:
        if not d["via"]:
            raise ProgramError(f"{idea.path.name}:{d['line']}: {d['kind']} {d['name']}: say which command it may run: `via ollama`"
                               + (" (`[program via vm_stat]`)" if d.get("marked") else ""))
    return decls, reh


def _act_clause(idea, name):
    for t, _ in idea.clauses:
        if isinstance(t, E.Struct) and t.name == "act" and len(t.args) == 2:
            try:
                cmd = E.to_pylist(t.args[1])
            except E.PrologError:
                cmd = None
            if cmd and isinstance(E.deref(cmd[0]), E.Atom) and E.deref(cmd[0]).name == name:
                fact = E.deref(t.args[0])
                return fact, [E.deref(x) for x in cmd[1:]]
    return None


def idea_world(idea, tool):
    """the rehearsal world of the file: `rehearse m1: a b` lines (the tool defaults to the first `via` command)"""
    if not idea.rehearse:
        return None
    return FakeWorld({m: {(t if t != "*" else tool): items for t, items in d.items()} for m, d in idea.rehearse.items()})


def build_tasks(idea, agent):
    """the specifications of the file's programs: [Task]. The conversation gives the examples and the names the cases use."""
    from logitration.translators import SimulatedTranslator
    sim = SimulatedTranslator()
    sim.bind(agent.vocab, agent.knowledge.main)
    seen = {}
    for client, _, _ in idea.conversation:
        try:
            it = sim.interpret(client, {})
        except Exception:
            continue
        goals = ([it["question"]["goal"]] if isinstance(it.get("question"), dict) and it["question"].get("goal") else []) + \
            list(it.get("facts") or []) + [e[4:] if e.startswith("not ") else e for e in it.get("events") or []]
        for g in goals:
            try:
                t, _ = T.parse(g, allow_vars=True)
            except T.TermError:
                continue
            ph = agent.vocab.get(t)
            if ph is None:
                continue
            for nm, a in zip(ph.argnames, T.args(t)):
                if not isinstance(a, E.Var):
                    v = str(T.value(a))
                    if v not in seen.setdefault(nm, []):
                        seen[nm].append(v)
    tasks = []
    for d in idea.programs:
        tool = d["via"][0]
        t = Task(d["kind"], d["name"], d["args"], d["says"], d["via"], line=d["line"], fact=d["fact"], file=idea.path.name,
                 consts={a: seen.get(a, []) for a in d["args"]}, examples=[(c, e) for c, e, _ in idea.conversation],
                 types=d.get("types"))
        t.marked = bool(d.get("marked"))
        if tool in INVENTORY and len(t.args) == 2:                       # an inventory: (machine, item) rows, the item is bare ("llama3")
            t.rows = lambda w, tool=tool: {(m, x) for m in w.machines() for x in w.items(m, tool)}
            t.effect = lambda w, a, tool=tool: a[1] in w.items(a[0], tool)
            t.bad = [(sorted(idea.rehearse)[0] if idea.rehearse else "m1", INVENTORY[tool])] if t.kind == "act" else []
            w = idea_world(idea, tool)
            if w is not None:                                           # the machines and items of the pretend world are cases too
                for a_, vals in ((t.args[0], w.machines()), (t.args[1], sorted({x for m in w.machines() for x in w.items(m, tool)}))):
                    t.consts[a_] = t.consts[a_] + [v for v in vals if v not in t.consts[a_]]
        tasks.append(t)
    return tasks


# ====================================================================== the header and the prompts
def header(task):
    """the signature: the decisive piece (instalador_meta measured it: 87 % of first answers right with it, 0 % without)"""
    door = (f"world.run(machine, command, *args)   runs ONE command on ONE machine, without a shell: world.run(\"m1\", \"{task.via[0]}\", ...).\n"
            "    It returns an object with .rc (int, the exit code), .out (str, stdout) and .err (str, stderr). It raises RuntimeError if the machine is unknown.\n"
            f"    Commands you may use: {', '.join(task.via)}. Nothing else is reachable (no files, no network, no subprocess).\n"
            "world.machines()                       returns the list of machine names (str).")
    if task.kind == "observe":
        params = ", ".join(f"{a}=None" for a in task.args)
        doc = (f'Return the list of ALL tuples ({", ".join(task.args)}) of str that are true now and agree with the arguments that are not None '
               f'(None means "any": it is a variable). Read-only: change nothing. Return [] when nothing matches.')
    else:
        params = ", ".join(task.args)
        doc = (f'Make true: "{task.sentence}". Arguments are str. Return a short text saying what was done. Raise RuntimeError(message) '
               "when it could not be done (never hide a failed command)." + (" Safe to run twice." if task.twice else ""))
    return (f"def {task.name}(world, {params}):\n    \"\"\"{doc}\"\"\"\n\n"
            f"The only door to the world:\n  {door}\nImports allowed: {', '.join(IMPORTS)}. Define the function {task.name}; helpers are allowed.")


def words(task):
    """what the user wrote about it, as a block of text (level 2)"""
    out = [f"What the user wrote about it ({task.file or 'the idea file'}, line {task.line}):",
           "  the agent says and understands:  " + "  |  ".join("«" + s + "»" for s in task.says)]
    names = [f"{a}: {', '.join(v)}" for a, v in task.consts.items() if v]
    if names:
        out.append("  names the user uses (conversation and pretend world):  " + "   ".join(names) + "   (written exactly like this)")
    if task.types:
        from logitration.phrases import Type
        out.append("  types of the arguments (a row that does not fit is refused before it reaches the rules):  "
                   + "   ".join(f"{a}: {Type(s).describe()}" for a, s in task.types.items()) + "   (each value is still returned as a str)")
    if task.examples:
        out.append("The user's conversation with the agent (client asks; the agent must answer with the lines after `agent:`):")
        for c, e in task.examples:
            out.append(f"  client: {c}")
            out += [f"  agent:  {x}" for x in e]
    return "\n".join(out)


def prompt(task, level=2, history=()):
    what = {"observe": "a script that LOOKS at the world (read-only) and reports facts", "act": "a script that CHANGES the world"}[task.kind]
    parts = [f"Write the Python script `{task.name}`: {what}."]
    if level >= 1:
        parts += ["", "SIGNATURE (write exactly this function):", header(task)]
    if level >= 2:
        parts += ["", words(task)]
    for src, err in list(history)[-2:]:
        parts += ["", "A previous attempt of yours:", src, f"It was rejected: {err}", "Write the corrected, complete script."]
    parts += ["", "Answer with the code only."]
    return "\n".join(parts)


def spec_text(task, level=2):
    """what `logi spec` shows: the specification as the model would receive it, and the cases the script will be tried on"""
    cases = case_list(task, None)
    lines = [f"PROGRAM {task.name}  ({task.kind})   {task.file}:{task.line}",
             f"  speaks as : " + " | ".join(task.says) + ("   [program]" if task.marked else ""),
             f"  fact      : {task.fact}({', '.join(task.args)})   commands allowed via: {', '.join(task.via)}",
             "  names the user uses (conversation and pretend world): " + ("; ".join(f"{a}: {', '.join(v)}" for a, v in task.consts.items() if v) or "none"),
             f"  context level {level} ({LEVELS[level]})" + (f"; {len(task.examples)} conversation turns as examples" if level >= 2 else ""),
             f"  cases it will be tried on ({len(cases)}):"]
    lines += [f"     - {c[0]}" for c in cases]
    lines += ["", "  --- the prompt, as it goes to the model ---", *["  | " + l for l in prompt(task, level).splitlines()]]
    return "\n".join(lines)


# ====================================================================== validation (AST) and the sandbox
def strip_code(text):
    t = (text or "").strip()
    m = re.search(r"```(?:python|py)?\s*\n(.*?)```", t, re.S)
    return (m.group(1) if m else t).strip()


def validate(src, task):
    """None if the script is acceptable, else the reason (it goes into the next prompt)"""
    if not src:
        return "the answer has no code"
    if len(src) > MAX_SOURCE:
        return f"too long ({len(src)} characters; at most {MAX_SOURCE})"
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return f"syntax error: {e.msg} (line {e.lineno})"
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in n.names] if isinstance(n, ast.Import) else [n.module or ""]
            for mod in mods:
                if mod.split(".")[0] not in IMPORTS or isinstance(n, ast.ImportFrom) and n.level:
                    return f"line {n.lineno}: import of `{mod}` is not allowed (only: {', '.join(IMPORTS)})"
        elif isinstance(n, (ast.ClassDef, ast.Global, ast.Nonlocal, ast.With, ast.AsyncFunctionDef, ast.Await, ast.Yield, ast.YieldFrom, ast.AsyncFor)):
            return f"line {n.lineno}: `{type(n).__name__}` is not allowed in a program"
        elif isinstance(n, ast.Name) and (n.id in BANNED_NAMES or n.id.startswith("__")):
            return f"line {n.lineno}: `{n.id}` is not allowed (no eval/exec/open/getattr, no dunder names)"
        elif isinstance(n, ast.Attribute):
            if n.attr.startswith("_"):
                return f"line {n.lineno}: attribute `{n.attr}` is not allowed (private or dunder)"
            if isinstance(n.value, ast.Name) and n.value.id == "world" and n.attr not in DOOR:
                return f"line {n.lineno}: world.{n.attr} does not exist (the door has: {', '.join(DOOR)})"
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) and n.func.value.id == "world" \
                and n.func.attr == "run" and len(n.args) >= 2 and isinstance(n.args[1], ast.Constant) and n.args[1].value not in task.via:
            return f"line {n.lineno}: command `{n.args[1].value}` is not declared (`via {' '.join(task.via)}`)"
    for n in tree.body:
        if not isinstance(n, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.Assign, ast.Expr)):
            return f"line {n.lineno}: at the top of a program only imports, functions and constants (not `{type(n).__name__}`)"
        if isinstance(n, ast.Expr) and not isinstance(n.value, ast.Constant):
            return f"line {n.lineno}: top-level code that runs on import is not allowed"
    fs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == task.name]
    if not fs:
        return f"there is no `def {task.name}(...)`"
    a = fs[0].args
    got = [x.arg for x in a.args]
    want = ["world"] + task.args
    if got != want or a.vararg or a.kwarg or a.kwonlyargs:
        return f"the signature must be `def {task.name}({', '.join(['world'] + task.args)})`; it is `def {task.name}({', '.join(got)})`"
    if task.kind == "observe" and len(a.defaults) != len(task.args):
        return f"every argument of an observe script defaults to None: `def {task.name}(world, {', '.join(x + '=None' for x in task.args)})`"
    if task.kind == "act" and a.defaults:
        return "an act script has no defaults: every argument is given"
    return None


def _importer(name, globals=None, locals=None, fromlist=(), level=0):
    if name.split(".")[0] not in IMPORTS:
        raise ImportError(f"import of {name} is not allowed")
    return __import__(name, globals, locals, fromlist, level)


def _sandbox():
    import builtins
    b = {k: getattr(builtins, k) for k in SAFE}
    b["__import__"] = _importer
    return {"__builtins__": b, "__name__": "program"}


def load_function(src, task):
    ns = _sandbox()
    exec(compile(src, f"<program {task.name}>", "exec"), ns)
    return ns[task.name]


class Timeout(Exception):
    pass


def call_with_timeout(fn, args, seconds=4):
    """run fn(*args); a loop that never ends becomes a Timeout (main thread: SIGALRM; elsewhere: no limit)"""
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "SIGALRM"):
        return fn(*args)

    def boom(*_):
        raise Timeout(f"took more than {seconds} s")
    old = signal.signal(signal.SIGALRM, boom)
    signal.alarm(seconds)
    try:
        return fn(*args)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


# ====================================================================== the rehearsal: cases from the conversation
def case_list(task, world):
    """[(label, kind, payload)] the script is tried on; the labels need no world (logi spec prints them)"""
    out = []
    n = len(task.args)
    if task.kind == "observe":
        out.append((f"{task.call([None] * n)}  everything the world says", "observe", {}))
        fixes = []
        for i, a in enumerate(task.args):
            fixes += [{i: c} for c in task.consts.get(a, [])[:3]]
        if n >= 2:
            firsts, seconds = task.consts.get(task.args[0], [])[:2], task.consts.get(task.args[1], [])[:2]
            fixes += [{0: x, 1: y} for x in firsts for y in seconds]
        for f in fixes:
            vals = [f.get(i) for i in range(n)]
            out.append((f"{task.call(vals)}  as a question", "observe", f))
    else:
        firsts = task.consts.get(task.args[0], []) if task.args else []
        lasts = task.consts.get(task.args[-1], []) if task.args else []
        pairs = task.pairs or [(x, y) for x in firsts[:2] for y in lasts[:2]] if n == 2 else [tuple(task.consts.get(a, [""])[0] if task.consts.get(a) else "" for a in task.args)]
        for p in pairs[:4]:
            out.append((f"{task.call(p)}  must do it{', twice,' if task.twice else ','} without raising", "act", {"args": p}))
        for b in task.bad:
            out.append((f"{task.call(b)}  cannot be done: must RAISE, not pretend", "bad", {"args": b}))
    return out


def rehearse(src, task, world, seconds=4):
    """run the script against every case on a fresh copy of the rehearsal world: (ok, report text, [per case (label, ok, detail)])"""
    try:
        fn = load_function(src, task)
    except Exception as e:
        return False, f"it did not load: {type(e).__name__}: {str(e)[:200]}", [("load", False, str(e)[:200])]
    results = []
    for label, kind, p in case_list(task, world):
        w = world.copy()
        door = Door(w, task.via)
        try:
            if kind == "observe":
                vals = [p.get(i) for i in range(len(task.args))]
                got = call_with_timeout(fn, [door] + vals, seconds)
                if not isinstance(got, (list, tuple, set)) or not all(isinstance(r, tuple) and len(r) == len(task.args) and all(isinstance(x, str) for x in r) for r in got):
                    results.append((label, False, f"returned {str(got)[:120]}: it must be a list of tuples of {len(task.args)} str"))
                    continue
                if task.rows is None:
                    results.append((label, True, "shape ok (no oracle for this tool)"))
                    continue
                want = {r for r in task.rows(w) if all(vals[i] is None or r[i] == vals[i] for i in range(len(vals)))}
                ok = set(got) == want and len(got) == len(set(got))
                results.append((label, ok, "ok" if ok else f"expected {sorted(want)}, got {sorted(map(tuple, got), key=str)}"))
            elif kind == "act":
                for _ in range(2 if task.twice else 1):
                    r = call_with_timeout(fn, [door] + list(p["args"]), seconds)
                if task.effect is None:
                    results.append((label, True, "ran (no oracle for this tool)"))
                    continue
                ok = bool(task.effect(w, p["args"]))
                results.append((label, ok, "ok" if ok else f"it returned {str(r)[:60]!r} but the world does not show it: {task.call(p['args'])} is still not true"))
            else:
                try:
                    r = call_with_timeout(fn, [door] + list(p["args"]), seconds)
                    results.append((label, False, f"it returned {str(r)[:60]!r} instead of raising: the command failed (the world said so) and the script hid it"))
                except Timeout as e:
                    results.append((label, False, f"Timeout: {e}"))
                except Exception as e:
                    results.append((label, True, f"raised {type(e).__name__}: {str(e)[:60]}"))
        except Exception as e:
            results.append((label, False, f"raised {type(e).__name__}: {str(e)[:160]}"))
    bad = [r for r in results if not r[1]]
    report = f"{len(results) - len(bad)}/{len(results)} cases" + "".join(f"\n  FAIL {l}\n       {d}" for l, _, d in bad[:3])
    return not bad, report, results


# ====================================================================== the loop: a coding agent
class Outcome:
    def __init__(self, task, level):
        self.task, self.level, self.attempts, self.src, self.accepted, self.informe = task, level, [], None, False, ""

    @property
    def calls(self):
        return len(self.attempts)

    @property
    def usd(self):
        return sum(a["meta"].get("usd", 0) or 0 for a in self.attempts)


class Budget:
    def __init__(self, calls):
        self.left, self.used = calls, 0

    def take(self):
        if self.left <= 0:
            raise BudgetExhausted(f"the budget of calls is spent ({self.used})")
        self.left -= 1
        self.used += 1


def program_one(task, llm, world, level=2, attempts=3, budget=None, on_attempt=None, history=(), start=1):
    """spec -> prompt -> model -> validate -> rehearse -> (error -> prompt with the error) -> accepted or an informe. `history` and
    `start` let a caller continue a cell (the study spreads the retries over rounds)."""
    out = Outcome(task, level)
    hist = list(history)
    for k in range(start, attempts + 1):
        p = prompt(task, level, hist)
        if budget:
            budget.take()
        text, meta = llm(SYSTEM, p)
        src = strip_code(text)
        v = validate(src, task)
        rep, results = None, []
        if v is None:
            ok, rep, results = rehearse(src, task, world)
        at = dict(k=k, prompt=p, response=text, validation=v, rehearsal=rep, results=results, meta=meta, accepted=v is None and ok, src=src)
        out.attempts.append(at)
        if on_attempt:
            on_attempt(out, at)
        if at["accepted"]:
            out.accepted, out.src = True, src
            return out
        hist.append((src, v or ("it ran but the rehearsal failed: " + rep)))
    out.informe = informe(task, out)
    return out


def informe(task, out):
    """when every attempt failed: what was wanted, why each was rejected, what it takes (for the human)"""
    lines = [f"{task.name} ({task.kind}) was NOT programmed after {out.calls} attempt(s). Wanted: «{task.sentence}» via {', '.join(task.via)}."]
    for a in out.attempts:
        lines.append(f"  attempt {a['k']}: " + (a["validation"] or "rehearsal failed: " + (a["rehearsal"] or "").replace("\n", " | ")[:300]))
    lines.append("  to continue: read `logi spec`, add a conversation turn that shows the case, or write the script by hand in programs/.")
    return "\n".join(lines)


# ====================================================================== the model
class ClaudeCLI:
    """`claude -p` with the Sonnet alias, no tools, no session, an empty folder. __call__(system, prompt) -> (text, meta);
    meta: model, usd (what the CLI reports), tokens in/out, ms. Your Claude Code login pays."""

    def __init__(self, model="sonnet", timeout=180, exe=None):
        self.model, self.timeout = model, timeout
        self.exe = exe or shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
        self.cwd = tempfile.mkdtemp(prefix="logi_program_")

    def __call__(self, system, prompt_text):
        t0 = time.time()
        r = subprocess.run([self.exe, "-p", "--model", self.model, "--output-format", "json", "--tools", "", "--no-session-persistence",
                            "--disable-slash-commands", "--setting-sources", "", "--strict-mcp-config", "--system-prompt", system],
                           input=prompt_text, capture_output=True, text=True, timeout=self.timeout, cwd=self.cwd)
        if r.returncode != 0:
            raise RuntimeError(f"claude rc={r.returncode}: {(r.stderr or r.stdout)[:150]}")
        j = json.loads(r.stdout)
        u = j.get("usage", {})
        return j.get("result", ""), {"model": ",".join(j.get("modelUsage", {}) or [self.model]), "usd": j.get("total_cost_usd", 0),
                                      "tokens_in": u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0),
                                      "tokens_out": u.get("output_tokens", 0), "ms": round((time.time() - t0) * 1000)}


# ====================================================================== anchoring: programs/ next to the idea file
def programs_dir(idea):
    return idea.path.parent / "programs"


def anchored(idea, name):
    p = programs_dir(idea) / f"{name}.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def anchor(idea, task, src, outcome, model="", reason=None):
    """keep the accepted script: programs/NAME.py (the script, with a header), NAME.json (version, reason, the specification) and
    NAME.vK.py for every version it replaces. Returns the metadata."""
    d = programs_dir(idea)
    d.mkdir(exist_ok=True)
    old = anchored(idea, task.name)
    version = (old or {}).get("version", 0) + 1
    if old and (d / f"{task.name}.py").exists():
        shutil.copy(d / f"{task.name}.py", d / f"{task.name}.v{old['version']}.py")
    meta = dict(name=task.name, kind=task.kind, version=version, anchored=time.strftime("%Y-%m-%d %H:%M"), model=model,
                attempts=outcome.calls, reason=reason or f"{task.file}:{task.line} needs a script ({task.kind}) for «{task.sentence}»",
                says=task.says, args=task.args, via=task.via, level=outcome.level, sha256=hashlib.sha256(src.rstrip().encode()).hexdigest(),
                spec=spec_text(task, outcome.level))
    head = (f"# program {task.name} ({task.kind}) v{version}, anchored {meta['anchored']} by `logi program` ({model}, attempt {outcome.calls})\n"
            f"# reason: {meta['reason']}\n# via: {', '.join(task.via)}   door: world.run / world.machines   imports: {', '.join(IMPORTS)}\n")
    (d / f"{task.name}.py").write_text(head + src.rstrip() + "\n")
    (d / f"{task.name}.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False))
    idx = d / "index.json"
    try:
        index = json.loads(idx.read_text())
    except (OSError, ValueError):
        index = {}
    index[task.name] = {"kind": task.kind, "version": version}
    idx.write_text(json.dumps(index, indent=1))
    return meta


class Programs:
    """the anchored scripts of an idea file, ready to run on a world: observer() is the `observe:` of the agent (it prints facts in
    your words), device() runs the commands of act/2. A script that is not there, or no longer validates, is reported and skipped."""

    def __init__(self, idea, vocab, world):
        self.idea, self.vocab, self.world = idea, vocab, world
        self.observes, self.acts, self.notes = [], {}, []
        for d in idea.programs:
            task = Task(d["kind"], d["name"], d["args"], d["says"], d["via"], line=d["line"], fact=d["fact"], file=idea.path.name)
            meta = anchored(idea, d["name"])
            f = programs_dir(idea) / f"{d['name']}.py"
            if not meta or not f.exists():
                self.notes.append(f"program `{d['name']}` ({d['kind']}, line {d['line']}) is not built yet: run `logi program {idea.path.name}`")
                continue
            src = f.read_text()
            body = src[src.index("\n", src.index("\n", src.index("\n") + 1) + 1) + 1:]
            if hashlib.sha256(body.rstrip().encode()).hexdigest() != meta["sha256"]:
                self.notes.append(f"program `{d['name']}`: programs/{d['name']}.py changed since it was anchored; re-run `logi program --force`")
                continue
            why = validate(body, task)
            if why:
                self.notes.append(f"program `{d['name']}` no longer validates: {why}")
                continue
            fn = load_function(body, task)
            if d["kind"] == "observe":
                self.observes.append((task, fn))
            else:
                self.acts[d["name"]] = (task, fn)

    def observer(self):
        if not self.observes:
            return None

        def run():
            lines = []
            for task, fn in self.observes:
                rows = call_with_timeout(fn, [Door(self.world, task.via)] + [None] * len(task.args), 20)
                ph = self.vocab.by_sig[(task.fact, len(task.args))]
                lines += [ph.render(list(r)) for r in rows]
            return "\n".join(lines)
        return run

    def device(self):
        if not self.acts:
            return None

        def run(command):
            parts = command.split()
            if not parts or parts[0] not in self.acts:
                raise RuntimeError(f"no program for the command {command!r}")
            task, fn = self.acts[parts[0]]
            if len(parts) - 1 != len(task.args):
                raise RuntimeError(f"{parts[0]} takes {len(task.args)} argument(s)")
            return call_with_timeout(fn, [Door(self.world, task.via)] + parts[1:], 60)
        return run
