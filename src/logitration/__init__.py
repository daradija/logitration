# Copyright (c) 2026 David Ragel
# SPDX-License-Identifier: BUSL-1.1
# Licensed under the Business Source License 1.1; see LICENSE.
"""logitration: you write ONE idea file (a conversation, your logic, your meta-interpreter) and try it:

    logi try circuit.logi            talk to it in the terminal (:why :facts :rules :example :reload)
    logi check circuit.logi          replay the file's conversation and say if the agent answers as written
    logi serve circuit.logi          publish it over A2A

The file format is in logitration/idea.py. Below, the same agent from Python (advanced use):

    from logitration import LogicAgent

    agent = LogicAgent(rules="lamp.pl", phrases={"lamp(X)": "{X} is a lamp", "on(X)": "{X} is on"})
    print(agent.say("l1 is a lamp"))        # the agent repeats what it understood ...
    print(agent.say("yes"))                 # ... and loads it after your yes
    print(agent.say("why is l1 on?"))       # the rules answer; the translator only translates
    agent.serve(port=8923)                  # the same agent, published over A2A
    LogicAgent.connect("http://127.0.0.1:8923/").say("is l1 on?")   # and talked to from anywhere

The rules decide what is true; the translator (simulated, claude-cli or anthropic-api) only turns text into facts
and questions and back; nothing the client writes is ever run as Prolog.
"""
import re
import threading
import time
import uuid

from logitration import a2a
from logitration import terms as T
from logitration.dialogue import Conversation, Reply
from logitration.knowledge import LIMITS, Knowledge, Rejection
from logitration.phrases import PhraseError, Vocabulary
from logitration.translators import Translator, TranslatorError, make_translator

__all__ = ["LogicAgent", "Reply", "Translator", "PhraseError", "Rejection", "TranslatorError", "load", "Idea", "IdeaError"]
__version__ = "0.1.0"


