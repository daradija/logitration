"""Study: how many scripts does the `-- program` loop get right, with how much context? (07-11, MAQUETA: a pretend world, a recorded task list
written by the same author as the validator and the oracle.) Nine script tasks over a pretend cluster, three levels of context (0 name,
1 + signature, 2 + the user's words and conversation), at most 3 attempts per cell, at most 58 model calls in all (the first attempts of
every cell first, then the retries in rounds, until the calls run out). Real model: claude CLI, alias `sonnet`.

    python tests/study_programs.py            runs it (calls the model: ~55 calls) and writes tests/out/estudio_programas.{jsonl,json,txt}
    python tests/study_programs.py --report   rebuilds the .txt and .json from the .jsonl, no calls
    python tests/study_programs.py --rescore  re-scores the saved answers with the current rehearsal world (no calls), keeps the originals
"""
import json
import math
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.dont_write_bytecode = True

from logitration import programmer as P            # noqa: E402
from logitration.rehearsal import FakeWorld        # noqa: E402

OUT = HERE / "out"
MAX_CALLS, ATTEMPTS = 58, 3
CONF1 = "# app config\nport = 8080\nworkers=4\n\nlog_level = info\n  mode =  fast\n"
CONF2 = "port=9090\n# nothing else here\n"
STATE = {"m1": {"ollama": ["mistral", "qwen2"], "brew": ["jq", "wget"], "files": {"/etc/app.conf": CONF1}, "ports": [22, 8080, 11434],
                "services": {"nginx": True, "redis": False, "cron": True}},
         "m2": {"ollama": ["phi3"], "brew": ["htop"], "files": {"/etc/app.conf": CONF2}, "ports": [22], "services": {"cron": True, "ollama": False}}}


def world():
    return FakeWorld(STATE)


def conf(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def inv(tool):
    return lambda w: {(m, x) for m in w.machines() for x in w.items(m, tool)}


def tasks():
    T = P.Task
    return [
        T("observe", "installed", ["M", "X"], ["{X} is installed on {M}"], ["ollama"], rows=inv("ollama"),
          consts={"M": ["m1", "m2"], "X": ["mistral", "qwen2", "llama3"]},
          examples=[("which models are on m1?", ["mistral is on m1"]), ("is llama3 on m1?", ["No: llama3 is not on m1"])]),
        T("act", "install", ["M", "X"], ["{X} is installed on {M}"], ["ollama"], effect=lambda w, a: a[1] in w.items(a[0], "ollama"),
          consts={"M": ["m1", "m2"], "X": ["llama3", "mistral"]}, bad=[("m1", "nosuchmodel-xyz")],
          examples=[("get llama3 installed on m1", ["Done: llama3 is installed on m1"])]),
        T("act", "uninstall", ["M", "X"], ["{X} is removed from {M}"], ["ollama"], effect=lambda w, a: a[1] not in w.items(a[0], "ollama"),
          consts={"M": ["m1"], "X": ["mistral"]}, bad=[("m1", "llama3")], twice=False,
          examples=[("get mistral removed from m1", ["Done: mistral is removed from m1"])]),
        T("observe", "formulas", ["M", "P"], ["{P} is a brew package on {M}"], ["brew"], rows=inv("brew"),
          consts={"M": ["m1", "m2"], "P": ["jq", "htop", "tree"]},
          examples=[("which brew packages are on m1?", ["jq is a brew package on m1", "wget is a brew package on m1"])]),
        T("act", "brew_install", ["M", "P"], ["{P} is a brew package on {M}"], ["brew"], effect=lambda w, a: a[1] in w.items(a[0], "brew"),
          consts={"M": ["m1", "m2"], "P": ["tree", "jq"]}, bad=[("m2", "nosuchformula-xyz")],
          examples=[("get tree installed with brew on m2", ["Done: tree is a brew package on m2"])]),
        T("observe", "setting", ["M", "K", "V"], ["{K} is set to {V} on {M}"], ["cat"],
          rows=lambda w: {(m, k, v) for m in w.machines() for k, v in conf(w.state[m]["files"].get("/etc/app.conf", "")).items()},
          consts={"M": ["m1", "m2"], "K": ["port", "mode"], "V": ["8080", "fast"]},
          examples=[("what is port set to on m1?", ["port is set to 8080 on m1"]), ("what is mode set to on m1?", ["mode is set to fast on m1"])]),
        T("observe", "listening", ["M", "P"], ["port {P} is open on {M}"], ["ss"],
          rows=lambda w: {(m, str(p)) for m in w.machines() for p in w.state[m]["ports"]},
          consts={"M": ["m1", "m2"], "P": ["22", "8080", "631"]},
          examples=[("is port 8080 open on m1?", ["Yes: port 8080 is open on m1"]), ("which ports are open on m2?", ["port 22 is open on m2"])]),
        T("observe", "running", ["M", "S"], ["{S} is running on {M}"], ["systemctl"],
          rows=lambda w: {(m, s) for m in w.machines() for s, on in w.state[m]["services"].items() if on},
          consts={"M": ["m1", "m2"], "S": ["nginx", "redis", "cron"]},
          examples=[("which services are running on m1?", ["nginx is running on m1", "cron is running on m1"]), ("is redis running on m1?", ["No: redis is not running on m1"])]),
        T("act", "start", ["M", "S"], ["{S} is running on {M}"], ["systemctl"], effect=lambda w, a: bool(w.state[a[0]]["services"].get(a[1])),
          consts={"M": ["m1", "m2"], "S": ["redis", "ollama"]}, bad=[("m1", "nosuchservice")], pairs=[("m1", "redis"), ("m2", "ollama")],
          examples=[("get redis running on m1", ["Done: redis is running on m1"])]),
    ]


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (c - h) / d), min(1.0, (c + h) / d))


