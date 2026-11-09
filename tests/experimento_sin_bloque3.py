"""Experimento 1: ¿hace falta el bloque 3 (metaintérprete)?
Parte A (sin LLM): quita el bloque 3 de circuit/shop/cluster (copias ANTERIORES al rediseño) y repite sus conversaciones.
Parte B (claude-cli, <=15 llamadas): ¿el LLM propone el bloque 3 a partir de las palabras y las reglas? ¿explica solo?
Uso: python3 experimento_sin_bloque3.py A | B   (escribe tests/out/experimento_sin_bloque3_{A,B}.txt)"""
import json, re, subprocess, sys, tempfile
from pathlib import Path

AQUI = Path(__file__).resolve().parent
ANTES = AQUI.parents[1] / "curso_previo_20261107_diseno" / "libreria_antes"
sys.path.insert(0, str(AQUI.parent / "src"))
sys.dont_write_bytecode = True
from logitration import load, idea as I          # noqa: E402
from logitration.idea import check               # noqa: E402

EJ = ["circuit", "shop", "cluster"]
OUT = AQUI / "out"


def sin_bloque3(texto):
    return re.split(r"\n== 3\.", texto)[0].rstrip() + "\n"


def con_bloque3(texto, bloque):
    return sin_bloque3(texto) + "\n== 3. meta-interpreter\n" + bloque.strip() + "\n"


def correr(texto, nombre):
    d = Path(tempfile.mkdtemp(prefix="exp_b3_")) / f"{nombre}.logi"
    d.write_text(texto)
    a = load(str(d))
    res = check(a)
    return res, a


def parte_a():
    log = []
    tot = {}
    for e in EJ:
        txt = (ANTES / "examples" / f"{e}.logi").read_text()
        r0, _ = correr(txt, e)
        r1, a1 = correr(sin_bloque3(txt), e)
        ok0 = sum(1 for x in r0 if not x[3]); ok1 = sum(1 for x in r1 if not x[3])
        tot[e] = (ok0, ok1, len(r0))
        log.append(f"=== {e}: con bloque 3 {ok0}/{len(r0)} turnos como se escribieron; SIN bloque 3 {ok1}/{len(r1)}")
        for (c, exp, rep, falta, n), (_, _, rep0, _, _) in zip(r1, r0):
            if falta or rep != rep0:
                log.append(f"  línea {n} cliente: {c}")
                log.append(f"    CON   : {rep0!s}".replace("\n", "\n            "))
                log.append(f"    SIN   : {rep!s}".replace("\n", "\n            "))
    (OUT / "experimento_sin_bloque3_A.txt").write_text("\n".join(log) + "\n")
    print("\n".join(log))
    print(tot)


LIBRODOC = """The agent reads a "meta-interpreter" block of Prolog with five OPTIONAL predicates (Words = an atom or a list of atoms/variables joined by spaces):
  missing(F)        the agent asks the client for fact F before loading what the client described
  ask(F, Words)     how to ask for F
  can_change(F)     facts the agent may change by itself when the client says "make G"/"get G"
  act(F, Command)   Command (a list of words) sent to the device when the agent changes F
  explain(G, Words) the reason to say for G instead of G's proof
If you write nothing: missing = asks nothing; ask = the phrase with a '?'; can_change = any fact marked [changes]; act = nothing is sent; explain = the proof of G, in the words of the file."""


def claude(system, prompt):
    r = subprocess.run(["claude", "-p", "--model", "claude-sonnet-5-5", "--tools", "", "--no-session-persistence",
                        "--disable-slash-commands", "--setting-sources", "", "--strict-mcp-config", "--system-prompt", system],
                       input=prompt, capture_output=True, text=True, timeout=180, cwd=tempfile.mkdtemp())
    return r.stdout.strip()


def parte_b():
    log, n = [], 0
    sistema = ("You write the 'meta-interpreter' block of a .logi file. Reply ONLY with the Prolog lines of that block (no explanations, no fences). "
               "Use comments with % when useful.\n" + LIBRODOC)
    for e in EJ:
        txt = (ANTES / "examples" / f"{e}.logi").read_text()
        base = sin_bloque3(txt)
        conv = re.search(r"== 1\. conversation\n(.*?)\n== 2\.", txt, re.S).group(1)
        logica = txt.split("== 2. logic")[1].split("== 3.")[0]
        words_rules = f"name/about: {e}\n== 2. logic{logica}"
        for cond, prompt in (("A palabras+reglas", "Here is the logic of the file (words and rules):\n\n" + words_rules +
                                "\n\nWrite the meta-interpreter block (section 3)."),
                             ("B palabras+reglas+conversación", "Here is the logic of the file (words and rules):\n\n" + words_rules +
                                "\n\nAnd the conversation the file must support (agent lines = what the agent's reply must contain):\n\n" + conv +
                                "\n\nWrite the meta-interpreter block (section 3) so that the conversation works.")):
            out = claude(sistema, prompt); n += 1
            out = re.sub(r"^```\w*\n|\n```$", "", out.strip())
            try:
                res, a = correr(con_bloque3(txt, out), e)
                ok = sum(1 for x in res if not x[3])
                fallos = [(c, rep) for c, exp, rep, falta, ln in res if falta]
                msg = f"{ok}/{len(res)} turnos"
            except Exception as ex:
                ok, fallos, msg = -1, [], f"NO CARGA: {str(ex)[:160]}"
            log.append(f"=== {e} · {cond} · llamada {n}: {msg}\n--- bloque propuesto por el LLM:\n{out}\n--- turnos que fallan:")
            for c, rep in fallos:
                log.append(f"  cliente: {c}\n    dijo: {str(rep)[:240]}")
    # el LLM explica solo (sin metaintérprete ni motor): palabras + reglas + hechos, pregunta por qué no
    pregs = {"circuit": ("mains is a source. l1 is a lamp. sw is a switch. f1 is a fuse. f1 gets current from mains if f1 is ok. l1 gets current from f1 if sw is up. sw is down.", "why is l1 not lit?"),
             "shop": ("o2 is an order of cable. lamp is in stock. o2 is paid. cable is not in stock. o2 is not packed.", "why is o2 not ready to ship?"),
             "cluster": ("n2 is a node with 64 GB of ram and 500 GB of disk. n3 is a node with 256 GB of ram and 2000 GB of disk. j1 is a job. j1 needs 120 GB of ram and 300 GB of disk.", "where can j1 run, and why not on n2?")}
    for e, (hechos, preg) in pregs.items():
        txt = (ANTES / "examples" / f"{e}.logi").read_text()
        logica = txt.split("== 2. logic")[1].split("== 3.")[0]
        out = claude("You answer questions about a small system using ONLY the dictionary (how each predicate is said in words) and the Prolog rules you are given. Explain the reason in plain language, 2-4 sentences. Do not invent anything.",
                     f"Dictionary and rules:\n{logica}\n\nFacts told by the client: {hechos}\n\nQuestion: {preg}"); n += 1
        log.append(f"=== {e} · el LLM explica SOLO (llamada {n}) · pregunta: {preg}\n{out}")
    log.append(f"(llamadas a claude-cli: {n})")
    (OUT / "experimento_sin_bloque3_B.txt").write_text("\n".join(log) + "\n")
    print("\n".join(log))


if __name__ == "__main__":
    {"A": parte_a, "B": parte_b}[sys.argv[1]]()
