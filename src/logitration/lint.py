# Copyright (c) 2026 David Ragel
# SPDX-License-Identifier: BUSL-1.1
# Licensed under the Business Source License 1.1; see LICENSE.
"""logi lint: what the idea file leaves unsaid, found before anyone asks (no conversation, no translator needed).

It reads the file and lists, with the line:
  * predicates the rules call that nothing defines (no phrase, no facts, no rules)    -- a typo, or a rule you forgot
  * predicates with rules and no phrase (they show in answers as Prolog; explain/2 about them is never used)
  * predicates nobody uses (rules nobody calls; phrases no rule reads)
  * types (`capacity(N:name, R:ram|disk, G:int>=0)`): a constant in a fact or rule that is not of the type of its place, a variable
    that two places want to be of types no value has in common, an enumeration that disagrees with `[R: ...]`
  * a predicate a program computes (`[program via CMD]`) next to the ones written in the file, told by a command, or stated by the client
  * meta-interpreter lines that cannot do anything (a predicate that is not missing/ask/can_change/act/explain), or written under
    the subsection of another meta-interpreter (check: explain; execute: can_change, act; complete: missing, ask)
It does NOT know your logic: a rule you forgot to write (a disk limit, say) leaves nothing to find. That is yours to test
with the conversation of the file (logi check) and with questions whose answer you know.
"""
import re

from logitration import engine as E
from logitration.knowledge import META, META_PART

META_NAMES = sorted({k[0] for k in META})
GOAL_ARGS = {"findall": (1,), "aggregate_all": (1,), "bagof": (1,), "setof": (1,), "forall": (0, 1), "call": (0,), "once": (0,), "ignore": (0,),
             "\\+": (0,), ",": (0, 1), ";": (0, 1), "->": (0, 1), "*->": (0, 1), "not": (0,)}
_BUILTIN = E.Prolog()


def _sig(key):
    m = re.match(r"^([a-z]\w*)\s*(?:\((.*)\))?$", key.strip())
    return (m.group(1), len([a for a in m.group(2).split(",")]) if m.group(2) else 0)


def _fmt(sig):
    return f"{sig[0]}/{sig[1]}"


def _sig_of(t):
    t = E.deref(t)
    if isinstance(t, E.Atom):
        return (t.name, 0)
    if isinstance(t, E.Struct):
        return (t.name, len(t.args))
    return None


def _builtin(sig):
    return sig in E.SISTEMA or sig in E.CONTROL or sig in _BUILTIN.db


def _goals(body, out):
    """the predicates a body calls: [(sig, term)]"""
    b = E.deref(body)
    s = _sig_of(b)
    if s is None:
        return
    if isinstance(b, E.Struct) and b.name in GOAL_ARGS:
        for i in GOAL_ARGS[b.name]:
            if i < len(b.args):
                g = E.deref(b.args[i])
                while isinstance(g, E.Struct) and g.name == "^" and len(g.args) == 2:
                    g = E.deref(g.args[1])
                _goals(g, out)
        return
    out.append((s, b))


def _functors(t, out):
    t = E.deref(t)
    s = _sig_of(t)
    if s:
        out.add(s)
    if isinstance(t, E.Struct):
        for a in t.args:
            _functors(a, out)


