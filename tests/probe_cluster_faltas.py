"""What the library does when something is missing from tests/fixtures/cluster_v1.logi (the first cluster example) (real runs, no network, no LLM).
Each case edits one thing of the file, replays the same client turns with `logi try` (stdin) and records what the agent
answered; a run that does not end in 60 s is reported as a hang.
Since 07-11 each case also runs `logi lint` on the edited file and records what it says (errors and warnings only).
Run: python tests/probe_cluster_faltas.py      (writes tests/out/cluster_faltas.json and prints one line per case)
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SRC = (ROOT / "tests" / "fixtures" / "cluster_v1.logi").read_text()
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
FACTS = ["n1 is a node. n1 has 16 GB of ram. n1 has 100 GB of disk. n2 is a node. n2 has 64 GB of ram. n2 has 500 GB of disk",
         "n3 is a node. n3 has 256 GB of ram. n3 has 2000 GB of disk. j1 is a job. j1 needs 120 GB of ram. j1 needs 300 GB of disk",
         "j2 is a job. j2 needs 8 GB of ram. j2 needs 20 GB of disk. j3 is a job. j3 needs 1500 GB of disk"]
ASK3 = "j3 needs 4 GB of ram"


def drop(text, line):
    assert line in text, line
    return text.replace(line + "\n", "", 1)


# (id, what is removed, edit(text) -> text, client turns after loading)
CASES = [
    ("hecho_ram", "un hecho: «n2 has 64 GB of ram» (el cliente no lo dijo)",
     lambda t: t, [FACTS[0].replace(" n2 has 64 GB of ram.", ""), FACTS[1], FACTS[2], ASK3, "yes", "is j1 runnable on n2?"]),
    ("hecho_nodo", "un nodo desconocido: «is j1 runnable on n9?» (n9 nunca se nombró)",
     lambda t: t, [*FACTS, ASK3, "yes", "is j1 runnable on n9?"]),
    ("trabajo_desconocido", "un trabajo desconocido: «get j9 running on n3»",
     lambda t: t, [*FACTS, ASK3, "yes", "get j9 running on n3"]),
    ("regla_disco", "una regla: short_disk(J, N) :- ...",
     lambda t: drop(t, "short_disk(J, N) :- need_disk(J, D), disk(N, K), K < D."), [*FACTS, ASK3, "yes", "is j3 runnable on n1?"]),
    ("regla_recursiva", "una regla mala: runnable(J, N) :- runnable(J, N). (bucle)",
     lambda t: t.replace("runnable(J, N) :- job(J)", "runnable(J, N) :- runnable(J, N).\nrunnable(J, N) :- job(J)"),
     [*FACTS, ASK3, "yes", "is j1 runnable on n1?"]),
    ("frase_pred", "una frase: la línea de palabras de short_ram",
     lambda t: drop(t, "short_ram(J, N) = {N} lacks the ram of {J}"), [*FACTS, ASK3, "yes", "why is j1 too big for n2?"]),
    ("meta_missing", "el bloque meta: missing y ask",
     lambda t: drop(drop(t, "missing(need_ram(J, _)) :- job(J), \\+ need_ram(J, _)."),
                    "ask(need_ram(J, _), ['How many GB of ram does', J, 'need to run?'])."),
     [FACTS[0], FACTS[1], FACTS[2], "yes", "is j3 runnable on n1?"]),
    ("meta_can_change", "el bloque meta: can_change (queda el valor por defecto)",
     lambda t: drop(t, "can_change(runs(J, N)) :- runnable(J, N)."), [*FACTS, ASK3, "yes", "get j1 running on n2"]),
    ("meta_act", "el bloque meta: act (no hay orden real)",
     lambda t: drop(t, "act(runs(J, N), [launch, J, N])."), [*FACTS, ASK3, "yes", "get j1 running on n3"]),
    ("meta_sintaxis", "el bloque meta con un error de sintaxis (falta el punto de act)",
     lambda t: t.replace("act(runs(J, N), [launch, J, N]).", "act(runs(J, N), [launch, J, N])"), [*FACTS, ASK3, "yes", "get j1 running on n3"]),
    ("meta_variable_libre", "una orden con nodo sin nombrar: running(J) :- runs(J, _). y «get j1 running»",
     lambda t: t.replace("runnable(J, N)  =", "running(J)      = {J} is running\nrunnable(J, N)  =").replace("== 3.", "running(J) :- runs(J, _).\n\n== 3."),
     [*FACTS, ASK3, "yes", "get j1 running"]),
    ("pred_meta_inventado", "el bloque meta con un predicado que no existe: retry(runs(J, N)).",
     lambda t: t + "retry(runs(J, N)).\n", [*FACTS, ASK3, "yes", "get j1 running on n3"]),
]


def lint_of(text):
    d = Path(tempfile.mkdtemp(prefix="cluster_lint_"))
    p = d / "cluster.logi"
    p.write_text(text)
    r = subprocess.run([sys.executable, "-m", "logitration", "lint", str(p)], capture_output=True, text=True, env=ENV, timeout=60, cwd=str(ROOT))
    return [l.strip() for l in (r.stdout + r.stderr).splitlines() if l.strip().startswith(("error", "warning")) or l.startswith("lint ")][:6]


def run(text, turns):
    d = Path(tempfile.mkdtemp(prefix="cluster_falta_"))
    p = d / "cluster.logi"
    p.write_text(text)
    try:
        r = subprocess.run([sys.executable, "-m", "logitration", "try", str(p)], input="".join(t + "\n" for t in turns),
                           capture_output=True, text=True, env=ENV, timeout=60, cwd=str(ROOT))
        return "exit %d" % r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired as e:
        return "HANG (60 s)", (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or ""), ""


def main():
    out = []
    for cid, what, edit, turns in CASES:
        status, so, se = run(edit(SRC), turns)
        last = [l for l in (so + se).strip().splitlines() if l.strip()]
        lint = lint_of(edit(SRC))
        out.append({"id": cid, "quita": what, "estado": status, "ultimas": last[-4:], "stderr": se.strip().splitlines()[-2:], "lint": lint})
        print(f"{cid:20s} {status:12s} " + " | ".join(x.strip() for x in last[-3:])[:260], flush=True)
        print(f"{'':20s} lint: " + " | ".join(lint)[:300], flush=True)
    (HERE / "out" / "cluster_faltas.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
