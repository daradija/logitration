"""The electrical A2A agent: card, skills, task cycle and log. No HTTP (that is server.py)."""
import collections
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

from circuit_a2a import core
from circuit_a2a.core import LIMITS, Rejection, Session, Store, Timeout, indented, validate_definition, validate_events, _check_keys
from circuit_a2a.composites import expand_definition, expand_events

HERE = Path(__file__).resolve().parent
VERSION = "0.2"
STATES = {"submitted": "submitted", "working": "working", "completed": "completed",
          "input-required": "needs input", "rejected": "rejected", "failed": "failed", "canceled": "canceled"}

QUESTIONS = {  # name -> (accepted fields, description)
    "lit": ({"step"}, "which lamps are lit after event `step` (default: the last one)"),
    "supporting": ({"part", "step"}, "which events support the lamp `part` being lit"),
    "how": ({"part", "step"}, "the proof (tree) that the lamp `part` is lit"),
    "why": ({"part", "from_step", "to_step"}, "why the lamp `part` changed between events `from_step` and `to_step`"),
    "why_not": ({"part", "step"}, "why the lamp `part` is NOT lit (ALL the causes: «and» failures, «or» routes)"),
    "timeline": ({"from_step", "to_step", "part"}, "which lamps are lit at each step between `from_step` and `to_step` (default: from setup to the last one); `part` optional"),
}
SKILLS = {
    "define_circuit": {"keys": {"skill", "circuit", "parts", "wires", "events", "positions", "replace", "action"}},
    "ask": {"keys": {"skill", "circuit", "question"} | set().union(*(v[0] for v in QUESTIONS.values()))},
    "explain": {"keys": {"skill", "circuit", "question", "format"} | set().union(*(v[0] for v in QUESTIONS.values()))},
    "teach": {"keys": {"skill", "topic"}},
    "converse": {"keys": {"skill", "text"}},
}
SKILL_ALIAS = {}


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def agent_card(url):
    return {
        "name": "circuit",
        "description": "Logic agent for electrical circuits (lamps, switches, two-way switches, plugs, fuses, wires). "
                       "IT SPEAKS ENGLISH: the client describes the circuit in natural language, the agent interviews them (asks for what is missing), repeats in "
                       "words what it understood and, once the client says yes, loads it; afterwards it answers in English which lamps are lit, why they are lit, why they are NOT lit, "
                       "why they changed and gives the timeline. A rule core (Prolog) decides every answer; the language model only translates.",
        "version": VERSION,
        "protocolVersion": "0.3.0",
        "url": url,
        "preferredTransport": "JSONRPC",
        "provider": {"organization": "circuit-a2a (logitration)", "url": url},
        "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": True},
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["application/json", "text/plain"],
        "securitySchemes": {},
        "security": [],
        "limits": LIMITS,
        "notes": "Listens on 127.0.0.1 only. Main skill: `converse` (ONE part kind=text in English; always use the same message.contextId to "
                 "continue the conversation; if the task ends in 'input-required' the agent is asking you something: answer in the same conversation). "
                 "Advanced interface (JSON, part kind=data): define_circuit, ask, explain, teach.",
        "skills": [
            {"id": "converse", "name": "Converse about a circuit (natural language)",
             "description": "Describe your circuit in English (parts, what feeds what, switches and their position, events in order). The agent "
                            "asks for what is missing, repeats in words what it understood and waits for your «yes» or your correction. Then ask in English: "
                            "«why is the lamp not lit?», «which lamps are lit at the end?», «why did l1 go out?», «timeline». You can describe several "
                            "circuits in the same conversation and extend one with new events (the agent warns that the last step changes).",
             "tags": ["natural-language", "interview", "circuit", "why", "why-not"],
             "examples": ["I have a power source. From it wire c1 goes to the lamp l1, but in between there is a switch s1 that closes "
                          "the circuit when it is up; right now it is down. Then I raise s1. Why is l1 not lit at the start?",
                          "Yes, that is correct.", "And which lamps are lit at the end?"],
             "inputModes": ["text/plain"], "outputModes": ["text/plain", "application/json"]},
            {"id": "define_circuit", "name": "Define a circuit",
             "description": "Defines (or extends with events) a circuit: parts, wires with a condition, and events. One session per circuit.",
             "tags": ["circuit", "session"],
             "examples": ['{"skill":"define_circuit","circuit":"minimal","parts":[{"name":"source","type":"source"},'
                          '{"name":"c1","type":"wire"},{"name":"l1","type":"lamp"}],'
                          '"wires":[{"to":"c1","from":"source"},{"to":"l1","from":"c1"}]}'],
             "inputModes": ["application/json"], "outputModes": ["application/json", "text/plain"]},
            {"id": "ask", "name": "Ask",
             "description": "Questions from the catalogue: " + "; ".join(f"{k} ({v[1]})" for k, v in QUESTIONS.items()),
             "tags": ["lit", "supporting", "how", "why", "why_not"],
             "examples": ['{"skill":"ask","circuit":"hallway","question":"why_not","part":"l2"}'],
             "inputModes": ["application/json"], "outputModes": ["application/json", "text/plain"]},
            {"id": "explain", "name": "Explain",
             "description": "Like ask, but returns the readable explanation: indented proof tree and a plain-English sentence. "
                            "format: indented | tree | plain.",
             "tags": ["tree", "proof"],
             "examples": ['{"skill":"explain","circuit":"hallway","question":"how","part":"l1","format":"indented"}'],
             "inputModes": ["application/json"], "outputModes": ["application/json", "text/plain"]},
            {"id": "teach", "name": "Teach (how you get taught)",
             "description": "For the agent that arrives without knowing the format: message templates, minimal examples with their expected "
                            "answer, question catalogue, error codes and limits. Topics: format, examples, questions, errors, limits.",
             "tags": ["format", "examples"],
             "examples": ['{"skill":"teach","topic":"format"}'],
             "inputModes": ["application/json"], "outputModes": ["application/json", "text/plain"]},
        ],
    }


