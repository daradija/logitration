"""The `converse` skill: the logic agent talks NATURAL LANGUAGE (English) with the client.

Who does what (and what each layer does NOT do):
  translator (LLM)  natural -> draft/question (JSON of a fixed shape) and core -> English. It decides no truth.
  this module       conversation state, interview (gaps and ambiguities are detected here, deterministically), confirmation
                    in words (generated from what is going to be loaded, not from what the LLM "believes" it understood), versioning.
  validator         core.validate_definition / validate_events: EVERYTHING that comes out of the LLM goes through it before reaching the core.
  core (Prolog)     decides the truth. Queries are built by core.py with Atom/Struct; no client or LLM text is ever read as Prolog.
One conversation = one A2A contextId. Phases: start -> interview -> confirming -> active (and back to interview if they describe another circuit).
"""
import difflib
import json
import re
import threading
import time
import uuid

from circuit_a2a import core
from circuit_a2a.core import Rejection, Session, LIMITS, validate_definition, validate_events, run_in_core
from circuit_a2a.composites import expand_definition, expand_events
from circuit_a2a.translator import TranslatorError, strip_accents

MAX_TEXT = 4000
MAX_CONTEXTS = 24
INTENTS = ("describe", "extend", "ask", "confirm", "deny", "help", "other")
DRAFT_TYPES = ("source", "wire", "socket", "fuse", "lamp", "switch", "two_way_switch", "plug")
DRAFT_ACTIONS = ("raise", "lower", "blow", "repair", "plug", "unplug")
QUESTION_TYPES = ("lit", "supporting", "how", "why", "why_not", "timeline")
NEED_LAMP = ("supporting", "how", "why", "why_not")
RE_YES = re.compile(r"^(yes|yeah|yep|sure|ok|okay|correct|exactly|agreed|perfect|right|i confirm|that is right|that is correct|it is correct|all correct|affirmative)"
                    r"(,? ?(it is correct|that is correct|that is right|thanks|thank you|that is it|all good|go ahead))?$")
RE_NO = re.compile(r"^(no|no, it is not correct|it is not correct|not correct|incorrect|that is not it|negative)$")

EXAMPLE = (
    "You can describe it in your own words, no format needed. Example:\n"
    "\"I have a mains input. The wire c1 runs from it to the lamp l1, but in between there is a switch s1 that closes the circuit "
    "when it is up; right now it is down. Then I raise s1. Why is l1 not lit at the start?\"\n"
    "Tell me: which parts there are (power source, wires, sockets, switches, two-way switches, plugs, fuses, lamps), what feeds what "
    "(and which switch or fuse cuts it, and in which position it closes), which position each switch is in at the start, and what happens next, in order "
    "(raising or lowering a switch, blowing or repairing a lamp or a fuse, plugging or unplugging a plug). Then I will repeat in words what I "
    "understood so you can tell me whether it is correct, and you can ask me: which lamps are lit, why one is lit, why one is NOT lit, why it changed, "
    "or what happens to the lamps over the course of the events.")

EXAMPLE_SHORT = (
    "Here is an example of how to describe it, in your own words and with no format: \"I have a mains input. The wire c1 runs from it to the lamp l1, but in "
    "between there is a switch s1 that closes the circuit when it is up; right now it is down. Then I raise s1. Why is l1 not lit at the start?\". "
    "I need to know the parts, what feeds what (and what cuts it, and in which position it closes), the initial position of each switch and what happens next, in order.")

CAPABILITIES = ("I only know about circuits: you describe them to me, I confirm what I understood and I answer questions about them (what is lit, how we know, "
                "why it changed, why it is NOT lit, timeline). I do not run instructions, commands or code.")


# ------------------------------------------------------------------ sanitizing what the translator returns (it is never trusted)
def clean_text(t):
    t = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", t)
    return t.strip()


def clean_name(x, notes=None):
    if not isinstance(x, str):
        return "" if x is None else str(x)[:40]
    n = re.sub(r"[\s\-]+", "_", strip_accents(x.strip().lower()))
    if n != x and notes is not None:
        notes.append(f"name normalized: {x!r} -> {n!r}")
    return n[:60]


def _ref(r):
    if r in ("initial", "final"):
        return r
    if isinstance(r, dict) and isinstance(r.get("event"), int) and not isinstance(r.get("event"), bool) and 0 <= r["event"] <= 10000:
        return {"event": r["event"]}
    if isinstance(r, int) and not isinstance(r, bool) and 0 <= r <= 10000:
        return {"event": r}
    return None


def sanitize_draft(b, notes):
    if not isinstance(b, dict):
        return None
    out = {"parts": [], "wires": [], "events": [], "no_events": bool(b.get("no_events"))}
    for p in (b.get("parts") if isinstance(b.get("parts"), list) else [])[:200]:
        if not isinstance(p, dict):
            notes.append("part that is not an object: dropped")
            continue
        pos = p.get("position")
        pos = strip_accents(pos.strip().lower()) if isinstance(pos, str) and pos.strip() else None
        t = p.get("type")
        out["parts"].append({"name": clean_name(p.get("name"), notes), "type": strip_accents(t.strip().lower()) if isinstance(t, str) else t, "position": pos})
    for c in (b.get("wires") if isinstance(b.get("wires"), list) else [])[:400]:
        if not isinstance(c, dict):
            notes.append("wire that is not an object: dropped")
            continue
        if "two_way_switch" in c:
            q = {"two_way_switch": clean_name(c.get("two_way_switch"), notes)}
            for k in ("to", "from", "from_up", "from_down", "to_up", "to_down"):
                if k in c and c[k] is not None:
                    q[k] = clean_name(c[k], notes)
        else:
            q = {"to": clean_name(c.get("to"), notes), "from": clean_name(c.get("from"), notes)}
            when = c.get("when")
            if isinstance(when, str) and when.strip():
                q["when"] = re.sub(r"\s+", "", strip_accents(when.lower()))
            elif isinstance(when, dict):
                q["when"] = when
        out["wires"].append(q)
    out["events"] = sanitize_events(b.get("events"), notes)
    return out


def sanitize_events(lst, notes):
    out = []
    for e in (lst if isinstance(lst, list) else [])[:400]:
        if isinstance(e, dict):
            a = e.get("action")
            out.append({"action": strip_accents(a.strip().lower()) if isinstance(a, str) else a, "part": clean_name(e.get("part"), notes)})
        else:
            notes.append("event that is not an object: dropped")
    return out


