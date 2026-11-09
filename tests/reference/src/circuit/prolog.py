"""A small Prolog interpreter of our own, in Python: it reads, unifies and solves with backtracking.

It loads the same files as SWI-Prolog (server.pl, queries.pl, the meta-interpreters in src/metainterp) untouched,
and serves them the same way: `python3 prolog.py server.pl` does what `swipl -q server.pl` does.

Four parts:
  1. the reader: text -> terms (tokens, and operators with their priority, op/3 included)
  2. unification: two terms are made equal by binding variables; each binding goes on the trail so it can be undone
  3. resolution: a goal is tried against each clause whose head unifies, in order; one Python generator per goal,
     so asking for the next solution IS backtracking. The cut (!) is an exception that rises up to the call
     that set it.
  4. the system predicates our files use (findall, assertz, format, sort...)

Usage: python3 prolog.py file.pl ...            (loads; if there is initialization(G, main), runs G and exits)
       python3 prolog.py -g Goal -t halt f.pl   (like swipl: loads, proves Goal, exits)
"""
import functools
import subprocess
import sys
from pathlib import Path

sys.setrecursionlimit(100000)


# ---------------------------------------------------------------- terms
class Atom:
    table = {}
    __slots__ = ("name",)

    def __new__(cls, name):
        a = cls.table.get(name)
        if a is None:
            a = cls.table[name] = object.__new__(cls)
            a.name = name
        return a

    def __repr__(self):
        return self.name


class Var:
    __slots__ = ("ref", "id")
    n = 0

    def __init__(self):
        self.ref = None
        Var.n += 1
        self.id = Var.n


class Struct:
    __slots__ = ("name", "args")

    def __init__(self, name, args):
        self.name = name
        self.args = tuple(args)


class PrologError(Exception):
    def __init__(self, term):
        self.term = term


class Cut(Exception):
    def __init__(self, barrier):
        self.barrier = barrier


class Halt(Exception):
    def __init__(self, code=0):
        self.code = code


NIL, TRUE, EOF = Atom("[]"), Atom("true"), Atom("end_of_file")


def make_list(xs, tail=NIL):
    for x in reversed(xs):
        tail = Struct(".", (x, tail))
    return tail


def deref(t):
    while isinstance(t, Var) and t.ref is not None:
        t = t.ref
    return t


def to_pylist(t):
    xs = []
    t = deref(t)
    while isinstance(t, Struct) and t.name == "." and len(t.args) == 2:
        xs.append(t.args[0])
        t = deref(t.args[1])
    if t is not NIL:
        raise PrologError(Struct("type_error", (Atom("list"), t)))
    return xs


def type_error_of(kind, t):
    return PrologError(Struct("error", (Struct("type_error", (Atom(kind), t)), Var())))


# ---------------------------------------------------------------- unification and trail
trail = []


def bind(v, t):
    v.ref = t
    trail.append(v)


def undo(mark):
    while len(trail) > mark:
        trail.pop().ref = None


def unify(a, b):
    a, b = deref(a), deref(b)
    if a is b:
        return True
    if isinstance(a, Var):
        bind(a, b)
        return True
    if isinstance(b, Var):
        bind(b, a)
        return True
    if isinstance(a, Struct):
        if not isinstance(b, Struct) or a.name != b.name or len(a.args) != len(b.args):
            return False
        return all(unify(x, y) for x, y in zip(a.args, b.args))
    return type(a) is type(b) and a == b and not isinstance(a, Atom)


def copy_term_(t, vs=None):
    vs = {} if vs is None else vs
    t = deref(t)
    if isinstance(t, Var):
        if t not in vs:
            vs[t] = Var()
        return vs[t]
    if isinstance(t, Struct):
        return Struct(t.name, [copy_term_(a, vs) for a in t.args])
    return t


def variables(t, acc):
    t = deref(t)
    if isinstance(t, Var):
        if t not in acc:
            acc.append(t)
    elif isinstance(t, Struct):
        for a in t.args:
            variables(a, acc)
    return acc


# standard order: Var < Number < Atom < Compound (arity, name, arguments)
def compare_terms(a, b):
    a, b = deref(a), deref(b)
    ka, kb = order_class(a), order_class(b)
    if ka != kb:
        return -1 if ka < kb else 1
    if ka == 0:
        return (a.id > b.id) - (a.id < b.id)
    if ka == 1:
        return (a > b) - (a < b)
    if ka == 2:
        return (a.name > b.name) - (a.name < b.name)
    if len(a.args) != len(b.args):
        return -1 if len(a.args) < len(b.args) else 1
    if a.name != b.name:
        return -1 if a.name < b.name else 1
    for x, y in zip(a.args, b.args):
        c = compare_terms(x, y)
        if c:
            return c
    return 0


def order_class(t):
    return 0 if isinstance(t, Var) else 1 if isinstance(t, (int, float)) else 2 if isinstance(t, Atom) else 3


sort_key = functools.cmp_to_key(compare_terms)


# ---------------------------------------------------------------- operadores
OPS = {}   # name -> {"prefix": (p, type), "infix": (p, type)}