def run():
    OUT.mkdir(exist_ok=True)
    llm = P.ClaudeCLI("sonnet")
    budget = P.Budget(MAX_CALLS)
    cells = [{"task": t, "level": lv, "hist": [], "done": False, "ok": False, "attempts": []} for lv in (0, 1, 2) for t in tasks()]
    log = (OUT / "estudio_programas.jsonl").open("w")
    w0 = world()
    for rnd in range(1, ATTEMPTS + 1):
        for c in cells:
            if c["done"]:
                continue
            try:
                o = P.program_one(c["task"], llm, w0, level=c["level"], attempts=rnd, history=c["hist"], start=rnd, budget=budget)
            except P.BudgetExhausted:
                c["done"] = True
                c["note"] = f"not retried: the budget of {MAX_CALLS} calls was spent"
                continue
            at = o.attempts[0]
            c["attempts"].append(at)
            err = at["validation"] or ("it ran but the rehearsal failed: " + at["rehearsal"])
            c["hist"].append((at["src"], err))
            c["done"] = c["ok"] = at["accepted"]
            log.write(json.dumps(dict(task=c["task"].name, kind=c["task"].kind, level=c["level"], k=at["k"], accepted=at["accepted"], validation=at["validation"],
                                      rehearsal=at["rehearsal"], meta=at["meta"], prompt=at["prompt"], response=at["response"],
                                      date=time.strftime("%Y-%m-%d %H:%M:%S")), ensure_ascii=False) + "\n")
            log.flush()
            print(f"round {rnd} level {c['level']} {c['task'].name:13} {'OK' if at['accepted'] else 'x '} calls so far {budget.used}", flush=True)
    log.close()
    report()