def sanitize_interpretation(raw, notes):
    if not isinstance(raw, dict):
        raise TranslatorError("the interpretation is not an object")
    it = raw.get("intent")
    it = it if it in INTENTS else "other"
    if raw.get("intent") not in INTENTS:
        notes.append(f"unknown intent {raw.get('intent')!r}")
    circ = raw.get("circuit")
    circ = clean_name(circ, None) if isinstance(circ, str) and circ.strip() else None
    pr = raw.get("question")
    q = None
    if isinstance(pr, dict) and pr.get("type") in QUESTION_TYPES:
        pz = pr.get("part")
        c2 = pr.get("circuit")
        pzs = [clean_name(x, None) for x in (pr.get("parts") if isinstance(pr.get("parts"), list) else []) if isinstance(x, str) and x.strip()][:8]
        q = {"type": pr["type"], "part": clean_name(pz, None) if isinstance(pz, str) and pz.strip() else None, "parts": pzs,
             "circuit": clean_name(c2, None) if isinstance(c2, str) and c2.strip() else None,
             "step": _ref(pr.get("step")), "from_step": _ref(pr.get("from_step")), "to_step": _ref(pr.get("to_step"))}
    elif pr:
        notes.append(f"question dropped (type {pr.get('type') if isinstance(pr, dict) else pr!r})")
    doubts = [d.strip()[:240] for d in (raw.get("doubts") if isinstance(raw.get("doubts"), list) else []) if isinstance(d, str) and d.strip()][:6]
    return {"intent": it, "circuit": circ, "draft": sanitize_draft(raw.get("draft"), notes),
            "new_events": sanitize_events(raw.get("new_events"), notes), "question": q, "doubts": doubts}


# ------------------------------------------------------------------ compile the draft (everything goes through the validator)
def compile_draft(draft):
    """draft -> (model, events, user_actions). Raises Rejection if the validator does not admit it."""
    d = {"parts": draft["parts"], "wires": draft["wires"], "events": draft["events"]}
    d_base, info = expand_definition(d)
    model = validate_definition(d_base)
    model["user_type"], model["wire_groups"] = info["user_type"], info["groups"]
    ev_b, user_acts = expand_events(draft["events"], info["user_type"])
    events = validate_events(ev_b, model["type"])
    return model, events, user_acts


def circuit_name(x):
    n = clean_name(x, None) if isinstance(x, str) else ""
    n = re.sub(r"[^a-z0-9_]", "_", n)
    if not n or not n[0].isalpha():
        n = "circuit"
    return n[:20]


# ------------------------------------------------------------------ deterministic English
ACTION_TEXT = {"raise": "{} is raised", "lower": "{} is lowered", "blow": "{} blows", "repair": "{} is repaired",
               "plug": "{} is plugged in", "unplug": "{} is unplugged"}


def state_text(k, x, ut):
    pl = ut.get(x) == "plug"
    return {"up": "plugged in" if pl else "up", "down": "unplugged" if pl else "down", "ok": "in good condition",
            "blown": "blown"}.get(k, k)


def fact_text(h, ut, mode="is"):
    """'up(s1)' -> 's1 is up' (mode is) | 's1 up' (mode left: for "event 3 left s1 up")"""
    m = re.match(r"^(\w+)\((.*)\)$", h)
    if not m:
        return h
    k, a = m.group(1), m.group(2)
    if k in ("up", "down", "ok", "blown"):
        return f"{a} {state_text(k, a, ut)}" if mode == "left" else f"{a} is {state_text(k, a, ut)}"
    if k == "has_current":
        return f"{a} has current"
    if k == "lamp":
        return f"{a} is a lamp"
    if k == "connected":
        d, o = [x.strip() for x in a.split(",")]
        return f"{d} is connected to {o}"
    return h


def join_list(xs, conj="and"):
    xs = list(xs)
    if not xs:
        return ""
    if len(xs) == 1:
        return xs[0]
    return ", ".join(xs[:-1]) + f" {conj} " + xs[-1]


def are_lit(ls):
    return f"{join_list(ls)} {'is' if len(ls) == 1 else 'are'} lit"


def cap(s):
    return s[:1].upper() + s[1:]


class Circuit:
    """A confirmed circuit of the conversation (what the client described, already validated)."""

    def __init__(self, name, session, model, events, actions, draft):
        self.name, self.session = name, session
        self.model, self.events, self.actions, self.draft = model, list(events), list(actions), draft
        self.setup = None
        self.n = None
        self.versions = []

    @property
    def ut(self):
        return self.model.get("user_type", {})

    def lamps(self):
        return [n for n in self.model["order"] if self.model["type"][n] == "lamp"]

    def label(self, n):
        """event number n, in words"""
        if n is None:
            return "?"
        if n <= self.setup:
            return "the assembly"
        k = n - self.setup
        if 1 <= k <= len(self.events):
            return f"event {k} ({ACTION_TEXT[self.actions[k - 1]].format(self.events[k - 1][1])})"
        return f"step {n}"

    def moment_label(self, n):
        if n == self.setup:
            return "in the initial state (assembled, before any event)"
        k = n - self.setup
        if 1 <= k <= len(self.events):
            return f"after event {k} ({ACTION_TEXT[self.actions[k - 1]].format(self.events[k - 1][1])})"
        return f"after step {n}"

    def point_label(self, n):
        """a moment as the end of a range ("between X and Y")"""
        if n == self.setup:
            return "the initial state"
        k = n - self.setup
        if 1 <= k <= len(self.events):
            return f"event {k} ({ACTION_TEXT[self.actions[k - 1]].format(self.events[k - 1][1])})"
        return f"step {n}"

    def event_list(self):
        return [f"{k}: {ACTION_TEXT[a].format(pz)}" for k, (a, (_, pz)) in enumerate(zip(self.actions, self.events), 1)]


def summary_text(name, model, events, actions, extra_assumptions=()):
    """What is going to be loaded, in words. Generated FROM the validated model: what the client confirms is exactly what gets assembled."""
    ut, ptype, pos = model.get("user_type", {}), model["type"], model["pos"]
    nom = {"source": "power source", "wire": "wire", "socket": "socket", "fuse": "fuse", "lamp": "lamp",
           "switch": "switch", "two_way_switch": "two-way switch", "plug": "plug"}
    parts = []
    for n in model["order"]:
        t = ut.get(n, ptype[n])
        s = f"{nom[t]} {n}"
        if ptype[n] == "switch":
            s += f" (at the start {state_text(pos[n], n, ut)})"
        elif ptype[n] in ("lamp", "fuse"):
            s += " (in good condition)"
        parts.append(s)
    out = [f"This is what I understood (circuit \"{name}\"):", "Parts: " + "; ".join(parts) + "."]
    cs = []
    for d, o, c in model["wires"]:
        s = f"{d} gets current from {o}"
        if c:
            k, pz = c
            s += f" if {fact_text(f'{k}({pz})', ut)}"
        else:
            s += ", always"
        cs.append("- " + s)
    out.append("What feeds what:\n" + ("\n".join(cs) if cs else "- (no wires yet)"))
    if events:
        out.append("Events, in order:\n" + "\n".join(f"{k}. {ACTION_TEXT[a].format(pz)}" for k, (a, (_, pz)) in enumerate(zip(actions, events), 1)))
    else:
        out.append("Events: none (just the assembly).")
    if extra_assumptions:
        out.append("Note, I am leaving it as you said, but please look at this: " + " | ".join(extra_assumptions))
    out.append("Is that correct? Answer 'yes', or tell me what to change or what is missing.")
    return "\n".join(out)