def lint(idea):
    """-> [(level, line or None, message)]  level: 'error' | 'warning' | 'note' | 'info'. Errors of syntax stop earlier (IdeaError)."""
    out = []
    phrased = {_sig(k): n for k, n in idea.word_lines.items()}
    defined, rules, calls, mentioned = {}, {}, [], set()
    for t, line in idea.clauses:
        if isinstance(t, E.Struct) and t.name == ":-" and len(t.args) == 2:
            head, body = E.deref(t.args[0]), E.deref(t.args[1])
        else:
            head, body = t, E.TRUE
        hs = _sig_of(head)
        if hs is None:
            continue
        defined.setdefault(hs, line)
        if body is not E.TRUE:
            rules.setdefault(hs, line)
            gs = []
            _goals(body, gs)
            calls += [(g, line, hs) for g, _ in gs]
        for a in (head.args if isinstance(head, E.Struct) else ()):
            _functors(a, mentioned)
        _functors(body, mentioned)
    called = {g for g, _, _ in calls}
    meta_sigs = set(META)
    # 1. a call to something nothing defines
    seen = set()
    for g, line, owner in calls:
        if g in defined or g in phrased or _builtin(g) or g in seen:
            continue
        seen.add(g)
        other = sorted({s[1] for s in list(defined) + list(phrased) if s[0] == g[0]})
        why = f"only {g[0]}/{', '.join(str(a) for a in other)} exists" if other else "no phrase, no facts, no rules"
        out.append(("error", line, f"{_fmt(g)} is called (in a rule of {_fmt(owner)}) but {why}: a typo, or a line you forgot"))
    # 2./3. predicates with rules and no phrase; rules nobody calls
    for sig, line in sorted(defined.items(), key=lambda kv: kv[1]):
        if sig in meta_sigs or sig in phrased:
            continue
        what = "rules" if sig in rules else "facts"
        if sig[0] in META_NAMES:
            want = sorted(k[1] for k in META if k[0] == sig[0])
            out.append(("warning", line, f"{_fmt(sig)} is not part of the meta-interpreter ({sig[0]}/{want[0]} is): it is never used"))
        elif sig in called or sig in mentioned:
            out.append(("note", line, f"{_fmt(sig)} has {what} but no phrase: it is a helper; answers that mention it show Prolog text"))
        else:
            out.append(("warning", line, f"{_fmt(sig)} has {what} but no phrase and nothing calls it: it is never used"
                        + (" (the meta-interpreter only knows " + ", ".join(META_NAMES) + ")" if idea.section_of.get(line) == "meta" else "")))
    # 4. the meta-interpreter about a predicate that has no phrase or does not exist
    for t, line in idea.clauses:
        head = E.deref(t.args[0]) if isinstance(t, E.Struct) and t.name == ":-" and len(t.args) == 2 else t
        hs = _sig_of(head)
        if hs in meta_sigs and isinstance(head, E.Struct):
            tgt = _sig_of(head.args[0])
            if tgt and tgt not in phrased:
                what = "has no phrase" if tgt in defined else "does not exist"
                out.append(("warning", line, f"{_fmt(hs)} is about {_fmt(tgt)}, which {what}: this line is never used"))
    # 4b. a meta line written under the subsection of another meta-interpreter
    for t, line in idea.clauses:
        head = E.deref(t.args[0]) if isinstance(t, E.Struct) and t.name == ":-" and len(t.args) == 2 else t
        hs = _sig_of(head)
        part = idea.part_of.get(line)
        if hs in meta_sigs and part and META_PART[hs[0]] != part:
            out.append(("warning", line, f"{_fmt(hs)} is read by the `{META_PART[hs[0]]}` meta-interpreter but is written under `-- {part}`: "
                        f"it works, but belongs under `-- {META_PART[hs[0]]}`"))
    # 5. phrases nobody uses (a fact the client may state that no rule or meta line reads)
    for sig, line in sorted(phrased.items(), key=lambda kv: kv[1]):
        if sig not in rules and sig not in called and sig not in mentioned:
            out.append(("warning", line, f"the phrase of {_fmt(sig)} is never used: no rule or meta line reads it, so what the client says about it changes nothing"))
    # 6. the `-- program` meta-interpreter: scripts the agent still lacks
    if idea.programs and not idea.rehearse:
        out.append(("warning", idea.programs[0]["line"], "programs are declared but there is no `rehearse m1: ...` line: nothing to try the scripts on"))
    for d in idea.programs:
        if d["kind"] == "observe" and (d["fact"], len(d["args"])) in rules:
            out.append(("warning", d["line"], f"observe {d['fact']}/{len(d['args'])}: it has rules; a script observes facts (no rules)"))
        from logitration.programmer import anchored
        if not anchored(idea, d["name"]):
            out.append(("note", d["line"], f"program `{d['name']}` ({d['kind']}) is not built: `logi spec` shows its specification, `logi program` builds it"))
    # 7. types: the constants of facts and rules, the variables that two places type differently, the enumerations that disagree
    out += _types(idea, phrased)
    # 8. [program]: a script declared under `-- program` whose phrase does not say so
    for d in idea.programs:
        if d["kind"] == "observe" and not d.get("marked"):
            out.append(("note", d["line"], f"observe {d['fact']}/{len(d['args'])}: a program computes this fact but its phrase does not say so: "
                        f"add `[program]` to the word line (line {phrased.get((d['fact'], len(d['args'])), '?')})"))
    # info: where each fact comes from, and what the client can ask
    out += origins(idea, phrased, rules, defined)
    asked = sorted(_fmt(s) for s in phrased if s in rules)
    out.append(("info", None, "the client asks (phrase and rules): " + (", ".join(asked) or "nothing")))
    out.sort(key=lambda x: ({"error": 0, "warning": 1, "note": 2, "info": 3}[x[0]], x[1] or 0))
    return out


def _program_sigs(idea):
    """(name, arity) of every fact a program computes: the marked ones and the `observe pred(...) via` ones"""
    return {(d["fact"], len(d["args"])): d for d in idea.programs if d["kind"] == "observe"}


def origins(idea, phrased, rules, defined):
    """info lines: for each fact predicate, WHO provides it. Four ways: written in the file (a fact with no rule), told by the
    client, printed by the `observe:` command, computed by a program."""
    prog = _program_sigs(idea)
    facts = [s for s in phrased if s not in rules]
    written = sorted(_fmt(s) for s in facts if s in defined and s not in prog)
    rest = sorted(_fmt(s) for s in facts if s not in defined and s not in prog)
    out = []
    if written:
        out.append(("info", None, "written in the file (facts): " + ", ".join(written)))
    if idea.observe:
        out.append(("info", idea.observe_line, f"the agent looks at the world with `{idea.observe}` (it runs only with --observe)"))
    out.append(("info", None, "the client states (phrase, no rule): " + (", ".join(rest) or "nothing")
                + (" (or the observe: command prints them)" if idea.observe and rest else "")))
    for s, d in sorted(prog.items(), key=lambda kv: kv[1]["line"]):
        from logitration.programmer import anchored
        meta = anchored(idea, d["name"])
        out.append(("info", d["line"], f"computed by a program: {_fmt(s)} via {', '.join(d['via'])} "
                    + (f"(built, v{meta['version']})" if meta else "(not built yet: `logi program`)")))
    for d in idea.programs:
        if d["kind"] == "act":
            out.append(("info", d["line"], f"an action script: {d['name']} via {', '.join(d['via'])} (what `act` runs)"))
    return out


