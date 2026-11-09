"""The electrical agent inside: validation, sessions (one Prolog per circuit) and the question catalogue.

Security rules (finding: server.pl would run any query it received):
  * the client sends structured JSON; its text is NEVER turned into a Prolog goal;
  * each name is validated with a closed expression and every term (facts and queries) is built by THIS module with
    Atom/Struct/int; no client text goes through the Prolog reader;
  * the question catalogue is a fixed list; anything else is rejected with a code;
  * the interpreter's run/2 builtin executes arbitrary commands: the .pl files loaded here never call it.
The interpreter is src/circuit/prolog.py, imported by path (neither copied nor changed) and loading its .pl files untouched.
"""
import importlib.util
import re
import queue
import signal
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]   # src/
CIRCUIT_DIR = ROOT / "circuit"

_spec = importlib.util.spec_from_file_location("prolog_circuit", CIRCUIT_DIR / "prolog.py")
P = importlib.util.module_from_spec(_spec)
sys.modules["prolog_circuit"] = P
_spec.loader.exec_module(P)

LIMITS = {
    "bytes_per_message": 65536,
    "parts": 60,
    "wires": 120,
    "client_events": 200,
    "total_events": 400,
    "seconds_per_question": 5,
    "live_sessions": 16,
    "session_ttl_s": 3600,
    "tree_nodes": 400,
}

TYPES = ("source", "wire", "socket", "fuse", "lamp", "switch")
ACTIONS = {"raise": "up", "lower": "down", "blow": "blown", "repair": "ok"}
RE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
RE_CIRCUIT = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
RE_COND = re.compile(r"^(up|down|ok)\(([a-z][a-z0-9_]{0,23})\)$")
RESERVED = {"true", "proof"}


class Rejection(Exception):
    """The request is rejected before reaching Prolog. code: stable, for programs; detail: for people."""

    def __init__(self, code, detail, path=None):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail, self.path = code, detail, path

    def to_json(self):
        e = {"code": self.code, "detail": self.detail}
        if self.path:
            e["path"] = self.path
        return e


class Timeout(BaseException):
    pass


