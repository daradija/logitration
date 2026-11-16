# Copyright (c) 2026 David Ragel
# SPDX-License-Identifier: BUSL-1.1
# Licensed under the Business Source License 1.1; see LICENSE.
"""Mirror between the local building and its screen (`logi building`). Stdlib only, Python 3.9+. Started by `logi building start`; do not run by hand.

The screen (percebe_local) was written for a world of Claude sessions: session files, transcripts and hook events. The local building keeps its
state in <building>/estado/*.json. This process READS the building's state files every second and WRITES, for the screen only:
  - one session file per room (with this process's pid, which is what the screen checks to know that the room is alive),
  - one transcript per room (<home>/.claude/projects/<slug>/<sid>.jsonl) rebuilt from the room's chat and mailbox, append only,
  - hook-like events (tool, what it does) and the room map (new rooms only: after that the screen owns where rooms stand).
It never writes into the building's folders and opens no sockets.

Environment (set by `logi building start`): ESPEJO_ESTADO (building estado dir), ESPEJO_HOME (the screen's HOME), ESPEJO_CACHE (the screen's cache), ESPEJO_CWD.
"""
import json
import os
import re
import sys
import time
import uuid

ESTADO = os.environ.get("ESPEJO_ESTADO", "")
HOME = os.environ.get("ESPEJO_HOME", "")
CACHE = os.environ.get("ESPEJO_CACHE", "")
CWD = os.environ.get("ESPEJO_CWD", "") or os.path.join(HOME, "work")
NOMBRE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
SES = os.path.join(HOME, ".claude", "sessions")
PROY = os.path.join(HOME, ".claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", CWD))
CADA = float(os.environ.get("ESPEJO_CADA", "1"))


def lee(p, d):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def lineas(p):
    out = []
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for l in f:
                try:
                    x = json.loads(l)
                    if isinstance(x, dict):
                        out.append(x)
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def escribe(p, txt):
    t = p + ".tmp"
    with open(t, "w", encoding="utf-8") as f:
        f.write(txt)
    os.replace(t, p)


def sid_de(n):
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, "logi_building/" + n))


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".000Z"


def salas():
    pl = lee(os.path.join(ESTADO, "plano.json"), {}) or {}
    dire = pl.get("directivos") or {}
    ag = lee(os.path.join(ESTADO, "agentes.json"), {}) or {}
    nombres = [n for n in dire.values() if isinstance(n, str) and NOMBRE.match(n)]
    return pl, dire, [n for n in nombres if n in ag or os.path.isfile(os.path.join(ESTADO, n + ".json"))]


def une_plano(pl, dire, vivos):
    """The screen owns the room map once a room is on it. The building only adds new rooms and removes the ones it deleted."""
    ruta = os.path.join(CACHE, "plano.json")
    p = lee(ruta, {}) or {}
    p.setdefault("directivos", {})
    p.setdefault("sids", {})
    cambio = False
    ya = {v: k for k, v in p["directivos"].items()}
    for sala, n in dire.items():
        if n in vivos and n not in ya:
            if sala in p["directivos"]:
                libres = [str(i) for i in range(500) if str(i) not in p["directivos"]]
                sala = libres[0]
            p["directivos"][sala] = n
            cambio = True
    for sala, n in list(p["directivos"].items()):
        if n not in vivos:
            del p["directivos"][sala]
            cambio = True
    sids = {s: sid_de(n) for s, n in p["directivos"].items()}
    if sids != p["sids"]:
        p["sids"] = sids
        cambio = True
    if cambio or not os.path.isfile(ruta):
        escribe(ruta, json.dumps(p, ensure_ascii=False, indent=1))
    for fn, orig in (("rotulos", pl.get("rotulos")), ("emblemas", pl.get("emblemas"))):
        f = os.path.join(CACHE, fn + ".json")
        if isinstance(orig, dict) and not os.path.isfile(f):    # seeded once; later edits belong to the screen
            escribe(f, json.dumps(orig, ensure_ascii=False))


