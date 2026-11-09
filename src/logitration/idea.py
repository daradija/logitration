# Copyright (c) 2026 David Ragel
# SPDX-License-Identifier: BUSL-1.1
# Licensed under the Business Source License 1.1; see LICENSE.
"""The idea file: the one place where the user writes what is theirs. Everything else is the library.

    # circuit.logi
    name: circuit
    about: electrical circuits

    == conversation          1. how a client talks to the agent and what it must answer
    client: l1 is a lamp ...
    agent:  Before I load it I need to know: ...      (each agent line must appear in the reply; part of it is enough)

    == facts                 2. FACTS: the words of what is observed or told, and the structure they share
    lamp(X)     = {X} is a lamp | there is a lamp called {X}
    state(X, V) = {X} is {V} | raise {X}{V=up}          [changes; V: up down]
    observe: python3 look.py                            (optional: a read-only command that prints the facts)

    == rules                 3. RULES: what is true (and the words of what the rules decide)
    lit(L) = {L} is lit
    lit(L) :- lamp(L), state(L, on).

    == meta-interpreters     4. what runs ON facts and rules: three, each optional (see META and META_PART)
    -- check                 decides and explains:   explain/2
    -- execute               acts on the world:      can_change/1, act/2
    -- complete              asks what is missing:   missing/1, ask/2
    missing(state(S, _)) :- switch(S), \\+ state(S, _).

The older layout still works: `== logic` (words and rules together) and one `== meta-interpreter` (the five predicates, no
subsections: each one is read by the interpreter it belongs to).

A word line is `pred(Args) = template | template ... [options]` (no final dot); options, separated by ';':
`changes` (the fact changes over time; its last argument is the value), `V: a b c` (allowed values of V),
`not: {X} is off` (how to say it is false), `program via CMD` (a program computes this fact: see programmer.py).
The arguments may carry a type, checked before anything reaches the rules (inspired by Clorm):
`capacity(N:name, R:ram|disk, G:int>=0) = {N} has {G} GB of {R}`; a line `type resource = ram|disk|gpu` names one to reuse
(`R:resource`). Any other line of logic/meta is Prolog. Lines starting with # are comments.
"""
import re
from pathlib import Path

from logitration import engine as E
from logitration.knowledge import META, META_PART

RE_SECTION = re.compile(r"^==\s*(?:\d+\.\s*)?([A-Za-z][A-Za-z -]*?)\s*(?:==)?\s*$")
_ARG = r"[A-Z][A-Za-z0-9_]*(?:\s*:\s*[^,()]+?)?"                      # an argument, with its type if it has one: G:int>=0
RE_WORDS = re.compile(r"^([a-z][a-z0-9_]*(?:\(\s*" + _ARG + r"(?:\s*,\s*" + _ARG + r")*\s*\))?)\s*=\s*(.+?)\s*(?:\[([^\]]*)\])?\s*$")
RE_TYPEDEF = re.compile(r"^type\s+([a-z][a-z0-9_]*)\s*=\s*(.+?)\s*$")
RE_PROGRAM_OPT = re.compile(r"^program(?:\s+via\s+(.+))?$")
RE_PART = re.compile(r"^--\s*([A-Za-z]+)\s*(?:--)?\s*$")
RE_OBSERVE = re.compile(r"^observe\s*:\s*(?!-)(.+?)\s*$")
SECTIONS = {"conversation": "conversation", "logic": "logic", "facts": "facts", "rules": "rules", "meta": "meta",
            "meta-interpreter": "meta", "meta interpreter": "meta", "metainterpreter": "meta",
            "meta-interpreters": "meta", "meta interpreters": "meta", "metainterpreters": "meta"}
PARTS = ("check", "execute", "complete")
PROGRAM = "program"                                     # the fourth subsection: not Prolog (see programmer.py)


class IdeaError(ValueError):
    pass