@contextmanager
def deadline(seconds):
    def alarm(*_):
        raise Timeout()
    previous = signal.signal(signal.SIGALRM, alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


# ------------------------------------------------------------------ the core lives in the main thread
# SIGALRM (the time limit) only works there. The server threads (which wait for the LLM) hand to the main thread whatever touches Prolog.
_QUEUE = queue.Queue()


def run_in_core(fn):
    """run fn() in the main thread (where the time limit lives); if we are already in it, directly"""
    if threading.current_thread() is threading.main_thread():
        return fn()
    ev, box = threading.Event(), {}
    _QUEUE.put((fn, box, ev))
    ev.wait()
    if "exc" in box:
        raise box["exc"]
    return box["r"]


def serve_queue(wait=0.3):
    """the main thread serves one pending job (returns True if there was one)"""
    try:
        fn, box, ev = _QUEUE.get(timeout=wait)
    except queue.Empty:
        return False
    try:
        box["r"] = fn()
    except BaseException as e:      # includes Timeout
        box["exc"] = e
    ev.set()
    return True


# ------------------------------------------------------------------ validation of the definition
def _check_name(v, path):
    if not isinstance(v, str) or not RE_NAME.match(v) or v in RESERVED:
        raise Rejection("invalid_name", f"{v!r} is not a valid name (lowercase initial, letters/digits/_, at most 24)", path)
    return v


def _check_keys(d, allowed, path):
    if not isinstance(d, dict):
        raise Rejection("invalid_format", "a JSON object was expected", path)
    for k in d:
        if k not in allowed:
            raise Rejection("unknown_field", f"field not allowed: {k!r}", f"{path}.{k}" if path else k)


def _check_int(v, path, minimum=1):
    if isinstance(v, bool) or not isinstance(v, int) or v < minimum:
        raise Rejection("invalid_step", f"an integer >= {minimum} was expected, got {v!r}", path)
    return v


def condition_of(c, path):
    """'up(s1)' | 'down(s1)' | 'ok(f1)' | {'type':..., 'part':...} -> (type, part)"""
    if isinstance(c, dict):
        _check_keys(c, {"type", "part"}, path)
        c = f"{c.get('type')}({c.get('part')})"
    m = RE_COND.match(c) if isinstance(c, str) else None
    if not m:
        raise Rejection("invalid_condition", f"condition not allowed: {c!r}; use up(x), down(x) or ok(x)", path)
    return m.group(1), m.group(2)


def validate_definition(d):
    """Check parts and wires; return the normalised model. May raise Rejection."""
    parts, wires = d.get("parts"), d.get("wires", [])
    if not isinstance(parts, list) or not parts:
        raise Rejection("invalid_format", "'parts' must be a non-empty list", "parts")
    if not isinstance(wires, list):
        raise Rejection("invalid_format", "'wires' must be a list", "wires")
    if len(parts) > LIMITS["parts"]:
        raise Rejection("too_large", f"more than {LIMITS['parts']} parts", "parts")
    if len(wires) > LIMITS["wires"]:
        raise Rejection("too_large", f"more than {LIMITS['wires']} wires", "wires")
    types, pos, order, missing = {}, {}, [], []
    for i, p in enumerate(parts):
        path = f"parts[{i}]"
        _check_keys(p, {"name", "type", "position"}, path)
        n = _check_name(p.get("name"), path + ".name")
        if n in types:
            raise Rejection("duplicate_part", f"part {n} appears twice", path + ".name")
        t = p.get("type")
        if t not in TYPES:
            raise Rejection("unknown_type", f"unknown type {t!r}; allowed: {', '.join(TYPES)}", path + ".type")
        po = p.get("position")
        if t == "switch":
            if po is None:
                missing.append(n)
            elif po not in ("up", "down"):
                raise Rejection("invalid_position", f"position {po!r}: up or down", path + ".position")
        elif po is not None:
            raise Rejection("invalid_position", f"only switches have a position ({n} is {t})", path + ".position")
        types[n], pos[n] = t, po
        order.append(n)
    wire_list = []
    for i, c in enumerate(wires):
        path = f"wires[{i}]"
        _check_keys(c, {"to", "from", "when"}, path)
        to_, from_ = c.get("to"), c.get("from")
        for k, v in (("to", to_), ("from", from_)):
            _check_name(v, f"{path}.{k}")
            if v not in types:
                raise Rejection("unknown_part", f"wire {i} uses {v}, which is not among the parts", f"{path}.{k}")
            if types[v] == "switch":
                raise Rejection("invalid_wire", f"{v} is a switch: it carries no current, it only conditions wires", f"{path}.{k}")
        if to_ == from_:
            raise Rejection("wire_cycle", f"wire {i} joins {to_} to itself", path)
        cond = None
        if c.get("when") is not None:
            k, pz = condition_of(c["when"], path + ".when")
            if pz not in types:
                raise Rejection("unknown_part", f"the condition uses {pz}, which is not among the parts", path + ".when")
            if k in ("up", "down") and types[pz] != "switch":
                raise Rejection("invalid_condition", f"{k}({pz}): {pz} is not a switch", path + ".when")
            if k == "ok" and types[pz] not in ("lamp", "fuse"):
                raise Rejection("invalid_condition", f"ok({pz}): {pz} cannot be blown (only a lamp or a fuse)", path + ".when")
            cond = (k, pz)
        wire_list.append((to_, from_, cond))
    # no loops: to -> from must not lead back to the start (even if the condition does not hold today)
    nxt = {}
    for to_, from_, _ in wire_list:
        nxt.setdefault(to_, []).append(from_)
    state = {}

    def dfs(x, trail):
        state[x] = 1
        for y in nxt.get(x, []):
            if state.get(y) == 1:
                raise Rejection("wire_cycle", "the wires form a loop: " + " -> ".join(trail + [x, y]), "wires")
            if y not in state:
                dfs(y, trail + [x])
        state[x] = 2
    for x in list(nxt):
        if x not in state:
            dfs(x, [])
    return {"order": order, "type": types, "pos": pos, "wires": wire_list, "missing_position": missing}


def validate_events(events, types, path="events"):
    if not isinstance(events, list):
        raise Rejection("invalid_format", "'events' must be a list", path)
    if len(events) > LIMITS["client_events"]:
        raise Rejection("too_large", f"more than {LIMITS['client_events']} events in one message", path)
    out = []
    for i, e in enumerate(events):
        r = f"{path}[{i}]"
        _check_keys(e, {"action", "part"}, r)
        a = e.get("action")
        if a not in ACTIONS:
            raise Rejection("invalid_action", f"action {a!r}; allowed: {', '.join(ACTIONS)}", r + ".action")
        pz = _check_name(e.get("part"), r + ".part")
        if pz not in types:
            raise Rejection("unknown_part", f"event {i} uses {pz}, which does not exist", r + ".part")
        if a in ("raise", "lower") and types[pz] != "switch":
            raise Rejection("invalid_action", f"{a} {pz}: {pz} is not a switch", r)
        if a in ("blow", "repair") and types[pz] not in ("lamp", "fuse"):
            raise Rejection("invalid_action", f"{a} {pz}: only lamps and fuses can be blown or repaired", r)
        out.append((a, pz))
    return out


# ------------------------------------------------------------------ terms (built by this module)
def A(x):
    return P.Atom(x)


def S(name, *args):
    return P.Struct(name, args)


def assembly_terms(m):
    ev = []
    for n in m["order"]:
        t = m["type"][n]
        ev.append(S("part", A(n), A(t)))
        if t in ("lamp", "fuse"):
            ev.append(S("ok", A(n)))
        elif t == "switch":
            ev.append(S(m["pos"][n], A(n)))
    for to_, from_, c in m["wires"]:
        ev.append(S("connects", A(to_), A(from_), S(c[0], A(c[1])) if c else A("true")))
    return ev


def event_term(action, part):
    return S(ACTIONS[action], A(part))


# ------------------------------------------------------------------ conversion of results
def txt(t):
    return P.write_term(t, True)


def plain_txt(t):
    return P.write_term(t)


def py_list(t):
    out, t = [], P.deref(t)
    while isinstance(t, P.Struct) and t.name == "." and len(t.args) == 2:
        out.append(P.deref(t.args[0]))
        t = P.deref(t.args[1])
    return out


def event_pair(t):
    """E-Fact -> {'event': E, 'fact': 'fact'}"""
    t = P.deref(t)
    return {"event": P.deref(t.args[0]), "fact": txt(t.args[1])}


def conjuncts(b):
    b = P.deref(b)
    if isinstance(b, P.Struct) and b.name == "&" and len(b.args) == 2:
        return conjuncts(b.args[0]) + conjuncts(b.args[1])
    return [b]


class Session:
    def __init__(self, name, model, events, user_actions=None):
        self.name = name
        self.model = model              # normalised parts/wires
        self.events = list(events)      # [(action, part)]
        # what the user said (plug/unplug where the core sees raise/lower)
        self.user_actions = list(user_actions) if user_actions else [a for a, _ in self.events]
        self.created = self.last = time.time()
        self.restarts = 0
        self.build()
        self.versions = [{"version": 1, "last": self.n, "events": len(self.events)}]

    def build(self):
        """A fresh, clean Prolog: load the rules (untouched) and record the events."""
        pl = P.Prolog()
        pl.dir = CIRCUIT_DIR
        pl.consult("queries.pl")
        pl.consult("explain.pl")
        self.pl = pl
        self.n = 0
        for t in assembly_terms(self.model):
            self.record(t)
        self.assembled = self.n
        for a, pz in self.events:
            self.record(event_term(a, pz))

    def record(self, term):
        self.n += 1
        self.pl.add(S("event", self.n, term))

    def restart(self):
        self.restarts += 1
        self.build()

    def add_events(self, events, user_actions=None):
        if self.n + len(events) > LIMITS["total_events"]:
            raise Rejection("too_large", f"the circuit would reach more than {LIMITS['total_events']} events")
        before = self.n
        for k, (a, pz) in enumerate(events):
            self.events.append((a, pz))
            self.user_actions.append(user_actions[k] if user_actions else a)
            self.record(event_term(a, pz))
        self.versions.append({"version": len(self.versions) + 1, "last": self.n, "events": len(self.events)})
        return before

    def event_map(self):
        """name -> event number of everything assembled or done (what the client needs to ask about steps)"""
        n, parts = 0, {}
        ut = self.model.get("user_type", {})
        for name in self.model["order"]:
            t = self.model["type"][name]
            n += 1
            d = {"type": ut.get(name, t), "part_event": n}
            if t in ("lamp", "fuse", "switch"):
                n += 1
                d["state_event"] = n
                if t == "switch":
                    d["initial_position"] = self.model["pos"][name]
            parts[name] = d
        group_of = {}
        for g in self.model.get("wire_groups", []):
            for b in g["bases"]:
                group_of[b] = g["two_way_switch"]
        wires = []
        for i, (to_, from_, c) in enumerate(self.model["wires"]):
            n += 1
            e = {"event": n, "to": to_, "from": from_, "when": f"{c[0]}({c[1]})" if c else None}
            if i in group_of:
                e["of_two_way_switch"] = group_of[i]
            wires.append(e)
        assert n == self.assembled, (n, self.assembled)
        events = []
        for k, (a, pz) in enumerate(self.events, 1):
            n += 1
            events.append({"index": k, "event": n, "action": self.user_actions[k - 1], "part": pz})
        return {"parts": parts, "wires": wires, "events": events, "assembly_until": self.assembled, "last": self.n,
                "initial_step": self.assembled,
                "numbering": "step = event; the initial state (assembled, no client events) is 'assembly_until'; client event k is assembly_until + k"}

    # --- queries
    def solve(self, goal_fn, deadline_s=None):
        """goal_fn() -> (goal, output). Return the value of the variables of the first solution, already in Python."""
        deadline_s = deadline_s or LIMITS["seconds_per_question"]
        self.last = time.time()
        mark = len(P.trail)
        try:
            with deadline(deadline_s):
                goal, output = goal_fn()
                for _ in self.pl.solve(goal, object()):
                    return output()
                return None
        except P.PrologError as e:
            raise Rejection("engine_error", "the Prolog engine raised an error: " + txt(e.term)) from None
        finally:
            P.undo(mark)

    def lamps(self):
        return [n for n in self.model["order"] if self.model["type"][n] == "lamp"]

    def step(self, m, path="step"):
        if m is None:
            return self.n
        _check_int(m, path)
        if m > self.n:
            raise Rejection("invalid_step", f"step {m} does not exist: the circuit goes up to {self.n}", path)
        return m

    def lamp_part(self, p, path="part"):
        _check_name(p, path)
        if p not in self.model["type"]:
            raise Rejection("unknown_part", f"{p} is not a part of circuit {self.name}", path)
        if self.model["type"][p] != "lamp":
            raise Rejection("not_a_lamp", f"{p} is a {self.model['type'][p]}, not a lamp", path)
        return p

    # --- catalogue questions (each builds ITS query)
    def lit(self, n):
        def q():
            ls = P.Var()
            return S("lit", n, ls), lambda: [plain_txt(x) for x in py_list(ls)]
        on = self.solve(q)
        return {"step": n, "lit": on, "unlit": [l for l in self.lamps() if l not in on],
                "query": f"lit({n}, Ls)"}

    def supporting(self, n, l):
        def q():
            es = P.Var()
            return S("supporting", n, A(l), es), lambda: [event_pair(x) for x in py_list(es)]
        r = self.solve(q)
        if r is None:
            return {"step": n, "part": l, "lit": False, "supporting": [], "query": f"supporting({n}, {l}, Es)"}
        return {"step": n, "part": l, "lit": True, "supporting": r, "query": f"supporting({n}, {l}, Es)"}

    def fact_events(self):
        """after how/supporting: which event put each fact of the KB there (from_event/2)."""
        res = {}
        mark = len(P.trail)
        try:
            h, e, lst = P.Var(), P.Var(), P.Var()
            g = S("findall", S("-", h, e), S("from_event", h, e), lst)
            for _ in self.pl.solve(g, object()):
                for pair in py_list(lst):
                    res.setdefault(txt(pair.args[0]), P.deref(pair.args[1]))
                break
        finally:
            P.undo(mark)
        return res

    def how(self, n, l):
        def q():
            a = P.Var()
            return S("how", n, A(l), a), lambda: self._tree(a)
        r = self.solve(q)
        if r is None:
            return {"step": n, "part": l, "lit": False, "tree": None, "prolog_tree": None,
                    "query": f"how({n}, {l}, A)"}
        tree, text = r
        return {"step": n, "part": l, "lit": True, "tree": tree, "prolog_tree": text,
                "query": f"how({n}, {l}, A)"}

    def _tree(self, a):
        """turn the proof (still bound) into JSON; each fact carries the event that put it there"""
        evs = self.fact_events()
        count = [0]

        def node(t):
            t = P.deref(t)
            count[0] += 1
            if count[0] > LIMITS["tree_nodes"]:
                raise Rejection("too_large", "the proof tree exceeds the node limit")
            h = txt(t.args[0])
            d = {"fact": h}
            if h in evs:
                d["event"] = evs[h]
            body = P.deref(t.args[1])
            if not (isinstance(body, P.Atom) and body.name == "true"):
                d["because"] = [node(c) for c in conjuncts(body)]
            return d
        return node(a), txt(a)

    def why(self, n1, n2, l):
        def q():
            r = P.Var()
            return S("why", n1, n2, A(l), r), lambda: (P.deref(r).name, [event_pair(x) for x in py_list(P.deref(r).args[0])], txt(r))
        r = self.solve(q)
        base = {"part": l, "from_step": n1, "to_step": n2, "query": f"why({n1}, {n2}, {l}, R)"}
        if r is None:
            now = l in self.lit(n2)["lit"]
            return {**base, "change": "no_change", "events": [], "now_lit": now}
        change, evs, text = r
        return {**base, "change": {"turned_on": "turned_on", "turned_off": "turned_off"}[change], "events": evs,
                "prolog": text, "now_lit": change == "turned_on"}

    def why_not(self, n, l):
        def q():
            f = P.Var()
            return S("why_not", n, A(l), f), lambda: self._fails(f)
        base = {"step": n, "part": l, "query": f"why_not({n}, {l}, F)"}
        r = self.solve(q)
        if r is None:
            return {**base, "lit": l in self.lit(n)["lit"], "fails": None, "missing": []}
        return {**base, "lit": False, **r}

    def _fails(self, f):
        part, causes = P.deref(f).args
        missing = []
        for c in py_list(causes):
            h, cause = c.args
            cause = P.deref(cause)
            missing.append({"would_need": txt(h),
                            "left_by": ({"event": P.deref(cause.args[0]), "fact": txt(cause.args[1])}
                                        if isinstance(cause, P.Struct) else None)})
        return {"fails": txt(part), "missing": missing}

    # --- extended queries (all of them VALID queries to the core, built by this module)
    def evaluate(self, n, terms):
        """of these terms (up(x), ok(x), has_current(x)...), which ones are PROVED after event n -> set of texts"""
        if not terms:
            return set()
        lst = P.NIL
        for t in reversed(terms):
            lst = S(".", t, lst)
        self.solve(lambda: (S("translate", n), lambda: True))

        def q():
            c, ok = P.Var(), P.Var()
            return (S("findall", c, S(",", S("member", c, lst), S("prove", c)), ok),
                    lambda: {txt(x) for x in py_list(ok)})
        return self.solve(q) or set()

    def cause_of(self, n, term):
        def q():
            c = P.Var()
            return S("cause", n, term, c), lambda: self._cause(c)
        return self.solve(q)

    @staticmethod
    def _cause(c):
        c = P.deref(c)
        if isinstance(c, P.Struct) and c.name == "-":
            return {"event": P.deref(c.args[0]), "fact": txt(c.args[1])}
        return None

    def paths_to_source(self, x, limit=40):
        incoming = {}
        for to_, from_, c in self.model["wires"]:
            incoming.setdefault(to_, []).append((from_, c))
        paths, truncated = [], [False]

        def dfs(p, via, wires):
            if len(paths) >= limit:
                truncated[0] = True
                return
            if self.model["type"][p] == "source":
                paths.append((via, wires))
                return
            for from_, c in incoming.get(p, []):
                dfs(from_, via + [from_], wires + [(p, from_, c)])
        dfs(x, [x], [])
        return paths, truncated[0]

    def why_not_all(self, n, l):
        """EVERYTHING that stops l from shining. Reading: ALL the `failures` must be fixed (and); under has_current ONE `path` is enough (or);
        within a path ALL its `cuts` must be fixed (and). The truth of each condition comes from the core (prove/1); this
        module only walks the wires, which are data."""
        base = self.why_not(n, l)
        if base["lit"]:
            return {**base, "failures": []}
        parts = {"ok": S("ok", A(l)), "has_current": S("has_current", A(l))}
        paths, truncated = self.paths_to_source(l)
        conds = {}
        for _, wires in paths:
            for _, _, c in wires:
                if c:
                    conds[f"{c[0]}({c[1]})"] = S(c[0], A(c[1]))
        ok = self.evaluate(n, list(parts.values()) + list(conds.values()))
        failures = []
        if txt(parts["ok"]) not in ok:
            failures.append({"part": f"ok({l})",
                             "cuts": [{"would_need": f"ok({l})", "left_by": self.cause_of(n, parts["ok"])}]})
        warning = None
        if txt(parts["has_current"]) not in ok:
            rs = []
            for via, wires in paths:
                cuts, seen = [], set()
                for _, _, c in wires:
                    h = f"{c[0]}({c[1]})" if c else None
                    if h and h not in ok and h not in seen:
                        seen.add(h)
                        cuts.append({"would_need": h, "left_by": self.cause_of(n, conds[h])})
                rs.append({"via": via, "cuts": cuts})
            if not rs:
                warning = f"no wire leads from a source to {l}"
            elif any(not r["cuts"] for r in rs):
                warning = "inconsistency: a path without cuts but without current (this should not happen)"
            failures.append({"part": f"has_current({l})", "one_path_suffices": True, "paths": rs, "paths_truncated": truncated})
        if not failures:
            warning = "the core does not prove the lamp but no part of its law fails (this should not happen)"
        return {**base, "failures": failures, "warning": warning,
                "reading": "To make it shine ALL the failures must be fixed (and); under has_current ONE path is enough (or); within a path "
                           "ALL its cuts must be fixed (and). The core's `missing` field is the union of the first cuts of each path (or)."}

    def states_of(self, l, n1, n2):
        """[(step, lit)] at the ends and at each client event in between (a half-done assembly does not count)"""
        ms = [n1] + [m for m in range(n1 + 1, n2) if m >= self.assembled] + [n2]
        if len(ms) > 160:
            raise Rejection("too_large", "the stretch has more than 160 steps; ask about a shorter one")
        return [(m, l in self.lit(m)["lit"]) for m in ms]

    def why_ext(self, n1, n2, l):
        """the core's why + stretch by stretch: warn if the ends are equal but there were changes in between, and give each change
        between consecutive steps (where the semantics of queries.pl is exact)"""
        r = self.why(n1, n2, l)
        states = self.states_of(l, n1, n2)
        segments = []
        for (ma, ea), (mb, eb) in zip(states, states[1:]):
            if ea != eb:
                t = self.why(ma, mb, l)
                segments.append({"from_step": ma, "to_step": mb, "change": t["change"], "events": t["events"]})
        r["states"] = [{"step": m, "lit": e} for m, e in states]
        r["segments"] = segments
        r["total_changes"] = len(segments)
        if r["change"] == "no_change" and segments:
            r["warning"] = (f"{l} is the same at event {n1} and at {n2}, but it changed {len(segments)} times in between: "
                            "look at `segments` (each change between consecutive steps)")
        elif len(segments) > 1:
            r["warning"] = (f"between {n1} and {n2} {l} changed {len(segments)} times; the core's causes for the whole range may drag in "
                            "events that do not explain the final change: use `segments`")
        return r

    def timeline(self, from_step, to_step, part=None):
        ms = [m for m in range(from_step, to_step + 1) if m >= self.assembled or m == from_step]
        if len(ms) > 160:
            raise Rejection("too_large", "the timeline has more than 160 steps")
        lamps, prev, rows = self.lamps(), None, []
        for m in ms:
            on = self.lit(m)["lit"]
            row = {"step": m, "event_index": (m - self.assembled) if m >= self.assembled else None, "lit": on,
                   "unlit": [x for x in lamps if x not in on]}
            if m > self.assembled and (m - self.assembled) <= len(self.events):
                a, pz = self.user_actions[m - self.assembled - 1], self.events[m - self.assembled - 1][1]
                row["what_happened"] = {"action": a, "part": pz}
            if prev is not None:
                row["turn_on"] = [x for x in on if x not in prev]
                row["turn_off"] = [x for x in prev if x not in on]
            prev = on
            rows.append(row)
        return {"from_step": from_step, "to_step": to_step, "lamps": lamps, "part": part, "steps": rows,
                "query": f"lit(N, Ls) for each N from {from_step} to {to_step}"}

    def sentence(self, kind, **a):
        """the plain-English sentence, with the example's own explain.pl"""
        def q():
            t = P.Var()
            if kind == "unlit":
                return S("unlit", a["n"], A(a["l"]), t), lambda: plain_txt(t)
            if kind == "holds_up":
                return S("holds_up", a["n"], A(a["l"]), t), lambda: plain_txt(t)
            return S("change", a["n1"], a["n2"], A(a["l"]), t), lambda: plain_txt(t)
        return self.solve(q)


# ------------------------------------------------------------------ indented tree
def indented(node, prefix="", last=True, root=True):
    mark = "" if root else ("└─ " if last else "├─ ")
    ev = f"   [event {node['event']}]" if "event" in node else ""
    lines = [prefix + mark + node["fact"] + ev]
    children = node.get("because", [])
    new = prefix + ("" if root else ("   " if last else "│  "))
    for i, h in enumerate(children):
        lines += indented(h, new, i == len(children) - 1, False)
    return lines


# ------------------------------------------------------------------ session store
class Store:
    def __init__(self):
        self.d = {}

    def expire(self):
        now = time.time()
        for k in [k for k, s in self.d.items() if now - s.last > LIMITS["session_ttl_s"]]:
            del self.d[k]

    def get(self, name):
        self.expire()
        if name not in self.d:
            raise Rejection("unknown_circuit", f"there is no circuit called {name!r}; define it first", "circuit")
        return self.d[name]

    def put(self, s, replace=False):
        self.expire()
        if s.name in self.d and not replace:
            raise Rejection("circuit_exists", f"{s.name!r} already exists; use replace:true, events, or action:'cancel'", "circuit")
        if s.name not in self.d and len(self.d) >= LIMITS["live_sessions"]:
            raise Rejection("too_many_sessions", f"there are {len(self.d)} live circuits (limit {LIMITS['live_sessions']}); cancel one")
        self.d[s.name] = s

    def remove(self, name):
        self.get(name)
        del self.d[name]