class Sala:
    def __init__(self, n):
        self.n = n
        self.sid = sid_de(n)
        self.vistos = set()
        for x in lineas(os.path.join(PROY, self.sid + ".jsonl")):     # after a restart, what is already written is not written again
            u = str(x.get("uuid") or "")
            self.vistos.update(("c:" + u, "b:" + u))

    def eventos(self):
        """New (t, key, kind, text, extra) items from the room's chat and mailbox that are not yet in the transcript."""
        n = self.n
        out = []
        for m in lineas(os.path.join(ESTADO, n + ".charla.jsonl")):
            k = "c:" + str(m.get("uuid") or m.get("t"))
            if k not in self.vistos:
                out.append((float(m.get("t") or 0), k, "a", str(m.get("texto", "")), {}))
        ent = {x.get("id"): x.get("estado") for x in lineas(os.path.join(ESTADO, n + ".entrega.jsonl"))}
        hechos = {x.get("id") for x in lineas(os.path.join(ESTADO, n + ".buzon.jsonl.hecho"))}
        for m in lineas(os.path.join(ESTADO, n + ".buzon.jsonl")):
            if m.get("tipo") == "permiso":
                continue
            k = "b:" + str(m.get("id") or m.get("t"))
            if k in self.vistos:
                continue
            de = str(m.get("de", ""))
            if de in ("usuario", "enlace"):
                if m.get("id") not in hechos and ent.get(m.get("id")) not in ("entregado", "visto", "contestado"):
                    continue          # a message still waiting in the mailbox (room held, busy typing) is not in the conversation yet
                out.append((float(m.get("t") or 0), k, "u", str(m.get("texto", "")), {"id": str(m.get("id") or "")}))
            else:
                out.append((float(m.get("t") or 0), k, "p", str(m.get("texto", "")), {"de": str(m.get("desde") or de)}))
        out.sort(key=lambda x: x[0])
        return out

    def linea(self, t, k, tipo, texto, extra):
        base = {"uuid": (extra.get("id") or k.split(":", 1)[1])[:36], "timestamp": iso(t), "sessionId": self.sid, "cwd": CWD}
        if tipo == "a":
            base.update(type="assistant", message={"role": "assistant", "content": [{"type": "text", "text": texto}]})
        elif tipo == "u":
            base.update(type="user", message={"role": "user", "content": texto}, origin={"kind": "human"}, turnOrigin="human", promptSource="typed")
        else:
            cuerpo = '<cross-session-message from-name="%s">\n%s\n</cross-session-message>' % (extra.get("de", "?").replace('"', ""), texto)
            base.update(type="user", message={"role": "user", "content": cuerpo}, origin={"kind": "peer"})
        return json.dumps(base, ensure_ascii=False, separators=(",", ":")) + "\n"


def un_ciclo(salas_vivas, previo):
    pl, dire, nombres = salas()
    une_plano(pl, dire, set(nombres))
    ev = []
    os.makedirs(PROY, exist_ok=True)
    for n in nombres:
        s = salas_vivas.setdefault(n, Sala(n))
        e = lee(os.path.join(ESTADO, n + ".json"), {}) or {}
        permiso = e.get("permiso") if isinstance(e.get("permiso"), dict) else None
        caida = e.get("estado") in ("caida", "baja") or e.get("estado_detalle") == "retenida_swap"
        trabaja = e.get("estado") in ("trabaja", "busy")
        est = "waiting" if permiso else ("busy" if trabaja else "idle")
        ruta = os.path.join(PROY, s.sid + ".jsonl")
        nuevos = s.eventos()
        if nuevos or not os.path.isfile(ruta):
            with open(ruta, "a", encoding="utf-8") as f:
                if f.tell() == 0:      # like Claude's own transcripts, the file starts with a header line (the screen's tail reader skips its first line)
                    f.write(json.dumps({"type": "summary", "summary": n, "sessionId": s.sid}, separators=(",", ":")) + "\n")
                for t, k, tipo, texto, extra in nuevos:
                    f.write(s.linea(t, k, tipo, texto, extra))
                    s.vistos.add(k)
        sesion = os.path.join(SES, "lb_%s.json" % n)
        if caida:
            try:
                os.remove(sesion)
            except OSError:
                pass
        else:
            escribe(sesion, json.dumps({"kind": "interactive", "pid": os.getpid(), "sessionId": s.sid, "name": n, "status": est, "cwd": CWD,
                                        "startedAt": int(e.get("epoch") or time.time()) * 1000, "procStart": "mirror"}))
        sig = (e.get("estado"), e.get("herramienta"), e.get("que"), e.get("t"))
        if trabaja and previo.get(n) != sig:
            ev.append({"session_id": s.sid, "hook_event_name": "PreToolUse", "tool_name": e.get("herramienta") or "Task", "description": str(e.get("que") or "")[:80]})
        elif not trabaja and previo.get(n) is not None and previo.get(n)[0] in ("trabaja", "busy"):
            ev.append({"session_id": s.sid, "hook_event_name": "Stop"})
        previo[n] = sig
    for f in os.listdir(SES) if os.path.isdir(SES) else []:       # a room that disappeared leaves no session
        if f.startswith("lb_") and f.endswith(".json") and f[3:-5] not in nombres:
            try:
                os.remove(os.path.join(SES, f))
            except OSError:
                pass
    if ev:
        with open(os.path.join(CACHE, "eventos.jsonl"), "a", encoding="utf-8") as f:
            for x in ev:
                f.write(json.dumps(x, ensure_ascii=False, separators=(",", ":")) + "\n")


def main():
    if not (ESTADO and HOME and CACHE):
        sys.exit("started by `logi building start` only (missing environment)")
    os.makedirs(SES, exist_ok=True)
    os.makedirs(CACHE, exist_ok=True)
    salas_vivas, previo = {}, {}
    while True:
        try:
            un_ciclo(salas_vivas, previo)
        except Exception as x:       # a bad file must not kill the mirror
            print("mirror:", type(x).__name__, x, file=sys.stderr, flush=True)
        time.sleep(CADA)


if __name__ == "__main__":
    main()
