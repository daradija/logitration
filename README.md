# logitration
## I want to control an electric circuit: what do I write?
One file, `circuit.logi` ([examples/circuit.logi](examples/circuit.logi), 57 lines with comments). In this order:

**1. Conversation.** How a client talks to the agent and what it must answer. It is also your test.
```
== 1. conversation
client: mains is a source. l1 is a lamp. sw is a switch. f1 is a fuse. l1 gets current from f1 if sw is up
agent:  Is sw up or down?
client: sw is down ... yes ... make l1 lit
agent:  Done: sw is up (command: relay sw up).
```

**2. Facts** (the words of what the client tells, or the agent sees) and **3. Rules** (what follows; clients ask these):
```
== 2. facts
lamp(X)     = {X} is a lamp
state(X, V) = {X} is {V} | raise {X}{V=up} | {X} blows{V=blown}   [changes; V: ok up down blown]
== 3. rules
lit(L) = {L} is lit
lit(L) :- lamp(L), \+ state(L, blown), current(L).
```

**4. Meta-interpreters** (check: `explain(G, Words)`; execute; complete; all optional):
```
== 4. meta-interpreters
-- execute
can_change(state(S, _)) :- switch(S).          % switches yes; fuses only by hand
act(state(S, V), [relay, S, V]).
-- complete
missing(state(S, _)) :- switch(S), \+ state(S, _).    % ask(...) says how to ask it
# try, check, serve:
logi try circuit.logi   |   logi check circuit.logi (8/8 turns as written)   |   logi serve circuit.logi --port 8923 (an A2A agent)
```
## I want to control something to answer an LLM's queries: what do I write?
The same four parts. [examples/cluster.logi](examples/cluster.logi): a cluster that runs language-model jobs. **The client does
not tell the state: the agent looks at the cluster** (`observe:`) and the client only asks (where can j1 run, which node has the
most ram) and finally asks for a concrete instance. If it asks something not understood, the agent teaches how to ask.
```
client: where can j1 run?                      # a variable in a parameter: runnable(j1, N)
agent:  j1 is runnable on n3.
client: which node has the most ram?           # max over the facts
agent:  n3 has the most ram.
client: where can j3 run?
agent:  To answer that I need to know: How many GB of ram does j3 need to run?     # complete
client: get j1 running on n3                   # a concrete instance: execute
agent:  Done: j1 is running on n3 (command: launch j1 n3).
== 2. facts
capacity(N:name, R:ram|disk, G:int>=0) = {N} has {G} GB of {R}   # typed arguments; what every resource shares: nodes HAVE, jobs NEED
demand(J:name, R:ram|disk, G:int>=0)   = {J} needs {G} GB of {R}
observe: python3 observe_cluster.py            # read-only; prints one fact per line, in these words
== 3. rules
runnable(J, N) = {J} is runnable on {N} | where can {J} run{N=?}        # {N=?}: the client asks for N
most(N, R) :- aggregate_all(max(C, M), capacity(M, R, C), max(_, N)).
# try, serve:
logi try cluster.logi --observe         # --observe lets the file's observe: command run
logi serve cluster.logi --observe --translator claude-cli --device ./launcher   # ./launcher gets "launch j1 n3"
```
## Types in the arguments, and facts that a program computes (inspired by Clorm)
*Inspired by Clorm* (typed predicates for ASP): the arguments of a predicate may carry a type, written in its signature: `name`, `int>=0` (`int>0<=100`),
`ram|disk|gpu`, `any`; `type resource = ram|disk|gpu` names one to reuse (`R:resource`). A fact the client states (or a program reads) whose argument does not fit
is **refused before it reaches the rules**, saying which argument and what it must be, in the client's language when a model phrases it; the same for the constants
of a question. `logi lint` finds a constant of the wrong type in a fact or rule, and one variable that two places want of types no value shares. The engine is not touched.
Of the 8 badly formed facts of the study (`tests/out/medicion_tipos.txt`) the engine refuses 5 with no options, 7 with `[R: ram disk]`, 8 with types.

