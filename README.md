# logitration

**Control agents with logic.** You write your idea in one file, `.logi`: a sample conversation, the facts, the rules. Logitration turns it into an
agent that people and other agents can talk to. A small Prolog engine decides every answer and explains it with its proof; the language model only
translates between the client's words and facts, and puts the conclusion back into words. Website, concept and status: **https://logitration.com**

## Principles
* **Logic decides, the model translates.** Rules and facts settle what is true and why (same input, same conclusion). The language model
  writes no Prolog and decides nothing; nothing a client writes is ever run as code. Every conclusion carries the rule and the facts behind it.
* **The agent does not invent what it does not know.** It asks the client, observes the world (`observe:`), or computes the fact with a program. Before loading
  anything, it repeats in words what it understood, and only what the client confirms is loaded.
* **The agent is an A2A agent.** `logi serve` publishes it with the [A2A](https://a2a-protocol.org) protocol: its card is at `/.well-known/agent-card.json`, other agents call it
  with JSON-RPC `message/send`, in natural language. Inside, Prolog stays invisible.
* **One `.logi` file with your idea.** Conversation (your specification and your test), facts, rules, and optional meta-interpreters (check, execute, complete, program).
  Nobody writes a parser, a prompt or a server.
* **The system teaches how to ask.** If a client says something it does not understand, the agent answers with how to say it, using your own phrases.
* **Zero dependencies.** The Prolog engine is inside the library (Python 3.9+, standard library only).
* **What it is not:** not a prompt framework and not a chatbot with tools. The model never has the last word; the rules do.

## Learn more
* **[logitration.com](https://logitration.com)**: the concept, how it works (a section of the home page), [Get started](https://logitration.com/get-started/),
  [Evidence](https://logitration.com/evidence/) (what has been measured, with its limits), [Roadmap](https://logitration.com/roadmap/), [License](https://logitration.com/license/).

## Quick start
```
git clone https://github.com/daradija/logitration && cd logitration
python3 -m pip install --upgrade pip && pip install -e .        # Python 3.9+, no dependencies
logi try examples/circuit.logi            # talk to the agent in the terminal (no model needed: simulated translator)
logi check examples/circuit.logi          # replay the file's conversation; exit 1 if a turn does not answer as written
logi serve examples/circuit.logi --port 8923      # A2A agent: card at http://localhost:8923/.well-known/agent-card.json
```

## Examples
### I want to control an electric circuit: what do I write?
One file, `circuit.logi` ([examples/circuit.logi](examples/circuit.logi), 57 lines with comments). In this order:

**1. Conversation.** How a client talks to the agent and what it must answer. It is also your test.
```
== 1. conversation
client: mains is a source. l1 is a lamp. sw is a switch. f1 is a fuse. l1 gets current from f1 if sw is up
agent:  Is sw up or down?
client: sw is down ... yes ... make l1 lit
agent:  Done: sw is up (command: relay sw up).
```
**2. Facts** (what the client tells, or the agent sees) and **3. Rules** (what follows; clients ask these):
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
```

### I want to control something to answer an LLM's queries: what do I write?
The same four parts. [examples/cluster.logi](examples/cluster.logi): a cluster that runs language-model jobs. **The client does not tell the state: the agent
looks at the cluster** (`observe:`) and the client only asks, and finally asks for a concrete instance. If it asks something not understood, the agent teaches how to ask.
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
capacity(N:name, R:ram|disk, G:int>=0) = {N} has {G} GB of {R}   # typed arguments
observe: python3 observe_cluster.py            # read-only; prints one fact per line, in these words
== 3. rules
runnable(J, N) = {J} is runnable on {N} | where can {J} run{N=?}        # {N=?}: the client asks for N
most(N, R) :- aggregate_all(max(C, M), capacity(M, R, C), max(_, N)).
```
```
logi try examples/cluster.logi --observe
logi serve examples/cluster.logi --observe --translator claude-cli --device ./launcher   # ./launcher gets "launch j1 n3"
```

## The three layers and the four meta-interpreters
**Facts** (what is there), **rules** (what follows) and **meta-interpreters** (what runs *on* facts and rules): sections `== 2. facts`, `== 3. rules`, `== 4. meta-interpreters`.

| meta-interpreter | does | you may write | if you write nothing |
|---|---|---|---|
| `-- check` | decides and explains: is, which, why, why not, change, timeline | `explain(G, Words)` | shows the proof, in your words |
| `-- execute` | acts: "make G" changes what is allowed, the rules confirm, the device is told | `can_change(F)`; `act(F, Command)` | may change any fact that `changes`; sends nothing |
| `-- complete` | finds what is missing, asks, teaches how to ask, looks at the world | `missing(F)`; `ask(F, Words)` | asks nothing; teaches from your phrases |
| `-- program` | **writes the script that is missing**: validated, tried on pretend machines, anchored | `observe pred(Args) via cmd`, `act name via cmd`, `rehearse m1: ...` | no scripts: `observe:` and the device stay yours |

Without the first three the agent still loads, answers and explains, but asks nothing before loading, sends no command and may change anything marked `changes`
([tests/out/experimento_sin_bloque3_A.txt](tests/out/experimento_sin_bloque3_A.txt)). Programs: [examples/modelos.logi](examples/modelos.logi); `logi spec` shows each script's specification (no model),
`logi program` has a model write, validate, try and anchor them. Measured on 9 script tasks ([tests/out/estudio_programas.txt](tests/out/estudio_programas.txt); small n, one author): right first time 0/9 with the name only,
8/9 with signature plus your words; never run on real machines yet.

## Types, and facts that a program computes (inspired by Clorm)
*Inspired by Clorm* (typed predicates for ASP): an argument may carry a type in the signature: `name`, `int>=0` (`int>0<=100`), `ram|disk|gpu`, `any`; `type resource = ram|disk|gpu` names one to reuse.
A fact whose argument does not fit is **refused before it reaches the rules**, saying which argument and what it must be. `logi lint` finds a constant of the wrong type. The engine is not touched.
A fact can also be **computed by a program**; `logi lint` and `logi spec` tell, for every fact, who provides it (the file, the client, `observe:`, or a program):
```
mem_free(N:name, MB:int>=0) = {N} has {MB} MB free     [program via vm_stat]    # examples/memoria.logi
runnable(J, N) :- need(J, Need), mem_free(N, Free), Free >= Need.               # logi check examples/memoria.logi --program
```

## The file, and `logi`
Before the first section: `name:` and `about:`. `#` starts a comment. `== N. name` starts a section. **conversation**: `client:` lines each followed by `agent:` lines that must appear in the reply.
**facts** and **rules**: `pred(Args) = template | template ... [options]`; the first template is how the agent speaks, all are understood. `{V=up}` fixes a value, `{V=?}` makes it a question. Options: `changes`, `set`, `V: a b c`, `not: ...`, `program via CMD`.
Every other line is Prolog. A predicate with rules is something clients **ask**; one with words and no rules, something they **tell**.

| command | does |
|---|---|
| `logi try IDEA` | conversation in the terminal. `:why [goal]` `:facts` `:rules` `:example` `:lint` `:reload` `:quit` |
| `logi lint IDEA` / `logi check IDEA` | what the file leaves unsaid / replays its conversation |
| `logi serve IDEA --port N` | A2A agent. `logi talk URL [message]` talks to one |
| `logi spec IDEA` / `logi program IDEA` | the specification of each missing script / a model writes them |

Options: `--translator simulated|claude-cli|anthropic-api` (default `simulated`: no model), `--model`, `--device PROGRAM`, `--observe`, `--program`.
From Python: `load("examples/cluster.logi", translator=..., device=callable, observe=True)` returns a `LogicAgent` ([examples/python_api/](examples/python_api/)).
Tests, no network, no model: `python tests/test_library.py` (103), `test_ideas.py` (93), `test_design.py` (69), `test_program.py` (77), `test_types.py` (53).
Limits: `check` compares text, so with an LLM the wording may differ; `observe:` and `programs/` are the file author's code: trust the file.

## License
Logitration is licensed under the **Business Source License 1.1** (see [LICENSE](LICENSE)). You may read, copy, modify and share it; use in production is free for
individuals, research and teaching, non-profits, and organizations under 1,000,000 EUR annual revenue; evaluation and development are free for everyone. Each version
becomes AGPL 3 four years after its release. Other production use needs a commercial license: contact contact@logitration.com. Contributions: [CONTRIBUTING.md](CONTRIBUTING.md).
Contact address: contact@logitration.com.
