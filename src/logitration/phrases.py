# Copyright (c) 2026 David Ragel
# SPDX-License-Identifier: BUSL-1.1
# Licensed under the Business Source License 1.1; see LICENSE.
"""Phrases: how each predicate is said in natural language. The only vocabulary the agent has.

The user writes a dict (or a JSON file with the same dict):

    {"lamp(X)":         "{X} is a lamp",
     "feeds(To, From)": ["{To} gets current from {From}", "{From} feeds {To}"],
     "state(X, V)":     {"say": "{X} is {V}", "changes": True, "ask": "Is {X} up or down?"},
     "lit(L)":          "{L} is lit"}

* the key is the predicate with named arguments; the value is one template, a list (the first one is used to speak,
  all of them to understand), or a dict with
    say      the template(s)
    set      True for a fact that is simply there or not (installed(m1, llama3)): it changes over time like `changes`, but all its
             arguments are its identity, so several can be true at once and "make" adds it
    changes  True if the fact changes over time (it can be an event). The LAST argument is the value; the others say
             what it is about, so `state(s1, up)` replaces `state(s1, down)`. With one argument it is on/off.
    ask      how to ask the client for it when it is missing (see missing/1 in the README)
    not      how to say it is false (default: "is" -> "is not", else "it is not true that ...")
    values   {"V": ["up", "down"]}: the only values an argument may take (checked before anything reaches the rules)
    types    {"G": "int>=0", "R": "ram|disk", "N": "name"}: the type of an argument, checked on every fact the client states (or a
             program observes) and on the constants of a question, before anything reaches the rules. Types: `name` (a lowercase
             name), `int` (with bounds: `int>=0`, `int>0<=100`), `a|b|c` (one of these names), `any`. In a .logi file they are
             written in the signature: capacity(N:name, R:ram|disk, G:int>=0). Inspired by Clorm (typed predicates for ASP).
    program  True (or {"via": ["vm_stat"]}) for a fact that a PROGRAM computes (a script that looks at the machines): see programmer.py
  In a template, {V=up} fixes an argument without words, for understanding only: "raise {X}{V=up}" = state(X, up).
  {V=?} means "the client is asking for V": "where can {J} run{N=?}" understands "where can j1 run?" as runnable(j1, N).
* which predicates the client may STATE and which it may ASK is read from the rules (Knowledge decides it):
  a predicate with a phrase and no rule is a fact the client states; one defined by rules is a question.
"""
import json
import re
from pathlib import Path

from logitration import engine as E
from logitration import terms as T

RE_KEY = re.compile(r"^\s*([a-z][a-z0-9_]{0,31})\s*(?:\(\s*([A-Z][A-Za-z0-9_]*(?:\s*,\s*[A-Z][A-Za-z0-9_]*)*)\s*\))?\s*$")
RE_TYPE_INT = re.compile(r"^int((?:\s*(?:>=|<=|>|<)\s*-?\d+)*)$")
RE_BOUND = re.compile(r"(>=|<=|>|<)\s*(-?\d+)")
RE_ATOM = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
WH = r"(?:what|who|which(?:\s+\w+)?|whatever)"


class PhraseError(ValueError):
    pass


