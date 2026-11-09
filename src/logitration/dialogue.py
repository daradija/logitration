# Copyright (c) 2026 David Ragel
# SPDX-License-Identifier: BUSL-1.1
# Licensed under the Business Source License 1.1; see LICENSE.
"""The conversation: one per session. Who does what:

  translator   text -> fixed JSON (never trusted)            |  result -> text (checked, optional)
  this module  sanitises that JSON, asks what missing/1 says is missing, repeats the description in words (made from
               the validated facts, not from what the model believes) and loads it only after "yes"; then routes questions
  knowledge    validates every fact and goal, and decides every answer

The three meta-interpreters (interpreters.py) do the work: Check answers and explains, Execute acts, Complete asks what is
missing, looks at the world and teaches how to ask. This module keeps the conversation: who said what, and the yes.

Phases: new -> confirming -> active (a new description, or a correction, goes back to confirming).
"""
import json
import re

from logitration import engine as E
from logitration import terms as T
from logitration.interpreters import Check, Complete, Execute
from logitration.knowledge import Rejection, World
from logitration.reply import Reply
from logitration.translators import INTENTS, KINDS, TranslatorError

MAX_TEXT = 4000


def _strs(x, n=400):
    return [s.strip() for s in (x if isinstance(x, list) else []) if isinstance(s, str) and s.strip()][:n]


def sanitize(raw):
    """whatever the translator returned -> the closed shape (unknown keys and types dropped)"""
    if not isinstance(raw, dict):
        raise TranslatorError("the translation is not an object")
    q = raw.get("question")
    if isinstance(q, dict) and q.get("kind") in KINDS and isinstance(q.get("goal"), str):
        def ref(v):
            return v if v in ("start", "end") or (isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 100000) else None
        q = {"kind": q["kind"], "goal": q["goal"].strip()[:200], "step": ref(q.get("step")), "from": ref(q.get("from")), "to": ref(q.get("to"))}
    else:
        q = None
    intent = raw.get("intent") if raw.get("intent") in INTENTS else "other"
    return {"intent": intent, "facts": _strs(raw.get("facts")), "events": _strs(raw.get("events")),
            "remove": _strs(raw.get("remove")), "question": q, "doubts": _strs(raw.get("doubts"), 4)}