class LogicAgent:
    """Your logic, its phrases, a translator and A2A, in one object.

    rules       a .pl file or Prolog text. Predicates with a phrase and no rule are facts the client states;
                predicates with rules are questions. Optional missing/1: what to ask before loading a description.
    phrases     dict {"pred(Arg, ...)": template(s) or {"say", "changes", "ask", "not", "values"}}, or a JSON file
    translator  "simulated" (default, no LLM) | "claude-cli" | "anthropic-api" | a Translator object
    model       the model for the Claude backends (default claude-sonnet-5-5)
    name, domain, description   how the agent presents itself (the A2A card)
    tools       {"pred/arity": python_function}: Python functions your rules can call (last argument = result)
    limits      overrides of logitration.knowledge.LIMITS (sizes and the time per question)
    device      callable(command) -> output: runs the commands that act/2 gives when the agent makes something true
    observe     callable() -> text: the agent looks at the world itself before answering (read-only). The text has one fact per
                line, in the words of your phrases ("n1 has 16 GB of ram") or as Prolog ("ram(n1, 16)."); lines that are not facts
                are ignored. The client then only asks.
    """

    def __init__(self, rules, phrases, translator="simulated", model=None, name="logic-agent", domain=None,
                 description=None, tools=None, limits=None, sessions=32, session_ttl=3600, device=None, observe=None):
        self._url = None
        self.observe = observe        # callable() -> text of facts (see above), or None
        self.device = device          # callable(command text) -> output; called when the agent acts (act/2 of the meta-interpreter)
        self.vocab = Vocabulary(phrases)
        self.knowledge = Knowledge(rules, self.vocab, tools=tools, limits=limits)
        self.translator = make_translator(translator, model)
        self.name = name
        self.domain = domain or "this domain"
        self.description = description or (f"A logic agent about {self.domain}: tell it the facts in natural language, "
                                           "then ask what is true and why. Rules decide; a language model only translates.")
        if hasattr(self.translator, "bind"):
            try:
                self.translator.bind(self.vocab, self.knowledge.main, domain=self.domain)
            except TypeError:
                self.translator.bind(self.vocab, self.knowledge.main)
        self._sessions, self._max, self._ttl = {}, sessions, session_ttl
        self._lock = threading.Lock()
        self._server = None

    # ------------------------------------------------------------ talking
    def say(self, text, session="main"):
        """one turn of conversation; returns a Reply (a str with .state and .data)"""
        if self._url:
            text_, data, state = a2a.reply_parts(a2a.send(self._url, session, {"kind": "text", "text": text}))
            return Reply(text_, state, data)
        return self._conversation(session).turn(text)

    def ask(self, kind, goal, step=None, session="main"):
        """a structured question on a loaded conversation, no translator: kind is|which|why|why_not|change|timeline,
        goal like "lit(l1)" or "lit(X)", step an int, "start" or "end". Returns the logic's result (dict)."""
        q = {"kind": kind, "goal": goal, "step": step}
        if self._url:
            _, data, state = a2a.reply_parts(a2a.send(self._url, session, {"kind": "data", "data": {"ask": q}}))
            return data
        return self._ask_reply(q, session).data

    def _ask_reply(self, q, session):
        c = self._conversation(session)
        if c.world is None:
            return Reply("Nothing is loaded in this conversation yet.", "rejected", {"error": {"code": "nothing_loaded"}})
        if q.get("kind") not in ("is", "which", "why", "why_not", "change", "timeline", "make") or not isinstance(q.get("goal"), str):
            return Reply("Invalid question.", "rejected", {"error": {"code": "invalid_question"}})
        q = {"kind": q["kind"], "goal": q["goal"], "step": q.get("step"), "from": q.get("from"), "to": q.get("to")}
        for k in ("step", "from", "to"):
            if not (q[k] is None or q[k] in ("start", "end") or (isinstance(q[k], int) and not isinstance(q[k], bool))):
                return Reply("Invalid step.", "rejected", {"error": {"code": "invalid_step"}})
        try:
            r = c.answer(q)
        except Rejection as e:
            return Reply(e.detail, "rejected", {"error": e.to_json()})
        return r

    def _conversation(self, session):
        session = str(session)[:64]
        with self._lock:
            now = time.time()
            for k in [k for k, (c, t) in self._sessions.items() if now - t > self._ttl]:
                del self._sessions[k]
            if session not in self._sessions:
                if len(self._sessions) >= self._max:
                    oldest = min(self._sessions, key=lambda k: self._sessions[k][1])
                    del self._sessions[oldest]
                self._sessions[session] = (Conversation(self), now)
            c = self._sessions[session][0]
            self._sessions[session] = (c, now)
            return c

    # ------------------------------------------------------------ A2A
    def serve(self, port=8923, host="127.0.0.1", block=True):
        """publish this agent over A2A. block=False runs it in a background thread and returns the URL."""
        self._server, url = a2a.serve(self, host=host, port=port, block=block)
        return url

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    @classmethod
    def connect(cls, url):
        """a LogicAgent that talks to a remote A2A agent: same say() and ask()"""
        self = cls.__new__(cls)
        self._url = url if url.endswith("/") else url + "/"
        self.card = a2a.fetch_card(self._url)
        self.name = self.card.get("name")
        return self

    def facts_from_text(self, text):
        """what an observation printed -> ([fact terms], [(line, why it is not a fact)]). One fact per line, as a sentence of your
        phrases or as Prolog; only facts the client may state (the closed vocabulary), never rules or commands."""
        from logitration.translators import SimulatedTranslator
        sim = SimulatedTranslator()
        sim.bind(self.vocab, self.knowledge.main)
        facts, unread = [], []
        for raw in str(text)[:200000].splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                if re.match(r"^[a-z][a-z0-9_]*\(.*\)\.?$", line):
                    facts.append(self.knowledge.fact(line.rstrip(".")))
                    continue
                said = sim._fact(re.sub(r"\s+", " ", line.rstrip(".").lower()))
                if not said or said.startswith("not "):
                    unread.append((line, "not a sentence of the phrases"))
                else:
                    facts.append(self.knowledge.fact(said))
            except Rejection as e:
                unread.append((line, e.detail[:120]))
            if len(facts) > self.knowledge.limits["facts"]:
                raise Rejection("too_large", f"the observation has more than {self.knowledge.limits['facts']} facts")
        return facts, unread

    # ------------------------------------------------------------ texts the agent uses
    def help_text(self, short=False):
        facts = [p.says[0] for p in self.vocab.facts()]
        qs = [p.says[0] for p in self.vocab.questions()]
        ex = lambda s: re.sub(r"\{(\w+)\}", lambda m: "<" + m.group(1) + ">", s)
        if short:
            return "You can tell me things like: " + "; ".join(ex(s) for s in facts[:4]) + "."
        return ("You can tell me, in your own words: " + "; ".join(ex(s) for s in facts) + ". "
                "Things that happen afterwards go in order (\"then ...\"). Then ask me whether something is true, which ones, "
                "why, why not, or how it changed, about: " + "; ".join(ex(s) for s in qs) + ".")

    def names_in(self, world):
        out = set()
        if world:
            for f in world.facts + [t for t, _ in world.events]:
                out.update(str(T.value(a)) for a in T.args(f))
        return out


from logitration.idea import Idea, IdeaError, load  # noqa: E402  (after LogicAgent: idea.load builds one)
