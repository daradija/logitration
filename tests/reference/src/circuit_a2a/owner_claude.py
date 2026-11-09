"""owner_claude: a Claude session plays the OWNER of a circuit and interviews the logic agent.

The owner (an LLM reached through the chosen backend: claude-cli or anthropic-api) sees ONLY the agent card and the agent's text. It has the
facts of its circuit in prose (examples/scenarios/<name>.json) and a list of questions to ask at the end. Afterwards this script compares what the
agent loaded and every factual answer with a plain-Python simulator (the scenario's ground truth) and writes a transcript.

  python -m circuit_a2a owner_claude --scenario workshop [--url http://127.0.0.1:8923/] [--max 30]

Writes examples/sessions/owner_claude_<scenario>.txt (readable transcript) and .json (data) unless --out-dir says otherwise."""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from circuit_a2a.client import Simulator
from circuit_a2a.wire import DEFAULT_URL
from circuit_a2a import wizard
from circuit_a2a.translator import LLMTranslator, TranslatorError

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = REPO / "out"
SCEN_DIR = REPO / "examples" / "scenarios"
SESSIONS = REPO / "examples" / "sessions"

# ======================================================================================= scenarios (the truth lives here, the agent does not see it)
def load_scenarios():
    """examples/scenarios/*.json -> {name: scenario} (JSON lists are turned back into tuples)."""
    out = {}
    for f in sorted(SCEN_DIR.glob("*.json")):
        e = json.loads(f.read_text())
        e["parts"] = {n: tuple(v) for n, v in e["parts"].items()}
        e["wires"] = [tuple(w) for w in e["wires"]]
        e["events"] = [tuple(x) for x in e["events"]]
        e["extra_events"] = [tuple(x) for x in e["extra_events"]]
        out[f.stem] = e
    return out


SCENARIOS = load_scenarios()


def truth_model(e, extra=False):
    """the scenario's truth as a Simulator (two-way switch/plug = switch) + events"""
    base = {"parts": [{"name": n, "type": "switch" if t in ("two_way_switch", "plug") else t, **({"position": p} if p else {})} for n, (t, p) in e["parts"].items()],
            "wires": [{"to": d, "from": o, **({"when": s} if s else {})} for d, o, s in e["wires"]]}
    return Simulator(base), e["events"] + (e["extra_events"] if extra else [])


def truth_states(e, extra=False):
    sim, evs = truth_model(e, extra)
    st = [(sim.lit(), dict(sim.pos), dict(sim.ok))]
    for a, pz in evs:
        sim.apply(a, pz)
        st.append((sim.lit(), dict(sim.pos), dict(sim.ok)))
    return st


# ======================================================================================= A2A HTTP client
def rpc(url, method, params, timeout=300):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read())
    return resp, (time.perf_counter() - t0) * 1000


def text_of(t):
    st = t["status"]
    parts = (st.get("message") or {}).get("parts", [])
    txt = "\n".join(p["text"] for p in parts if p["kind"] == "text")
    if not txt:
        for a in t.get("artifacts", []):
            txt = "\n".join(p["text"] for p in a["parts"] if p["kind"] == "text")
    data = {}
    for a in t.get("artifacts", []):
        for p in a["parts"]:
            if p["kind"] == "data":
                data = p["data"]
    for p in parts:
        if p["kind"] == "data":
            data = p["data"]
    return txt, data


# ======================================================================================= the LLM client
CLIENT_SYSTEM = """You are an agent (the CLIENT) talking over the A2A protocol to another agent, the LOGIC AGENT, so that it helps you understand an electrical circuit of yours.
This is the logic agent's card (the only thing you know about it):
{card}

You talk to it in natural English, message by message. You do NOT use JSON or odd formats: only text, the way a person would.
YOUR CIRCUIT (real facts; this is your information, the agent does not know it):
{facts}

YOUR QUESTIONS (ask them once the circuit is loaded, one at a time, in your own words, and wait for the answer before the next one):
{questions}

YOUR STYLE: {style}

RULES
- Start by describing your circuit, however seems natural to you. Do not invent anything that is not in your facts: if the agent asks something your facts do not say, say you do not know or that there is none.
- If the agent asks you something, answer exactly that and accurately according to your facts.
- If the agent repeats what it understood, CHECK it carefully part by part against your facts. If there is any error or something is missing, say so and correct it; if everything is correct, say "yes, that is correct".
- When you receive an answer to a question of yours, read it: if it makes no sense with your facts, say so in the next message ("that does not add up because..."); if it is correct, move on to the next question.
- When you have asked ALL your questions (including the EXTENSION) and received their answers, write exactly FIN.
- Your output is ONLY the text of the next message for the agent (or FIN). No quotation marks or explanations of your own.
"""