def _typed(idea, phrased):
    """{(name, arity): {arg index: Type}} of the phrases that carry types, and {(name, arity): argument names}"""
    from logitration.phrases import Type
    types, names = {}, {}
    for key, value in idea.words.items():
        sig = _sig(key)
        m = re.match(r"^[a-z]\w*\s*\((.*)\)$", key.strip())
        argn = [a.strip() for a in m.group(1).split(",")] if m else []
        names[sig] = argn
        t = {argn.index(a): Type(s) for a, s in (value.get("types") or {}).items() if a in argn}
        for a, vals in (value.get("values") or {}).items():
            if a in argn:
                t.setdefault(argn.index(a), Type("|".join(vals)) if all(re.match(r"^[a-z][a-z0-9_]*$", v) for v in vals) and len(vals) > 1 else Type("any"))
        if t:
            types[sig] = t
    return types, names


def _types(idea, phrased):
    from logitration.phrases import Type
    types, names = _typed(idea, phrased)
    out = []
    # an enumeration written as `[R: ram disk]` and as a type must agree
    for key, value in idea.words.items():
        sig = _sig(key)
        for a, vals in (value.get("values") or {}).items():
            ty = (value.get("types") or {}).get(a)
            if ty:
                T_ = Type(ty)
                bad = [v for v in vals if T_.check(v)]
                if bad:
                    out.append(("error", idea.word_lines.get(key), f"{_fmt(sig)}: {a} is `{ty}` but `[{a}: ...]` also allows {', '.join(bad)}: the two lists disagree"))
    if not types:
        return out

    def val(a):
        a = E.deref(a)
        if isinstance(a, E.Atom):
            return a.name
        return a if isinstance(a, int) and not isinstance(a, bool) else None
    for t, line in idea.clauses:
        head, body = (E.deref(t.args[0]), E.deref(t.args[1])) if isinstance(t, E.Struct) and t.name == ":-" and len(t.args) == 2 else (t, E.TRUE)
        places = []                                              # (term, role) of every phrased goal of the clause
        hs = _sig_of(head)
        if hs in types or hs in phrased:
            places.append(head)
        gs = []
        if body is not E.TRUE:
            _goals(body, gs)
        places += [g for _, g in gs if _sig_of(g) in phrased]
        seen = {}
        for g in places:
            sig = _sig_of(g)
            for i, a in enumerate(g.args if isinstance(g, E.Struct) else ()):
                ty = types.get(sig, {}).get(i)
                if ty is None:
                    continue
                a = E.deref(a)
                if isinstance(a, E.Var):
                    seen.setdefault(id(a), []).append((ty, sig, i))
                else:
                    v = val(a)
                    why = ty.check(v) if v is not None else None
                    if why:
                        out.append(("error", line, f"{_fmt(sig)}: {names[sig][i]} is {E.write_term(a, True)}, but it must be {ty.describe()} ({why})"))
        for uses in seen.values():
            first = uses[0]
            for other in uses[1:]:
                if not first[0].compatible(other[0]):
                    out.append(("error", line, f"one variable stands for {names[first[1]][first[2]]} of {_fmt(first[1])} ({first[0].describe()}) "
                                f"and for {names[other[1]][other[2]]} of {_fmt(other[1])} ({other[0].describe()}): no value is both"))
                    break
    return out


def show(items, name, out=print, levels=("error", "warning", "note", "info")):
    """print the items of these levels; returns the number of errors"""
    n = {"error": 0, "warning": 0, "note": 0}
    for level, line, msg in items:
        if level in n:
            n[level] += 1
        if level in levels:
            out(f"  {level:7} {name}:{line}: {msg}" if line else f"  {level:7} {msg}")
    out(f"lint {name}: {n['error']} error(s), {n['warning']} warning(s), {n['note']} note(s)")
    return n["error"]


def at_load(agent, out=print):
    """what try/check/serve say when the file is loaded: only errors and warnings, and where to see the rest"""
    for note in getattr(agent, "program_notes", []):
        out("note: " + note)
    items = [x for x in getattr(agent, "lint", []) if x[0] in ("error", "warning")]
    for level, line, msg in items[:8]:
        out(f"{level}: {agent.idea.path.name}:{line}: {msg}")
    if len(items) > 8:
        out(f"... {len(items) - 8} more")
    if items:
        out(f"(logi lint {agent.idea.path.name} lists everything the file leaves unsaid)")