def _words(q):
    return {w for w in re.findall(r"[a-z0-9_]+", strip_accents(q.lower())) if len(w) > 3}


def similar(q, others):
    """is q almost the same question as any of others? (Jaccard of words longer than 3 letters)"""
    a = _words(q)
    for o in others:
        b = _words(o)
        if a and b and len(a & b) / len(a | b) >= 0.34:
            return True
    return False


def question_from_rejection(r):
    """The validator rejected something that came from the translator: we ask the client about it in plain language."""
    c, d = r.code, r.detail
    base = {
        "duplicate_part": f"I see two parts with the same name ({d}). Are they the same part or different ones? If they are different, what should I call the second one?",
        "unknown_part": f"A part is mentioned that you have not described to me ({d}). Which part is it? Describe it (what type it is and what it connects to) or tell me which of the ones I already have it is mixed up with.",
        "invalid_wire": f"Switches do not carry current: they only open or close the wire they control ({d}). Tell me between which two parts that wire runs and which switch cuts it.",
        "wire_cycle": f"The wires you told me about form a loop ({d}) and I cannot reason with loops. Could you check where each part gets its current from?",
        "invalid_condition": f"A condition does not fit ({d}). A wire can depend on a switch being up or down, or on a fuse or a lamp being in good condition. Could you clarify what controls it?",
        "invalid_action": f"An event does not fit ({d}). The events I understand: raising or lowering a switch or two-way switch, blowing or repairing a lamp or a fuse, plugging or unplugging a plug. What exactly happened?",
        "unknown_type": f"I do not recognise a part type ({d}). I understand: power source, wire, socket, fuse, lamp, switch, two-way switch and plug. Which one is it closest to?",
        "invalid_name": f"A name does not work for me ({d}). Give me short names with letters, digits and underscores, starting with a letter.",
        "invalid_position": f"A position does not fit ({d}). Switches are up or down; plugs are plugged or unplugged.",
        "too_large": f"That is too big for me ({d}). Could you describe a smaller part of it?",
    }
    return base.get(c, f"Something you told me does not fit ({d}). Could you clarify it?")


# ------------------------------------------------------------------ checking the wording
def _tokens(s):
    return set(re.findall(r"[a-z][a-z0-9_]*", strip_accents(s.lower())))


def _integers(o, acc):
    if isinstance(o, bool):
        return
    if isinstance(o, int):
        acc.add(o)
    elif isinstance(o, dict):
        for v in o.values():
            _integers(v, acc)
    elif isinstance(o, (list, tuple)):
        for v in o:
            _integers(v, acc)


def check_wording(text, circ, data, extra_names=(), extra_ints=()):
    """What the LLM wrote may only cite parts and steps/events that are in the core's output. -> list of violations."""
    universe = set(circ.model["order"])
    allowed = (_tokens(json.dumps(data, ensure_ascii=False)) & universe) | set(extra_names)
    bad = []
    illegal = (_tokens(text) & universe) - allowed
    if illegal:
        bad.append("parts that are not in the result: " + ", ".join(sorted(illegal)))
    if re.search(r"\b(has_current|ok|is_lit|connected|up|down|blown)\(", text):
        bad.append("Prolog notation in the text (name(argument))")
    nums = set(extra_ints)
    _integers(data, nums)
    t = strip_accents(text.lower())
    for m in re.finditer(r"\b(?:step|moment)s?\s+(\d+)", t):
        if int(m.group(1)) not in nums:
            bad.append(f"step {m.group(1)} that is not in the result")
    for m in re.finditer(r"\bevents?\s+(\d+)((?:\s*(?:,|and|or|to|-)\s*\d+)*)", t):
        for k in [int(m.group(1))] + [int(x) for x in re.findall(r"\d+", m.group(2))]:
            if (circ.setup + k) not in nums and k not in nums and k != 0:
                bad.append(f"event {k} that is not in the result")
    return sorted(set(bad))


# ------------------------------------------------------------------ the state of a conversation
class Context:
    def __init__(self, cid):
        self.id = cid
        self.lock = threading.RLock()
        self.created = self.last = time.time()
        self.phase = "start"           # start | interview | confirming | active
        self.draft = None
        self.draft_name = None
        self.circuits = {}
        self.current = None
        self.history = []
        self.saved_question = None
        self.events_asked = False
        self.doubts_asked = set()
        self.example_shown = False
        self.last_question = None
        self.interview_round = 0
        self.assumptions = []
        self.stats = {"turns": 0, "interview_rounds_total": 0, "llm_interpret": 0, "llm_phrase": 0, "llm_ms": 0.0,
                      "llm_wordings_ok": 0, "wordings_rejected": 0, "deterministic_wordings": 0,
                      "validator_rejections": [], "llm_failures": 0, "name_corrections": 0, "shortcuts_without_llm": 0}
        self.session_no = 0