class LLMClient:
    """The owner: an LLM (through the chosen backend) that plays the circuit's owner. It only sees the agent card and the agent's text."""

    def __init__(self, card, sc, backend):
        self.system = CLIENT_SYSTEM.format(card=json.dumps(card, ensure_ascii=False)[:6000], facts=sc["facts"],
                                           questions="\n".join(f"{i}. {q}" for i, q in enumerate(sc["questions"], 1)), style=sc["style"])
        self.backend = backend

    def next(self, history):
        prompt = "CONVERSATION SO FAR:\n" + (history or "(you have not said anything yet)") + "\n\nWrite your next message for the logic agent."
        for _ in range(3):
            try:
                out = self.backend.raw(self.system, prompt)
            except TranslatorError:
                continue
            if out:
                return out
        raise RuntimeError("the LLM owner does not answer")


# ======================================================================================= verification against the truth
def norm_action(a):
    return {"plug": "raise", "unplug": "lower"}.get(a, a)


def rename(x, alias):
    """names the agent invented (the terse client did not give them) are mapped to the truth's names before comparing"""
    if not alias:
        return x
    pat = re.compile(r"\b(" + "|".join(sorted(map(re.escape, alias), key=len, reverse=True)) + r")\b")
    return json.loads(pat.sub(lambda m: alias[m.group(1)], json.dumps(x, ensure_ascii=False)))


def compare_model(mp, e, extra):
    """what the agent loaded (map) against the scenario's truth -> list of differences"""
    diff = []
    pz = mp["parts"]
    for n, (t, p) in e["parts"].items():
        if n not in pz:
            diff.append(f"missing part {n} ({t})")
            continue
        if pz[n]["type"] != t:
            diff.append(f"{n}: loaded type {pz[n]['type']!r}, truth {t!r}")
        if p and pz[n].get("initial_position") != p:
            diff.append(f"{n}: loaded initial position {pz[n].get('initial_position')!r}, truth {p!r}")
    for n in pz:
        if n not in e["parts"]:
            diff.append(f"extra part: {n} ({pz[n]['type']})")
    tv = sorted((d, o, s or "") for d, o, s in e["wires"])
    ld = sorted((c["to"], c["from"], c["when"] or "") for c in mp["wires"])
    for c in tv:
        if c not in ld:
            diff.append(f"missing wire {c[0]} <- {c[1]} {('when ' + c[2]) if c[2] else ''}".strip())
    for c in ld:
        if c not in tv:
            diff.append(f"extra or different wire: {c[0]} <- {c[1]} {('when ' + c[2]) if c[2] else ''}".strip())
    ev_t = [(norm_action(a), p) for a, p in e["events"]]
    ev_l = [(norm_action(s["action"]), s["part"]) for s in mp["events"]]
    if ev_l[:len(ev_t)] != ev_t:
        diff.append(f"loaded events {ev_l} != truth {ev_t}")
    return diff


def paths(sim, x):
    if sim.type[x] == "source":
        return [[]]
    out = []
    for d, o, w in sim.wires:
        if d == x:
            for r in paths(sim, o):
                out.append([(d, o, w)] + r)
    return out


def verify_answer(data, e, states, setup):
    """data = data part of an answer to a question. -> (verdict 'ok'|'DISAGREES'|'n/a', detail)"""
    ue = data.get("understood_question")
    rn = data.get("core_result")
    if not ue or rn is None:
        return "n/a", ""
    qtype = ue["type"]
    sim, _ = truth_model(e)

    def k_of(m):
        return m - setup

    def st(k):
        return states[k] if 0 <= k < len(states) else None
    try:
        if qtype == "lit":
            s = st(k_of(ue["step"]))
            return ("ok", "") if s and sorted(rn["lit"]) == sorted(s[0]) else ("DISAGREES", f"agent {rn['lit']} truth {s[0] if s else None}")
        if qtype in ("how", "supporting"):
            s = st(k_of(ue["step"]))
            return ("ok", "") if s and rn["lit"] == (ue["part"] in s[0]) else ("DISAGREES", f"{ue['part']} lit: agent {rn['lit']}")
        if qtype == "why_not":
            s = st(k_of(ue["step"]))
            lit, pos, good = s
            sim.pos, sim.ok = dict(pos), dict(good)
            l = ue["part"]
            if l in lit:
                return ("ok", "") if rn["lit"] else ("DISAGREES", "the truth says it is lit")
            expected = (["ok"] if not good[l] else []) + (["has_current"] if not sim.current(l) else [])
            parts = [f["part"].split("(")[0] for f in rn["failures"]]
            if parts != expected:
                return "DISAGREES", f"agent failures {parts} truth {expected}"
            for f in rn["failures"]:
                if "paths" in f:
                    exp = sorted(tuple(sorted({w for _, _, w in r if w and not sim.holds(w)})) for r in paths(sim, l))
                    got = sorted(tuple(sorted(c["would_need"] for c in r["cuts"])) for r in f["paths"])
                    if exp != got:
                        return "DISAGREES", f"paths agent {got} truth {exp}"
            return "ok", ""
        if qtype == "why":
            l, k1, k2 = ue["part"], k_of(ue["from_step"]), k_of(ue["to_step"])
            trans = [(a, a + 1) for a in range(k1, k2) if (l in states[a][0]) != (l in states[a + 1][0])]
            got = [(k_of(t["from_step"]), k_of(t["to_step"])) for t in rn["segments"]]
            return ("ok", "") if got == trans else ("DISAGREES", f"segments agent {got} truth {trans}")
        if qtype == "timeline":
            got = [f["lit"] for f in rn["steps"]]
            exp = [states[k_of(f["step"])][0] for f in rn["steps"]]
            return ("ok", "") if [sorted(x) for x in got] == [sorted(x) for x in exp] else ("DISAGREES", f"agent {got} truth {exp}")
    except Exception as ex:
        return "DISAGREES", f"could not verify: {ex!r}"
    return "n/a", ""


