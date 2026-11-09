# Copyright (c) 2026 David Ragel
# SPDX-License-Identifier: BUSL-1.1
# Licensed under the Business Source License 1.1; see LICENSE.
"""The logic core: your rules, the facts the client stated (with their history), and the questions.

Everything that is TRUE comes from here. The questions are generic and work on any rule file:
  is G?        does G hold at a step                          (engine)
  which G?     every instance of G that holds                 (engine)
  why G?       the proof tree of G and the changing facts it rests on
  why_not G?   the and/or tree of what fails, and the ways to make G true (each way = changes that are all needed)
  change G     how G went true/false between two steps, and the event that flipped it each time
  timeline G   which instances of G hold at each step
Facts that `change` (see phrases.py) form the history: step 0 is the confirmed description, step k is after event k.
"""
import itertools
import re
import threading
import time
from pathlib import Path

from logitration import engine as E
from logitration import terms as T
from logitration.phrases import PhraseError

LIMITS = {
    "facts": 400,              # facts in one world
    "events": 200,             # events in one world
    "seconds_per_question": 5,
    "tree_nodes": 400,
    "alternatives": 64,        # ways to fix listed by why_not
    "depth": 40,
}
BIG_STACK = 512 * 1024 * 1024      # bytes of stack for the thread that runs the rules
_IN = threading.local()
_STACK = threading.Lock()          # threading.stack_size is global: one thread starts at a time
ENGINE_LOCK = threading.RLock()    # the engine's trail is module-level: one question at a time


# The meta-interpreter's vocabulary: what the user may write around the logic (all optional; see idea.py).
META = {("missing", 1): "ask for this before loading a description",
        ("ask", 2): "ask(Fact, Words): how to ask for a missing fact",
        ("can_change", 1): "what the agent may change by itself when asked to make something true",
        ("act", 2): "act(Fact, Command): the command for the device when the agent changes Fact",
        ("explain", 2): "explain(Goal, Words): how to say why Goal holds, instead of its proof"}


# which of the three meta-interpreters reads each predicate: check decides and explains, execute acts on the world,
# complete asks for what is missing. The fourth meta-interpreter, `program` (writes a missing script), is not Prolog: see programmer.py
META_PART = {"explain": "check", "can_change": "execute", "act": "execute", "missing": "complete", "ask": "complete"}


def words(t):
    """a Words term -> text: an atom, a number, or a list of them joined by spaces"""
    t = E.deref(t)
    if isinstance(t, E.Var):
        return None
    if (isinstance(t, E.Struct) and t.name == "." and len(t.args) == 2) or t is E.NIL:
        try:
            parts = [words(x) for x in E.to_pylist(t)]
        except E.PrologError:
            return T.text(t)
        return re.sub(r"\s+([?.,!:;])", r"\1", " ".join(p for p in parts if p))
    v = T.value(t)
    return str(v)


class Rejection(Exception):
    """Refused before reaching the rules. code: stable, for programs; detail: for people."""

    def __init__(self, code, detail):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail

    def to_json(self):
        return {"code": self.code, "detail": self.detail}


class World:
    """What one client told: the facts of the description and the events after it, in order. Never changed in place:
    a new World is made for each change (so cached snapshots stay right)."""
    _serial = itertools.count(1)

    def __init__(self, facts=(), events=()):
        self.serial = next(World._serial)
        self.facts = list(facts)          # ground terms
        self.events = list(events)        # [(ground term, negated)]
        self._snaps = None

    def copy(self):
        return World(self.facts, self.events)

    @property
    def last(self):
        return len(self.events)


def _first(pl, g):
    """does g have a solution? (bindings undone)"""
    mark = len(E.trail)
    it = pl.solve_closed(g)
    try:
        for _ in it:
            return True
        return False
    finally:
        it.close()
        E.undo(mark)