class Dialogue:
    def __init__(self, agent, translator):
        self.agent = agent
        self.translator = translator
        self.contexts = {}
        self.guard = threading.Lock()
        self.counter = 0

    # ---------------------------------------------------------------- contexts
    def context(self, cid):
        with self.guard:
            now = time.time()
            for k in [k for k, c in self.contexts.items() if now - c.last > 7200]:
                del self.contexts[k]
            if cid not in self.contexts:
                if len(self.contexts) >= MAX_CONTEXTS:
                    raise Rejection("too_many_sessions", f"there are {len(self.contexts)} live conversations (cap {MAX_CONTEXTS})")
                self.counter += 1
                c = self.contexts[cid] = Context(cid)
                c.session_no = self.counter
            c = self.contexts[cid]
            c.last = now
            return c

    def new_id(self):
        return "conv-" + uuid.uuid4().hex[:10]

    # ---------------------------------------------------------------- entry
    def turn(self, cid, text):
        """-> dict {state: completed|input-required|failed, text, data}"""
        c = self.context(cid)
        with c.lock:
            c.stats["turns"] += 1
            text = clean_text(text)
            if len(text) > MAX_TEXT:
                raise Rejection("too_large", f"the message has {len(text)} characters (cap {MAX_TEXT})")
            t0 = time.perf_counter()
            res = self._turn(c, text)
            res.setdefault("data", {})
            res["data"]["conversation"] = {"context": cid, "phase": c.phase, "circuits": list(c.circuits),
                                           "current_circuit": c.current, "interview_rounds": c.stats["interview_rounds_total"],
                                           "turn_ms": round((time.perf_counter() - t0) * 1000)}
            c.history.append(("client", text[:300]))
            c.history.append(("agent", res["text"][:300]))
            c.history = c.history[-12:]
            c.last_question = res["text"] if res["state"] == "input-required" else None
            return res

    def _ask(self, c, text, data=None):
        if c.phase in ("start", "interview", "confirming"):
            c.interview_round += 1
            c.stats["interview_rounds_total"] += 1
        return {"state": "input-required", "text": text, "data": data or {}}

    def _done(self, text, data=None):
        return {"state": "completed", "text": text, "data": data or {}}

    # ---------------------------------------------------------------- the turn
    def _turn(self, c, text):
        if not text:
            c.phase = c.phase if c.phase != "start" else "interview"
            return self._ask(c, "Nothing reached me. " + self._help(c))
        tn = strip_accents(text.lower()).strip(" ?!.,;:")
        # shortcuts that need no LLM: the answer to the confirmation
        if c.phase == "confirming":
            if RE_YES.match(tn):
                c.stats["shortcuts_without_llm"] += 1
                return self._confirm(c)
            if RE_NO.match(tn):
                c.stats["shortcuts_without_llm"] += 1
                return self._ask(c, "All right, what needs to change or what is missing? Tell me in your own words and I will repeat what I understand.")
        if c.phase == "interview" and c.draft and tn in ("nothing else", "nothing more", "no more events", "none", "no, nothing else", "nothing", "nothing more than that"):
            c.stats["shortcuts_without_llm"] += 1
            c.draft["no_events"] = True
            return self._analyze(c)
        if tn in ("help", "hello", "hi", "hey", "good morning", "good afternoon", "what can you do", "how do i describe a circuit"):
            c.stats["shortcuts_without_llm"] += 1
            if c.phase == "start":
                c.phase = "interview"
            return self._ask(c, self._help(c)) if c.phase != "active" else self._done(self._help(c))
        try:
            it = self._interpret(c, text)
        except TranslatorError as e:
            c.stats["llm_failures"] += 1
            if c.phase == "start":
                c.phase = "interview"
            return self._ask(c, f"I could not interpret your message (the translator failed: {e}). Could you say it another way, shorter or in parts?") \
                if c.phase != "active" else self._done(f"I could not interpret your message (the translator failed: {e}). Could you say it another way?")
        return self._dispatch(c, it, text)

    def _help(self, c):
        c.example_shown = True
        if c.circuits:
            return CAPABILITIES + "\n" + EXAMPLE
        return "I am a logic agent for electrical circuits. " + CAPABILITIES + "\n" + EXAMPLE

    def _llm_context(self, c):
        circuits = {}
        for n, a in c.circuits.items():
            circuits[n] = {"parts": {p: a.ut.get(p, a.model["type"][p]) for p in a.model["order"]}, "events": a.event_list()}
        return {"phase": c.phase, "current_draft": c.draft, "circuits": circuits, "current_circuit": c.current or c.draft_name,
                "last_question_asked_by_the_agent": (c.last_question or "")[:600],
                "recent_client_messages": [t[:500] for r, t in c.history if r == "client"][-2:]}

    def _interpret(self, c, text):
        t0 = time.perf_counter()
        try:
            raw = self.translator.interpret(text, self._llm_context(c), ctx_id=c.id)
        finally:
            c.stats["llm_interpret"] += 1
            c.stats["llm_ms"] += (time.perf_counter() - t0) * 1000
        notes = []
        it = sanitize_interpretation(raw, notes)
        c.stats["name_corrections"] += sum(1 for n in notes if n.startswith("name normalized"))
        if notes:
            self.agent.log_extra({"type": "translation_sanitized", "context": c.id, "notes": notes[:8]})
        b = it["draft"] or {}
        self.agent.log_extra({"type": "interpretation", "context": c.id, "phase": c.phase, "intent": it["intent"], "parts": len(b.get("parts", [])),
                              "wires": len(b.get("wires", [])), "events": len(b.get("events", [])), "new_events": len(it["new_events"]),
                              "question": (it["question"] or {}).get("type"), "doubts": it["doubts"][:3]})
        return it

    # ---------------------------------------------------------------- dispatch by intent
    def _dispatch(self, c, it, text):
        i = it["intent"]
        if i == "describe" and it["draft"] is not None and (it["draft"]["parts"] or it["draft"]["wires"] or it["draft"]["events"]):
            c.phase = "interview"
            c.draft = it["draft"]
            if (not c.draft_name or c.draft_name == "circuit") and it["circuit"]:
                c.draft_name = circuit_name(it["circuit"])
            elif not c.draft_name:
                c.draft_name = "circuit"
            if it["question"]:
                c.saved_question = it["question"]
            return self._analyze(c, it["doubts"])
        if c.circuits and c.phase == "active" and it["new_events"] and i in ("extend", "describe", "ask"):
            # what the translator extracted is not thrown away: new events get recorded (and if it also asked something, it is answered afterwards)
            return self._extend(c, it)
        if i == "extend":
            if c.circuits and it["new_events"]:
                return self._extend(c, it)
            if c.circuits and it["doubts"]:        # it wanted to extend but it is not clear with what: ask (ambiguity)
                return self._done(" ".join(it["doubts"][:2]) + " Tell me and I will record it.")
        if i == "ask" and it["question"]:
            pr = it["question"]
            if c.circuits and it["doubts"] and pr["type"] in NEED_LAMP and not pr.get("part") and not pr.get("parts"):
                return self._done(" ".join(it["doubts"][:2]))
            if c.circuits:
                if c.phase in ("interview", "confirming") and c.draft:
                    return self._ask(c, "Before answering I need to close the circuit we are describing. " + self._continue_interview(c))
                return self._answer(c, it["question"], text)
            if c.draft:
                c.saved_question = it["question"]
                return self._ask(c, "I will answer that as soon as we have closed the circuit. " + self._continue_interview(c))
            c.phase = "interview"
            c.saved_question = it["question"]
            return self._ask(c, "You have not described any circuit yet, so I have nothing to answer about. " + self._help(c))
        if i == "confirm":
            if c.phase == "confirming":
                if it["question"]:
                    c.saved_question = it["question"]
                return self._confirm(c)
            return self._ask(c, "I have nothing pending confirmation. " + (self._continue_interview(c) if c.draft else "Describe the circuit to me.")) \
                if c.phase != "active" else self._done("I have nothing pending confirmation; you can ask me about the circuit or describe another one.")
        if i == "deny":
            if c.phase == "confirming":
                return self._ask(c, "All right, what needs to change or what is missing?")
            return self._ask(c, "All right. " + (self._continue_interview(c) if c.draft else "")) if c.phase != "active" else self._done("All right.")
        if i == "help":
            if c.phase == "start":
                c.phase = "interview"
            r = self._help(c)
            return self._ask(c, r) if c.phase != "active" else self._done(r)
        # other (or an intent with no usable content)
        if c.phase == "start":
            c.phase = "interview"
        msg = "I did not understand that as part of the description of a circuit or as a question about one. " + CAPABILITIES
        if c.phase == "active":
            return self._done(msg + " You can ask me about the circuit or describe another one.")
        return self._ask(c, msg + ("\n" + EXAMPLE if not c.example_shown else "\n" + (self._continue_interview(c) if c.draft else "Describe the circuit to me.")))

    def _continue_interview(self, c):
        if c.phase == "confirming":
            return self._confirmation_text(c)
        r = self._analyze(c)
        return r["text"]

    # ---------------------------------------------------------------- interview: gaps, ambiguities, confirmation
    def _gaps(self, c, model, events, actions):
        """-> (hard, soft): what is missing or does not fit (deterministic)"""
        b = c.draft
        ptype, ut = model["type"], model["user_type"]
        hard, soft = [], []
        if not any(t == "source" for t in ptype.values()):
            hard.append("Where does the current come from? I see no power source (the mains, a battery...). Give me its name.")
        if not any(t == "lamp" for t in ptype.values()):
            hard.append("You have not described any lamp. Which lamps are there (lamps, bulbs...) and which wire do they get current from?")
        if model["missing_position"]:
            q = model["missing_position"]
            est = "plugged or unplugged" if all(ut.get(x) == "plug" for x in q) else "up or down"
            hard.append(f"What position is {join_list(q, 'and')} in at the start ({est})?")
        dest = {d for d, o, cnd in model["wires"]}
        orig = {o for d, o, cnd in model["wires"]}
        conds = {cnd[1] for d, o, cnd in model["wires"] if cnd}
        for n in model["order"]:
            t = ptype[n]
            if t == "source":
                continue
            if t == "lamp" and n not in dest:
                hard.append(f"{n} gets current from nothing. Which wire or part feeds it?")
            elif t in ("wire", "socket") and n not in dest and n not in orig:
                hard.append(f"{n} does not appear to be connected to anything. What does it get current from, or what does it feed?")
            elif t in ("wire", "socket") and n not in dest:
                hard.append(f"{n} feeds other parts but I do not know where it gets current from. Where does it come from?")
            elif t in ("switch", "fuse") and n not in conds and n not in dest:
                what = "What does it cut or open?" if t == "switch" else "What does it protect?"
                hard.append(f"{n} does not control any wire. {what}")
        loose = [q for q in hard if "does not appear to be connected" in q or "does not control any wire" in q or "gets current from nothing" in q]
        if not model["wires"] and len(loose) > 1:
            # the client has not said yet how anything is connected: a single question instead of one per part
            hard = [q for q in hard if q not in loose]
            names = [n for n in model["order"] if ptype[n] != "source"]
            hard.append("I am missing the connections: what gets current from what? Tell me, starting from the power source, where each of these parts gets its current "
                        "from: " + join_list(names) + "; and which switch, two-way switch, plug or fuse cuts each stretch (and in which position it closes).")
        # path to the source (no conditions): each lamp
        incoming = {}
        for d, o, cnd in model["wires"]:
            incoming.setdefault(d, []).append(o)
        for n in model["order"]:
            if ptype[n] == "lamp" and n in dest:
                seen, stack, found = set(), [n], False
                while stack:
                    x = stack.pop()
                    if ptype[x] == "source":
                        found = True
                        break
                    for o in incoming.get(x, []):
                        if o not in seen:
                            seen.add(o)
                            stack.append(o)
                if not found:
                    hard.append(f"I cannot find any path of wires from the power source to {n}. Which wire is missing?")
        # events that do nothing (deterministic ambiguity)
        pos = dict(model["pos"])
        good = {n: True for n, t in ptype.items() if t in ("lamp", "fuse")}
        for k, ((a, pz), au) in enumerate(zip(events, actions), 1):
            if a == "raise" and pos.get(pz) == "up" or a == "lower" and pos.get(pz) == "down":
                est = state_text(pos[pz], pz, ut)
                soft.append(f"Event {k} ({ACTION_TEXT[au].format(pz)}) changes nothing: {pz} was already {est} at that point. Did you mean the opposite, or had you already changed it before?")
            elif a == "blow" and not good.get(pz, True):
                soft.append(f"Event {k} ({ACTION_TEXT[au].format(pz)}) changes nothing: {pz} was already blown. Did you mean another part?")
            elif a == "repair" and good.get(pz, True):
                soft.append(f"Event {k} ({ACTION_TEXT[au].format(pz)}) changes nothing: {pz} was already in good condition. Did you mean another part?")
            if a in ("raise", "lower"):
                pos[pz] = "up" if a == "raise" else "down"
            elif a in ("blow", "repair"):
                good[pz] = a == "repair"
        if not events and not b.get("no_events") and not c.events_asked:
            soft.append("Does anything happen after the assembly (raising or lowering switches, blowing or repairing a lamp or a fuse, plugging or unplugging)? "
                        "Tell me in order, or say \"nothing else\".")
        return hard, soft

    def _analyze(self, c, llm_doubts=()):
        b = c.draft
        if not b or not b["parts"]:
            c.phase = "interview"
            c.example_shown = True
            return self._ask(c, "I do not have any part yet. " + EXAMPLE_SHORT)
        try:
            model, events, actions = compile_draft(b)
        except Rejection as r:
            c.stats["validator_rejections"].append({"code": r.code, "detail": r.detail[:120], "path": r.path})
            self.agent.log_extra({"type": "validator_caught", "context": c.id, "code": r.code, "detail": r.detail[:160], "path": r.path})
            c.phase = "interview"
            return self._ask(c, question_from_rejection(r), {"validator_rejection": r.to_json()})
        c.phase = "interview"
        hard, soft = self._gaps(c, model, events, actions)
        # translator doubts: only those not asked already
        fresh = []
        for d in llm_doubts:
            k = strip_accents(d.lower())[:80]
            if k not in c.doubts_asked:
                fresh.append(d)
        questions = list(hard)[:4]
        if not hard:
            fresh = [q for q in fresh if not similar(q, soft)]      # the translator sometimes repeats the question about events
            already = [q for q in soft if strip_accents(q.lower())[:80] in c.doubts_asked]
            soft = [q for q in soft if q not in already]
            # what was already asked and the client left as it was: taken as accepted, but kept in view in the confirmation
            c.assumptions = [("I still see that " + q[0].lower() + q[1:]) for q in
                             [re.sub(r"\s*[^.?!]*\?\s*$", "", q).strip() for q in already]][:3]
            questions += soft + fresh
            if c.interview_round >= 5 and questions:      # do not get stuck: confirm with the assumptions in view
                c.assumptions = c.assumptions + questions[:4]
                questions = []
        if questions:
            for q in questions:
                c.doubts_asked.add(strip_accents(q.lower())[:80])
            for q in fresh:
                c.doubts_asked.add(strip_accents(q.lower())[:80])
            if any("after the assembly" in q for q in questions):
                c.events_asked = True
            head = ""
            if not c.example_shown and (not model["wires"] or len(hard) >= 3):     # poor description: teach with a short example
                head = EXAMPLE_SHORT + "\n\n"
                c.example_shown = True
            if len(questions) == 1:
                txt = head + "To understand it properly: " + questions[0]
            else:
                txt = head + "To understand it properly I need to clear up these things:\n" + "\n".join(f"{k}. {q}" for k, q in enumerate(questions, 1))
            return self._ask(c, txt, {"missing": questions})
        c.phase = "confirming"
        c.example_shown = True
        return self._ask(c, self._confirmation_text(c, model, events, actions))

    def _confirmation_text(self, c, model=None, events=None, actions=None):
        if model is None:
            model, events, actions = compile_draft(c.draft)
        name = c.draft_name
        notice = ""
        if name in c.circuits:
            name = self._free_name(c, name)
            notice = f"\n(There is already a circuit \"{c.draft_name}\" in this conversation; I will call this one \"{name}\".)"
        return summary_text(name, model, events, actions, c.assumptions) + notice

    def _free_name(self, c, name):
        k = 2
        while f"{name}_{k}" in c.circuits:
            k += 1
        return f"{name}_{k}"

    # ---------------------------------------------------------------- confirm: it gets loaded into the core
    def _confirm(self, c):
        try:
            model, events, actions = compile_draft(c.draft)
        except Rejection as r:
            c.phase = "interview"
            return self._ask(c, question_from_rejection(r))
        name = c.draft_name
        if name in c.circuits:
            name = self._free_name(c, name)
        sname = f"c{c.session_no}_{name}"[:32]
        store = self.agent.store

        def assemble():
            s = Session(sname, model, events, actions)
            store.put(s, True)
            lit = s.lit(s.n)
            return s.event_map(), lit, s.n, s.assembled, s.lit(s.assembled)
        mp, lit, n, setup, lit0 = run_in_core(assemble)
        a = Circuit(name, sname, model, events, actions, c.draft)
        a.setup, a.n = setup, n
        a.versions = [{"version": 1, "last": n}]
        c.circuits[name] = a
        c.current = name
        saved = c.saved_question
        c.draft, c.draft_name, c.saved_question, c.assumptions = None, None, None, []
        c.events_asked, c.doubts_asked, c.interview_round = False, set(), 0
        c.phase = "active"
        lamps = a.lamps()
        txt = (f"Done: I have loaded the circuit \"{name}\" ({len(model['order'])} parts, {len(model['wires'])} wires, {len(events)} events). "
               f"Initial state: " + (are_lit(lit0["lit"]) if lit0["lit"] else "no lamp is lit") + "."
               + (f" After the last event (event {len(events)}): " + (are_lit(lit["lit"]) if lit["lit"] else "no lamp is lit") + "." if events else "")
               + (f"\nNumbered events: " + "; ".join(a.event_list()) + "." if events else "")
               + f"\nYou can ask me, for example: which lamps are lit at a given moment, why {lamps[0]} is lit or NOT lit, why it changed, "
               "or the timeline.")
        data = {"circuit": name, "map": mp, "lamps": lamps, "lit_final": lit["lit"], "version": 1}
        if saved:
            r = self._answer(c, saved, "", forced_circuit=name)
            return {"state": r["state"], "text": txt + "\n\nAnd to your question:\n" + r["text"], "data": {**data, **r["data"]}}
        return self._done(txt, data)

    # ---------------------------------------------------------------- extend: new events
    def _extend(self, c, it):
        name = it["circuit"] if it["circuit"] in c.circuits else c.current
        if it["question"] and it["question"].get("circuit") in c.circuits:
            name = it["question"]["circuit"]
        a = c.circuits[name]
        try:
            ev_b, user_acts = expand_events(it["new_events"], a.ut)
            new = validate_events(ev_b, a.model["type"])
        except Rejection as r:
            c.stats["validator_rejections"].append({"code": r.code, "detail": r.detail[:120], "path": r.path})
            self.agent.log_extra({"type": "validator_caught", "context": c.id, "code": r.code, "detail": r.detail[:160], "path": r.path})
            return self._done(question_from_rejection(r), {"validator_rejection": r.to_json()})
        # the deterministic ambiguity also holds when extending: do they do anything?
        pos = dict(a.model["pos"])
        good = {n: True for n, t in a.model["type"].items() if t in ("lamp", "fuse")}
        for (x, pz) in a.events:
            if x in ("raise", "lower"):
                pos[pz] = "up" if x == "raise" else "down"
            elif x in ("blow", "repair"):
                good[pz] = x == "repair"
        warnings = []
        for k, ((x, pz), au) in enumerate(zip(new, user_acts), len(a.events) + 1):
            if x == "raise" and pos.get(pz) == "up" or x == "lower" and pos.get(pz) == "down":
                warnings.append(f"event {k} ({ACTION_TEXT[au].format(pz)}) changes nothing: {pz} was already {state_text(pos[pz], pz, a.ut)}")
            elif x == "blow" and not good.get(pz, True):
                warnings.append(f"event {k} ({ACTION_TEXT[au].format(pz)}) changes nothing: {pz} was already blown")
            elif x == "repair" and good.get(pz, True):
                warnings.append(f"event {k} ({ACTION_TEXT[au].format(pz)}) changes nothing: {pz} was already in good condition")
            if x in ("raise", "lower"):
                pos[pz] = "up" if x == "raise" else "down"
            elif x in ("blow", "repair"):
                good[pz] = x == "repair"

        def apply():
            s = self._session(a)
            before = s.add_events(new, user_acts)
            return before, s.n, s.lit(s.n), s.event_map()
        before, now, lit, mp = run_in_core(apply)
        a.events += new
        a.actions += user_acts
        a.n = now
        v = len(a.versions) + 1
        a.versions.append({"version": v, "last": now})
        k0 = len(a.events) - len(new)
        txt = (f"Recorded in \"{name}\": " + "; ".join(f"event {k0 + j}: {ACTION_TEXT[au].format(pz)}" for j, (au, (_, pz)) in enumerate(zip(user_acts, new), 1)) + ".\n"
               f"ATTENTION (version {v}): the last moment changes. It used to be after event {k0} (step {before}); now it is after event {len(a.events)} (step {now}). "
               f"Questions without a moment now refer to the new end; the earlier moments still hold as before.\n"
               f"After the new end: " + (are_lit(lit["lit"]) if lit["lit"] else "no lamp is lit") + ".")
        if warnings:
            txt += "\nWarning: " + "; ".join(warnings) + ". If that was not what you meant, tell me and I will review it."
        data = {"circuit": name, "version": v, "last_step_before": before, "last_step_now": now, "map": mp, "lit_final": lit["lit"]}
        if it["question"]:
            r = self._answer(c, it["question"], "", forced_circuit=name)
            return {"state": r["state"], "text": txt + "\n\nAnd to your question:\n" + r["text"], "data": {**data, **r["data"]}}
        return self._done(txt, data)

    def _session(self, a):
        """the live Session of the circuit; if it expired (1 h), it is rebuilt from what the client described"""
        try:
            s = self.agent.store.get(a.session)
            if s.n == a.n:
                return s
        except Rejection:
            pass
        s = Session(a.session, a.model, a.events, a.actions)
        self.agent.store.put(s, True)
        return s

    # ---------------------------------------------------------------- answering a question
    def _pick_circuit(self, c, q, forced=None):
        if forced:
            return c.circuits[forced], None
        nm = q.get("circuit")
        if nm in c.circuits:
            return c.circuits[nm], None
        if q.get("part"):
            cand = [a for a in c.circuits.values() if q["part"] in a.model["type"]]
            if len(cand) == 1:
                return cand[0], None
            if len(cand) > 1:
                if c.current in [a.name for a in cand]:
                    return c.circuits[c.current], None
                return None, f"The part {q['part']} exists in several circuits ({', '.join(a.name for a in cand)}). Which one are you asking about?"
        if len(c.circuits) == 1 or c.current in c.circuits:
            return c.circuits[c.current or next(iter(c.circuits))], None
        return None, f"I have several circuits ({', '.join(c.circuits)}). Which one are you asking about?"

    def _moment(self, a, ref, default, what):
        if ref is None:
            return default, None
        if ref == "initial":
            return a.setup, None
        if ref == "final":
            return a.n, None
        k = ref["event"]
        if k > len(a.events):
            return None, f"You say event {k}, but there are only {len(a.events)} events in \"{a.name}\". Which one do you mean?"
        return a.setup + k, None

    def _answer(self, c, q, client_text, forced_circuit=None):
        a, doubt = self._pick_circuit(c, q, forced_circuit)
        if doubt:
            return self._done(doubt)
        if q["type"] in NEED_LAMP and len(q.get("parts") or []) > 1:
            # "why is none lit / both": one answer per lamp, each with its own core and its own check
            parts = []
            for pz in q["parts"][:6]:
                r = self._answer_one(c, a, {**q, "part": pz, "parts": []}, client_text)
                parts.append(r)
            return {"state": "completed", "text": "\n\n".join(r["text"] for r in parts),
                    "data": {**parts[0]["data"], "answers": [r["data"] for r in parts]}}
        if q["type"] in NEED_LAMP and not q.get("part") and len(q.get("parts") or []) == 1:
            q = {**q, "part": q["parts"][0]}
        return self._answer_one(c, a, q, client_text)

    def _answer_one(self, c, a, q, client_text):
        qtype = q["type"]
        d = {"question": qtype}
        assumptions = []
        if qtype in NEED_LAMP:
            lamps = a.lamps()
            pz = q.get("part")
            if not pz:
                if len(lamps) == 1:
                    pz = lamps[0]
                    assumptions.append(f"I take it you are asking about {pz}, the only lamp in \"{a.name}\".")
                else:
                    return self._done(f"Which lamp are you asking about? In \"{a.name}\" there are: {join_list(lamps)}.")
            elif pz not in a.model["type"]:
                close = difflib.get_close_matches(pz, lamps, n=2, cutoff=0.5)
                if close:
                    return self._done(f"I have no part called {pz} in \"{a.name}\". Do you mean {join_list(close, 'or')}? The lamps are: {join_list(lamps)}.")
                return self._done(f"I have no part called {pz} in \"{a.name}\". The lamps are: {join_list(lamps)}.")
            elif a.model["type"][pz] != "lamp":
                return self._done(f"{pz} is not a lamp, it is a {a.ut.get(pz, a.model['type'][pz]).replace('_', '-')}; I explain why a lamp is lit or not. The lamps are: {join_list(lamps)}.")
            d["part"] = pz
        if qtype in ("lit", "supporting", "how", "why_not"):
            n, e = self._moment(a, q.get("step"), a.n, "step")
            if e:
                return self._done(e)
            if q.get("step") is None and a.n != a.setup:
                assumptions.append(f"You did not say at which moment: I take the end ({a.moment_label(n)}).")
            d["step"] = n
        elif qtype == "why":
            n1, e1 = self._moment(a, q.get("from_step"), a.setup, "from_step")
            n2, e2 = self._moment(a, q.get("to_step"), a.n, "to_step")
            if e1 or e2:
                return self._done(e1 or e2)
            if n1 >= n2:
                return self._done(f"The start ({a.point_label(n1)}) has to be before the end ({a.point_label(n2)}). Between which events do you want to look?")
            if q.get("from_step") is None and q.get("to_step") is None:
                assumptions.append("You did not say between which events: I look from the initial state to the end and explain each change.")
            d["from_step"], d["to_step"] = n1, n2
        else:  # timeline
            if q.get("part"):
                if q["part"] not in a.model["type"] or a.model["type"][q["part"]] != "lamp":
                    return self._done(f"{q['part']} is not a lamp of \"{a.name}\". The lamps are: {join_list(a.lamps())}.")
                d["part"] = q["part"]
        agent = self.agent

        def run():
            s = self._session(a)
            name = agent._question_name(d)
            data, base = agent._answer(name, s, d)
            return name, data, base
        try:
            name, data, base = run_in_core(run)
        except Rejection as r:
            c.stats["validator_rejections"].append({"code": r.code, "detail": r.detail[:120], "path": r.path})
            self.agent.log_extra({"type": "validator_caught", "context": c.id, "code": r.code, "detail": r.detail[:160], "path": r.path})
            return self._done(f"I could not answer: {r.detail}. Could you make the question more precise?", {"validator_rejection": r.to_json()})
        wording, via = self._phrase(c, a, name, d, data, base, client_text)
        understood = self._understood(a, name, d)
        foot = "\n".join(assumptions)
        txt = wording + ("\n\n" + foot if foot else "") + "\n\n(Question understood: " + understood + ".)"
        return self._done(txt, {"understood_question": {"type": name, **d, "circuit": a.name, "version": len(a.versions)},
                                "core_result": clean_result(data), "wording": via, "assumptions": assumptions})

    def _understood(self, a, name, d):
        if name in ("lit", "supporting", "how", "why_not"):
            q = {"lit": "which lamps are lit", "supporting": f"what keeps {d.get('part')} lit", "how": f"how we know that {d.get('part')} is lit",
                 "why_not": f"why {d.get('part')} is NOT lit"}[name]
            return f"{q} {a.moment_label(d['step'])}, in \"{a.name}\""
        if name == "why":
            return f"why {d['part']} changed between {a.point_label(d['from_step'])} and {a.point_label(d['to_step'])} in \"{a.name}\""
        return f"timeline of \"{a.name}\""

    # ---------------------------------------------------------------- wording: LLM with a check, or deterministic
    def _phrase(self, c, a, name, d, data, base, client_text):
        det = phrase_det(a, name, d, data)
        mat = phrasing_material(a, name, d, data, base, client_text, det)
        hint = None
        for attempt in (1, 2):
            t0 = time.perf_counter()
            try:
                txt = self.translator.phrase(mat, ctx_id=c.id, hint=hint)
            except TranslatorError:
                c.stats["llm_failures"] += 1
                txt = None
                hint = None
            finally:
                if self.translator.name != "simulated":
                    c.stats["llm_phrase"] += 1
                    c.stats["llm_ms"] += (time.perf_counter() - t0) * 1000
            if txt is None:
                break
            extra = _tokens(json.dumps(mat["event_labels"], ensure_ascii=False)) | _tokens(json.dumps(mat["moment_or_range"], ensure_ascii=False)) | {d.get("part") or ""}
            bad = check_wording(txt, a, data, extra_names=extra, extra_ints={a.setup, a.n})
            if not bad:
                c.stats["llm_wordings_ok"] += 1
                return txt, "llm" + ("" if attempt == 1 else " (2nd attempt)")
            c.stats["wordings_rejected"] += 1
            self.agent.log_extra({"type": "wording_rejected", "context": c.id, "attempt": attempt, "violations": bad[:5], "question": name})
            hint = "; ".join(bad)[:300] + ". Use only what is in the result."
        c.stats["deterministic_wordings"] += 1
        return det, "deterministic"