def op(p, kind, name):
    fixity = "prefix" if kind in ("fy", "fx") else "postfix" if kind in ("xf", "yf") else "infix"
    OPS.setdefault(name, {})[fixity] = (p, kind)


for _p, _t, _ns in [(1200, "xfx", ":- -->"), (1200, "fx", ":- ?-"), (1100, "xfy", "; |"), (1105, "xfy", "|"),
                    (1050, "xfy", "-> *->"), (1000, "xfy", ","), (990, "xfx", ":="), (900, "fy", "\\+"),
                    (700, "xfx", "= \\= == \\== @< @> @=< @>= =.. is =:= =\\= < > =< >= >:< :<"),
                    (600, "xfy", ":"), (500, "yfx", "+ - /\\ \\/ xor"),
                    (400, "yfx", "* / // mod rem << >> div rdiv divmod"), (200, "xfx", "**"), (200, "xfy", "^"),
                    (200, "fy", "- + \\"), (100, "yfx", "."), (1, "fx", "$"),
                    (1150, "fx", "dynamic discontiguous initialization meta_predicate module_transparent multifile "
                                 "public thread_local table")]:
    for _n in _ns.split():
        op(_p, _t, _n)


# ---------------------------------------------------------------- the reader
SYMBOLS = set("+-*/\\^<>=~:.?@#&$")


class Token:
    __slots__ = ("kind", "value", "space", "end")

    def __init__(self, kind, value, space, end=0):
        self.kind, self.value, self.space, self.end = kind, value, space, end   # space: whitespace before; end: position


def tokens(s):
    """Split the text into tokens. Kinds: atom, qatom, var, num, str, punct, end."""
    i, n, out, space = 0, len(s), [], True
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
            space = True
            continue
        if c == "%":
            while i < n and s[i] != "\n":
                i += 1
            space = True
            continue
        if c == "/" and s[i + 1:i + 2] == "*":
            j = s.find("*/", i + 2)
            i = n if j < 0 else j + 2
            space = True
            continue
        j = i
        if c.isdigit():
            while j < n and s[j].isdigit():
                j += 1
            if j + 1 < n and s[j] == "." and s[j + 1].isdigit():
                j += 1
                while j < n and s[j].isdigit():
                    j += 1
                out.append(Token("num", float(s[i:j]), space))
            else:
                out.append(Token("num", int(s[i:j]), space))
        elif c == "_" or c.isupper():
            while j < n and (s[j].isalnum() or s[j] == "_"):
                j += 1
            out.append(Token("var", s[i:j], space))
        elif c.isalpha():
            while j < n and (s[j].isalnum() or s[j] == "_"):
                j += 1
            out.append(Token("atom", s[i:j], space))
        elif c in "'\"`":
            j, text = i + 1, []
            while True:
                if j >= n:
                    raise SyntaxError("unterminated quote")
                if s[j] == c:
                    if s[j + 1:j + 2] == c:
                        text.append(c)
                        j += 2
                        continue
                    j += 1
                    break
                if s[j] == "\\":
                    e = s[j + 1]
                    text.append({"n": "\n", "t": "\t", "\\": "\\", "'": "'", '"': '"', "`": "`"}.get(e, e))
                    j += 2
                    continue
                text.append(s[j])
                j += 1
            out.append(Token("qatom" if c == "'" else "str", "".join(text), space))
        elif c in "()[]{},|":
            j = i + 1
            out.append(Token("punct", c, space))
        elif c in "!;":
            j = i + 1
            out.append(Token("atom", c, space))
        elif c in SYMBOLS:
            if c == "." and (i + 1 >= n or s[i + 1].isspace() or s[i + 1] == "%"):
                out.append(Token("end", ".", space, i + 1))
                i += 1
                space = True
                continue
            while j < n and s[j] in SYMBOLS:
                j += 1
            out.append(Token("atom", s[i:j], space))
        else:
            raise SyntaxError(f"unexpected character {c!r}")
        i = j
        space = False
    return out