# ------------------------------------------------------------------ teach
EXAMPLES = [
    {"name": "minimal", "idea": "a source, a wire and a lamp: the lamp is lit",
     "steps": [
         {"message": {"skill": "define_circuit", "circuit": "minimal",
                      "parts": [{"name": "source", "type": "source"}, {"name": "c1", "type": "wire"}, {"name": "l1", "type": "lamp"}],
                      "wires": [{"to": "c1", "from": "source"}, {"to": "l1", "from": "c1"}]},
          "expected": {"step": 6, "lit": ["l1"]}},
         {"message": {"skill": "ask", "circuit": "minimal", "question": "lit"},
          "expected": {"lit": ["l1"], "unlit": []}}]},
    {"name": "with_switch", "idea": "a switch that is down cuts the current; you learn why and test it by raising it",
     "steps": [
         {"message": {"skill": "define_circuit", "circuit": "with_switch",
                      "parts": [{"name": "source", "type": "source"}, {"name": "s1", "type": "switch", "position": "down"},
                                {"name": "c1", "type": "wire"}, {"name": "l1", "type": "lamp"}],
                      "wires": [{"to": "c1", "from": "source"}, {"to": "l1", "from": "c1", "when": "up(s1)"}]},
          "expected": {"lit": []}},
         {"message": {"skill": "ask", "circuit": "with_switch", "question": "why_not", "part": "l1"},
          "expected": {"fails": "has_current(l1)", "missing": [{"would_need": "up(s1)", "left_by": {"event": 3, "fact": "down(s1)"}}]}},
         {"message": {"skill": "define_circuit", "circuit": "with_switch", "events": [{"action": "raise", "part": "s1"}]},
          "expected": {"lit": ["l1"]}},
         {"message": {"skill": "ask", "circuit": "with_switch", "question": "why", "part": "l1", "from_step": 8, "to_step": 9},
          "expected": {"change": "turned_on", "events": [{"event": 9, "fact": "up(s1)"}]}}]},
    {"name": "with_fuse", "idea": "a blown fuse is explained by the event that blew it",
     "steps": [
         {"message": {"skill": "define_circuit", "circuit": "with_fuse",
                      "parts": [{"name": "source", "type": "source"}, {"name": "f1", "type": "fuse"},
                                {"name": "c1", "type": "wire"}, {"name": "l1", "type": "lamp"}],
                      "wires": [{"to": "c1", "from": "source"}, {"to": "l1", "from": "c1", "when": "ok(f1)"}],
                      "events": [{"action": "blow", "part": "f1"}]},
          "expected": {"lit": []}},
         {"message": {"skill": "ask", "circuit": "with_fuse", "question": "why_not", "part": "l1"},
          "expected": {"fails": "has_current(l1)", "missing": [{"would_need": "ok(f1)", "left_by": {"event": 9, "fact": "blown(f1)"}}]}}]},
]

