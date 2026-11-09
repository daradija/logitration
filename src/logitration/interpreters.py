# Copyright (c) 2026 David Ragel
# SPDX-License-Identifier: BUSL-1.1
# Licensed under the Business Source License 1.1; see LICENSE.
"""The three meta-interpreters: what runs ON the facts and the rules. Each one is a function with a name.

  check     decides and explains: is / which / why / why not / change / timeline, and "best" through the rules.
            What you may write for it: explain/2 (how to say why a goal holds). By default: the proof, in your words.
  execute   acts on the world: "make G true" changes what can_change/1 allows, the rules confirm, act/2 sends the command
            to the device. By default: any fact that changes may change, and nothing is sent.
  complete  finds what is missing and asks for it: missing/1 says what is needed, ask/2 how to ask; it also teaches the client
            how to ask (examples made from your phrases), and looks at the world itself (observe:) before answering.
            Future: completing a missing PROGRAM, not only a missing fact (instalador_meta's `despierta`): not in this library.

They are all optional: with none of them written the agent loads, answers and explains (see tests/out/experimento_sin_bloque3_A.txt).
The facts (what is true now, how it is said) and the rules (what follows) are not here: they are the file's sections 2 and 3.
"""
import json
import re

from logitration import engine as E
from logitration import terms as T
from logitration.knowledge import Rejection, World
from logitration.phrases import speak
from logitration.reply import Reply
from logitration.translators import TranslatorError


class _Part:
    """one meta-interpreter of one conversation: it reads the conversation's world and the agent's knowledge"""

    def __init__(self, conv):
        self.c = conv

    k = property(lambda s: s.c.k)
    vocab = property(lambda s: s.c.vocab)
    agent = property(lambda s: s.c.agent)
    world = property(lambda s: s.c.world, lambda s, v: setattr(s.c, "world", v))

    def __getattr__(self, name):                 # anything else it reads (client_text, pending ...) is the conversation's
        if name == "c":
            raise AttributeError(name)
        return getattr(self.c, name)