def rescore():
    """CORRECCIÓN POSTERIOR, declarada: el brew de mentira devolvía rc 0 para `brew list --formula NOMBRE` aunque NOMBRE no estuviera instalado
    (el real devuelve 1): dos primeros intentos de brew_install, correctos para el brew real, salieron como fallos. Se corrigió el mundo de ensayo y se
    RE-PUNTUARON las salidas guardadas, sin llamar al modelo; los veredictos originales quedan en estudio_programas_original.jsonl."""
    orig = OUT / "estudio_programas_original.jsonl"
    if not orig.exists():
        orig.write_text((OUT / "estudio_programas.jsonl").read_text())
    by_name = {t.name: t for t in tasks()}
    out, cambios = [], []
    for l in orig.read_text().splitlines():
        r = json.loads(l)
        t = by_name[r["task"]]
        src = P.strip_code(r["response"])
        v = P.validate(src, t)
        ok, rep = False, None
        if v is None:
            ok, rep, _ = P.rehearse(src, t, world())
        if ok != r["accepted"]:
            cambios.append(f"{r['task']} nivel {r['level']} intento {r['k']}: {'fallo' if r['accepted'] is False else 'ok'} -> {'ok' if ok else 'fallo'}")
        r["accepted_original"], r["accepted"], r["validation"], r["rehearsal"] = r["accepted"], ok, v, rep
        out.append(r)
    (OUT / "estudio_programas.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in out) + "\n")
    (OUT / "estudio_programas_correccion.txt").write_text("\n".join(cambios) + "\n")
    print("veredictos cambiados:", cambios)
    report(correccion=cambios)


def report(correccion=None):
    rows = [json.loads(l) for l in (OUT / "estudio_programas.jsonl").read_text().splitlines() if l.strip()]
    by = {}
    for r in rows:
        by.setdefault((r["level"], r["task"]), []).append(r)
    levels = sorted({k[0] for k in by})
    names = [t.name for t in tasks()]
    kinds = {t.name: t.kind for t in tasks()}
    n = len(names)
    lines = ["ESTUDIO: scripts que escribe el metaintérprete `program` (logitration/libreria, tests/study_programs.py)",
             f"CITA: modelo claude CLI alias sonnet ({rows[0]['meta'].get('model')}), n = {n} tareas por nivel (1 muestra por celda, temperatura por defecto), "
             f"{len(rows)} llamadas, {min(r['date'] for r in rows)} a {max(r['date'] for r in rows)}, hasta {ATTEMPTS} intentos por celda; "
             "mundo SIMULADO = MAQUETA (máquinas de mentira en memoria: ollama, brew, cat, ss, systemctl con formatos de los reales); aceptación = "
             "validador AST + casos con oráculo del mundo de ensayo.", ""]
    lines.append(f"{'nivel de contexto':58} {'1.er intento':>22} {'tras reintentos':>22} {'llamadas':>9} {'USD':>7}")
    summary = {}
    for lv in levels:
        first = sum(1 for nm in names if by.get((lv, nm)) and by[(lv, nm)][0]["accepted"])
        final = sum(1 for nm in names if any(r["accepted"] for r in by.get((lv, nm), [])))
        calls = sum(len(by.get((lv, nm), [])) for nm in names)
        usd = sum(r["meta"].get("usd", 0) or 0 for nm in names for r in by.get((lv, nm), []))
        f1, f2 = wilson(first, n), wilson(final, n)
        summary[lv] = dict(first=first, final=final, n=n, calls=calls, usd=round(usd, 3), ic_first=[round(x, 2) for x in f1], ic_final=[round(x, 2) for x in f2])
        lines.append(f"{lv} {P.LEVELS[lv]:56} {first}/{n} = {100 * first / n:3.0f}% [{100 * f1[0]:2.0f}-{100 * f1[1]:3.0f}] {final}/{n} = {100 * final / n:3.0f}% [{100 * f2[0]:2.0f}-{100 * f2[1]:3.0f}] {calls:9d} {usd:7.3f}")
    lines += ["", "IC 95 % de Wilson sobre las 9 tareas (no sobre muestras de una tarea). Con n = 9 una tarea son 11 puntos.", ""]
    lines.append(f"{'tarea':14} {'tipo':8}" + "".join(f"  nivel {lv}: intentos/resultado" for lv in levels))
    for nm in names:
        cell = []
        for lv in levels:
            rs = by.get((lv, nm), [])
            cell.append(f"{len(rs)} / {'ok' if any(r['accepted'] for r in rs) else 'NO'}" + (" (1.º" + ("; la llamada 2 sobró tras el re-puntuado" if len(rs) > 1 else "") + ")" if rs and rs[0]["accepted"] else ""))
        lines.append(f"{nm:14} {kinds[nm]:8}" + "".join(f"  {c:24}" for c in cell))
    lines += ["", "POR TIPO (1.er intento / tras reintentos)"]
    for kind in ("observe", "act"):
        ns = [nm for nm in names if kinds[nm] == kind]
        for lv in levels:
            f = sum(1 for nm in ns if by.get((lv, nm)) and by[(lv, nm)][0]["accepted"])
            g = sum(1 for nm in ns if any(r["accepted"] for r in by.get((lv, nm), [])))
            lines.append(f"  {kind:8} nivel {lv}: {f}/{len(ns)}  /  {g}/{len(ns)}")
    lines += ["", "QUÉ FALLÓ (error del primer intento fallido de cada celda; el motivo que volvió al modelo)"]
    for lv in levels:
        for nm in names:
            rs = by.get((lv, nm), [])
            if rs and not rs[0]["accepted"]:
                why = rs[0]["validation"] or "ensayo: " + (rs[0]["rehearsal"] or "").replace("\n", " | ")
                lines.append(f"  nivel {lv} {nm:13} {why[:230]}")
    kinds_err = {"validador": 0, "ensayo": 0}
    for r in rows:
        if not r["accepted"]:
            kinds_err["validador" if r["validation"] else "ensayo"] += 1
    lines += ["", f"Intentos fallidos: {kinds_err['validador']} por el validador (forma), {kinds_err['ensayo']} por el ensayo (comportamiento).", "",
              "INCERTIDUMBRE Y LÍMITES (declarados):",
              "  - 1 muestra por celda: el mismo prompt da respuestas distintas (instalador_meta midió ~8 puntos de ruido entre dos ejecuciones idénticas con n = 21 x 3); con n = 9, diferencias de <= 2 tareas no son señal.",
              "  - Las tareas, el validador, el oráculo y los casos los escribió el mismo autor; los casos pueden ser más severos o más laxos que lo que necesita un usuario (p. ej. «debe lanzar si el comando falla»).",
              "  - Mundo de mentira: los formatos copian a los reales (ollama list con :latest, ss con IPv6), el comportamiento está simplificado; nada de esto se ha probado en máquinas reales.",
              "  - Un solo modelo (sonnet) y una sola fecha. Los reintentos solo llevan el error del ensayo (no el código del oráculo). Lo que el ensayo no cubre (permisos, red lenta, formatos de otra versión) no está medido.",
              "  - Llamadas por nivel desiguales: los reintentos se repartieron por rondas y, si se agotaba el tope (58), las celdas sin reintentar se anotan como tales."]
    unretried = [(lv, nm) for lv in levels for nm in names if by.get((lv, nm)) and not any(r["accepted"] for r in by[(lv, nm)]) and len(by[(lv, nm)]) < ATTEMPTS]
    if unretried:
        lines.append(f"  - Celdas sin agotar los {ATTEMPTS} intentos por tope de llamadas: {', '.join(f'nivel {lv} {nm}' for lv, nm in unretried)}.")
    if (OUT / "estudio_programas_correccion.txt").exists():
        cs = [c for c in (OUT / "estudio_programas_correccion.txt").read_text().splitlines() if c.strip()]
        lines += ["", "CORRECCIÓN POSTERIOR (declarada): el brew de mentira devolvía rc 0 para `brew list --formula NOMBRE` aunque NOMBRE no estuviera instalado (el real devuelve 1);",
                  "  dos primeros intentos de brew_install, correctos para el brew real, salieron como fallos. Se corrigió el mundo de ensayo y se RE-PUNTUARON las salidas guardadas",
                  "  (sin llamar al modelo; las llamadas y el coste son los originales; los veredictos originales, en estudio_programas_original.jsonl). Veredictos cambiados: " + (", ".join(cs) or "ninguno") + ".",
                  "HALLAZGOS QUE NO SON FALLOS DEL MODELO (leídos en las salidas guardadas):",
                  "  - nivel 0 (0/9): los 27 intentos fueron rechazados por el VALIDADOR (8 por `with`, que el validador prohíbe en bloque; 6 por `open`; 6 por código que corre al importar, como print o __main__; 2 por `input`; 5 por otras formas):",
                  "    sin firma el modelo escribe un programa de línea de comandos, no una función con la puerta world.run, y el error de un reintento dice qué sobra, no qué falta; el validador es más estricto de lo necesario con `with`. No es una medida de la capacidad del modelo sin contexto, sino de este validador sin cabecera.",
                  "  - setting: la ruta /etc/app.conf no estaba en ningún prompt; los dos aciertos (niveles 1 y 2) fueron de un script que PRUEBA una lista de rutas posibles. Una especificación que no dice dónde está el fichero acierta por tanteo.",
                  "  - uninstall: pedí que quitar algo que no está sea un error (el caso «no se puede hacer»); el modelo lo trató como éxito idempotente («Done… it was not installed»). Es una decisión de especificación mía, no un error de Python."]
    (OUT / "estudio_programas.txt").write_text("\n".join(lines) + "\n")
    (OUT / "estudio_programas.json").write_text(json.dumps(dict(levels=summary, rows=len(rows), tasks=names), indent=1, ensure_ascii=False))
    print("\n".join(lines))


if __name__ == "__main__":
    rescore() if "--rescore" in sys.argv else report() if "--report" in sys.argv else run()