class Reader:
    """Operator-precedence parser over the token list of ONE clause."""
    def __init__(self, toks):
        self.t, self.i, self.vars = toks, 0, {}

    def peek(self, k=0):
        return self.t[self.i + k] if self.i + k < len(self.t) else Token("end", ".", True)

    def take(self):
        tk = self.peek()
        self.i += 1
        return tk

    def expect(self, v):
        tk = self.take()
        if tk.value != v or tk.kind not in ("punct", "atom"):
            raise SyntaxError(f"expected {v!r} but found {tk.value!r}")

    def starts_term(self, tk):
        if tk.kind == "end" or (tk.kind == "punct" and tk.value in ")]},|"):
            return False
        if tk.kind == "atom" and "infix" in OPS.get(tk.value, {}) and "prefix" not in OPS.get(tk.value, {}):
            sig = self.peek(1)
            return sig.kind == "punct" and sig.value == "(" and not sig.space
        return True

    def term(self, maxp=1200, arg=False):
        """arg: inside the arguments of a compound or a list, the comma separates (like SWI, no 999 cap)."""
        left, pleft = self.primary(maxp, arg)
        return self.infixes(left, pleft, maxp, arg)

    def argument(self):
        return self.term(1200, arg=True)

    def infixes(self, left, pleft, maxp, arg=False):
        while True:
            tk = self.peek()
            if arg and tk.kind == "punct" and tk.value in ",|":
                return left
            name = tk.value if tk.kind == "atom" or (tk.kind == "punct" and tk.value in ",|") else None
            info = OPS.get(name, {}).get("infix") if name else None
            if not info:
                return left
            p, kind = info
            if name == "|":
                p, name = 1100, ";"
            if p > maxp:
                return left
            maxleft = p if kind == "yfx" else p - 1
            if pleft > maxleft:
                return left
            self.take()
            right = self.term(p if kind == "xfy" else p - 1, arg)
            left, pleft = Struct(name, (left, right)), p

    def primary(self, maxp, arg=False):
        tk = self.take()
        if tk.kind == "num":
            return tk.value, 0
        if tk.kind == "var":
            if tk.value == "_":
                return Var(), 0
            if tk.value not in self.vars:
                self.vars[tk.value] = Var()
            return self.vars[tk.value], 0
        if tk.kind == "str":
            return Atom(tk.value), 0
        if tk.kind == "punct":
            if tk.value == "(":
                t = self.term(1200)
                self.expect(")")
                return t, 0
            if tk.value == "[":
                if self.peek().value == "]" and self.peek().kind == "punct":
                    self.take()
                    return self.after_name("[]", maxp)
                xs = [self.argument()]
                while self.peek().kind == "punct" and self.peek().value == ",":
                    self.take()
                    xs.append(self.argument())
                tail = NIL
                if self.peek().kind == "punct" and self.peek().value == "|":
                    self.take()
                    tail = self.argument()
                self.expect("]")
                return make_list(xs, tail), 0
            if tk.value == "{":
                if self.peek().value == "}":
                    self.take()
                    return Atom("{}"), 0
                t = self.term(1200)
                self.expect("}")
                return Struct("{}", (t,)), 0
            raise SyntaxError(f"unexpected {tk.value!r}")
        if tk.kind == "end":
            raise SyntaxError("unexpected end of clause")
        return self.after_name(tk.value, maxp, tk.kind == "qatom", arg)

    def after_name(self, name, maxp, quoted=False, arg=False):
        sig = self.peek()
        if sig.kind == "punct" and sig.value == "(" and not sig.space:
            self.take()
            args = [self.argument()]
            while self.peek().kind == "punct" and self.peek().value == ",":
                self.take()
                args.append(self.argument())
            self.expect(")")
            return Struct(name, args), 0
        if name == "-" and not quoted and sig.kind == "num" and not sig.space:
            self.take()
            return -sig.value, 0
        pre = None if quoted else OPS.get(name, {}).get("prefix")
        if pre and self.starts_term(sig):
            p, kind = pre
            if p > maxp:
                p = 999
            x = self.term(p if kind == "fy" else p - 1, arg)
            return Struct(name, (x,)), p
        p = max([v[0] for v in OPS.get(name, {}).values()], default=0)
        return Atom(name), (p if p <= maxp else 0)


def read_clauses(text):
    """All the clauses of a text, each with its variable dictionary."""
    toks, start = tokens(text), 0
    for k, tk in enumerate(toks):
        if tk.kind == "end":
            yield read_one(toks[start:k])
            start = k + 1
    if start < len(toks):
        raise SyntaxError("missing final period")


def read_one(toks):
    lec = Reader(toks)
    t = lec.term(1200)
    if lec.i != len(toks):
        raise SyntaxError(f"extra token {lec.peek().value!r}")
    return t, lec.vars