class Check(_Part):
    """decides and explains (reads explain/2)"""

    def summary(self, world, step):
        main = self.k.main
        args = ", ".join("X%d" % i for i in range(main.arity))
        goal, _ = self.k.goal(f"{main.name}({args})" if main.arity else main.name)
        found = self.k.which(world, step, goal)
        if not found:
            return main.render(["nothing"] * main.arity) + "."
        return "; ".join(self.vocab.say(t) for t in found) + "."

    def _step(self, ref, default):
        w = self.world
        if ref is None:
            return default
        if ref == "start":
            return 0
        if ref == "end":
            return w.last
        if ref > w.last:
            raise Rejection("invalid_step", f"there is no step {ref}: the events go from 1 to {w.last}")
        return ref

    def answer(self, q):
        goal, _ = self.k.goal(q["goal"])
        w = self.world
        unknown = self.k.unknown_names(w, goal)
        if unknown:
            return self._unknown(q, unknown)
        self.k.begin_misses()
        try:
            r = self._answer(q, goal)
        finally:
            gaps = self.k.end_misses(w)
        if gaps and r.state in ("completed", "failed") and "missing_info" not in r.data.get("result", {}) and "missing_info" not in r.data:
            r = self._with_gaps(r, gaps)
        return r

    def _unknown(self, q, unknown):
        """a question about a name nobody told the agent about: no Yes/No on something it does not know"""
        w = self.world
        known = sorted(n for n in self.agent.names_in(w) - set(self.k.constants) if not n.lstrip("-").isdigit())
        who = ", ".join(unknown)
        text = (f"I do not know {who}: " + ("it is" if len(unknown) == 1 else "they are") + " not in what you have told me"
                + (f" (I know: {', '.join(known[:12])}{', ...' if len(known) > 12 else ''})." if known else "."))
        res = {"kind": q["kind"], "unknown": unknown, "known": known[:50]}
        return Reply(self._phrase(q, res, text), "input-required", {"question": q, "result": res, "error": {"code": "unknown_name", "detail": text}})

    @staticmethod
    def _gap_text(gaps):
        lines = [f"{sent} (I have it for {', '.join(who)})" for sent, who in gaps]
        return "To decide this I am missing: " + "; ".join(lines) + ". The answer above counts it as absent: tell me and I will answer again."

    def _with_gaps(self, r, gaps):
        """the answer used the absence of a fact that other things have: say it (the rules cannot know it was forgotten)"""
        text = r.text + "\n" + self._gap_text(gaps)
        data = {**r.data, "missing_info": [{"missing": sent, "known_for": who} for sent, who in gaps]}
        return Reply(text, r.state, data)

    def _answer(self, q, goal):
        w, kind = self.world, q["kind"]
        step = self._step(q.get("step"), w.last)
        when = "" if step == w.last else f" at {self.k.label(w, step)}"
        if kind == "make":
            return self.c.execute.make(goal, q)
        if kind in ("why", "why_not", "is") and not T.is_ground(goal):
            kind = "which"
        if kind == "which":
            found = self.k.which(w, step, goal)
            res = {"kind": kind, "step": step, "found": [T.text(t) for t in found]}
            text = ("; ".join(self.vocab.say(t) for t in found) if found else self.vocab.say(goal, unknown="nothing")) + when + "."
        elif kind == "is":
            yes = self.k.holds(w, step, goal)
            res = {"kind": kind, "step": step, "holds": yes}
            text = ("Yes: " if yes else "No: ") + self.vocab.say(goal, negated=not yes) + when + "."
        elif kind in ("why", "why_not"):
            if self.k.holds(w, step, goal):
                r = self.k.why(w, step, goal)
                res = {"kind": "why", "step": step, "holds": True, **r}
                text = self._say_why(goal, r, when)
            else:
                r = self.k.why_not(w, step, goal)
                res = {"kind": "why_not", "step": step, "holds": False, **r}
                text = self._say_why_not(goal, r, when)
        elif kind == "change":
            a, b = self._step(q.get("from"), 0), self._step(q.get("to"), w.last)
            r = self.k.change(w, goal, a, b)
            res = {"kind": kind, **r}
            text = self._say_change(goal, r)
        else:
            a, b = self._step(q.get("from"), 0), self._step(q.get("to"), w.last)
            rows = self.k.timeline(w, goal, a, b)
            res = {"kind": kind, "rows": rows}
            text = "\n".join(f"{row['label']}: " + ("; ".join(self.vocab.say(T.parse(x)[0]) for x in row["holds"])
                                                     or self.vocab.say(goal, unknown="nothing")) for row in rows)
        res["labels"] = {str(s): self.k.label(w, s) for s in range(w.last + 1)}
        gaps = self.k.end_misses(w, clear=False)
        if gaps:
            res["missing_info"] = [{"missing": sent, "known_for": who} for sent, who in gaps]
            text += "\n" + self._gap_text(gaps)
        return Reply(self._phrase(q, res, text), "completed", {"question": q, "result": res})

    def _since(self, s):
        return "since the start" if s == 0 else f"since step {s}" if s else ""

    def _say_why(self, goal, r, when):
        lines = [f"{self.vocab.say(goal)}{when}, because:"] + _indent(r["tree"])
        if r["rests_on"]:
            lines.append("It rests on: " + "; ".join(f"{x['say']} ({self._since(x['since'])})" for x in r["rests_on"]) + ".")
        return "\n".join(lines)

    def _say_why_not(self, goal, r, when):
        head = f"{self.vocab.say(goal, negated=True)}{when}."
        ways = r["ways"]
        if not ways:
            return head + " Nothing that can change would make it true (it depends only on how things are described)."

        def leaf(x):
            now = f"now {x['now']}" if x.get("now") else ""
            since = self._since(x.get("since"))
            extra = ", ".join(p for p in (now, since) if p)
            return x["say"] + (f" ({extra})" if extra else "")

        def way(w):
            return " and ".join(leaf(x) for x in w)
        if len(ways) == 1:
            return head + " It would need: " + way(ways[0]) + "."
        return head + " Any ONE of these would make it true (within each, ALL of it is needed):\n" + "\n".join(
            f"{i}. {way(w)}" for i, w in enumerate(ways, 1))

    def _say_change(self, goal, r):
        g = self.vocab.say(goal)
        a, b = self.k.label(self.world, r["from"]), self.k.label(self.world, r["to"])
        if not r["flips"]:
            return f"It did not change between {a} and {b}: " + (g if r["at_start"] else self.vocab.say(goal, negated=True)) + " all along."
        parts = [("it became true" if f["now"] else "it stopped being true") + f" at {f['label']}" for f in r["flips"]]
        return f"'{g}' changed {len(r['flips'])} time(s) between {a} and {b}: " + "; ".join(parts) + "."

    def _phrase(self, q, res, text):
        """an LLM may rephrase; its text is used only if every name and number it cites is in the result"""
        try:
            out = self.agent.translator.phrase({"client_message": self.client_text, "question": q, "result": res, "plain_answer": text})
        except TranslatorError:
            return text
        if not out:
            return text
        blob = json.dumps(res, ensure_ascii=False) + text
        known = self.agent.names_in(self.world)
        for tok in set(re.findall(r"\b[a-z][a-z0-9_]*\b|\b\d+\b", out)):
            if (tok.isdigit() and tok not in blob) or (tok in known and tok not in blob):
                return text
        return out