class Type:
    """The type of one argument of a predicate. `any` | `name` | `int` with bounds | an enumeration `a|b|c`.
    check(v) -> None when v (a str for a name, an int for a number) has the type, else why not, in a few words."""

    def __init__(self, spec):
        s = re.sub(r"\s+", " ", str(spec).strip())
        self.spec, self.lo, self.hi, self.values = s, None, None, None
        m = RE_TYPE_INT.match(s)
        if s in ("any", "name"):
            self.kind = s
        elif m:
            self.kind = "int"
            for op, n in RE_BOUND.findall(m.group(1)):
                n = int(n)
                if op in (">=", ">"):
                    self.lo = max(self.lo if self.lo is not None else n, n + (op == ">"))
                else:
                    self.hi = min(self.hi if self.hi is not None else n, n - (op == "<"))
            if self.lo is not None and self.hi is not None and self.lo > self.hi:
                raise PhraseError(f"type {s!r}: no number satisfies it")
        elif "|" in s:
            self.kind = "enum"
            self.values = [v.strip() for v in s.split("|")]
            if not all(RE_ATOM.match(v) for v in self.values):
                raise PhraseError(f"type {s!r}: write the allowed names as  a|b|c  (lowercase letters, digits, _)")
        else:
            raise PhraseError(f"unknown type {s!r}: use name, int (int>=0, int>0<=100), a|b|c, or any")

    def describe(self):
        if self.kind == "any":
            return "anything"
        if self.kind == "name":
            return "a name (lowercase letters, digits and _)"
        if self.kind == "enum":
            return "one of " + ", ".join(self.values)
        if self.lo is not None and self.hi is not None:
            return f"a whole number from {self.lo} to {self.hi}"
        if self.lo is not None:
            return f"a whole number, {self.lo} or more"
        if self.hi is not None:
            return f"a whole number, {self.hi} or less"
        return "a whole number"

    def check(self, v):
        if self.kind == "any":
            return None
        if self.kind == "name":
            return None if isinstance(v, str) and RE_ATOM.match(v) else f"{v!r} is not a name"
        if self.kind == "enum":
            return None if isinstance(v, str) and v in self.values else f"{v!r} is not one of them"
        if isinstance(v, bool) or not isinstance(v, int):
            return f"{v!r} is not a whole number"
        if (self.lo is not None and v < self.lo) or (self.hi is not None and v > self.hi):
            return f"{v} is out of range"
        return None

    def compatible(self, other):
        """False when no value can have both types (a variable used as both: the file contradicts itself)"""
        a, b = self, other
        if "any" in (a.kind, b.kind):
            return True
        if a.kind == "enum" and b.kind == "enum":
            return bool(set(a.values) & set(b.values))
        if a.kind == "enum" or b.kind == "enum":
            return "int" not in (a.kind, b.kind)
        if a.kind != b.kind:
            return False
        if a.kind == "int":
            lo = max(x for x in (a.lo, b.lo) if x is not None) if (a.lo, b.lo) != (None, None) else None
            hi = min(x for x in (a.hi, b.hi) if x is not None) if (a.hi, b.hi) != (None, None) else None
            return lo is None or hi is None or lo <= hi
        return True


class Phrase:
    def __init__(self, key, value):
        m = RE_KEY.match(key)
        if not m:
            raise PhraseError(f"phrase key {key!r}: write it as name(Arg1, Arg2) with capitalised argument names")
        self.name = m.group(1)
        self.argnames = [a.strip() for a in m.group(2).split(",")] if m.group(2) else []
        self.arity = len(self.argnames)
        if isinstance(value, (str, list)):
            value = {"say": value}
        if not isinstance(value, dict) or "say" not in value:
            raise PhraseError(f"phrase {key!r}: give a template, a list of templates or a dict with 'say'")
        unknown = set(value) - {"say", "changes", "set", "ask", "not", "values", "types", "program"}
        if unknown:
            raise PhraseError(f"phrase {key!r}: unknown option(s) {sorted(unknown)}")
        says = value["say"] if isinstance(value["say"], list) else [value["say"]]
        if not says or not all(isinstance(s, str) and s.strip() for s in says):
            raise PhraseError(f"phrase {key!r}: empty template")
        for s in says + [value.get("ask") or "", value.get("not") or ""]:
            for ph in re.findall(r"\{(\w+)(?:=[a-z0-9_?]+)?\}", s):
                if ph not in self.argnames:
                    raise PhraseError(f"phrase {key!r}: template uses {{{ph}}}, which is not an argument")
        if re.search(r"\{\w+=", says[0]):
            raise PhraseError(f"phrase {key!r}: the first template is used to speak, so it cannot fix values ({{V=...}})")
        vals = value.get("values") or {}
        if not isinstance(vals, dict) or any(k not in self.argnames or not isinstance(v, list) for k, v in vals.items()):
            raise PhraseError(f"phrase {key!r}: 'values' must be {{\"Arg\": [allowed, ...]}}")
        self.values = {self.argnames.index(k): [str(x) for x in v] for k, v in vals.items()}
        tys = value.get("types") or {}
        if not isinstance(tys, dict) or any(k not in self.argnames for k in tys):
            raise PhraseError(f"phrase {key!r}: 'types' must be {{\"Arg\": \"int>=0\"}} with the names of its arguments")
        self.types = {self.argnames.index(k): Type(v) for k, v in tys.items()}
        prog = value.get("program")
        self.program = bool(prog)                       # a program computes this fact (a script that looks at the machines)
        self.program_via = list(prog.get("via") or []) if isinstance(prog, dict) else []
        self.says = says
        self.set = bool(value.get("set"))               # a member of a set that comes and goes: its key is ALL its arguments
        self.changes = bool(value.get("changes")) or self.set
        self.ask_template = value.get("ask")
        self.not_template = value.get("not")
        self.kind = None          # "fact" or "question": set by Knowledge after reading the rules

    def type_error(self, i, v):
        """why value v is not valid for argument i (None if it is), in the words of the phrase"""
        t = self.types.get(i)
        why = t.check(v) if t else None
        if why is None:
            return None
        return f"{self.argnames[i]} must be {t.describe()} ({why}); the phrase is «{self.says[0]}»"

    @property
    def signature(self):
        return self.name, self.arity

    def pattern(self):
        return f"{self.name}({', '.join(self.argnames)})" if self.arity else self.name

    # ---- speaking
    def _fill(self, template, values, unknown):
        def one(m):
            k = self.argnames.index(m.group(1))
            v = values[k] if k < len(values) else None
            return unknown if v is None else str(v)
        return re.sub(r"\{(\w+)\}", one, template)

    def render(self, values, unknown="?", negated=False):
        if negated:
            if self.not_template:
                return self._fill(self.not_template, values, unknown)
            s = self._fill(self.says[0], values, unknown)
            for verb in (" is ", " are ", " has ", " can "):
                if verb in s:
                    neg = {" has ": " does not have ", " can ": " cannot "}.get(verb, verb + "not ")
                    return s.replace(verb, neg, 1)
            return "it is not true that " + s
        return self._fill(self.says[0], values, unknown)

    def render_ask(self, values):
        if self.ask_template:
            return self._fill(self.ask_template, values, "?")
        return self.render(values, unknown="?") + "?"

    # ---- understanding (used by the simulated translator)
    def regexes(self, wh=False, form="plain"):
        """[(regex, argnames in group order, fixed values)] for each template that admits `form`:
        plain "l1 is lit" | ask "is l1 lit" | ask_not "is l1 not lit" | make "l1 lit" (after "make"). wh=True lets a placeholder be 'what/which X'."""
        out = []
        for s in self.says:
            if form != "plain":
                m = re.match(r"^\{(\w+)\} (is|are) (.*)$", s.strip())
                if not m:
                    continue
                s = (f"{{{m.group(1)}}} " + m.group(3)) if form == "make" else \
                    f"is {{{m.group(1)}}} " + ("not " if form == "ask_not" else "") + m.group(3)
            out.append(compile_template(s, wh))
        return out