# ------------------------------------------------------------------ material for the LLM and deterministic wording
def clean_result(data):
    return {k: v for k, v in data.items() if k not in ("prolog_tree", "prolog")}


def phrasing_material(a, name, d, data, base, client_text, det):
    res = clean_result(data)
    nums = set()
    _integers(res, nums)
    nums |= {d.get(k) for k in ("step", "from_step", "to_step") if d.get(k) is not None}
    labels = {str(n): a.label(n) for n in sorted(nums) if isinstance(n, int) and n > 0}
    res.pop("warning", None)
    res.pop("reading", None)
    return {"client_question": client_text[:500], "question_type": name, "circuit": a.name,
            "moment_or_range": {k: {"event": d[k], "label": a.moment_label(d[k])} for k in ("step", "from_step", "to_step") if d.get(k) is not None},
            "result": res, "event_labels": labels, "core_base_sentence": base[:1500],
            "the_only_lamps_are": a.lamps(), "reference_wording": det}


def _cut_text(a, c):
    h = fact_text(c["would_need"], a.ut)
    lo = c.get("left_by")
    if lo:
        now = fact_text(lo["fact"], a.ut)
        return f"{h} (not now: {now} since {a.label(lo['event'])})"
    return h


def phrase_det(a, name, d, data):
    """Deterministic English from the core's output (the safety net: without the LLM nothing can be invented)."""
    ut = a.ut
    if name == "lit":
        mom = a.moment_label(d["step"])
        e, ap = data["lit"], data["unlit"]
        if not e:
            return f"{cap(mom)}, no lamp is lit."
        return f"{cap(mom)}, {are_lit(e)}" + (f"; {join_list(ap)} {'is' if len(ap) == 1 else 'are'} unlit." if ap else ".")
    if name == "supporting":
        l, mom = d["part"], a.moment_label(d["step"])
        if not data["lit"]:
            return f"{l} is not lit {mom}, so there is nothing supporting it."
        hs = [f"{fact_text(x['fact'], ut)} (set by {a.label(x['event'])})" for x in data["supporting"]]
        return f"{l} is lit {mom} and these facts support it: " + "; ".join(hs) + "."
    if name == "how":
        l, mom = d["part"], a.moment_label(d["step"])
        if not data["lit"]:
            return f"{l} is not lit {mom}, so there is no proof that it is lit."

        def path(node):
            kids = node.get("because", [])
            x = re.match(r"^has_current\((\w+)\)$", node["fact"]).group(1)
            if not kids:
                return [x], []
            cond, nxt = kids[0], kids[1]
            conds = [(h["fact"], h.get("event")) for h in cond.get("because", [])]
            rest, rc = path(nxt)
            return [x] + rest, conds + rc
        root = [h for h in data["tree"]["because"] if h["fact"].startswith("has_current")][0]
        via, conds = path(root)
        txt = f"{l} is lit {mom} because it is a lamp, it is in good condition and current reaches it along the path {' <- '.join(via)}."
        if conds:
            txt += " Each stretch works because " + join_list([f"{fact_text(h, ut)} (set by {a.label(e)})" for h, e in conds]) + "."
        else:
            txt += " That path has no conditions: it is always closed."
        return txt
    if name == "why":
        l = d["part"]
        sp = data["segments"]
        if not sp:
            return (f"{l} does not change between {a.point_label(d['from_step'])} and {a.point_label(d['to_step'])}: it stays "
                    f"{'lit' if data['now_lit'] else 'unlit'}, and it did not change at any moment in between either.")
        out = []
        for t in sp:
            what = "came on" if t["change"] == "turned_on" else "went out"
            ev = [f"{a.label(x['event'])} left {fact_text(x['fact'], ut, 'left')}" for x in t["events"]]
            out.append(f"{l} {what} {a.moment_label(t['to_step'])} because " + (join_list(ev) if ev else "something changed") + ".")
        head = ""
        if data["change"] == "no_change":
            head = (f"Between {a.point_label(d['from_step'])} and {a.point_label(d['to_step'])} {l} ends up as it started "
                    f"({'lit' if data['now_lit'] else 'unlit'}), but along the way it changed {len(sp)} times. ")
        elif len(sp) > 1:
            head = f"{l} changed {len(sp)} times between {a.point_label(d['from_step'])} and {a.point_label(d['to_step'])}. "
        return head + " ".join(out)
    if name == "why_not":
        l, mom = d["part"], a.moment_label(d["step"])
        if data["lit"]:
            return f"{l} is lit {mom}; there is nothing to explain."
        out = [f"{l} is not lit {mom}."]
        for f in data["failures"]:
            if "paths" not in f:
                lo = f["cuts"][0]["left_by"]
                out.append(f"- {l} is blown" + (f" (since {a.label(lo['event'])})." if lo else "."))
                continue
            rs = f["paths"]
            if not rs:
                out.append(f"- There is no wire carrying current from the source to {l}.")
            elif len(rs) == 1:
                out.append(f"- No current reaches it. There is only one path ({' <- '.join(rs[0]['via'])}); for current to arrive, all of these would have to hold at once: "
                           + join_list([_cut_text(a, c) for c in rs[0]["cuts"]]) + ".")
            else:
                out.append(f"- No current reaches it through any of the {len(rs)} possible paths; fixing ANY ONE of them (or) would be enough:")
                for i, r in enumerate(rs, 1):
                    out.append(f"    * path {i} ({' <- '.join(r['via'])}): all of these have to hold at once (and): " + join_list([_cut_text(a, c) for c in r["cuts"]]) + ".")
        if len(data["failures"]) > 1:
            out.append("There is more than one cause at the same time: for it to be lit, ALL of them have to be fixed.")
        return "\n".join(out)
    # timeline
    out = []
    for f in data["steps"]:
        mom = cap(a.moment_label(f["step"]))
        s = f"{mom}: " + (are_lit(f["lit"]) if f["lit"] else "no lamp is lit")
        ch = []
        if f.get("turn_on"):
            ch.append(f"{join_list(f['turn_on'])} come(s) on")
        if f.get("turn_off"):
            ch.append(f"{join_list(f['turn_off'])} go(es) out")
        out.append(s + ("; " + " and ".join(ch) if ch else "") + ".")
    return "\n".join(out)