class Execute(_Part):
    """acts on the world (reads can_change/1 and act/2)"""

    def make(self, goal, q):
        """change what the meta-interpreter allows (can_change/1) so that goal holds; each change is an event, and its
        command (act/2) goes to the device. The rules check the result before anything is done."""
        w = self.world
        if not T.is_ground(goal):
            return Reply("Say which one: " + self.vocab.say(goal) + ".", "input-required", {"question": q})
        if self.k.holds(w, w.last, goal):
            return Reply(f"Already true: {self.vocab.say(goal)}.", "completed", {"question": q, "result": {"kind": "make", "done": []}})
        ways = self.k.why_not(w, w.last, goal)["ways"]
        refused = []
        for way in ways:
            try:
                changes = self._changes(way)
            except T.TermError:
                loose = [x["say"] for x in way if _has_var(x["fact"])]
                res = {"kind": "make", "done": [], "refused": [{"way": [x["fact"] for x in way], "why": "incomplete"}]}
                text = ("I cannot do that yet: «" + "; ".join(loose or [self.vocab.say(goal)]) + "» lacks something where the ? is. "
                        "Say it in the same sentence and I will do it.")
                return Reply(self.c.check._phrase(q, res, text), "input-required", {"question": q, "result": res})
            if changes is None:
                refused.append((way, "it needs something to stop being true that I cannot set to a value"))
                continue
            blocked = [x["say"] for x, (t, n) in zip(way, changes) if not self.k.can_change(w, t)]
            if blocked:
                refused.append((way, "I am not allowed to change: " + "; ".join(blocked)))
                continue
            trial = World(w.facts, w.events + changes)
            self.k.snapshots(trial)
            if not self.k.holds(trial, trial.last, goal):
                refused.append((way, "the rules say it would still not be true"))
                continue
            return self._do(goal, q, changes)
        res = {"kind": "make", "done": [], "refused": [{"way": [x["fact"] for x in way], "why": why} for way, why in refused]}
        if not refused:
            return Reply(f"I cannot make that true: nothing that can change would make {self.vocab.say(goal)}.", "completed",
                         {"question": q, "result": res})
        lines = [f"I cannot make it true by myself: {self.vocab.say(goal)}. It would need:"]
        lines += [f"{i}. " + " and ".join(x["say"] for x in way) + f" ({why})" for i, (way, why) in enumerate(refused, 1)]
        return Reply("\n".join(lines), "completed", {"question": q, "result": res})

    def _changes(self, way):
        """a way (leaves of why_not) -> events [(term, negated)], or None if one leaf cannot be turned into an event"""
        out = []
        for x in way:
            t = T.parse(x["fact"], allow_vars=False)[0]
            ph = self.vocab.get(t)
            if x["kind"] == "need":
                out.append((t, False))
            elif ph.arity == 1 or ph.set:
                out.append((t, True))
            else:                                   # something must stop being true: another allowed value of its last argument
                cur = str(T.value(T.args(t)[-1]))
                others = [v for v in ph.values.get(ph.arity - 1, []) if v != cur]
                if not others:
                    return None
                out.append((T.build(ph.name, [T.value(a) for a in T.args(t)[:-1]] + [others[0]]), False))
        return out

    def _do(self, goal, q, changes):
        done, said = [], []
        device = getattr(self.agent, "device", None)
        for t, neg in changes:
            cmd = self.k.act(self.world, t)
            note = ""
            if cmd and device:
                try:
                    out = device(cmd)
                except Exception as e:                 # the device refused: stop here, keep what was done
                    said.append(f"the device failed on '{cmd}': {str(e)[:120]}")
                    break
                note = f" (device: {cmd}" + (f" -> {str(out).strip()[:80]}" if out else "") + ")"
            elif cmd:
                note = f" (command: {cmd})"
            self.world = World(self.world.facts, self.world.events + [(t, neg)])
            done.append({"fact": T.text(t), "negated": neg, "command": cmd})
            said.append(self.vocab.say(t, negated=neg) + note)
        self.k.snapshots(self.world)
        ok = self.k.holds(self.world, self.world.last, goal)
        text = "Done: " + "; ".join(said) + ".\nNow: " + (self.vocab.say(goal) if ok else self.vocab.say(goal, negated=True)) + "."
        return Reply(text, "completed" if ok else "failed", {"question": q, "result": {"kind": "make", "done": done, "holds": ok},
                                                              "steps": self.world.last})