class Knowledge:
    def __init__(self, rules, vocab, tools=None, limits=None):
        self.vocab = vocab
        self.limits = {**LIMITS, **(limits or {})}
        self.pl = E.Prolog()
        with ENGINE_LOCK:
            try:
                if isinstance(rules, Path) or (isinstance(rules, str) and rules.strip().endswith(".pl") and "\n" not in rules.strip()):
                    self.pl.consult(str(Path(rules).resolve()))
                else:
                    self.pl.consult_text(rules)
            except E.PrologError as e:
                raise PhraseError(f"the rules do not load: {T.text(e.term)}")
            except SyntaxError as e:
                raise PhraseError(f"the rules do not load: {e}")
        for name, fn in (tools or {}).items():
            self._add_tool(name, fn)
        self.base = {}        # facts written in the rule file for predicates the client may also state
        for ph in vocab:
            cls = self.pl.db.get(ph.signature, [])
            if any(not (isinstance(c.body, E.Atom) and c.body is E.TRUE) for c in cls) or ph.signature in self.pl.tools:
                ph.kind = "question"
            else:
                ph.kind = "fact"
                self.base[ph.signature] = cls
                self.pl.dynamics.add(ph.signature)
        if not vocab.questions():
            raise PhraseError("no phrase names a predicate defined by your rules: the agent would have nothing to answer")
        self.main = vocab.questions()[0]         # the first question phrase: summaries, timeline, "why did X change"
        self.pl.watch = {ph.signature for ph in vocab if ph.kind == "fact" and ph.arity >= 2 and not ph.changes}
        self.constants = self._constants()
        self.has_missing = ("missing", 1) in self.pl.db
        self.meta = {k for k in META if k in self.pl.db}
        self._loaded = None

    def _constants(self):
        """every name the rules and phrases write themselves (so that a name in a question is known or not)"""
        out = {str(v) for ph in self.vocab for vals in ph.values.values() for v in vals}
        out |= {v for ph in self.vocab for t in ph.types.values() for v in (t.values or [])}

        def walk(t):
            t = E.deref(t)
            if isinstance(t, E.Atom):
                out.add(t.name)
            elif isinstance(t, E.Struct):
                for a in t.args:
                    walk(a)
        for cls in self.pl.db.values():
            for c in cls:
                for a in (c.head.args if isinstance(c.head, E.Struct) else ()):
                    walk(a)
                walk(c.body)
        return out

    def unknown_names(self, world, goal):
        """names in the arguments of goal that are in no fact or event of the world and in none of the rules: [name]"""
        known = set(self.constants)
        for t in world.facts + [t for t, _ in world.events]:
            known.update(str(T.value(a)) for a in T.args(t))
        return [str(T.value(a)) for a in T.args(goal) if isinstance(E.deref(a), E.Atom) and str(T.value(a)) not in known]

    def begin_misses(self):
        self.pl.misses = set()

    def end_misses(self, world, clear=True):
        """what the last question needed and nobody said: [(sentence with ?, [who has it])].
        Only when the thing is of a kind (it is in a one-argument fact such as node(n2)) and most things of that kind have
        this property (one fact each, at least two of them and two thirds) but this one has none. Edges and optional facts (few things have them) are not reported."""
        miss = self.pl.misses or set()
        if clear:
            self.pl.misses = None
        kinds = []
        for ph in self.vocab:
            if ph.kind == "fact" and ph.arity == 1:
                kinds.append({str(T.value(c.head.args[0])) for c in self.pl.db.get(ph.signature, []) if isinstance(c.head, E.Struct)})
        out = []
        for sig, who in sorted(miss, key=lambda m: (m[0], m[1])):
            firsts = [str(T.value(c.head.args[0])) for c in self.pl.db.get(sig, []) if isinstance(c.head, E.Struct)]
            if not firsts or len(firsts) != len(set(firsts)):
                continue
            if not any(who in members and 3 * len(members & set(firsts)) >= 2 * len(members) and len(members & set(firsts)) >= 2 for members in kinds):
                continue
            ph = self.vocab.by_sig[sig]
            out.append((ph.render([who] + [None] * (ph.arity - 1)), sorted(firsts)))
        return out

    # ------------------------------------------------------------ tools: Python functions as predicates
    def _add_tool(self, name, fn):
        """name 'price/2' -> price(In, Out): fn(in) returns a value, a list of values (several answers) or None (no answer)."""
        try:
            pname, arity = name.split("/")
            arity = int(arity)
        except ValueError:
            raise PhraseError(f"tool {name!r}: name it 'predicate/arity', e.g. 'price/2'")

        def call(*args):
            ins = [T.value(a) for a in args[:-1]]
            if any(isinstance(E.deref(a), E.Var) for a in args[:-1]):
                return
            out = fn(*ins)
            for v in (out if isinstance(out, (list, tuple)) else [] if out is None else [out]):
                mark = len(E.trail)
                if E.unify(args[-1], T.to_term(v)):
                    yield
                E.undo(mark)
        self.pl.tools[(pname, arity)] = call

    # ------------------------------------------------------------ validation of what the translator produced
    def fact(self, text, path="fact"):
        """'lamp(l1)' -> ground term of a predicate the client may state. Raises Rejection."""
        try:
            t, _ = T.parse(text, allow_vars=False)
        except T.TermError as e:
            self._untyped_text(text)                     # a typed argument that cannot even be read says WHICH one and what it must be
            raise Rejection("invalid_fact", f"{path}: {e}")
        ph = self.vocab.get(t)
        if ph is None or ph.kind != "fact":
            known = ", ".join(p.pattern() for p in self.vocab.facts())
            raise Rejection("unknown_fact", f"{text!r} is not something you can state here (allowed: {known})")
        for a in T.args(t):
            if not (isinstance(a, int) or (isinstance(a, E.Atom) and T.RE_NAME.match(a.name))):
                raise Rejection("invalid_fact", f"{text!r}: arguments must be names (lowercase, letters/digits/_) or integers")
        for i, allowed in ph.values.items():
            if str(T.value(T.args(t)[i])) not in allowed:
                raise Rejection("invalid_value", f"{text!r}: {ph.argnames[i]} must be one of {', '.join(allowed)}")
        self._types(text, ph, t)
        return t

    def _types(self, text, ph, t):
        """the typed arguments (see phrases.Type) of a term whose arguments are all constants or variables"""
        for i, a in enumerate(T.args(t)):
            if i in ph.types and not isinstance(a, E.Var):
                why = ph.type_error(i, T.value(a))
                if why:
                    raise Rejection("invalid_type", f"{text!r}: {why}")

    def _untyped_text(self, text):
        """text that does not parse as a term (a quoted text, a decimal, a variable): if the predicate has types, find the argument
        that breaks its type from the raw text and refuse it with the type's words. Returns normally when nothing is known."""
        m = re.match(r"^\s*([a-z][a-z0-9_]*)\s*\((.*)\)\s*$", str(text), re.S)
        if not m:
            return
        raw, depth, cur, quote = [], 0, "", False
        for c in m.group(2):
            if c == '"':
                quote = not quote
            if not quote and c == "(":
                depth += 1
            elif not quote and c == ")":
                depth -= 1
            if c == "," and not quote and depth == 0:
                raw.append(cur.strip())
                cur = ""
            else:
                cur += c
        raw.append(cur.strip())
        ph = self.vocab.by_sig.get((m.group(1), len(raw)))
        if ph is None:
            return
        for i, r in enumerate(raw):
            if i not in ph.types:
                continue
            v = int(r) if re.match(r"^-?\d{1,9}$", r) else r if re.match(r"^[a-z][a-z0-9_]{0,31}$", r) else None
            why = ph.type_error(i, v) if v is not None else \
                f"{ph.argnames[i]} must be {ph.types[i].describe()} ({r!r} is not); the phrase is «{ph.says[0]}»"
            if why:
                raise Rejection("invalid_type", f"{text!r}: {why}")

    def event(self, text, negated=False):
        t = self.fact(text, "event")
        if not self.vocab.get(t).changes:
            changing = ", ".join(p.pattern() for p in self.vocab.facts() if p.changes) or "none"
            raise Rejection("not_a_change", f"{text!r} cannot change over time (facts that change: {changing})")
        if negated and self.vocab.get(t).arity > 1 and not self.vocab.get(t).set:
            raise Rejection("not_a_change", f"say the new value of {text!r} instead of 'not'")
        return (t, negated)

    def goal(self, text):
        """a question goal: a phrased predicate, variables allowed."""
        try:
            t, names = T.parse(text, allow_vars=True)
        except T.TermError as e:
            raise Rejection("invalid_question", str(e))
        ph = self.vocab.get(t)
        if ph is None:
            raise Rejection("unknown_question", f"{text!r}: you can ask about " + ", ".join(p.pattern() for p in self.vocab))
        self._types(text, ph, t)
        return t, names

    # ------------------------------------------------------------ history
    def _key(self, t):
        ph = self.vocab.get(t)
        a = [T.text(x) for x in T.args(t)]
        return (ph.name, ph.arity, tuple(a if ph.set or ph.arity == 1 else a[:-1]))

    def snapshots(self, world):
        """per step: {fact text: (term, step it was set)} and {key: step it was cleared}"""
        if world._snaps is not None:
            return world._snaps
        if len(world.facts) > self.limits["facts"] or len(world.events) > self.limits["events"]:
            raise Rejection("too_large", f"more than {self.limits['facts']} facts or {self.limits['events']} events")
        cur, cleared = {}, {}
        for t in world.facts:
            ph = self.vocab.get(t)
            if ph.changes:
                k = self._key(t)
                for x in [x for x, (u, _) in cur.items() if self.vocab.get(u).changes and self._key(u) == k]:
                    del cur[x]
            cur[T.text(t)] = (t, 0)
        snaps = [(dict(cur), dict(cleared))]
        for step, (t, neg) in enumerate(world.events, 1):
            k = self._key(t)
            for x in [x for x, (u, _) in cur.items() if self.vocab.get(u).changes and self._key(u) == k]:
                del cur[x]
                cleared[k] = step
            if not neg:
                cur[T.text(t)] = (t, step)
                cleared.pop(k, None)
            snaps.append((dict(cur), dict(cleared)))
        world._snaps = snaps
        return snaps

    def _load(self, world, step):
        """put the facts of `step` in the engine (only the client's predicates are touched)"""
        sig = (world.serial, step)
        if self._loaded == sig:
            return self.snapshots(world)[step]
        snap = self.snapshots(world)[step]
        for s, base in self.base.items():
            self.pl.db[s] = list(base)
        for t, _ in snap[0].values():
            self.pl.db[T.signature(t)].append(E.Clause(t, E.TRUE))
        self._loaded = sig
        return snap

    def _run(self, world, step, fn):
        """run fn() with the world loaded at `step`, under the lock and the deadline. The engine recurses deeply (a rule that
        calls itself would crash the interpreter on the small stack of the main thread), so the outermost call runs in a
        thread with a big stack: a loop ends as a timeout or 'too deep', never as a crash."""
        if not (isinstance(step, int) and 0 <= step <= world.last):
            raise Rejection("invalid_step", f"step {step!r} does not exist: the steps go from 0 (the start) to {world.last}")
        if getattr(_IN, "worker", False):
            return self._run_here(world, step, fn)
        box = {}

        def work():
            _IN.worker = True
            try:
                box["ok"] = self._run_here(world, step, fn)
            except BaseException as e:      # handed back to the caller's thread
                box["err"] = e
        with _STACK:
            old = threading.stack_size(BIG_STACK)
            try:
                th = threading.Thread(target=work, daemon=True)
                th.start()
            finally:
                threading.stack_size(old)
        th.join()
        if "err" in box:
            raise box["err"]
        return box["ok"]

    def _run_here(self, world, step, fn):
        with ENGINE_LOCK:
            mark = len(E.trail)
            self.pl.deadline = time.monotonic() + self.limits["seconds_per_question"]
            try:
                snap = self._load(world, step)
                return fn(snap)
            except E.Timeout:
                raise Rejection("timeout", f"the rules took more than {self.limits['seconds_per_question']} s on this question")
            except E.PrologError as e:
                raise Rejection("engine_error", "the rules raised an error: " + T.text(e.term))
            except RecursionError:
                raise Rejection("too_deep", "the rules recursed too deeply (a loop in the facts?)")
            finally:
                self.pl.deadline = None
                E.undo(mark)

    # ------------------------------------------------------------ the questions
    def holds(self, world, step, goal):
        return self._run(world, step, lambda snap: _first(self.pl, goal))

    def which(self, world, step, goal):
        """the distinct instances of goal that hold (as terms copied out of the engine)"""
        def q(snap):
            seen, out = set(), []
            mark = len(E.trail)
            for _ in self.pl.solve_closed(goal):
                c = E.copy_term_(goal)
                if T.text(c) not in seen:
                    seen.add(T.text(c))
                    out.append(c)
                if len(out) >= 200:
                    break
            E.undo(mark)
            return sorted(out, key=T.text)
        return self._run(world, step, q)

    def missing(self, world):
        """what the rules' missing/1 says is still needed before loading (terms), at the end of the pending world"""
        if not self.has_missing:
            return []
        v = E.Var()
        return [T.args(t)[0] for t in self.which(world, world.last, E.Struct("missing", [v]))]

    # ---- the user's meta-interpreter (META): each falls back to a default when the user wrote nothing
    def _first_words(self, name, g):
        """Words of name(g, W) (first solution), or None; call inside _run"""
        if (name, 2) not in self.meta:
            return None
        w = E.Var()
        mark = len(E.trail)
        it = self.pl.solve_closed(E.Struct(name, [g, w]))
        try:
            for _ in it:
                return words(w)
            return None
        finally:
            it.close()
            E.undo(mark)

    def ask_words(self, world, fact):
        """how to ask for a missing fact (ask/2), or None for the phrase's default"""
        if ("ask", 2) not in self.meta:
            return None
        return self._run(world, world.last, lambda snap: self._first_words("ask", fact))

    def can_change(self, world, fact):
        """may the agent change this fact by itself? default: any fact that changes"""
        ph = self.vocab.get(fact)
        if ph is None or not ph.changes:
            return False
        if ("can_change", 1) not in self.meta:
            return True
        return self._run(world, world.last, lambda snap: _first(self.pl, E.Struct("can_change", [fact])))

    def act(self, world, fact):
        """the device command for changing this fact (act/2), or None"""
        if ("act", 2) not in self.meta:
            return None
        return self._run(world, world.last, lambda snap: self._first_words("act", fact))

    def label(self, world, step):
        """how a step is called: 0 is the start, k is the k-th event"""
        if step == 0:
            return "the start"
        t, neg = world.events[step - 1]
        return f"step {step} ({self.vocab.say(t, negated=neg)})"

    # ---- why: the proof
    def why(self, world, step, goal):
        def q(snap):
            facts = snap[0]
            count = [0]
            for children in self._prove(goal, facts, count, 0):
                if len(children) == 1:
                    return children[0]
                return {"goal": T.text(goal), "say": self.vocab.say(goal), "because": children}
            return None
        tree = self._run(world, step, q)
        if tree is None:
            return None
        rests = []
        self._leaves(tree, rests)
        return {"tree": tree, "rests_on": rests}

    def _leaves(self, node, acc):
        for c in node.get("because", []):
            if "since" in c and c.get("changes") and not any(r["fact"] == c["fact"] for r in acc):
                acc.append({k: c[k] for k in ("fact", "say", "since")})
            self._leaves(c, acc)

    def _prove(self, g, facts, count, depth):
        """generator of proofs of g; each proof is the list of child nodes (phrased predicates only; the rest is flattened)"""
        count[0] += 1
        if count[0] > self.limits["tree_nodes"] * 20 or depth > self.limits["depth"]:
            return
        g = E.deref(g)
        sig = T.signature(g)
        if sig == (",", 2):
            for a in self._prove(g.args[0], facts, count, depth):
                for b in self._prove(g.args[1], facts, count, depth):
                    yield a + b
            return
        if sig in ((";", 2), ("->", 2), ("findall", 3), ("forall", 2), ("call", 1)) or sig in E.CONTROL and sig != ("\\+", 1):
            mark = len(E.trail)
            for _ in self.pl.solve_closed(g):
                yield []
            E.undo(mark)
            return
        if sig == ("\\+", 1):
            if not _first(self.pl, g.args[0]):
                inner = E.deref(g.args[0])
                if self.vocab.get(inner):
                    yield [{"goal": "\\+ " + T.text(inner), "say": self.vocab.say(inner, negated=True)}]
                else:
                    yield []
            return
        ph = self.vocab.get(g)
        if sig in E.SISTEMA or sig in self.pl.tools or (sig not in self.pl.db and sig not in self.pl.dynamics):
            mark = len(E.trail)
            for _ in self.pl.solve_closed(g):
                yield []
            E.undo(mark)
            return
        if ph is not None and ("explain", 2) in self.meta:
            said = self._first_words("explain", g)          # the user's explain/2 replaces the proof of g (g may still have variables)
            if said and _first(self.pl, g):
                yield [{"goal": T.text(g), "say": said}]
                return
        for cl in list(self.pl.db.get(sig, [])):
            mark = len(E.trail)
            head, body = cl.renombra()
            if E.unify(head, g):
                if isinstance(body, E.Atom) and body is E.TRUE:
                    txt = T.text(g)
                    leaf = {"goal": txt, "say": self.vocab.say(g) if ph else txt}
                    if txt in facts:
                        leaf.update(fact=txt, since=facts[txt][1], changes=bool(ph and ph.changes))
                    yield [leaf] if ph else []
                else:
                    for children in self._prove(body, facts, count, depth + 1):
                        if ph:
                            yield [{"goal": T.text(g), "say": self.vocab.say(g), "because": children}]
                        else:
                            yield children
            E.undo(mark)

    # ---- why not: the and/or tree of failures
    def why_not(self, world, step, goal):
        def q(snap):
            if _first(self.pl, goal):
                return None
            count = [0]
            tree = self._fail(goal, snap, count, 0, ())
            return tree
        tree = self._run(world, step, q)
        if tree is None:
            return {"tree": None, "ways": []}
        ways = self._ways(tree)
        return {"tree": tree, "ways": ways}

    def _leaf_need(self, g, snap):
        facts, cleared = snap
        ph = self.vocab.get(g)
        leaf = {"kind": "need", "fact": T.text(g), "say": self.vocab.say(g), "now": None, "since": None}
        if T.is_ground(g):
            k = self._key(g)
            for t, s in facts.values():
                if self.vocab.get(t).changes and self._key(t) == k:
                    leaf["now"], leaf["since"] = self.vocab.say(t), s
                    leaf["now_fact"] = T.text(t)
                    break
            else:
                if k in cleared:
                    leaf["since"] = cleared[k]
        return leaf if ph.changes else None

    def _fail(self, g, snap, count, depth, ancestors):
        """why g fails: None if it cannot be fixed by changing facts; a leaf; or {goal, alternatives:[[failures]]}"""
        count[0] += 1
        if count[0] > self.limits["tree_nodes"] or depth > self.limits["depth"]:
            return None
        g = E.deref(g)
        sig = T.signature(g)
        txt = T.text(g)
        if txt in ancestors:
            return None
        if sig == ("\\+", 1):
            proof = None
            for children in self._prove(g.args[0], snap[0], [0], 0):
                proof = {"because": children}
                break
            rests = []
            if proof:
                self._leaves(proof, rests)
            if not rests:
                return None
            return {"kind": "not", "fact": rests[0]["fact"], "say": self.vocab.say(T.parse(rests[0]["fact"])[0], negated=True),
                    "now": rests[0]["say"], "since": rests[0]["since"]}
        if sig == (",", 2):
            alts = self._body([g], snap, count, depth, ancestors)
            return {"goal": txt, "say": txt, "alternatives": alts} if alts else None
        if sig == (";", 2):
            alts = []
            for part in (g.args[0], g.args[1]):
                sub = self._fail(part, snap, count, depth + 1, ancestors)
                if sub:
                    alts.append([sub])
            return {"goal": txt, "say": txt, "alternatives": alts} if alts else None
        if sig in E.CONTROL or sig in E.SISTEMA or sig in self.pl.tools:
            return None
        ph = self.vocab.get(g)
        if ph is not None and ph.kind == "fact":
            return self._leaf_need(g, snap)
        alts = []
        for cl in list(self.pl.db.get(sig, [])):
            mark = len(E.trail)
            head, body = cl.renombra()
            if E.unify(head, g):
                alts += self._body(T.conjuncts(body), snap, count, depth + 1, ancestors + (txt,))
            E.undo(mark)
            if len(alts) > self.limits["alternatives"]:
                break
        if not alts:
            return None
        if ph is None and len(alts) == 1 and len(alts[0]) == 1:
            return alts[0][0]                     # unphrased helper with one failure: show the failure itself
        return {"goal": txt, "say": self.vocab.say(g) if ph else txt, "alternatives": alts}

    def _body(self, goals, snap, count, depth, ancestors, failures=()):
        """the alternatives (each a list of failures) of a clause body: goals that succeed bind variables and are walked
        through; a goal that fails is explained; a failure that cannot be fixed discards the alternative"""
        if not goals:
            return [list(failures)] if failures else []
        g, rest = goals[0], goals[1:]
        out, sols = [], 0
        mark = len(E.trail)
        it = self.pl.solve_closed(g)
        try:
            for _ in it:
                sols += 1
                out += self._body(rest, snap, count, depth, ancestors, failures)
                if sols >= self.limits["alternatives"]:
                    break
        finally:
            it.close()
            E.undo(mark)
        if sols:
            return out
        sub = self._fail(g, snap, count, depth, ancestors)
        if sub is None:
            return []
        if T.is_ground(g):
            return self._body(rest, snap, count, depth, ancestors, failures + (sub,))
        return [list(failures) + [sub]]

    def _ways(self, node):
        """the tree -> the ways to make the goal true: [[leaf, ...] (all needed), ...] (any one way is enough); minimal"""
        def dnf(n):
            if "kind" in n:
                return [frozenset([(n["kind"], n["fact"])])], {(n["kind"], n["fact"]): n}
            ways, leaves = [], {}
            for alt in n["alternatives"]:
                acc = [frozenset()]
                for f in alt:
                    sub, lv = dnf(f)
                    leaves.update(lv)
                    acc = [a | b for a in acc for b in sub][: self.limits["alternatives"] * 4]
                ways += acc
            return ways, leaves
        ways, leaves = dnf(node)
        ways = sorted(set(ways), key=lambda w: (len(w), sorted(w)))
        minimal = [w for w in ways if not any(o < w for o in ways)]
        return [[leaves[x] for x in sorted(w)] for w in minimal[: self.limits["alternatives"]]]

    # ---- change and timeline
    def change(self, world, goal, a, b):
        """steps where the (ground) goal flipped between a and b, each with the event that did it"""
        if a > b:
            a, b = b, a
        states = [(s, self.holds(world, s, goal)) for s in range(a, b + 1)]
        flips = []
        for (s0, v0), (s1, v1) in zip(states, states[1:]):
            if v0 != v1:
                t, neg = world.events[s1 - 1]
                flips.append({"step": s1, "now": v1, "event": T.text(t) if not neg else "not " + T.text(t),
                              "label": self.label(world, s1)})
        return {"from": a, "to": b, "at_start": states[0][1], "at_end": states[-1][1], "flips": flips,
                "states": [{"step": s, "holds": v} for s, v in states]}

    def timeline(self, world, goal, a, b):
        rows = []
        for s in range(a, b + 1):
            rows.append({"step": s, "label": self.label(world, s), "holds": [T.text(t) for t in self.which(world, s, goal)]})
        return rows