def verify(log, e):
    """checks the loaded model and each answer against the scenario's truth -> (text lines, summary)"""
    lines = ["=" * 100, "VERIFICATION AGAINST THE SCENARIO'S TRUTH (plain-Python simulator, no Prolog)"]
    mp, summary = None, {"model_differences": None, "answers": []}
    setup, extra_applied = None, False
    for r in log:
        d = r.get("data") or {}
        if d.get("map") and mp is None:
            mp = rename(d["map"], e.get("alias"))
            setup = mp["assembly_until"]
            diff = compare_model(mp, e, False)
            summary["model_differences"] = diff
            lines.append(f"Loaded model (turn {r['turn']}): " + ("IDENTICAL to the truth." if not diff else f"{len(diff)} DIFFERENCES:"))
            lines += [f"   - {x}" for x in diff]
        if d.get("last_step_now") and d.get("map"):
            extra_applied = True
    st0, st1 = truth_states(e, False), truth_states(e, True)
    for r in log:
        d = r.get("data") or {}
        if setup is None or ("understood_question" not in d and "answers" not in d):
            continue
        for dd in d.get("answers") or [d]:
            dd = rename(dd, e.get("alias"))
            states = st1 if (extra_applied and dd["understood_question"].get("version", 1) >= 2) else st0
            v, det = verify_answer(dd, e, states, setup)
            summary["answers"].append({"turn": r["turn"], "type": dd["understood_question"]["type"], "verdict": v, "detail": det,
                                       "understood": dd["understood_question"], "wording": dd.get("wording")})
            lines.append(f"Turn {r['turn']}: {dd['understood_question']['type']} ({dd['understood_question'].get('part') or '-'}) -> {v} {det}  [wording: {dd.get('wording')}]")
    return lines, summary