class Idea:
    """A parsed idea file: name, about, conversation [(client text, [expected lines], line)], words (phrases dict),
    prolog (logic + meta text), and the raw sections (for :rules)."""

    def __init__(self, path):
        self.path = Path(path)
        if not self.path.exists() and self.path.with_suffix(".logi").exists():
            self.path = self.path.with_suffix(".logi")
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as e:
            raise IdeaError(f"cannot read {path}: {e}")
        self.name, self.about = self.path.stem, None
        self.conversation, self.words = [], {}
        self.raw = {"conversation": [], "logic": [], "facts": [], "rules": [], "meta": []}
        self.observe, self.observe_line = None, None            # observe: the read-only command that prints the facts
        self.program_lines = []                                # (line, text) under `-- program`
        self.part_of = {}                                       # line -> "check" | "execute" | "complete" (meta lines under a subsection)
        self.word_lines = {}                                   # "pred(Args)" -> line of its word line
        self.type_lines = {}                                   # "pred(Args)" -> line of its word line, for the ones that carry types
        self.type_decls = {}                                   # `type NAME = spec` lines: name -> (spec, line)
        self.program_marks = {}                                # "pred(Args)" -> (line, [commands]) of the facts a program computes ([program ...])
        self.section_of = {}                                   # line -> "logic" | "facts" | "rules" | "meta" (only the Prolog lines)
        prolog = {"logic": [], "facts": [], "rules": [], "meta": []}
        part = None
        lines = text.splitlines()
        aligned = [""] * len(lines)                            # the Prolog of the file, each line where it is in the file
        section = None
        for n, line in enumerate(lines, 1):
            where = f"{self.path.name}:{n}"
            if line.lstrip().startswith("#"):
                continue
            m = RE_SECTION.match(line.strip())
            if m:
                key = m.group(1).strip().lower()
                if key not in SECTIONS:
                    raise IdeaError(f"{where}: unknown section '== {m.group(1)}' (use: conversation, facts, rules, meta-interpreters; "
                                "or the older: logic, meta-interpreter)")
                section, part = SECTIONS[key], None
                continue
            pm = RE_PART.match(line.strip())
            if pm and section == "meta":
                part = pm.group(1).lower()
                if part not in PARTS + (PROGRAM,):
                    raise IdeaError(f"{where}: unknown meta-interpreter '-- {pm.group(1)}' (use: check, execute, complete, program)")
                continue
            if pm and section is not None:
                raise IdeaError(f"{where}: '-- {pm.group(1)}' only goes inside the meta-interpreters section")
            if section is None:
                if not line.strip():
                    continue
                h = re.match(r"^\s*(name|about)\s*:\s*(.+?)\s*$", line)
                if not h:
                    raise IdeaError(f"{where}: before the first '== section' only 'name:' and 'about:' lines are allowed")
                setattr(self, h.group(1), h.group(2))
                continue
            self.raw[section].append((n, line))
            if section == "meta" and part == PROGRAM:
                if line.strip():
                    self.program_lines.append((n, line))
                continue
            if section == "conversation":
                self._conversation_line(line, where, n)
                continue
            ob = RE_OBSERVE.match(line.strip()) if section in ("logic", "facts") else None
            if ob:
                if self.observe:
                    raise IdeaError(f"{where}: two 'observe:' lines (line {self.observe_line} already has one)")
                self.observe, self.observe_line = ob.group(1), n
                prolog[section].append("")
                continue
            td = RE_TYPEDEF.match(line.strip()) if section in ("logic", "facts", "rules") and not line.rstrip().endswith(".") else None
            if td:
                if td.group(1) in self.type_decls:
                    raise IdeaError(f"{where}: two `type {td.group(1)}` lines (line {self.type_decls[td.group(1)][1]} already has one)")
                self.type_decls[td.group(1)] = (td.group(2), n)
                prolog[section].append("")
                continue
            w = RE_WORDS.match(line) if not line.rstrip().endswith(".") and ":-" not in line else None
            if w and section in ("logic", "facts", "rules"):
                key = self._words(w, where, n)
                self.word_lines[key] = n
                prolog[section].append("")
            else:
                prolog[section].append(line)
                aligned[n - 1] = line
                self.section_of[n] = section
                if section == "meta" and part:
                    self.part_of[n] = part
        if not self.words:
            raise IdeaError(f"{self.path.name}: the facts/rules sections have no word lines (like  lamp(X) = {{X}} is a lamp)")
        self._resolve_types()
        self.logic_text = "\n".join(prolog["logic"] + prolog["facts"] + prolog["rules"])
        self.meta_text = "\n".join(prolog["meta"])
        self.prolog = "\n".join(aligned) + "\n"             # line k of it is line k of the file (errors say the line)
        self.lines = lines
        self.clauses = self._parse()
        self.meta_defined = sorted({f"{k[0]}/{k[1]}" for k in META
                                    if re.search(rf"^\s*{k[0]}\(", self.meta_text + "\n" + self.logic_text, re.M)})
        # the three meta-interpreters and what the file wrote for each (by predicate: the subsection is the author's label)
        self.interpreters = {p: [d for d in self.meta_defined if META_PART[d.split("/")[0]] == p] for p in PARTS}
        from logitration import programmer
        try:
            self.programs, self.rehearse = programmer.parse_declarations(self)
        except programmer.ProgramError as e:
            raise IdeaError(str(e))
        self.program_names = [f"{d['kind']} {d['name']}" for d in self.programs]       # the fourth, apart: it is not Prolog

    def _parse(self):
        """the Prolog clauses [(term, line)]; a syntax error says the line of the file and the line itself"""
        out = []
        try:
            for t, _, pos in E.read_clauses_located(self.prolog):
                out.append((E.deref(t), self.prolog.count("\n", 0, pos) + 1))
        except SyntaxError as e:
            at = getattr(e, "pos", 0)
            line = self.prolog.count("\n", 0, at) + 1
            msg = str(e.msg if hasattr(e, "msg") and e.msg else e)
            hint = ""
            prev = self.prolog.count("\n", 0, getattr(e, "prev_pos", at)) + 1
            first = self.prolog.count("\n", 0, getattr(e, "clause_pos", at)) + 1
            if msg.startswith(("extra token", "unexpected")) and prev < line:
                hint = f" (is a final '.' missing at the end of line {prev}?)"
            elif msg == "missing final period":
                hint = f" (the clause that starts here never ends with a '.')"
            elif first < line:
                hint = f" (the clause starts at line {first})"
            if " = " in self.lines[line - 1] and ":-" not in self.lines[line - 1] and self.section_of.get(line) in ("logic", "facts", "rules"):
                hint += " (if this is a word line it must look like  pred(Args) = {Arg} words  [options], with no final dot)"
            raise IdeaError(f"{self.path.name}:{line}: {msg}{hint}\n    {line:4} | {self.lines[line - 1].strip()}")
        return out

    def _conversation_line(self, line, where, n):
        if not line.strip():
            return
        m = re.match(r"^(client|agent)\s*:\s?(.*)$", line.strip())
        if m and m.group(1) == "client":
            self.conversation.append((m.group(2).strip(), [], n))
        elif m:
            if not self.conversation:
                raise IdeaError(f"{where}: the conversation starts with a 'client:' line")
            if m.group(2).strip():
                self.conversation[-1][1].append(m.group(2).strip())
        elif line[:1].isspace() and self.conversation:
            self.conversation[-1][1].append(line.strip())           # an agent answer that goes on
        else:
            raise IdeaError(f"{where}: in the conversation each line starts with 'client:' or 'agent:' (or is indented)")

    def _words(self, m, where, n=0):
        raw_key, says, opts = m.group(1), [s.strip() for s in m.group(2).split("|") if s.strip()], m.group(3)
        key = re.sub(r"\s*:\s*[^,()]+", "", raw_key)                    # capacity(N:name, G:int) -> capacity(N, G)
        types = {}
        for a in re.findall(rf"({_ARG})\s*[,)]", raw_key[raw_key.find("("):] if "(" in raw_key else ""):
            if ":" in a:
                arg, spec = a.split(":", 1)
                types[arg.strip()] = spec.strip()
        value = {"say": says}
        if types:
            value["types"] = types
            self.type_lines[key] = n
        for o in (opts or "").split(";"):
            o = o.strip()
            if not o:
                continue
            if o == "changes":
                value["changes"] = True
            elif o == "set":
                value["set"] = True
            elif o.startswith("not:"):
                value["not"] = o[4:].strip()
            elif RE_PROGRAM_OPT.match(o):
                via = (RE_PROGRAM_OPT.match(o).group(1) or "").replace(",", " ").split()
                value["program"] = {"via": via} if via else True
                self.program_marks[key] = (n, via)
            elif re.match(r"^[A-Z]\w*\s*:", o):
                arg, vals = o.split(":", 1)
                value.setdefault("values", {})[arg.strip()] = vals.split()
            else:
                raise IdeaError(f"{where}: unknown option [{o}] (use: changes; set; V: a b c; not: ...; program via CMD)")
        if key in self.words:
            raise IdeaError(f"{where}: two word lines for {key}")
        self.words[key] = value
        return key

    def _resolve_types(self):
        """the types written in the signatures: a named type (`type resource = ram|disk`) becomes its spec; a spec that is not a type
        says its line. Afterwards every phrase holds plain specs ('int>=0', 'ram|disk', 'name')."""
        from logitration.phrases import PhraseError, Type

        def spec_of(name, seen=()):
            s = re.sub(r"\s+", " ", name.strip())
            if s in self.type_decls and s not in ("any", "name", "int"):
                if s in seen:
                    raise IdeaError(f"{self.path.name}:{self.type_decls[s][1]}: the type {s} is defined by itself")
                return spec_of(self.type_decls[s][0], seen + (s,))
            return s
        for key, value in self.words.items():
            for arg, spec in list((value.get("types") or {}).items()):
                line = self.type_lines.get(key)
                try:
                    value["types"][arg] = spec_of(spec)
                    Type(value["types"][arg])
                except PhraseError as e:
                    raise IdeaError(f"{self.path.name}:{line}: argument {arg} of {key}: {e} (a name defined with `type NAME = ...` also works)")
        for name, (spec, line) in self.type_decls.items():
            try:
                Type(spec_of(spec))
            except PhraseError as e:
                raise IdeaError(f"{self.path.name}:{line}: type {name}: {e}")