class Conversation:
    def __init__(self, agent):
        self.agent = agent
        self.k = agent.knowledge
        self.vocab = agent.vocab
        self.world = None              # the loaded world (after "yes")
        self.pending = None            # the world being described, before "yes"
        self.saved_question = None
        self.last_asked = None
        self.observed = set()          # texts of the facts the agent saw itself (observe:), not told by the client
        self.unread = []               # lines of the last look that were not facts of the vocabulary
        self.check, self.execute, self.complete = Check(self), Execute(self), Complete(self)
        self.client_text = ""          # what the client wrote this turn (only to tell the phrasing model which language to use)

    def turn(self, text):
        if not isinstance(text, str) or not text.strip():
            return Reply("Say something: describe your situation or ask a question.", "input-required")
        if len(text) > MAX_TEXT:
            return Reply(f"That message is too long (more than {MAX_TEXT} characters).", "rejected", {"error": {"code": "too_large"}})
        self.client_text = text[:400]
        try:
            it = sanitize(self.agent.translator.interpret(text, self.context()))
        except TranslatorError as e:
            return Reply("I could not understand that right now (translator error). Please try again.", "failed",
                         {"error": {"code": "translator_error", "detail": str(e)[:200]}})
        try:
            return self._route(it)
        except Rejection as r:
            return Reply(self._rejected(r), "input-required", {"error": r.to_json()})

    def _rejected(self, r):
        """what the client hears when something it said is refused. A refused TYPE is said by the translator in the client's own
        language (the simulated one has none: English); if it cannot, English."""
        plain = f"I could not use that: {r.detail}."
        if r.code != "invalid_type":
            return plain
        try:
            said = self.agent.translator.phrase({"client_message": self.client_text, "rejected": r.detail})
        except TranslatorError:
            return plain
        return said or plain

    def _route(self, it):
        i = it["intent"]
        if i == "reset":
            self.__init__(self.agent)
            return Reply("Forgotten. Describe your situation from the start.", "input-required")
        if i == "tell" or it["facts"] or it["events"] or it["remove"]:
            return self._tell(it)
        if i == "yes":
            return self._yes()
        if i == "no" and self.pending is not None:
            self.last_asked = "What should I correct?"
            return Reply("Then tell me what to correct (what to add, or what to take out).", "input-required")
        if i == "ask" and it["question"]:
            q = it["question"]
            if self.agent.observe is not None and self.pending is None:
                self.complete.observe()                        # the agent looks at the world itself: the client only asks
            if self.world is None:
                if self.pending is not None:
                    self.saved_question = q
                    return Reply("I will answer that once you confirm the description. " + self._confirmation(), "input-required")
                return Reply("Tell me your situation first. " + self._how_to_talk(short=True), "input-required")
            gaps = self.complete.gaps_for(q)
            if gaps:
                self.saved_question = q
                return self.complete.ask_gaps(gaps, self.world, "To answer that I need to know: ")
            return self.answer(q)
        if it["doubts"]:
            return Reply(" ".join(it["doubts"]), "input-required")
        if i == "help":
            return self._teach(self._how_to_talk())
        return self._teach("I did not understand that as a fact or a question about " + self.agent.domain + ". "
                           "I do not run instructions, commands or code. " + self._how_to_talk(short=True))

    def _how_to_talk(self, short=False):
        """what the client may say: with observe: the agent sees the facts itself, so only questions are taught"""
        if self.agent.observe is not None:
            return f"I look at the {self.agent.domain} myself: you only need to ask."
        return self.agent.help_text(short=short)

    def _teach(self, text):
        """the answer to something not understood (or to 'what can I ask?'): not a refusal, the way to ask"""
        if self.agent.observe is not None and self.pending is None:
            self.complete.observe()                            # the examples use the names it sees now
        how, ex = self.complete.teach()
        return Reply((text + " " + how).strip(), "input-required", {"examples": ex} if ex else {})

    def _tell(self, it):
        facts = [self.k.fact(f) for f in it["facts"]]
        events = [self.k.event(e[4:] if e.startswith("not ") else e, e.startswith("not ")) for e in it["events"]]
        remove = {T.text(self.k.fact(f)) for f in it["remove"]}
        if self.world is not None and self.pending is None and not facts and not remove:
            # only events on a loaded world: they are added at once (like a diary)
            before = self.world.last
            self.world = World(self.world.facts, self.world.events + events)
            self.k.snapshots(self.world)
            lines = [f"Added {len(events)} event(s): " + "; ".join(self.k.label(self.world, s) for s in range(before + 1, self.world.last + 1)) + "."]
            lines.append("Now: " + self.summary(self.world, self.world.last))
            r = Reply(" ".join(lines), "completed", {"steps": self.world.last})
            return self._then_question(r, it["question"])
        base = self.pending or (self.world.copy() if self.world else World())
        old = [f for f in base.facts if T.text(f) not in remove]
        seen = {T.text(f) for f in old}
        new = old + [f for f in facts if T.text(f) not in seen and not seen.add(T.text(f))]
        self.pending = World(new, base.events + events)
        self.k.snapshots(self.pending)
        if it["question"]:
            self.saved_question = it["question"]
        gaps = self.k.missing(self.pending)
        if gaps:
            return self._ask_gaps(gaps)
        return Reply(self._confirmation(), "input-required", {"facts": [T.text(f) for f in self.pending.facts],
                                                                 "events": [T.text(t) for t, _ in self.pending.events]})

    def _confirmation(self):
        p = self.pending
        lines = ["This is what I understood:"] + [f"- {self.vocab.say(f)}" for f in p.facts if T.text(f) not in self.observed]
        if p.events:
            lines.append("Then, in order:")
            lines += [f"{k}. {self.vocab.say(t, negated=n)}" for k, (t, n) in enumerate(p.events, 1)]
        lines.append("Is that correct?")
        self.last_asked = "Is that correct?"
        return "\n".join(lines)

    def _yes(self):
        if self.pending is None:
            return Reply("There is nothing waiting for confirmation. Ask me something, or describe a new situation.", "input-required")
        gaps = self.k.missing(self.pending)
        if gaps:
            return self._ask_gaps(gaps)
        self.world, self.pending = self.pending, None
        self.last_asked = None
        n = self.world.last
        text = f"Loaded: {len(self.world.facts)} facts and {n} event(s). " + (
            f"At the start: {self.summary(self.world, 0)} At the end: " if n else "Now: ") + self.summary(self.world, n)
        r = Reply(text, "completed", {"facts": len(self.world.facts), "steps": n})
        q, self.saved_question = self.saved_question, None
        return self._then_question(r, q)

    def _then_question(self, r, q):
        if not q:
            return r
        a = self.answer(q)
        return Reply(r.text + "\n" + a.text, a.state, {**r.data, **a.data})


    # ------------------------------------------------------------ the meta-interpreters, by name
    def answer(self, q):
        return self.check.answer(q)

    def summary(self, world, step):
        return self.check.summary(world, step)

    def make(self, goal, q):
        return self.execute.make(goal, q)

    def _ask_gaps(self, gaps):
        return self.complete.ask_gaps(gaps, self.pending, "Before I load it I need to know: ")

    def context(self):
        w = self.pending or self.world
        return {"state": "confirming" if self.pending else ("active" if self.world else "new"),
                "facts_so_far": [T.text(f) for f in w.facts][:200] if w else [],
                "events_so_far": [T.text(t) for t, _ in w.events][:200] if w else [],
                "last_question_asked_by_the_agent": self.last_asked}

