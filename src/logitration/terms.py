# Copyright (c) 2026 David Ragel
# SPDX-License-Identifier: BUSL-1.1
# Licensed under the Business Source License 1.1; see LICENSE.
"""Terms: a tiny, closed parser for what the translator returns, and the way back to text.

Only three shapes are accepted: a name (`l1`), a variable (`X`), an integer, and `name(arg, ...)` built from those.
Nothing else (no operators, no quotes, no lists) gets in, and the result is built with the engine's Atom/Struct/Var
constructors: client or LLM text is never handed to the Prolog reader.
"""
import re

from logitration import engine as E

RE_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}$")
_TOKEN = re.compile(r"\s*(?:([a-z][a-z0-9_]{0,31})|([A-Z_][A-Za-z0-9_]{0,31})|(-?\d{1,9})|([(),]))")
MAX_DEPTH = 4
MAX_ARGS = 8


class TermError(ValueError):
    pass


def parse(text, allow_vars=True):
    """'feeds(l1, c0)' -> engine term. Returns (term, {var name: Var}). Raises TermError."""
    if not isinstance(text, str) or not text.strip() or len(text) > 400:
        raise TermError(f"not a term: {text!r}")
    toks, pos, s = [], 0, text.strip()
    while pos < len(s):
        m = _TOKEN.match(s, pos)
        if not m or m.end() == pos:
            raise TermError(f"unexpected text at {s[pos:pos + 12]!r}")
        toks.append(m.groups())
        pos = m.end()
    names = {}
    i = [0]

    def term(depth):
        if depth > MAX_DEPTH:
            raise TermError("term nested too deep")
        if i[0] >= len(toks):
            raise TermError("incomplete term")
        name, var, num, punct = toks[i[0]]
        i[0] += 1
        if num is not None:
            return int(num)
        if var is not None:
            if not allow_vars:
                raise TermError(f"variable {var} not allowed here")
            if var == "_":
                return E.Var()
            return names.setdefault(var, E.Var())
        if name is None:
            raise TermError(f"unexpected {punct!r}")
        if i[0] < len(toks) and toks[i[0]][3] == "(":
            i[0] += 1
            args = [term(depth + 1)]
            while i[0] < len(toks) and toks[i[0]][3] == ",":
                i[0] += 1
                args.append(term(depth + 1))
            if i[0] >= len(toks) or toks[i[0]][3] != ")":
                raise TermError("missing ')'")
            i[0] += 1
            if len(args) > MAX_ARGS:
                raise TermError("too many arguments")
            return E.Struct(name, args)
        return E.Atom(name)

    t = term(0)
    if i[0] != len(toks):
        raise TermError("text after the term")
    return t, names


def signature(t):
    """(name, arity) of a term."""
    t = E.deref(t)
    if isinstance(t, E.Struct):
        return t.name, len(t.args)
    if isinstance(t, E.Atom):
        return t.name, 0
    return None


def args(t):
    t = E.deref(t)
    return [E.deref(a) for a in t.args] if isinstance(t, E.Struct) else []


def text(t):
    """engine term -> Prolog text (variables as _G1...)."""
    return E.write_term(t, True)


def is_ground(t):
    t = E.deref(t)
    if isinstance(t, E.Var):
        return False
    if isinstance(t, E.Struct):
        return all(is_ground(a) for a in t.args)
    return True


def value(t):
    """an argument as Python: atom -> str, int -> int, anything else -> its text."""
    t = E.deref(t)
    if isinstance(t, E.Atom):
        return t.name
    if isinstance(t, (int, float)):
        return t
    return text(t)


def build(name, values):
    return E.Struct(name, [to_term(v) for v in values]) if values else E.Atom(name)


def to_term(v):
    if isinstance(v, bool):
        raise TermError("booleans are not terms")
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        return E.Atom(v)
    raise TermError(f"cannot turn {v!r} into a term")


def conjuncts(body):
    body = E.deref(body)
    if isinstance(body, E.Struct) and body.name == "," and len(body.args) == 2:
        return conjuncts(body.args[0]) + conjuncts(body.args[1])
    return [body]