# ---------------------------------------------------------------- the writer
def var_name(n):
    return chr(ord("A") + n % 26) + (str(n // 26) if n >= 26 else "")


def atom_text(name, quoted):
    if not quoted:
        return name
    if name in ("[]", "!", ";", "{}", ","):
        return name if name != "," else "','"
    if name and name[0].islower() and all(c.isalnum() or c == "_" for c in name):
        return name
    if name and all(c in SYMBOLS for c in name):
        return name
    return "'" + name.replace("\\", "\\\\").replace("'", "\\'") + "'"


def write_term(t, quoted=False, maxp=1200):
    t = deref(t)
    if isinstance(t, Var):
        return f"_G{t.id}"
    if isinstance(t, (int, float)):
        return str(t)
    if isinstance(t, Atom):
        s = atom_text(t.name, quoted)
        return f"({s})" if t.name in OPS and maxp < 1200 and max(v[0] for v in OPS[t.name].values()) > maxp else s
    if t.name == "." and len(t.args) == 2:
        parts, c = [], t
        while isinstance(c, Struct) and c.name == "." and len(c.args) == 2:
            parts.append(write_term(c.args[0], quoted, 999))
            c = deref(c.args[1])
        tail = "" if c is NIL else "|" + write_term(c, quoted, 999)
        return "[" + ",".join(parts) + tail + "]"
    if t.name == "$VAR" and len(t.args) == 1 and isinstance(deref(t.args[0]), int):
        return var_name(deref(t.args[0]))
    if t.name == "{}" and len(t.args) == 1:
        return "{" + write_term(t.args[0], quoted, 1200) + "}"
    info = OPS.get(t.name, {})
    if len(t.args) == 2 and "infix" in info:
        p, kind = info["infix"]
        a = write_term(t.args[0], quoted, p if kind == "yfx" else p - 1)
        b = write_term(t.args[1], quoted, p if kind == "xfy" else p - 1)
        o = atom_text(t.name, quoted)
        s = f"{a},{b}" if t.name == "," else f"{a} {o} {b}" if o[0].isalpha() else \
            f"{a}{o} {b}" if b[:1] in SYMBOLS else f"{a}{o}{b}"
        return f"({s})" if p > maxp else s
    if len(t.args) == 1 and "prefix" in info:
        p, kind = info["prefix"]
        a = write_term(t.args[0], quoted, p if kind == "fy" else p - 1)
        o = atom_text(t.name, quoted)
        s = f"{o} {a}" if o[0].isalpha() or a[:1] in SYMBOLS or a[:1].isdigit() else f"{o}{a}"
        return f"({s})" if p > maxp else s
    return atom_text(t.name, quoted) + "(" + ",".join(write_term(a, quoted, 999) for a in t.args) + ")"


# ---------------------------------------------------------------- the clause database
class Clause:
    __slots__ = ("head", "body", "nv")

    def __init__(self, head, body):
        vs = variables(Struct("c", (head, body)), [])
        ix = {v: Slot(k) for k, v in enumerate(vs)}                  # template: variables -> indices
        self.head, self.body, self.nv = template(head, ix), template(body, ix), len(vs)

    def renombra(self):
        fresh = [Var() for _ in range(self.nv)]
        return instantiate(self.head, fresh), instantiate(self.body, fresh)


class Slot:
    """In a clause template, the place of its k-th variable."""
    __slots__ = ("k",)

    def __init__(self, k):
        self.k = k


class Mold(Struct):
    """In the template, a structure with variables inside: it is copied when renaming. Those without any (ground
    terms) are kept as Struct and shared without copying: they never change."""
    __slots__ = ()


def template(t, ix):
    t = deref(t)
    if isinstance(t, Var):
        return ix[t]
    if isinstance(t, Struct):
        args = [template(a, ix) for a in t.args]
        return (Mold if any(type(a) in (Mold, Slot) for a in args) else Struct)(t.name, args)
    return t


def instantiate(t, fresh):
    if type(t) is Mold:
        return Struct(t.name, [instantiate(a, fresh) for a in t.args])
    if type(t) is Slot:
        return fresh[t.k]
    return t


def indicator(t):
    t = deref(t)
    if isinstance(t, Struct):
        return t.name, len(t.args)
    if isinstance(t, Atom):
        return t.name, 0
    if isinstance(t, Var):
        raise PrologError(Struct("error", (Atom("instantiation_error"), Var())))
    raise type_error_of("callable", t)


# ---------------------------------------------------------------- arithmetic
def evaluate(t):
    t = deref(t)
    if isinstance(t, (int, float)):
        return t
    if isinstance(t, Var):
        raise PrologError(Struct("error", (Atom("instantiation_error"), Var())))
    if isinstance(t, Struct) and len(t.args) == 2:
        a, b = evaluate(t.args[0]), evaluate(t.args[1])
        f = {"+": lambda: a + b, "-": lambda: a - b, "*": lambda: a * b, "//": lambda: int(a / b),
             "mod": lambda: a % b, "/": lambda: a // b if isinstance(a, int) and isinstance(b, int) and a % b == 0
             else a / b, "min": lambda: min(a, b), "max": lambda: max(a, b), "**": lambda: a ** b}.get(t.name)
        if f:
            return f()
    if isinstance(t, Struct) and len(t.args) == 1:
        a = evaluate(t.args[0])
        f = {"-": lambda: -a, "+": lambda: a, "abs": lambda: abs(a)}.get(t.name)
        if f:
            return f()
    raise type_error_of("evaluable", t)


# ---------------------------------------------------------------- the engine
class Prolog:
    def __init__(self):
        self.db = {}            # (name, arity) -> [Clause]
        self.dynamics = set()
        self.loaded = set()
        self.dir = Path.cwd()
        self.init_main = None
        self.input_stream = sys.stdin
        self.pending = ""      # read from the input stream but not yet consumed (read_term)
        self.out = sys.stdout
        self.consult_text(PRELUDE)

    # --- loading
    def consult(self, file):
        path = (self.dir / file)
        if path.suffix != ".pl" and not path.exists():
            path = path.with_name(path.name + ".pl")
        path = path.resolve()
        if path in self.loaded:
            return
        self.loaded.add(path)
        previous, self.dir = self.dir, path.parent
        try:
            self.consult_text(path.read_text())
        finally:
            self.dir = previous

    def consult_text(self, text):
        for t, _ in read_clauses(text):
            t = deref(t)
            if isinstance(t, Struct) and t.name == ":-" and len(t.args) == 1:
                self.directive(t.args[0])
            else:
                self.add(t)

    def directive(self, g):
        g = deref(g)
        if isinstance(g, Struct) and g.name == "initialization":
            if len(g.args) == 2 and deref(g.args[1]) is Atom("main"):
                self.init_main = g.args[0]
                return
            g = g.args[0]
        if not self.once(g):
            print(f"Warning: directive failed: {write_term(g, True)}", file=sys.stderr)

    def add(self, t, at_end=True):
        t = deref(t)
        if isinstance(t, Struct) and t.name == ":-" and len(t.args) == 2:
            head, body = t.args
        else:
            head, body = t, TRUE
        key = indicator(head)
        if key in SISTEMA or key in CONTROL:
            raise PrologError(Struct("error", (Struct("permission_error", (Atom("modify"), Atom("static_procedure"),
                                                                           Struct("/", (Atom(key[0]), key[1])))), Var())))
        cl = Clause(head, body)
        lst = self.db.setdefault(key, [])
        lst.append(cl) if at_end else lst.insert(0, cl)

    # --- queries from Python
    def once(self, g):
        for _ in self.solve(g, object()):
            return True
        return False

    def solutions(self, text):
        """Each solution of the goal (text), as a dictionary variable -> written term."""
        g, vs = read_one(tokens(text))
        mark = len(trail)
        for _ in self.solve(g, object()):
            yield {k: write_term(v) for k, v in vs.items()}
        undo(mark)

    # --- resolution: one generator per goal; each `yield` is a solution, asking for another is backtracking
    def solve(self, g, barrier):
        g = deref(g)
        if isinstance(g, Var):
            raise PrologError(Struct("error", (Atom("instantiation_error"), Var())))
        if isinstance(g, (int, float)):
            raise type_error_of("callable", g)
        name, n = (g.name, 0) if isinstance(g, Atom) else (g.name, len(g.args))
        key = (name, n)
        if key in CONTROL:
            yield from CONTROL[key](self, g, barrier)
            return
        if key in SISTEMA:
            yield from SISTEMA[key](self, *(g.args if n else ()))
            return
        cls = self.db.get(key)
        if cls is None:
            if key in self.dynamics:
                return
            raise PrologError(Struct("error", (Struct("existence_error", (
                Atom("procedure"), Struct("/", (Atom(name), n)))), Var())))
        own = object()                         # the cut barrier of this call
        mark = len(trail)
        first_arg = g.args[0] if n else None
        first_arg = deref(first_arg) if n else None
        try:
            for cl in list(cls):               # logical view: clauses added during the call do not enter it
                if n and not isinstance(first_arg, Var) and isinstance(cl.head.args[0], (Atom, int)) \
                        and cl.head.args[0] is not first_arg and cl.head.args[0] != first_arg:
                    continue                   # first-argument index
                head, body = cl.renombra()
                if unify(head, g):
                    yield from self.solve(body, own)
                undo(mark)
        except Cut as c:
            if c.barrier is not own:
                raise
            undo(mark)

    def solve_closed(self, g):
        """call/1: a cut inside g does not escape g."""
        own = object()
        try:
            yield from self.solve(g, own)
        except Cut as c:
            if c.barrier is not own:
                raise

    def first_solution(self, g):
        """Does g have any solution? (leaves the first one bound)"""
        it = self.solve_closed(g)
        for _ in it:
            it.close()
            return True
        return False

    def read_next_term(self):
        """The next term from the input stream (read/1): reads lines until there is a final period."""
        while True:
            toks = tokens(self.pending)
            end = next((k for k, tk in enumerate(toks) if tk.kind == "end"), None)
            if end is not None:
                self.pending = self.pending[toks[end].end:]
                return read_one(toks[:end])[0]
            line = self.input_stream.readline()
            if not line:
                if toks:
                    raise PrologError(Atom("syntax_error"))
                return EOF
            self.pending += line


# --- control: what looks at the cut barrier
def c_true(pl, g, b):
    yield


def c_fail(pl, g, b):
    return
    yield


def c_cut(pl, g, b):
    yield
    raise Cut(b)


def c_and(pl, g, b):
    for _ in pl.solve(g.args[0], b):
        yield from pl.solve(g.args[1], b)


def c_or(pl, g, b):
    left = deref(g.args[0])
    if isinstance(left, Struct) and left.name == "->" and len(left.args) == 2:
        mark = len(trail)
        if pl.first_solution(left.args[0]):
            yield from pl.solve(left.args[1], b)
        else:
            undo(mark)
            yield from pl.solve(g.args[1], b)
        return
    mark = len(trail)
    yield from pl.solve(left, b)
    undo(mark)
    yield from pl.solve(g.args[1], b)


def c_if(pl, g, b):
    if pl.first_solution(g.args[0]):
        yield from pl.solve(g.args[1], b)


def c_not(pl, g, b):
    mark = len(trail)
    found = pl.first_solution(g.args[0])
    undo(mark)
    if not found:
        yield


def c_call(pl, g, b):
    meta = deref(g.args[0])
    extra = g.args[1:]
    if extra:
        if isinstance(meta, Atom):
            meta = Struct(meta.name, extra)
        elif isinstance(meta, Struct):
            meta = Struct(meta.name, meta.args + tuple(extra))
        else:
            raise type_error_of("callable", meta)
    yield from pl.solve_closed(meta)


def c_findall(pl, g, b):
    template_, meta, res = g.args
    mark = len(trail)
    xs = []
    for _ in pl.solve_closed(meta):
        xs.append(copy_term_(template_))
    undo(mark)
    if unify(res, make_list(xs)):
        yield
    undo(mark)


def c_catch(pl, g, b):
    meta, pattern, recover = g.args
    mark = len(trail)
    try:
        for _ in pl.solve_closed(meta):
            yield
    except PrologError as e:
        undo(mark)
        ball = copy_term_(e.term)
        if unify(pattern, ball):
            yield from pl.solve_closed(recover)
        else:
            raise


CONTROL = {("true", 0): c_true, ("fail", 0): c_fail, ("false", 0): c_fail, ("!", 0): c_cut,
           (",", 2): c_and, (";", 2): c_or, ("->", 2): c_if, ("\\+", 1): c_not, ("findall", 3): c_findall,
           ("catch", 3): c_catch, **{("call", k): c_call for k in range(1, 9)}}


# --- system predicates: generators over the arguments
def succeeds_if(cond):
    def g(*_):
        if cond:
            yield
    return g()


def do_unify(a, b):
    mark = len(trail)
    if unify(a, b):
        yield
    undo(mark)


def s_unify(pl, a, b):
    yield from do_unify(a, b)


def s_not_unifiable(pl, a, b):
    mark = len(trail)
    ok = unify(a, b)
    undo(mark)
    if not ok:
        yield


def s_assertz(pl, t):
    pl.add(copy_term_(t))
    yield


def s_asserta(pl, t):
    pl.add(copy_term_(t), at_end=False)
    yield


def s_retractall(pl, head):
    key = indicator(head)
    pl.dynamics.add(key)
    cls = pl.db.setdefault(key, [])
    kept = []
    for cl in cls:
        mark = len(trail)
        h, _ = cl.renombra()
        if not unify(h, head):
            kept.append(cl)
        undo(mark)
    cls[:] = kept
    yield


def s_retract(pl, t):
    t = deref(t)
    head, body = (t.args if isinstance(t, Struct) and t.name == ":-" and len(t.args) == 2 else (t, TRUE))
    cls = pl.db.get(indicator(head), [])
    for cl in list(cls):
        mark = len(trail)
        h, c = cl.renombra()
        if unify(h, head) and unify(c, body):
            cls.remove(cl)
            yield
            return
        undo(mark)


def declare(pl, spec, action):
    spec = deref(spec)
    if isinstance(spec, Struct) and spec.name in (",", ".") and len(spec.args) == 2:
        for s in ([spec.args[0], spec.args[1]] if spec.name == "," else to_pylist(spec)):
            declare(pl, s, action)
        return
    if spec is NIL:
        return
    if isinstance(spec, Struct) and spec.name == "/":
        action((deref(spec.args[0]).name, deref(spec.args[1])))


def s_dynamic(pl, spec):
    declare(pl, spec, lambda k: (pl.dynamics.add(k), pl.db.setdefault(k, [])))
    yield


def s_discontiguous(pl, spec):
    yield


def s_ensure_loaded(pl, f):
    pl.consult(deref(f).name)
    yield


def s_op(pl, p, kind, names):
    names = deref(names)
    for nm in (to_pylist(names) if isinstance(names, Struct) and names.name == "." else [names]):
        op(deref(p), deref(kind).name, deref(nm).name)
    yield


def s_write(pl, t):
    pl.out.write(write_term(t))
    yield


def s_print(pl, t):
    pl.out.write(write_term(t, quoted=True))
    yield


def s_writeln(pl, t):
    pl.out.write(write_term(t) + "\n")
    yield


def s_nl(pl):
    pl.out.write("\n")
    yield


def s_flush(pl):
    pl.out.flush()
    yield


def s_halt(pl, code=0):
    raise Halt(deref(code))


def s_repeat(pl):
    while True:
        yield


def s_read_term(pl, stream, t, options):
    yield from do_unify(t, pl.read_next_term())


def s_read(pl, t):
    yield from do_unify(t, pl.read_next_term())


def s_is(pl, r, e):
    yield from do_unify(r, evaluate(e))


def comparison(f):
    return lambda pl, a, b: succeeds_if(f(evaluate(a), evaluate(b)))


def s_identical(pl, a, b):
    return succeeds_if(compare_terms(a, b) == 0)


def s_not_identical(pl, a, b):
    return succeeds_if(compare_terms(a, b) != 0)


def s_msort(pl, l, s):
    yield from do_unify(s, make_list(sorted(to_pylist(l), key=sort_key)))


def s_sort(pl, l, s):
    xs, out = sorted(to_pylist(l), key=sort_key), []
    for x in xs:
        if not out or compare_terms(out[-1], x):
            out.append(x)
    yield from do_unify(s, make_list(out))


def s_length(pl, l, n):
    yield from do_unify(n, len(to_pylist(l)))


def atomic_text(t):
    t = deref(t)
    if isinstance(t, Atom):
        return t.name
    if isinstance(t, (int, float)):
        return str(t)
    raise type_error_of("atomic", t)


def s_atomic_list_concat(pl, l, sep, r):
    yield from do_unify(r, Atom(atomic_text(sep).join(atomic_text(x) for x in to_pylist(l))))


def s_atomic_list_concat2(pl, l, r):
    yield from do_unify(r, Atom("".join(atomic_text(x) for x in to_pylist(l))))


def format_text(fmt, args):
    fmt, args = atomic_text(fmt), list(args)
    out, i = [], 0
    while i < len(fmt):
        c = fmt[i]
        if c == "~" and i + 1 < len(fmt):
            d = fmt[i + 1]
            i += 2
            if d in "wadp":
                out.append(write_term(args.pop(0), quoted=(d == "p")))
            elif d == "q":
                out.append(write_term(args.pop(0), quoted=True))
            elif d == "n":
                out.append("\n")
            elif d == "~":
                out.append("~")
            else:
                raise PrologError(Struct("format", (Atom(f"unsupported directive ~{d}"),)))
            continue
        out.append(c)
        i += 1
    return "".join(out)


def format_args(a):
    a = deref(a)
    return to_pylist(a) if (isinstance(a, Struct) and a.name == ".") or a is NIL else [a]


def s_format3(pl, target, fmt, args):
    target = deref(target)
    text = format_text(fmt, format_args(args))
    if isinstance(target, Struct) and target.name == "atom" and len(target.args) == 1:
        yield from do_unify(target.args[0], Atom(text))
    else:
        pl.out.write(text)
        yield


def s_format2(pl, fmt, args):
    pl.out.write(format_text(fmt, format_args(args)))
    yield


def s_format1(pl, fmt):
    pl.out.write(format_text(fmt, []))
    yield


def s_numbervars(pl, t, start, end):
    k = deref(start)
    for v in variables(t, []):
        bind(v, Struct("$VAR", (k,)))
        k += 1
    yield from do_unify(end, k)


def s_copy_term(pl, a, b):
    yield from do_unify(b, copy_term_(a))


def s_functor(pl, t, f, n):
    t = deref(t)
    if isinstance(t, Var):
        f, n = deref(f), deref(n)
        yield from do_unify(t, f if n == 0 else Struct(f.name, [Var() for _ in range(n)]))
    elif isinstance(t, Struct):
        yield from do_unify(Struct("f", (f, n)), Struct("f", (Atom(t.name), len(t.args))))
    else:
        yield from do_unify(Struct("f", (f, n)), Struct("f", (t, 0)))


def s_arg(pl, n, t, a):
    yield from do_unify(a, deref(t).args[deref(n) - 1])


def s_univ(pl, t, l):
    t = deref(t)
    if isinstance(t, Struct):
        yield from do_unify(l, make_list([Atom(t.name), *t.args]))
    elif isinstance(t, Var):
        xs = to_pylist(l)
        f = deref(xs[0])
        yield from do_unify(t, f if len(xs) == 1 else Struct(f.name, xs[1:]))
    else:
        yield from do_unify(l, make_list([t]))


def s_read_file_to_terms(pl, f, ts, options):
    path = pl.dir / deref(f).name
    yield from do_unify(ts, make_list([t for t, _ in read_clauses(path.read_text())]))


def s_throw(pl, t):
    raise PrologError(copy_term_(t))


def s_atom_length(pl, a, n):
    yield from do_unify(n, len(atomic_text(a)))


def s_atom_chars(pl, a, l):
    a = deref(a)
    if isinstance(a, Var):
        yield from do_unify(a, Atom("".join(atomic_text(c) for c in to_pylist(l))))
    else:
        yield from do_unify(l, make_list([Atom(c) for c in atomic_text(a)]))


def s_atom_codes(pl, a, l):
    a = deref(a)
    if isinstance(a, Var):
        yield from do_unify(a, Atom("".join(chr(deref(c)) for c in to_pylist(l))))
    else:
        yield from do_unify(l, make_list([ord(c) for c in atomic_text(a)]))


def s_run(pl, args, out):
    """run(+[Program|Args], -Output): run the program and return its output (without the final \\n); a number if it
    looks like one. WARNING: this is an UNSAFE builtin: it executes an arbitrary command given by the Prolog text,
    with no sandbox. Never expose it to untrusted programs or input (the A2A core must not load programs that call
    it). In SWI-Prolog the equivalent is process_create."""
    r = subprocess.run([atomic_text(a) for a in to_pylist(args)], capture_output=True, text=True)
    s = r.stdout.rstrip("\n")
    try:
        v = int(s)
    except ValueError:
        try:
            v = float(s)
        except ValueError:
            v = Atom(s)
    yield from do_unify(out, v)


def type_check(f):
    return lambda pl, t: succeeds_if(f(deref(t)))


SISTEMA = {
    ("=", 2): s_unify, ("\\=", 2): s_not_unifiable, ("==", 2): s_identical, ("\\==", 2): s_not_identical,
    ("assertz", 1): s_assertz, ("assert", 1): s_assertz, ("asserta", 1): s_asserta,
    ("retractall", 1): s_retractall, ("retract", 1): s_retract,
    ("dynamic", 1): s_dynamic, ("discontiguous", 1): s_discontiguous,
    ("ensure_loaded", 1): s_ensure_loaded, ("consult", 1): s_ensure_loaded, ("op", 3): s_op,
    ("write", 1): s_write, ("print", 1): s_print, ("writeln", 1): s_writeln, ("nl", 0): s_nl,
    ("flush_output", 0): s_flush, ("halt", 0): s_halt, ("halt", 1): s_halt, ("repeat", 0): s_repeat,
    ("read_term", 3): s_read_term, ("read", 1): s_read, ("is", 2): s_is,
    ("<", 2): comparison(lambda a, b: a < b), (">", 2): comparison(lambda a, b: a > b),
    ("=<", 2): comparison(lambda a, b: a <= b), (">=", 2): comparison(lambda a, b: a >= b),
    ("=:=", 2): comparison(lambda a, b: a == b), ("=\\=", 2): comparison(lambda a, b: a != b),
    ("@<", 2): lambda pl, a, b: succeeds_if(compare_terms(a, b) < 0), ("@>", 2): lambda pl, a, b: succeeds_if(compare_terms(a, b) > 0),
    ("msort", 2): s_msort, ("sort", 2): s_sort, ("length", 2): s_length,
    ("atomic_list_concat", 3): s_atomic_list_concat, ("atomic_list_concat", 2): s_atomic_list_concat2,
    ("format", 3): s_format3, ("format", 2): s_format2, ("format", 1): s_format1,
    ("numbervars", 3): s_numbervars, ("copy_term", 2): s_copy_term, ("functor", 3): s_functor,
    ("arg", 3): s_arg, ("=..", 2): s_univ, ("read_file_to_terms", 3): s_read_file_to_terms, ("throw", 1): s_throw,
    ("run", 2): s_run, ("atom_length", 2): s_atom_length, ("atom_chars", 2): s_atom_chars, ("atom_codes", 2): s_atom_codes,
    ("var", 1): type_check(lambda t: isinstance(t, Var)), ("nonvar", 1): type_check(lambda t: not isinstance(t, Var)),
    ("atom", 1): type_check(lambda t: isinstance(t, Atom)), ("number", 1): type_check(lambda t: isinstance(t, (int, float))),
    ("integer", 1): type_check(lambda t: isinstance(t, int)), ("compound", 1): type_check(lambda t: isinstance(t, Struct)),
    ("atomic", 1): type_check(lambda t: isinstance(t, (Atom, int, float))),
    ("callable", 1): type_check(lambda t: isinstance(t, (Atom, Struct))),
}

# what is a library in SWI, written here in Prolog
PRELUDE = r"""
member(X, [X|_]).
member(X, [_|T]) :- member(X, T).
memberchk(X, L) :- member(X, L), !.
append([], L, L).
append([H|T], L, [H|R]) :- append(T, L, R).
forall(C, A) :- \+ (C, \+ A).
maplist(_, []).
maplist(G, [X|Xs]) :- call(G, X), maplist(G, Xs).
maplist(_, [], []).
maplist(G, [X|Xs], [Y|Ys]) :- call(G, X, Y), maplist(G, Xs, Ys).
maplist(_, [], [], []).
maplist(G, [X|Xs], [Y|Ys], [Z|Zs]) :- call(G, X, Y, Z), maplist(G, Xs, Ys, Zs).
between(L, H, L) :- L =< H.
between(L, H, X) :- L < H, L1 is L + 1, between(L1, H, X).
last([X], X) :- !.
last([_|T], X) :- last(T, X).
reverse(L, R) :- reverse_(L, [], R).
reverse_([], R, R).
reverse_([H|T], A, R) :- reverse_(T, [H|A], R).
nth1(1, [X|_], X) :- !.
nth1(N, [_|T], X) :- N > 1, N1 is N - 1, nth1(N1, T, X).
"""


def main(argv):
    pl = Prolog()
    goals, end, files, i = [], None, [], 0
    while i < len(argv):
        if argv[i] == "-g":
            goals.append(argv[i + 1])
            i += 2
        elif argv[i] == "-t":
            end = argv[i + 1]
            i += 2
        elif argv[i] == "-q":
            i += 1
        else:
            files.append(argv[i])
            i += 1
    try:
        for f in files:
            pl.consult(f)
        for g in goals:
            t, _ = read_one(tokens(g))
            if not pl.once(t):
                print(f"Warning: goal ({g}) failed", file=sys.stderr)
                return 1
        if pl.init_main is not None:
            pl.once(pl.init_main)
            return 0
        if end:
            pl.once(read_one(tokens(end))[0])
    except Halt as h:
        pl.out.flush()
        return h.code
    except PrologError as e:
        print(f"ERROR: {write_term(e.term, True)}", file=sys.stderr)
        return 1
    finally:
        pl.out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
