# Copyright (c) 2026 David Ragel
# SPDX-License-Identifier: BUSL-1.1
# Licensed under the Business Source License 1.1; see LICENSE.
"""logi: one command for an idea file.

    logi try   IDEA [--translator simulated|claude-cli|anthropic-api] [--device PROGRAM] [--observe]   talk to it here
    logi check IDEA [--translator ...] [--observe]                                                    replay its conversation
    logi serve IDEA [--port 8923] [--host 127.0.0.1] [--translator ...] [--device PROGRAM] [--observe]   publish it over A2A
  --observe lets the file's `observe:` command run (the agent looks at the world itself, read-only); without it, it does not.
    logi lint  IDEA                                                                          what the file leaves unsaid
    logi spec  IDEA [--level 0|1|2]                                                          the specification of each script the file lacks (no model)
    logi program IDEA [--model sonnet] [--calls 12] [--force] [--trace DIR]                    a model writes them; validated, tried, anchored in programs/
  try/serve/check --program uses those scripts (on the file's rehearsal machines; --world ssh:HOST,HOST for real ones)
    logi talk  URL  [message]                                                                talk to a served agent
Spanish aliases: probar, comprobar, servir, hablar. Also: python -m logitration ...
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

from logitration.idea import Idea, IdeaError, check, load
from logitration.lint import at_load, lint, show as show_lint

ALIASES = {"probar": "try", "comprobar": "check", "servir": "serve", "hablar": "talk", "revisar": "lint", "especificar": "spec", "programar": "program"}
HELP = """commands (anything else is said to the agent):
  :why [goal]   why the last answer (or the goal) holds or not      (:porque)
  :facts        what the agent has been told so far                 (:hechos)
  :rules        your logic and meta-interpreter, as written         (:reglas)
  :example      replay the file's conversation and compare          (:ejemplo)
  :lint         what the file leaves unsaid (also: logi lint FILE)  (:revisar)
  :reload       re-read the file and replay what you typed          (:recargar)
  :help  :quit                                                      (:ayuda :salir)"""


def device_from(program):
    """--device PROGRAM: each command is run as `PROGRAM word word ...` (no shell); a non-zero exit is a failure"""
    if not program:
        return None

    def run(command):
        r = subprocess.run([program] + command.split(), capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise RuntimeError(f"exit {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}")
        return r.stdout.strip()
    return run


def world_from(spec):
    """--world: 'rehearsal' (the file's pretend machines, default) | 'ssh:HOST,HOST' (the real ones, over ssh)"""
    if not spec or spec == "rehearsal":
        return None
    if spec.startswith("ssh:"):
        from logitration.rehearsal import SshWorld
        return SshWorld([h for h in spec[4:].split(",") if h])
    raise IdeaError(f"--world: use rehearsal or ssh:HOST,HOST (not {spec!r})")


def _agent(a):
    return load(a.idea, translator=a.translator, model=a.model, device=device_from(getattr(a, "device", None)),
                observe=getattr(a, "observe", False), program=getattr(a, "program", False), world=world_from(getattr(a, "world", None)))


def observe_note(agent, out=print):
    """a file that declares `observe:` run without --observe: say that the agent will not look at the world"""
    if agent.idea.observe and agent.observe is None:
        out(f"note: {agent.idea.path.name}:{agent.idea.observe_line} observes the world with `{agent.idea.observe}`; "
            "it did not run (add --observe to let it)")


def show_check(results, out=print):
    bad = 0
    for client, expected, reply, missing, n in results:
        bad += bool(missing)
        out(f"{'ok  ' if not missing else 'FAIL'} line {n}  client: {client}")
        for e in expected:
            out(f"       {'✓' if e not in missing else '✗'} {e}")
        if missing:
            out("       the agent said:\n" + "\n".join("         | " + x for x in str(reply).splitlines()))
    out(f"{len(results) - bad}/{len(results)} turns answered as written")
    return bad


def cmd_lint(a):
    idea = Idea(a.idea)
    return 1 if show_lint(lint(idea), idea.path.name) else 0


def cmd_spec(a):
    from logitration import programmer
    idea = Idea(a.idea)
    if not idea.programs:
        print(f"{idea.path.name} declares no program (add `-- program` under the meta-interpreters: observe pred(Args) via cmd / act name via cmd)")
        return 1
    agent = load(a.idea)
    print("WHO PROVIDES EACH FACT")
    for level, line, msg in agent.lint:
        if level == "info" and msg.split(":")[0] in ("written in the file (facts)", "the client states (phrase, no rule)", "computed by a program") \
                or msg.startswith(("computed by a program", "an action script", "the agent looks at")):
            print(f"  {msg}")
    print()
    for t in programmer.build_tasks(idea, agent):
        print(programmer.spec_text(t, a.level))
        print()
    return 0


def write_trace(root, out):
    t = out.task
    d = Path(root) / t.name
    d.mkdir(parents=True, exist_ok=True)
    (d / "spec.txt").write_text(__import__("logitration.programmer", fromlist=["x"]).spec_text(t, out.level) + "\n")
    for at in out.attempts:
        k = at["k"]
        (d / f"attempt{k}_1_prompt.txt").write_text(at["prompt"] + "\n")
        (d / f"attempt{k}_2_response.txt").write_text(at["response"] + "\n")
        (d / f"attempt{k}_3_validation.txt").write_text((at["validation"] or "ok: the script passes the validator") + "\n")
        if at["rehearsal"] is not None:
            (d / f"attempt{k}_4_rehearsal.txt").write_text(at["rehearsal"] + "\n" + "\n".join(
                f"{'ok  ' if ok else 'FAIL'} {l}\n       {dt}" for l, ok, dt in at["results"]) + "\n")
    (d / "summary.json").write_text(json.dumps(dict(
        name=t.name, kind=t.kind, level=out.level, accepted=out.accepted, calls=out.calls, usd=round(out.usd, 4),
        attempts=[dict(k=at["k"], validation=at["validation"], rehearsal=(at["rehearsal"] or "").split("\n")[0], accepted=at["accepted"], meta=at["meta"])
                  for at in out.attempts], informe=out.informe), indent=1, ensure_ascii=False))


def cmd_program(a):
    from logitration import programmer
    idea = Idea(a.idea)
    if not idea.programs:
        print(f"{idea.path.name} declares no program")
        return 1
    agent = load(a.idea)
    world = programmer.idea_world(idea, idea.programs[0]["via"][0])
    if world is None:
        print("no pretend machines: add `rehearse m1: item item` under `-- program`")
        return 1
    llm = getattr(a, "llm", None) or programmer.ClaudeCLI(a.model)
    budget = programmer.Budget(a.calls)
    trace = a.trace or str(programmer.programs_dir(idea) / "trace")
    bad = 0
    for t in programmer.build_tasks(idea, agent):
        if programmer.anchored(idea, t.name) and not a.force:
            print(f"{t.name}: already anchored (v{programmer.anchored(idea, t.name)['version']}); --force builds it again")
            continue
        print(f"{t.name} ({t.kind}) via {', '.join(t.via)}: context level {a.level} ({programmer.LEVELS[a.level]})")

        def show(out, at):
            how = at["validation"] and f"validator: {at['validation']}" or "validator ok; rehearsal " + at["rehearsal"].split("\n")[0]
            print(f"  attempt {at['k']}: {how}" + (" -> ACCEPTED" if at["accepted"] else ""))
            for line in (at["rehearsal"] or "").split("\n")[1:5]:
                print("   " + line)
        try:
            out = programmer.program_one(t, llm, world, level=a.level, attempts=a.attempts, budget=budget, on_attempt=show)
        except programmer.BudgetExhausted as e:
            print(f"  stopped: {e}")
            bad += 1
            continue
        write_trace(trace, out)
        if out.accepted:
            meta = programmer.anchor(idea, t, out.src, out, model=out.attempts[-1]["meta"].get("model", a.model))
            print(f"  anchored programs/{t.name}.py v{meta['version']} ({out.calls} call(s), about ${out.usd:.3f})")
        else:
            bad += 1
            print(out.informe)
    print(f"calls: {budget.used}; traces in {trace}")
    return 1 if bad else 0


def cmd_check(a):
    agent = _agent(a)
    at_load(agent)
    observe_note(agent)
    if not agent.idea.conversation:
        print("the file has no conversation to check (write client:/agent: lines under '== conversation')")
        return 1
    return 1 if show_check(check(agent)) else 0


def say(agent, text):
    r = agent.say(text)
    print("agent> " + str(r).replace("\n", "\n       "))
    return r


def cmd_try(a, stdin=None):
    stdin = stdin or sys.stdin
    agent = _agent(a)
    idea = agent.idea
    at_load(agent)
    mi = "; ".join(f"{p}: {', '.join(v) or 'default'}" for p, v in idea.interpreters.items())
    if idea.programs:
        mi += "; program: " + ", ".join(idea.program_names)
    print(f"{idea.name}: {len(idea.words)} words, meta-interpreters ({mi}), "
          f"translator: {getattr(agent.translator, 'name', a.translator)}"
          + (f", looking at the world with `{idea.observe or 'the anchored programs'}`." if agent.observe else "."))
    observe_note(agent)
    print(f"The file has a {len(idea.conversation)}-turn conversation (:example replays it). :help for commands, :quit to leave.")
    typed, last_goal = [], None
    while True:
        print("you> ", end="", flush=True)
        line = stdin.readline()
        if not line:
            print()
            return 0
        line = line.strip()
        if not line:
            continue
        if line.startswith(":"):
            c, _, arg = line[1:].partition(" ")
            c = {"porque": "why", "revisar": "lint", "hechos": "facts", "reglas": "rules", "ejemplo": "example", "recargar": "reload",
                 "ayuda": "help", "salir": "quit", "q": "quit", "exit": "quit"}.get(c, c)
            if c == "quit":
                return 0
            if c == "help":
                print(HELP)
            elif c == "why":
                goal = arg.strip() or last_goal
                if not goal:
                    print("ask something first, or give a goal: :why lit(l1)")
                    continue
                r = agent._ask_reply({"kind": "why", "goal": goal}, "main")
                print(str(r))
            elif c == "facts":
                conv = agent._conversation("main")
                w = conv.pending or conv.world
                if w is None:
                    print("nothing yet")
                    continue
                print(("waiting for your yes:" if conv.pending else "loaded:"))
                for f in w.facts:
                    print("  " + agent.vocab.say(f))
                for k, (t, neg) in enumerate(w.events, 1):
                    print(f"  {k}. " + agent.vocab.say(t, negated=neg))
            elif c == "lint":
                show_lint(agent.lint, idea.path.name)
            elif c == "rules":
                for sec in ("facts", "rules", "logic", "meta"):
                    if not any(l.strip() for _, l in idea.raw[sec]):
                        continue
                    print(f"== {sec}")
                    last = None
                    for n, l in idea.raw[sec]:
                        if l.strip():
                            if sec == "meta" and idea.part_of.get(n) and idea.part_of[n] != last:
                                last = idea.part_of[n]
                                print(f"     -- {last}")
                            print(f"{n:4} {l}")
            elif c == "example":
                show_check(check(agent))
            elif c == "reload":
                try:
                    new = _agent(a)
                except IdeaError as e:
                    print(f"not reloaded: {e}")
                    continue
                agent, idea = new, new.idea
                for t in typed:
                    agent.say(t)
                print(f"reloaded {idea.path.name}; replayed {len(typed)} line(s) you typed")
            else:
                print(f"unknown command :{c}\n{HELP}")
            continue
        r = say(agent, line)
        typed.append(line)
        q = (r.data or {}).get("question")
        if isinstance(q, dict) and q.get("goal"):
            last_goal = q["goal"]


def cmd_serve(a):
    agent = _agent(a)
    at_load(agent, out=lambda m: print(m, file=sys.stderr))
    observe_note(agent, out=lambda m: print(m, file=sys.stderr))
    print(f"{agent.idea.name}: A2A on http://{a.host}:{a.port}/  (card: /.well-known/agent-card.json)  Ctrl-C to stop", flush=True)
    try:
        agent.serve(port=a.port, host=a.host, block=True)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_talk(a):
    from logitration import LogicAgent
    agent = LogicAgent.connect(a.url)
    if a.message:
        r = agent.say(" ".join(a.message), session=a.session)
        print(f"{r}\n[{r.state}]")
        return 0
    print(f"talking to {agent.name} at {a.url} (:quit to leave)")
    for line in sys.stdin:
        if line.strip() in (":quit", ":salir"):
            break
        if line.strip():
            r = agent.say(line.strip(), session=a.session)
            print("agent> " + str(r).replace("\n", "\n       "))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ALIASES:
        argv[0] = ALIASES[argv[0]]
    p = argparse.ArgumentParser(prog="logi", description="Try, check and serve an idea file (conversation + logic + meta-interpreter).")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, text in (("try", "talk to the idea in this terminal"), ("check", "replay the file's conversation"),
                       ("serve", "publish the idea over A2A")):
        s = sub.add_parser(name, help=text)
        s.add_argument("idea", help="the idea file (e.g. circuit.logi)")
        s.add_argument("--translator", default="simulated", choices=["simulated", "claude-cli", "anthropic-api"])
        s.add_argument("--model", default=None)
        s.add_argument("--observe", action="store_true", help="let the file's observe: command run (the agent looks at the world itself)")
        s.add_argument("--program", action="store_true", help="use the scripts anchored in programs/ (logi program builds them)")
        s.add_argument("--world", default=None, help="rehearsal (default: the file's pretend machines) | ssh:HOST,HOST")
        if name != "check":
            s.add_argument("--device", default=None, help="program that receives the commands of act/2 as arguments")
        if name == "serve":
            s.add_argument("--port", type=int, default=8923)
            s.add_argument("--host", default="127.0.0.1")
    s = sub.add_parser("spec", help="the specification of each script the file lacks (no model is called)")
    s.add_argument("idea")
    s.add_argument("--level", type=int, default=2, choices=[0, 1, 2], help="how much context the model gets (default 2)")
    s = sub.add_parser("program", help="a model writes the missing scripts: validated, tried, anchored in programs/")
    s.add_argument("idea")
    s.add_argument("--model", default="sonnet")
    s.add_argument("--level", type=int, default=2, choices=[0, 1, 2])
    s.add_argument("--attempts", type=int, default=3, help="model calls per script (default 3)")
    s.add_argument("--calls", type=int, default=12, help="most model calls in all (default 12)")
    s.add_argument("--force", action="store_true", help="build again scripts that are already anchored (a new version)")
    s.add_argument("--trace", default=None, help="folder for the spec, prompt, response, validation and rehearsal of every attempt")
    s = sub.add_parser("lint", help="list what the idea file leaves unsaid (no conversation needed)")
    s.add_argument("idea", help="the idea file")
    s = sub.add_parser("talk", help="talk to a served agent")
    s.add_argument("url")
    s.add_argument("message", nargs="*")
    s.add_argument("--session", default="logi-talk")
    a = p.parse_args(argv)
    try:
        return {"try": cmd_try, "check": cmd_check, "serve": cmd_serve, "talk": cmd_talk, "lint": cmd_lint,
                "spec": cmd_spec, "program": cmd_program}[a.cmd](a)
    except IdeaError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