def observer_from(command, cwd, timeout=30):
    """`observe: COMMAND` of the idea file -> a callable that runs it (no shell, in the folder of the file, read-only by
    contract) and returns the text it printed; a non-zero exit or a timeout raises RuntimeError"""
    import shlex
    import subprocess
    argv = shlex.split(command)

    def run():
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=str(cwd))
        except (OSError, subprocess.TimeoutExpired) as e:
            raise RuntimeError(f"{type(e).__name__}: {str(e)[:120]}")
        if r.returncode != 0:
            raise RuntimeError(f"exit {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}")
        return r.stdout
    return run


def load(path, translator="simulated", model=None, device=None, observe=False, program=False, world=None, **kw):
    """an idea file -> a LogicAgent (the same object `logi try` talks to and `logi serve` publishes).
    observe=True lets the file's `observe:` command run (the agent looks at the world itself); observe=callable uses that.
    program=True uses the scripts anchored in programs/ (see programmer.py) as the observe and the device, on `world`
    (default: the file's rehearsal world, pretend machines; pass an SshWorld for the real ones)."""
    from logitration import LogicAgent, PhraseError
    idea = Idea(path)
    obs = observe if callable(observe) else (observer_from(idea.observe, idea.path.parent) if observe and idea.observe else None)
    notes, progs = [], None
    try:
        agent = LogicAgent(rules=idea.prolog, phrases=idea.words, translator=translator, model=model, name=idea.name,
                           domain=idea.about or idea.name, device=device, observe=obs, **kw)
    except PhraseError as e:
        raise IdeaError(f"{idea.path.name}: {e}")
    agent.idea = idea
    if program and idea.programs:
        from logitration import programmer
        w = world or programmer.idea_world(idea, idea.programs[0]["via"][0])
        if w is None:
            notes.append("programs need a world: add `rehearse m1: ...` under `-- program`, or pass an SshWorld")
        else:
            progs = programmer.Programs(idea, agent.vocab, w)
            notes += progs.notes
            if progs.observer():
                agent.observe = progs.observer()
            if progs.device() and device is None:
                agent.device = progs.device()
    agent.program_notes = notes
    from logitration.lint import lint
    agent.lint = lint(idea)
    return agent


def _norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[│├└─]", " ", s)).strip().lower()


def check(agent, session=None):
    """replay the idea's conversation on a fresh session: [(client, expected, reply, missing lines, line)]"""
    import uuid
    session = session or "example-" + uuid.uuid4().hex[:8]
    out = []
    for client, expected, n in agent.idea.conversation:
        reply = agent.say(client, session=session)
        got = _norm(reply)
        missing = [e for e in expected if _norm(e) not in got]
        out.append((client, expected, reply, missing, n))
    return out