# ======================================================================================= the conversation
def converse(url, name, maximum, backend, agent_label="unknown", out_dir=None):
    e = SCENARIOS[name]
    card = json.load(urllib.request.urlopen(url.rstrip("/") + "/.well-known/agent-card.json", timeout=15))
    cli = LLMClient(card, e, backend)
    ctx = f"natural-{name}-{int(time.time())}"
    history, log, tid, reminders = "", [], None, 0
    lines = [f"TRANSCRIPT · owner_claude · scenario \"{name}\": {e['title']}", "@@HEADER@@", "The client only sees the card and the agent's text; its circuit's facts are in the client's prompt:", "  " + e["facts"][:1500], ""]
    t_start = time.time()
    for turn in range(1, maximum + 1):
        t0 = time.perf_counter()
        msg = cli.next(history)
        ms_cli = (time.perf_counter() - t0) * 1000
        if msg.strip().upper().startswith("FIN"):
            asked = sum(1 for r in log if (r.get("data") or {}).get("understood_question") or (r.get("data") or {}).get("answers"))
            if asked < len(e["questions"]) and reminders < 3:
                reminders += 1
                lines.append(f"[turn {turn}] (script reminder to the owner: {asked} of {len(e['questions'])} questions answered so far; FIN not accepted yet)\n")
                history += (f"\n[REMINDER FROM THE SCRIPT, not from the agent]: you have only received {asked} answers to your questions; your list has "
                            f"{len(e['questions'])}. Ask the next unasked question (the EXTENSION too) before writing FIN.\n")
                continue
            lines.append(f"[turn {turn}] CLIENT: FIN\n")
            log.append({"turn": turn, "client": "FIN"})
            break
        m = {"role": "user", "messageId": f"m{turn}", "contextId": ctx, "parts": [{"kind": "text", "text": msg}]}
        if tid:
            m["taskId"] = tid
        resp, ms_ag = rpc(url, "message/send", {"message": m})
        if "error" in resp:
            t = None
            txt, data, state = "JSON-RPC ERROR: " + json.dumps(resp["error"], ensure_ascii=False), {}, "error"
        else:
            t = resp["result"]
            txt, data = text_of(t)
            state = t["status"]["state"]
            tid = t["id"] if state == "input-required" else None
        lines += [f"[turn {turn}] CLIENT ({ms_cli / 1000:.1f} s):", msg, "", f"[turn {turn}] AGENT [{state}] ({ms_ag / 1000:.1f} s):", txt, ""]
        log.append({"turn": turn, "client": msg, "client_ms": round(ms_cli), "state": state, "agent": txt, "agent_ms": round(ms_ag), "data": data})
        history += f"\n[YOU]: {msg}\n[LOGIC AGENT] ({state}): {txt}\n"
    lines_v, summary = verify(log, e)
    lines += lines_v
    # ---- metrics
    ag = [r for r in log if "state" in r]
    rounds_until_loaded = 0
    for r in ag:
        if r["state"] == "input-required":
            rounds_until_loaded += 1
        elif (r.get("data") or {}).get("map"):
            break
    met = {"turns_with_agent": len(ag), "interview_rounds_until_loaded": rounds_until_loaded,
           "total_seconds": round(time.time() - t_start), "agent_ms_mean": round(sum(r["agent_ms"] for r in ag) / max(1, len(ag))),
           "agent_ms_max": max([r["agent_ms"] for r in ag] or [0]), "client_ms_mean": round(sum(r["client_ms"] for r in ag) / max(1, len(ag))),
           "states": {s: sum(1 for r in ag if r["state"] == s) for s in sorted({r["state"] for r in ag})},
           "ended_with_FIN": any(r.get("client") == "FIN" for r in log)}
    summary["metrics"] = met
    lines += ["", "METRICS: " + json.dumps(met, ensure_ascii=False)]
    n_ok = sum(1 for x in summary["answers"] if x["verdict"] == "ok")
    n_dis = sum(1 for x in summary["answers"] if x["verdict"] == "DISAGREES")
    summary["answers_ok"], summary["answers_disagree"] = n_ok, n_dis
    lines += [f"FACTUAL ANSWERS THAT MATCH THE TRUTH: {n_ok} of {n_ok + n_dis} verifiable ({n_dis} disagree)"]
    header = (f"REAL RUN · {time.strftime('%Y-%m-%d %H:%M:%S')} · owner backend {backend.name} (model {backend.model}) · agent translator {agent_label} · "
              f"{met['turns_with_agent']} agent turns · {met['total_seconds']} s · context {ctx} · agent {url}")
    lines = [l.replace("@@HEADER@@", header) for l in lines]
    out = Path(out_dir) if out_dir else SESSIONS
    out.mkdir(parents=True, exist_ok=True)
    (out / f"owner_claude_{name}.txt").write_text("\n".join(lines) + "\n")
    (out / f"owner_claude_{name}.json").write_text(json.dumps({"scenario": name, "header": header, "context": ctx, "log": log, "summary": summary}, ensure_ascii=False, indent=1))
    print(name, json.dumps(met, ensure_ascii=False))
    print(name, "model:", summary["model_differences"])
    print(name, "answers:", [(x["turn"], x["type"], x["verdict"]) for x in summary["answers"]])
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(prog="owner_claude", description="A Claude session plays the owner of a circuit and interviews the agent.")
    ap.add_argument("--scenario", required=True, choices=list(SCENARIOS))
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--max", type=int, default=30, help="maximum owner messages")
    ap.add_argument("--agent-translator", default="unknown", help="label of the SERVER's translator, written in the transcript header")
    ap.add_argument("--out-dir", default=None, help="default: examples/sessions")
    ap.add_argument("--reverify", action="store_true", help="do not talk: re-check a saved examples/sessions/owner_claude_<scenario>.json against the truth")
    wizard.add_arguments(ap)
    a = ap.parse_args(argv)
    if a.reverify:
        j = json.loads((Path(a.out_dir) if a.out_dir else SESSIONS).joinpath(f"owner_claude_{a.scenario}.json").read_text())
        l, r = verify(j["log"], SCENARIOS[a.scenario])
        print("\n".join(l))
        return
    kind, model, strip = wizard.choose(a, need_llm=True)      # the translator that PLAYS THE OWNER (the server has its own)
    backend = wizard.build(kind, model, strip)
    if not isinstance(backend, LLMTranslator):
        sys.exit("owner_claude needs a real LLM: use --translator claude-cli or anthropic-api")
    converse(a.url, a.scenario, a.max, backend, a.agent_translator, a.out_dir)


if __name__ == "__main__":
    main()