def _has_var(fact_text):
    try:
        T.parse(fact_text, allow_vars=False)
        return False
    except T.TermError:
        return True


def _indent(node, prefix="", last=True, root=True):
    mark = "" if root else ("└─ " if last else "├─ ")
    lines = [] if root else [prefix + mark + node["say"]]
    kids = node.get("because", [])
    new = "  " if root else prefix + ("   " if last else "│  ")
    for i, c in enumerate(kids):
        lines += _indent(c, new, i == len(kids) - 1, False)
    return lines



class Complete(_Part):
    """finds what is missing and asks for it (reads missing/1 and ask/2); teaches how to ask; looks at the world (observe:)"""

    # ---- what is missing
    def gaps_for(self, q):
        """what missing/1 still needs, only when the agent looks at the world itself (nobody confirmed that world) and only
        about the things the question names (all of it when it names none): [fact terms]"""
        if self.world is None or not self.c.observed or not self.k.has_missing:
            return []
        gaps = self.k.missing(self.world)
        if not gaps:
            return []
        goal, _ = self.k.goal(q["goal"])
        named = {str(T.value(a)) for a in T.args(goal) if T.is_ground(a)}
        if not named:
            return gaps
        return [g for g in gaps if T.args(g) and T.is_ground(T.args(g)[0]) and str(T.value(T.args(g)[0])) in named]

    def ask_gaps(self, gaps, base, lead):
        """the reply that asks for the missing facts, with ask/2's words (or the phrase with a '?')"""
        asks = []
        for g in gaps[:4]:
            ph = self.vocab.get(g)
            said = self.k.ask_words(base, g) if base is not None else None
            if said:
                asks.append(said)
            elif ph:
                asks.append(ph.render_ask([T.value(a) if T.is_ground(a) else None for a in T.args(g)]))
            else:
                asks.append(g.name if isinstance(g, E.Atom) else T.text(g))
        self.c.last_asked = " ".join(asks)
        return Reply(lead + " ".join(asks), "input-required", {"missing": [T.text(g) for g in gaps]})

    # ---- looking at the world: observe:
    def observe(self):
        """run the file's observe command (if the agent has one) and put what it prints in the world as facts; what an earlier
        look put there is replaced; what the client told stays. Rejection('observation_failed') when it cannot look."""
        obs = self.agent.observe
        if obs is None:
            return
        try:
            text = obs()
        except Exception as e:
            raise Rejection("observation_failed", f"I could not look at the {self.agent.domain} ({str(e)[:160]})")
        facts, unread = self.agent.facts_from_text(text)
        base = self.world or World()
        keep = [f for f in base.facts if T.text(f) not in self.c.observed]
        seen = {T.text(f) for f in keep}
        new = [f for f in facts if T.text(f) not in seen and not seen.add(T.text(f))]
        self.world = World(keep + new, base.events)
        self.k.snapshots(self.world)
        self.c.observed = {T.text(f) for f in new}
        self.c.unread = unread

    # ---- teaching how to ask
    def examples(self, limit=6):
        """valid things to say, made from the phrases of your questions and of the actions the agent may do, with names it knows:
        for each question one sentence (the one that asks for a variable if you wrote one, else yes/no, else 'what ...'),
        then 'get ...' for each action the agent is allowed to do."""
        w, bind = self.world, {}
        qs = list(self.vocab.questions())[:8]
        if w is not None:
            for ph in qs:                                  # an instance that holds gives names that go together
                goal = E.Struct(ph.name, [E.Var() for _ in ph.argnames]) if ph.arity else E.Atom(ph.name)
                try:
                    found = self.k.which(w, w.last, goal)[:1]
                except Rejection:
                    found = []
                for f in found:
                    for name, a in zip(ph.argnames, T.args(f)):
                        bind.setdefault(name, str(T.value(a)))
            for f in w.facts:
                ph = self.vocab.get(f)
                for name, a in zip(ph.argnames if ph else [], T.args(f)):
                    bind.setdefault(name, str(T.value(a)))
        vals = lambda ph: [bind.get(a) or f"<{a}>" for a in ph.argnames]
        out = []
        for ph in qs:
            wh = [s for s in ph.says if re.search(r"\{\w+=\?\}", s)]
            m = re.match(r"^\{(\w+)\} (is|are) (.*)$", ph.says[0])
            if wh:
                out.append(speak(wh[0], vals(ph), ph.argnames))
            elif m:
                out.append(speak(f"{m.group(2)} {{{m.group(1)}}} {m.group(3)}", vals(ph), ph.argnames))
            elif ph.arity:                                 # ask for whoever the sentence starts with: "what lacks the ram of j1?"
                first = re.search(r"\{(\w+)\}", ph.says[0])
                v = vals(ph)
                if first:
                    v[ph.argnames.index(first.group(1))] = "what"
                out.append(speak(ph.says[0], v, ph.argnames))
            else:
                out.append(speak(ph.says[0], [], ph.argnames))
        for ph in self.vocab:
            m = re.match(r"^\{(\w+)\} (is|are) (.*)$", ph.says[0]) if ph.kind == "fact" and ph.changes else None
            if not m:
                continue
            v = vals(ph)
            if w is not None and "<" not in "".join(v):
                try:
                    t = T.parse(f"{ph.name}({', '.join(v)})", allow_vars=False)[0] if ph.arity else E.Atom(ph.name)
                    if not self.k.can_change(w, t):
                        continue
                except (T.TermError, Rejection):
                    continue
            out.append("get " + speak(f"{{{m.group(1)}}} {m.group(3)}", v, ph.argnames))
        return [x + "?" if not x.startswith("get ") else x for x in out][:limit]

    def teach(self):
        """(text, examples): how to ask, in the agent's own phrases"""
        ex = self.examples()
        if not ex:
            return "", []
        return "You can ask me, for example: " + "; ".join(f"“{x}”" for x in ex) + ". Add “why” in front of a question and I give the reason.", ex