A fact can also be **computed by a program** (a script that looks at the machines, written by a model, see below). Say so in its phrase; `logi lint` and `logi spec` then tell,
for every fact, who provides it: written in the file, stated by the client, printed by `observe:`, or computed by a program.
```
mem_free(N:name, MB:int>=0) = {N} has {MB} MB free     [program via vm_stat]    # examples/memoria.logi: free RAM read by ssh (pretend machines here)
runnable(J, N) :- need(J, Need), mem_free(N, Free), Free >= Need.               # logi check memoria.logi --program   (--world ssh:HOST: real ones)
```
## The three layers and the four meta-interpreters
**Facts** (what is there), **rules** (what follows) and **meta-interpreters** (what runs *on* facts and rules) are the sections `== 2. facts`, `== 3. rules` and `== 4. meta-interpreters`, one subsection each:

| meta-interpreter | does | you may write | if you write nothing |
|---|---|---|---|
| `-- check` | decides and explains: is, which, why, why not, change, timeline | `explain(G, Words)`: the reason for G | shows the proof, in your words |
| `-- execute` | acts: "make G" changes what is allowed, the rules confirm, the device is told | `can_change(F)`: what it may change; `act(F, Command)` | may change any fact that `changes`; sends nothing |
| `-- complete` | finds what is missing, asks, teaches how to ask, looks at the world | `missing(F)`: what it needs; `ask(F, Words)`: how to ask | asks nothing; teaches from your phrases |
| `-- program` | **writes the script that is missing** (see below) | `observe pred(Args) via cmd`, `act name via cmd`, `rehearse m1: ...` | no scripts: `observe:` and the device stay yours |

`Words` is an atom or a list joined by spaces. The older layout (`== logic` and one `== meta-interpreter`) still works.
**Do I have to write the first three? No.** Without them the agent loads, answers and explains (the proof). What you lose
([tests/out/experimento_sin_bloque3_A.txt](tests/out/experimento_sin_bloque3_A.txt)): it asks nothing before loading, sends no command to
the device, may change **anything** marked `changes`, and explains with the proof, not with your words. A language model can draft
them from words and rules, but not what the device understands nor what may change ([_B](tests/out/experimento_sin_bloque3_B.txt)).
## The agent lacks a script: `-- program`
[examples/modelos.logi](examples/modelos.logi) (34 lines; also [traffic_light](examples/traffic_light.logi), [shop](examples/shop.logi)): "which models are on m1?", "is llama3 on m1?", "get llama3 installed on m1". The scripts
that look at the machines and install are **not** in the file. The `program` meta-interpreter builds the *specification* of each one
from what you wrote (your phrases, the names in the conversation, whether it reads or changes the world), adds the header (the
signature: the decisive piece), asks Sonnet for the Python, **validates** it (AST: only `re`, `json`, one door `world.run`, only the
commands you declared `via`), **tries** it against pretend machines with cases from your conversation, and on an error asks again with
the error (3 calls at most, then a report of what is missing). What passes is **anchored** in `programs/` (version, reason, specification).
Variables in the parameters serve as query and as action: `installed(m1, X)` lists, `installed(m1, llama3)` checks, `install(m1, llama3)` acts.

```
-- program
observe installed(M, X)    via ollama      # a script that LOOKS; M, X given = check, free = list
act install                via ollama      # the script behind act(installed(M, X), [install, M, X])
rehearse m1: mistral qwen2                 # pretend machines to try them on
# commands:
logi spec modelos.logi      # the specifications (no model, nothing written): read them first
logi program modelos.logi   # spec -> prompt -> model -> validate -> try -> (error -> again) -> anchor; traces in programs/trace/
logi check modelos.logi --program    # the file's conversation, now with its scripts (4/4)
```
Measured ([tests/out/estudio_programas.txt](tests/out/estudio_programas.txt): Sonnet, 9 script tasks, mock world, 1 sample per cell): right first
time 0/9 with the name only, 5/9 + signature, 8/9 + your words and conversation; 9/9 after retries at levels 1 and 2. Small n, one author: see the
limits there. Never run on real machines yet (`--world ssh:HOST`).
## The file
Before the first section: `name:` and `about:`. Lines starting with `#` are comments. `== N. name` starts a section (the number is free).