def compile_template(s, wh=False):
    parts, order, fixed = [], [], {}
    for piece in re.split(r"(\{\w+(?:=[a-z0-9_?]+)?\})", s.strip()):
        m = re.match(r"\{(\w+)(?:=([a-z0-9_?]+))?\}", piece)
        if m and m.group(2):
            fixed[m.group(1)] = m.group(2)
        elif m:
            order.append(m.group(1))
            alt = r"[a-z][a-z0-9_]*|-?\d+" + ("|" + WH if wh else "")
            parts.append(r"(?:the\s+)?(" + alt + r")")
        elif piece.strip():
            piece = piece.lower()
            words = ["(?:is|are)" if w == "is" else re.escape(w) for w in piece.split()]
            lead = r"\s+" if piece[0].isspace() else ""
            tail = r"\s+" if piece[-1].isspace() else ""
            parts.append(lead + r"\s+".join(words) + tail)
        elif piece:
            parts.append(r"\s+")
    return re.compile("^" + "".join(parts) + "$"), order, fixed


def speak(template, values, argnames, unknown="?"):
    """a template filled with values (a list by argument); {V=...} parts (understanding only) are dropped"""
    def one(m):
        if m.group(2):
            return ""
        k = argnames.index(m.group(1))
        v = values[k] if k < len(values) else None
        return unknown if v is None else str(v)
    return re.sub(r"\s+", " ", re.sub(r"\{(\w+)(=[a-z0-9_?]+)?\}", one, template)).strip()


class Vocabulary:
    """All the phrases, by (name, arity)."""

    def __init__(self, phrases):
        if isinstance(phrases, (str, Path)):
            p = Path(phrases)
            try:
                phrases = json.loads(p.read_text())
            except (OSError, ValueError) as e:
                raise PhraseError(f"cannot read phrases from {p}: {e}")
        if not isinstance(phrases, dict) or not phrases:
            raise PhraseError("phrases must be a non-empty dict (or a JSON file with one)")
        self.by_sig = {}
        for k, v in phrases.items():
            ph = Phrase(k, v)
            if ph.signature in self.by_sig:
                raise PhraseError(f"two phrases for {ph.name}/{ph.arity}")
            self.by_sig[ph.signature] = ph

    def __iter__(self):
        return iter(self.by_sig.values())

    def get(self, t):
        sig = T.signature(t)
        return self.by_sig.get(sig) if sig else None

    def facts(self):
        return [p for p in self if p.kind == "fact"]

    def questions(self):
        return [p for p in self if p.kind == "question"]

    def say(self, t, negated=False, unknown="?"):
        """engine term -> sentence (or its Prolog text if it has no phrase)."""
        ph = self.get(t)
        if ph is None:
            return ("not " if negated else "") + T.text(t)
        vals = [None if isinstance(a, E.Var) else T.value(a) for a in T.args(t)]
        return ph.render(vals, unknown=unknown, negated=negated)
