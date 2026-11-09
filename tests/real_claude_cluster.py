"""Real LLM (claude-cli) talks to the new examples/cluster.logi: the client asks in its own words, the agent looks at the
cluster. At most 12 calls to the model. Writes tests/out/real_claude_cluster.txt. Run: python tests/real_claude_cluster.py"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.dont_write_bytecode = True
from logitration import load  # noqa: E402

TURNS = ["What can I ask you?",
         "Where could job j1 be executed?",
         "Which machine has the most memory?",
         "Where can j3 run?",
         "j3 needs 4 GB of memory",
         "yes",
         "Please start j1 on node n3."]
MAX_CALLS = 12
agent = load(HERE.parent / "examples" / "cluster.logi", translator="claude-cli", observe=True,
             device=lambda cmd: "(device not connected: would run `%s`)" % cmd)
tr = agent.translator
log = []
for t in TURNS:
    if len(tr.log) >= MAX_CALLS:
        log.append(f"(stopped: {MAX_CALLS} calls used)")
        break
    r = agent.say(t, session="real")
    log.append(f"client> {t}\nagent>  " + str(r).replace("\n", "\n        ") + f"\n        [{r.state}; calls so far {len(tr.log)}]")
log.append(f"model calls: {len(tr.log)} ({sum(1 for x in tr.log if x['ok'])} ok); model: {tr.model}")
text = "\n".join(log)
(HERE / "out" / "real_claude_cluster.txt").write_text(text + "\n")
print(text)