* **conversation**: `client:` lines, each followed by `agent:` lines that must appear in the reply (a part of a line is enough).
* **facts** and **rules**: word lines `pred(Args) = template | template ... [options]` (no final dot). The first template is how the
  agent speaks; all are understood. `{V=up}` fixes a value without words; `{V=?}` makes it a question for V. Options: `changes`
  (the last argument is the value: `state(sw, up)` replaces `state(sw, down)`), `set` (simply there or not, like `installed(m1, llama3)`:
  all arguments are its identity, several are true at once, "make" adds it), `V: a b c` (the only values), `not: ...`; `program via CMD` (a script computes it). Every other line
  is Prolog (`aggregate_all(count|sum|max|min|bag|set, Goal, R)` included). A predicate with rules is something clients **ask**; one with
  words and no rules, something they **tell**.
* **observe: COMMAND** (in facts): run before answering (no shell, in the file's folder); one fact per line, in your words or as Prolog.
  Only with `--observe` / `load(..., observe=True)`. **meta-interpreters**: `-- check`, `-- execute`, `-- complete`, `-- program` (lint warns of the wrong one).
## logi
You write the idea; the library does the rest (the model that only translates, the checks, A2A). One file, four parts, one command
to try it. Install: `pip install -e .` (Python 3.9+, no dependencies; editable mode needs pip 21.3 or newer — with the pip 21.2.4 that ships with Python 3.9 on macOS, run `python3 -m pip install --upgrade pip` first, or use `pip install .`).

| command | does |
|---|---|
| `logi try IDEA` | conversation in the terminal. `:why [goal]` `:facts` `:rules` `:example` `:lint` `:reload` `:quit` |
| `logi lint IDEA` / `logi check IDEA` | what the file leaves unsaid (with the line) / replays its conversation, exit 1 if a turn does not answer as written |
| `logi serve IDEA --port N` | A2A: card at `/.well-known/agent-card.json`, JSON-RPC `message/send` at `/`. `logi talk URL [message]` talks to one |
| `logi spec IDEA` / `logi program IDEA` | the specification of each missing script (no model) / a model writes them, they are validated, tried and anchored (`--level`, `--calls`, `--force`, `--trace`) |

Aliases `probar`, `comprobar`, `servir`, `hablar`, `especificar`, `programar`. Options: `--translator simulated|claude-cli|anthropic-api`
(default `simulated`: no model), `--model`, `--device PROGRAM` (each command runs as `PROGRAM word ...`, no shell), `--observe`,
`--program` (use the anchored scripts as observe and device, on the file's pretend machines or `--world ssh:HOST,HOST`).
## From Python, and tests
`load("cluster.logi", translator=..., device=callable, observe=True, program=False)` returns a `LogicAgent` (`say()`, `ask(kind, goal)`, `serve()`,
`LogicAgent.connect(url)`); `LogicAgent(rules, phrases, ...)` builds one from a `.pl` and a dict of words ([examples/python_api/](examples/python_api/)).
Tests, no network, no model: `python tests/test_library.py` (103), `test_ideas.py` (93), `test_design.py` (69), `test_program.py` (77: specification,
validator, rehearsal, retry loop with a recorded model, anchoring, `logi spec`/`program`), `test_types.py` (53: types, `[program]`, lint). `tests/study_programs.py` is the study (calls the model).

Limits: nothing a client writes is run as Prolog. The meta-interpreters are these predicates, not your own `solve/1`. "make" takes the allowed
way with fewest changes. `check` compares text: with an LLM wording may differ. `observe:` and `programs/` are the file author's code: trust the file.
## License
Logitration is licensed under the **Business Source License 1.1** (see [LICENSE](LICENSE)). You may read, copy, modify and share it; use in production is free for
individuals, research and teaching, non-profits, and organizations under 1,000,000 EUR annual revenue; evaluation and development are free for everyone. Each version
becomes AGPL 3 four years after its release. Other production use needs a commercial license: contact contact@logitration.com. Contributions: [CONTRIBUTING.md](CONTRIBUTING.md).
Contact address: contact@logitration.com.