FORMAT = {
    "idea": "Each request is ONE JSON message in a part kind=data. 'skill' chooses the skill. Part names: lowercase first letter, "
            "letters/digits/_, at most 24. Types: source, wire, socket, fuse, lamp, switch. Current flows from `from` to "
            "`to` if `when` holds (up(x), down(x), ok(x)). Everything that is assembled or happens gets an event "
            "number, in this order: parts (a lamp or fuse takes 2 events: part + ok; a switch, 2: part + position), "
            "then wires (1 each), then events. That number is the 'step' used by the questions.",
    "templates": {
        "define_circuit": {"skill": "define_circuit", "circuit": "<name>",
                           "parts": [{"name": "<n>", "type": "<type>", "position": "<up|down, switch only>"}],
                           "wires": [{"to": "<n>", "from": "<n>", "when": "<optional: up(x)|down(x)|ok(x)>"}],
                           "events": [{"action": "<raise|lower|blow|repair>", "part": "<n>"}],
                           "replace": "<optional true: replaces an existing circuit>",
                           "action": "<optional: describe|reset|cancel>"},
        "ask": {"skill": "ask", "circuit": "<name>", "question": "<see topic questions>",
                "part": "<lamp>", "step": "<event, optional>", "from_step": "<event>", "to_step": "<event>"},
        "explain": {"skill": "explain", "circuit": "<name>", "question": "<how|why|why_not|supporting|lit>",
                    "part": "<lamp>", "step": "<optional>", "format": "<indented|tree|plain>"},
        "teach": {"skill": "teach", "topic": "<format|examples|questions|errors|limits>"},
    },
    "composites": {
        "two_way_switch": "part {type:two_way_switch, position:up|down}; joining wire {two_way_switch:x, to:D, from_up:A, from_down:B} (D<-A if up(x), D<-B if down(x)) or splitting wire {two_way_switch:x, from:O, to_up:A, to_down:B}. It expands into two conditional wires with opposite conditions.",
        "plug": "part {type:plug, position:plugged|unplugged}; conditions plugged(x)/unplugged(x); events plug_in/unplug (= raise/lower of the plug).",
        "map": "define_circuit returns `map`: part name -> event number (part_event/state_event), each wire with its event, each event with its number.",
        "converse": "To talk in English without JSON, use the `converse` skill (one part kind=text).",
    },
    "continuation": "If a definition lacks the position of a switch, the task stays in 'input-required' and asks for it; "
                    "answer in the SAME task (message.taskId) with {\"positions\": {\"s1\": \"down\"}}.",
    "response": "Each completed task carries an artifact with a data part (for programs) and a text part (for people).",
}
ERRORS = {
    "invalid_json": "the body is not JSON", "unstructured_part": "the message must be a single part kind=data (free text is not accepted)",
    "unknown_skill": "skill not in the catalogue", "unknown_field": "field not accepted in that skill",
    "invalid_format": "unexpected data type", "invalid_name": "malformed part or circuit name",
    "duplicate_part": "two parts with the same name", "unknown_type": "part type not in the list",
    "invalid_position": "position not accepted", "unknown_part": "a part is named that does not exist",
    "invalid_wire": "a wire touches a switch", "invalid_condition": "condition not accepted or on a part that cannot have it",
    "wire_cycle": "the wires form a loop", "invalid_action": "event not accepted", "too_large": "exceeds a limit",
    "invalid_step": "event number out of range", "not_a_lamp": "the question requires a lamp",
    "question_not_in_catalogue": "question that is not in the catalogue", "unknown_circuit": "there is no session with that name",
    "circuit_exists": "already exists; replace:true", "too_many_sessions": "cap on live sessions",
    "unknown_task": "taskId does not exist", "task_not_waiting": "that task is not waiting for data",
    "missing_field": "a required field is missing", "timeout": "the question exceeded the maximum time (state failed)",
    "engine_error": "the Prolog engine raised an error", "internal": "internal agent failure (state failed)",
}


def all_causes_text(r):
    """the full causes of why_not, with their «and» and their «or»"""
    out = ["All causes (ALL the failures must be fixed; in the current ONE route is enough; in a route, ALL its cuts):"]
    for f in r.get("failures", []):
        if "paths" not in f:
            c = f["cuts"][0]
            lo = c["left_by"]
            out.append(f"- {f['part']} does not hold" + (f": event {lo['event']} left {lo['fact']}." if lo else "."))
        else:
            out.append(f"- {f['part']} does not hold; possible routes (fixing ONE is enough):")
            for i, ru in enumerate(f["paths"], 1):
                cs = "; ".join(f"{c['would_need']}" + (f" (event {c['left_by']['event']} left {c['left_by']['fact']})" if c["left_by"] else "")
                               for c in ru["cuts"])
                out.append(f"    route {i}: {' <- '.join(ru['via'])}: {'ALL are needed: ' + cs if cs else 'no cuts'}")
    if r.get("warning"):
        out.append("Warning: " + r["warning"])
    return "\n".join(out)


class Agent:
    def __init__(self, url, log=None, translator=None):
        self.url = url
        self.store = Store()
        self.tasks = {}
        self.log = Path(log) if log else None      # optional JSONL file (A2A_LOG); the last records are always kept in memory
        self.recent = collections.deque(maxlen=500)
        self.counter = 0
        self.examples = None
        self._lock = threading.Lock()
        if translator is None:
            from circuit_a2a import translator as T
            translator = T.make_translator()
        self.translator = translator
        self.translator.log = self.log_extra
        from circuit_a2a.dialogue import Dialogue
        self.dialogue = Dialogue(self, translator)

    # ---------------------------------------------------------------- log (the border's view)
    def record(self, task, skill, data, ms, nbytes):
        reg = {"ts": now(), "task": task["id"], "context": task.get("contextId"), "skill": skill,
               "state": task["status"]["state"], "ms": round(ms, 1), "bytes_in": nbytes,
               "request": json.dumps(data, ensure_ascii=False)[:300] if data is not None else None}
        err = task["status"].get("message", {}).get("error")
        if err:
            reg["error"] = err.get("code")
        if skill == "converse":
            for p_ in task["status"].get("message", {}).get("parts", []):
                if p_["kind"] == "text":
                    reg["reply"] = p_["text"][:300]
        self._write(reg)

    def log_extra(self, reg):
        """log lines that are not a task: LLM calls, what the validator caught, rejected phrasings..."""
        reg = {"ts": now(), **reg}
        self._write(reg)

    def _write(self, reg):
        with self._lock:
            self.recent.append(reg)
            if self.log:
                with open(self.log, "a") as f:
                    f.write(json.dumps(reg, ensure_ascii=False) + "\n")

    def tail(self, n=20):
        return list(self.recent)[-n:]

    # ---------------------------------------------------------------- A2A
    def new_task(self, context):
        self.counter += 1
        t = {"kind": "task", "id": f"t-{self.counter}-{uuid.uuid4().hex[:6]}", "contextId": context or "no-circuit",
             "status": {"state": "submitted", "label": STATES["submitted"], "timestamp": now()}, "artifacts": [],
             "history": [{"state": "submitted", "label": STATES["submitted"], "timestamp": now()}], "_pending": None}
        self.tasks[t["id"]] = t
        if len(self.tasks) > 500:
            self.tasks.pop(next(iter(self.tasks)))
        return t

    def set_state(self, t, state, text=None, error=None, data=None):
        st = {"state": state, "label": STATES[state], "timestamp": now()}
        if text or error or data:
            parts = []
            if text:
                parts.append({"kind": "text", "text": text})
            if data:
                parts.append({"kind": "data", "data": data})
            st["message"] = {"role": "agent", "parts": parts}
            if error:
                st["message"]["error"] = error
        t["status"] = st
        t["history"].append({"state": state, "label": STATES[state], "timestamp": st["timestamp"]})

    def public(self, t):
        return {k: v for k, v in t.items() if not k.startswith("_")}

    def message(self, params, nbytes=0):
        """message/send: returns the task (always a task; failures are states, not exceptions)."""
        t0 = time.perf_counter()
        skill, data, task = None, None, None
        try:
            if not isinstance(params, dict) or not isinstance(params.get("message"), dict):
                raise Rejection("invalid_format", "params.message is missing")
            msg = params["message"]
            parts = msg.get("parts")
            text = self._natural_text(parts)
            if text is not None:
                return self._converse(msg, text, nbytes, t0)
            if (not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict)
                    or parts[0].get("kind") != "data" or not isinstance(parts[0].get("data"), dict)):
                raise Rejection("unstructured_part", "the message must be exactly ONE part: {kind:'text', text:'…'} (English, converse skill) or {kind:'data', data:{…}} (JSON, advanced interface)")
            data = parts[0]["data"]
            tid = msg.get("taskId")
            if tid is not None:
                task = self.tasks.get(tid)
                if task is None:
                    raise Rejection("unknown_task", f"there is no task {tid!r}")
                if task["status"]["state"] != "input-required":
                    raise Rejection("task_not_waiting", f"task {tid} is {task['status']['label']}; it is not waiting for data")
                skill = "define_circuit"
            else:
                skill = data.get("skill")
                skill = SKILL_ALIAS.get(skill, skill)
                task = self.new_task(data.get("circuit") if isinstance(data.get("circuit"), str) else None)
            self.set_state(task, "working")
            return self._execute(task, skill, data, nbytes, t0)
        except Rejection as r:
            if task is None:
                task = self.new_task(None)
            self.set_state(task, "rejected", f"{r.code}: {r.detail}", error=r.to_json())
            return self._close(task, skill, data, nbytes, t0)

    @staticmethod
    def _natural_text(parts):
        """ONE text part, or a data part {skill:'converse', text:'…'} -> the text; if it is not a conversation, None"""
        if not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict):
            return None
        p = parts[0]
        if p.get("kind") == "text" and isinstance(p.get("text"), str):
            return p["text"]
        if p.get("kind") == "data" and isinstance(p.get("data"), dict) and p["data"].get("skill") == "converse":
            d = p["data"]
            for k in d:
                if k not in ("skill", "text"):
                    raise Rejection("unknown_field", f"converse only accepts 'text', not {k!r}", k)
            if not isinstance(d.get("text"), str):
                raise Rejection("missing_field", "converse needs 'text' (English)", "text")
            return d["text"]
        return None

    def is_conversation(self, params):
        try:
            return self._natural_text(params["message"]["parts"]) is not None
        except Exception:
            return False

    def _converse(self, msg, text, nbytes, t0):
        """skill `converse`: one turn of the conversation (the context comes from message.contextId; the task from message.taskId if it answers a question of ours)"""
        cid, tid, task = msg.get("contextId"), msg.get("taskId"), None
        if cid is not None and (not isinstance(cid, str) or not re.match(r"^[A-Za-z0-9_.:-]{1,64}$", cid)):
            raise Rejection("invalid_format", "contextId: 1-64 characters [A-Za-z0-9_.:-]", "contextId")
        if tid is not None:
            task = self.tasks.get(tid)
            if task is None:
                raise Rejection("unknown_task", f"there is no task {tid!r}")
            cid = cid or task["contextId"]
            if task["status"]["state"] != "input-required":
                task = None            # a closed task accepts no more messages: this one goes in a new task of the same context
        cid = cid or self.dialogue.new_id()
        if task is None:
            task = self.new_task(cid)
        task["contextId"] = cid
        self.set_state(task, "working")
        try:
            res = self.dialogue.turn(cid, text)
            if res["state"] == "input-required":
                self.set_state(task, "input-required", res["text"], data=res["data"])
            else:
                task["artifacts"] = [{"artifactId": task["id"] + "-r", "name": "reply",
                                      "parts": [{"kind": "text", "text": res["text"]}, {"kind": "data", "data": res["data"]}]}]
                self.set_state(task, "completed", res["text"])
        except Rejection as r:
            self.set_state(task, "rejected", f"{r.code}: {r.detail}", error=r.to_json())
        except Timeout:
            self.set_state(task, "failed", "timeout: the core exceeded the maximum time on this question",
                           error={"code": "timeout", "detail": f"more than {LIMITS['seconds_per_question']} s"})
        except Exception as e:
            self.set_state(task, "failed", "internal: internal agent failure", error={"code": "internal", "detail": type(e).__name__})
            task["_internal"] = repr(e)
        return self._close(task, "converse", {"text": text}, nbytes, t0)

    def _execute(self, task, skill, data, nbytes, t0):
        try:
            if skill not in SKILLS:
                raise Rejection("unknown_skill", f"skill {skill!r} is not in the catalogue; available: {', '.join(SKILLS)}", "skill")
            if task["_pending"] is None:
                _check_keys(data, SKILLS[skill]["keys"], "")
            fn = getattr(self, "skill_" + skill)
            out = fn(task, data)
            if out is None:        # the skill already left the task in 'input-required'
                return self._close(task, skill, data, nbytes, t0)
            d, text = out
            task["artifacts"] = [{"artifactId": task["id"] + "-r", "name": "reply",
                                  "parts": [{"kind": "data", "data": d}, {"kind": "text", "text": text}]}]
            self.set_state(task, "completed")
        except Rejection as r:
            task["_pending"] = None
            self.set_state(task, "rejected", f"{r.code}: {r.detail}", error=r.to_json())
        except Timeout:
            ses = self.store.d.get(task["contextId"])
            if ses:
                ses.restart()   # the interpreter may have been left half-way: it is rebuilt from the definition
            self.set_state(task, "failed", "timeout: the question exceeded the maximum time; the session has been reset",
                           error={"code": "timeout", "detail": f"more than {LIMITS['seconds_per_question']} s"})
        except Exception as e:                      # the internal detail is never leaked to the client
            self.set_state(task, "failed", "internal: internal agent failure", error={"code": "internal", "detail": type(e).__name__})
            task["_internal"] = repr(e)
        return self._close(task, skill, data, nbytes, t0)

    def _close(self, task, skill, data, nbytes, t0):
        self.record(task, skill, data, (time.perf_counter() - t0) * 1000, nbytes)
        return self.public(task)

    def get_task(self, tid):
        t = self.tasks.get(tid)
        if t is None:
            raise Rejection("unknown_task", f"there is no task {tid!r}")
        return self.public(t)

    def cancel(self, tid):
        t = self.tasks.get(tid)
        if t is None:
            raise Rejection("unknown_task", f"there is no task {tid!r}")
        if t["status"]["state"] not in ("input-required", "submitted", "working"):
            raise Rejection("task_not_waiting", f"task {tid} has already finished ({t['status']['label']}); it cannot be canceled")
        t["_pending"] = None
        self.set_state(t, "canceled", "canceled by the client")
        self.record(t, "tasks/cancel", None, 0, 0)
        return self.public(t)

    # ---------------------------------------------------------------- skills
    def _circuit(self, data):
        a = data.get("circuit")
        if a is None:
            raise Rejection("missing_field", "'circuit' is missing", "circuit")
        if not isinstance(a, str) or not core.RE_CIRCUIT.match(a):
            raise Rejection("invalid_name", f"{a!r} is not a valid circuit name", "circuit")
        return a

    def skill_define_circuit(self, task, d):
        pend = task["_pending"]
        if pend is not None:                      # answering a 'needs input'
            _check_keys(d, {"positions", "skill"}, "")
            pos = d.get("positions")
            if not isinstance(pos, dict):
                raise Rejection("missing_field", "answer with {\"positions\": {\"<switch>\": \"up|down\"}}", "positions")
            definition = dict(pend)
            parts = [dict(p) for p in definition["parts"]]
            for p in parts:
                if p.get("name") in pos and p.get("type") == "switch":
                    p["position"] = pos[p["name"]]
            for k in pos:
                if not any(p.get("name") == k for p in parts):
                    raise Rejection("unknown_part", f"{k} is not a part of the pending definition", f"positions.{k}")
            definition["parts"] = parts
            task["_pending"] = None
            d = definition
        name = self._circuit(d)
        action = d.get("action")
        if action in ("cancel", "reset", "describe"):
            s = self.store.get(name)
            if action == "cancel":
                self.store.remove(name)
                return {"circuit": name, "canceled": True}, f"Circuit {name} canceled: its Prolog session has been closed."
            if action == "reset":
                s.restart()
                return ({"circuit": name, "reset": True, "step": s.n, "resets": s.restarts},
                        f"Circuit {name} reset: new interpreter, {s.n} events recorded again.")
            return self._summary(s, "described")
        if action is not None:
            raise Rejection("invalid_action", f"action {action!r}: describe, reset or cancel", "action")
        if "parts" not in d:
            # events on a circuit that already exists
            s = self.store.get(name)
            if "events" not in d:
                raise Rejection("missing_field", "neither 'parts' (define) nor 'events' (extend)", "parts")
            base_ev, user_acts = expand_events(d["events"], s.model.get("user_type", {}))
            before = s.add_events(validate_events(base_ev, s.model["type"]), user_acts)
            return self._summary(s, "extended", before=before)
        d_base, info = expand_definition(d)
        model = validate_definition(d_base)
        model["user_type"], model["wire_groups"] = info["user_type"], info["groups"]
        user_acts = None
        if "events" in d:
            base_ev, user_acts = expand_events(d["events"], info["user_type"])
            events = validate_events(base_ev, model["type"])
        else:
            events = []
        if model["missing_position"]:
            left = model["missing_position"]
            task["_pending"] = {k: d[k] for k in ("skill", "circuit", "parts", "wires", "events", "replace") if k in d}
            self.set_state(task, "input-required",
                           "What position is " + " and ".join(left) + " in: up or down?",
                           data={"missing": [{"part": q, "field": "position", "values": ["up", "down"]} for q in left],
                                 "how_to_answer": {"positions": {left[0]: "up|down"}}})
            return None
        replace = d.get("replace", False)
        if not isinstance(replace, bool):
            raise Rejection("invalid_format", "'replace' is true or false", "replace")
        if len(model["order"]) * 2 + len(model["wires"]) + len(events) > LIMITS["total_events"]:
            raise Rejection("too_large", "the circuit would exceed the event cap")
        s = Session(name, model, events, user_acts)
        self.store.put(s, replace)
        task["contextId"] = name
        return self._summary(s, "accepted")

    def _summary(self, s, verb, before=None):
        lit = s.lit(s.n)
        d = {"circuit": s.name, "result": verb, "step": s.n, "assembly_until": s.assembled,
             "parts": len(s.model["order"]), "wires": len(s.model["wires"]), "events": len(s.events),
             "lit": lit["lit"], "unlit": lit["unlit"], "version": len(s.versions), "map": s.event_map()}
        t = (f"Circuit {s.name} {verb}: {d['parts']} parts, {d['wires']} wires, {d['events']} events. "
             f"After event {s.n} " + (f"the lit lamps are {', '.join(lit['lit'])}." if lit["lit"] else "no lamp is lit."))
        if before is not None:
            d["last_step_before"] = before
            d["warning"] = (f"Version {d['version']}: the last step goes from {before} to {s.n}. Questions without `step` now refer "
                            f"to {s.n}; earlier steps (up to {before}) still hold as before.")
            t += "\n" + d["warning"]
        return d, t

    def _question_name(self, d, path=""):
        name = d.get("question")
        if name not in QUESTIONS:
            raise Rejection("question_not_in_catalogue", f"question not in the catalogue: {name!r}; available: {', '.join(QUESTIONS)}", "question")
        allowed = QUESTIONS[name][0]
        for k in d:
            if k in ("skill", "circuit", "question", "format"):
                continue
            if k not in allowed:
                raise Rejection("unknown_field", f"the question {name} does not accept the field {k!r}; it accepts: {', '.join(sorted(allowed))}", k)
        return name

    def _answer(self, name, s, d):
        """-> (data, text) of the catalogue question, building the query from already validated data"""
        if name == "lit":
            n = s.step(d.get("step"))
            r = s.lit(n)
            txt = (f"After event {n} the lit lamps are {', '.join(r['lit'])}." if r["lit"] else f"After event {n} no lamp is lit.")
            return r, txt
        if name == "timeline":
            n1 = s.step(d.get("from_step") if d.get("from_step") is not None else s.assembled, "from_step")
            n2 = s.step(d.get("to_step"), "to_step")
            if n1 > n2:
                raise Rejection("invalid_step", f"'from_step' ({n1}) must be before or equal to 'to_step' ({n2})", "from_step")
            pz = s.lamp_part(d["part"]) if d.get("part") is not None else None
            r = s.timeline(n1, n2, pz)
            lin = []
            for f in r["steps"]:
                q = f.get("what_happened")
                cab = f"event {f['step']}" + (f" (event {f['event_index']}: {q['action']} {q['part']})" if q else " (initial state)" if f.get("event_index") == 0 else "")
                lin.append(f"{cab}: " + (f"lit: {', '.join(f['lit'])}" if f["lit"] else "none lit"))
            return r, "\n".join(lin)
        l = s.lamp_part(d.get("part"))
        if name == "why":
            if d.get("from_step") is None or d.get("to_step") is None:
                raise Rejection("missing_field", "why needs 'from_step' and 'to_step'", "from_step")
            n1, n2 = s.step(d["from_step"], "from_step"), s.step(d["to_step"], "to_step")
            if n1 >= n2:
                raise Rejection("invalid_step", f"'from_step' ({n1}) must be before 'to_step' ({n2})", "from_step")
            r = s.why_ext(n1, n2, l)
            est = lambda b: "lit" if b else "unlit"
            if r["change"] == "no_change" and not r["segments"]:
                return r, f"{l} has not changed between event {n1} and {n2}: it is still {est(r['now_lit'])}."
            if len(r["segments"]) == 1 and (r["segments"][0]["from_step"], r["segments"][0]["to_step"]) == (n1, n2):
                return r, s.sentence("change", n1=n1, n2=n2, l=l)
            parts = [s.sentence("change", n1=t["from_step"], n2=t["to_step"], l=l) for t in r["segments"]]
            cab = (f"{l} is the same ({est(r['now_lit'])}) at event {n1} and at {n2}, but in between it changed {len(parts)} times: "
                   if r["change"] == "no_change" else f"{l} changed {len(parts)} times between event {n1} and {n2}: ")
            return r, cab + " ".join(parts)
        n = s.step(d.get("step"))
        if name == "supporting":
            r = s.supporting(n, l)
            return r, (s.sentence("holds_up", n=n, l=l) if r["lit"] else f"{l} is not lit after event {n}: there is nothing supporting it.")
        if name == "how":
            r = s.how(n, l)
            return r, ("\n".join(indented(r["tree"])) if r["lit"] else f"{l} is not lit after event {n}: there is no proof.")
        r = s.why_not_all(n, l)     # why_not, with ALL the causes
        if r["lit"]:
            return r, f"{l} is already lit after event {n}: there is nothing to explain."
        return r, s.sentence("unlit", n=n, l=l)          # the original example's text, untouched; the full causes go in `failures` and in explain

    def skill_ask(self, task, d):
        s = self.store.get(self._circuit(d))
        name = self._question_name(d)
        return self._answer(name, s, d)

    def skill_explain(self, task, d):
        s = self.store.get(self._circuit(d))
        name = self._question_name(d)
        fmt = d.get("format", "indented")
        if fmt not in ("indented", "tree", "plain"):
            raise Rejection("invalid_format", "format: indented, tree or plain", "format")
        data, sentence = self._answer(name, s, d)
        out = {"question": name, "format": fmt}
        if name == "how" and data.get("tree"):
            out["tree"] = data["tree"]
            out["indented"] = indented(data["tree"])
            text = "\n".join(out["indented"]) if fmt != "plain" else \
                f"{data['part']} is lit after event {data['step']}: it is a lamp, it is ok and current reaches it (see the tree)."
            if fmt == "plain":
                evs = s.supporting(data["step"], data["part"])
                text = s.sentence("holds_up", n=data["step"], l=data["part"])
                out["supporting"] = evs["supporting"]
        else:
            out["data"] = data
            text = sentence
            # a complete explanation of a change includes the proof that supports it
            if name == "why" and data["change"] != "no_change" and fmt != "plain":
                n = data["to_step"] if data["change"] == "turned_on" else data["from_step"]
                pr = s.how(n, data["part"])
                if pr["tree"]:
                    out["proof"] = {"step": n, "indented": indented(pr["tree"])}
                    text += f"\nProof that {data['part']} is lit at event {n}:\n" + "\n".join(out["proof"]["indented"])
        if name == "why_not" and not data.get("lit"):
            text += "\n" + all_causes_text(data)
        out["text"] = text
        return out, text

    # ---------------------------------------------------------------- teach
    def _examples_with_expected(self):
        """The examples are checked with the engine itself when requested (in throwaway sessions): what is expected is not a promise."""
        if self.examples is None:
            tmp = Agent(self.url, log="/dev/null")
            for ex in EXAMPLES:
                for step in ex["steps"]:
                    t = tmp.message({"message": {"role": "user", "parts": [{"kind": "data", "data": step["message"]}]}})
                    assert t["status"]["state"] == "completed", (ex["name"], t["status"])
                    dd = t["artifacts"][0]["parts"][0]["data"]
                    for k, v in step["expected"].items():
                        assert dd[k] == v, (ex["name"], k, dd[k], v)
            self.examples = EXAMPLES
        return self.examples

    def skill_teach(self, task, d):
        topic = d.get("topic", "index")
        topics = ("index", "format", "examples", "questions", "errors", "limits")
        if topic not in topics:
            raise Rejection("invalid_format", f"topic {topic!r}; available: {', '.join(topics)}", "topic")
        if topic == "index":
            return ({"topics": list(topics[1:]), "start_with": "format"},
                    "Topics to learn how to talk to me: format, examples, questions, errors, limits. Start with 'format'.")
        if topic == "format":
            ex = self._examples_with_expected()[0]
            return ({**FORMAT, "minimal_example": ex},
                    FORMAT["idea"] + "\nThe minimal example (a source, a wire and a lamp) is in 'minimal_example': send its first message and check that you "
                    "get what is expected.")
        if topic == "examples":
            exs = self._examples_with_expected()
            return ({"examples": exs}, "Three examples with their expected answer: " + ", ".join(e["name"] for e in exs) +
                    ". Each step carries the message and the fields the answer must contain (checked with the engine when requested).")
        if topic == "questions":
            cat = {k: {"fields": sorted(v[0]), "description": v[1]} for k, v in QUESTIONS.items()}
            return ({"questions": cat, "answers": {
                "lit": "{step, lit:[..], unlit:[..]}",
                "supporting": "{lit, supporting:[{event, fact}]}",
                "how": "{lit, tree:{fact, event?, because:[..]}, prolog_tree}",
                "why": "{change: turned_on|turned_off|no_change, events:[{event, fact}]}",
                "why_not": "{lit, fails, missing:[..] (first cuts of each path, alternatives), failures:[{part, cuts|paths:[{via, cuts:[{would_need, left_by}]}]}]} (ALL the causes: failures and, paths or, cuts and)",
                "timeline": "{steps:[{step, event_index, what_happened, lit, unlit, turn_on, turn_off}]}"}},
                    "Catalogue questions: " + "; ".join(f"{k}: {v[1]}" for k, v in QUESTIONS.items()))
        if topic == "errors":
            return ({"codes": ERRORS}, "A bad request ends in state 'rejected' with a stable code in status.message.error.code.")
        return ({"limits": LIMITS}, "Current limits: " + ", ".join(f"{k}={v}" for k, v in LIMITS.items()))
