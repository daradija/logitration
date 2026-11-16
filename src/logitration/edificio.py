#!/usr/bin/env python3
# Copyright (c) 2026 David Ragel
# SPDX-License-Identifier: BUSL-1.1
# Licensed under the Business Source License 1.1; see LICENSE.
"""A LOCAL BUILDING of agents for `logi`, with one command. Stdlib only, Python 3.9+.

    logi building start [--persistent] [--token-stdin] [--no-token] [--port N] [--open | --no-open] [--no-web]
    logi building open [--no-open]       (a new single-use sign-in link for the web screen of a running building)
    logi building status | stop | uninstall [--keep-token]

(`logi edificio ...` also works; or, without installing logi: python3 edificio.py start|status|stop|uninstall)

What `start` does (idempotent: running it again duplicates nothing and breaks nothing):
  1. checks requirements (python >= 3.9, `claude` in PATH or says so, a free port);
  2. installs the building package (edificio_pyro_0.9.3.tar.gz next to this file; fixed sha256, verified) under ~/logi_edificio/
     (never in anybody else's building: nothing outside ~/logi_edificio and ~/.logi_edificio);
  3. writes config.json with ONE free port picked automatically, listening ONLY on 127.0.0.1 (local A2A); no connection to any server;
  4. asks for the Claude TOKEN if missing: env var CLAUDE_CODE_OAUTH_TOKEN or LOGI_EDIFICIO_TOKEN, or the file ~/.logi_edificio/token (600), or it
     prompts you (hidden; the one from `claude setup-token`), or --token-stdin; empty Enter = use your `claude login` if there is a session,
     otherwise SIMULATED mode;
  5. starts the building, creates the `director` room (real Claude if there is a token or session; otherwise a FAKE claude, and it says so);
  6. starts the WEB SCREEN: the building drawn as a street building (the `percebe` screen of the author's own building, cleaned for local use: floor plan,
     rooms with their speech bubbles, chat with each room, pinned answers, play of each answer, notes, attachments) on 127.0.0.1 and a free port of its
     own, plus a small mirror process that feeds it from the building's state files; it opens in your browser when run from an interactive terminal
     (--open is the default there; --no-open prints the link instead; LOGI_BUILDING_NO_OPEN=1 never opens).
     Access needs a random local secret (~/.logi_edificio/web_token, mode 600; not a user password). The link that is opened is single use and expires.
No LaunchAgent by default; --persistent writes a plist in ~/Library/LaunchAgents only if that directory is yours.
The token: never in argv, logs, the `ps` environment or the plist: a 600 file, which the anchor hands to claude by file descriptor.
No pkill/pgrep: only processes whose pid the building itself saved (and verified with ps) are stopped.
"""
import getpass
import hashlib
import json
import secrets
import os
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time

VERSION_MODULO = "1.2 (16-11-2026)"
PAQUETE = "edificio_pyro_0.9.3.tar.gz"
PAQUETE_SHA256 = "8b06a162cc20b5cb22fb17ed97be1fce8f62e3c03d92098effcbe8941adc0164"
ETIQUETA_PLIST = "org.pyroboadvisor.edificio.logi"       # the building requires this prefix for its "supervisor"
PUERTOS_VEDADOS = {8795, 8798, 8806, 8874, 8895, 8899, 11434}   # ports reserved for the author's own machines; anything already listening is avoided too


def inst():
    return os.path.join(os.path.expanduser("~"), "logi_edificio")


def seg():
    return os.path.join(os.path.expanduser("~"), ".logi_edificio")


def pkg():
    return os.path.join(inst(), "edificio_pyro")


def webdir():
    return os.path.join(inst(), "web")


def _web_token():
    return os.path.join(seg(), "web_token")


def _token():
    return os.path.join(seg(), "token")


def _estado():
    return os.path.join(seg(), "estado.json")


def di(*a):
    print(*a, flush=True)


def lee_json(p, por_defecto=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return por_defecto


def escribe600(p, texto):
    os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
    fd = os.open(p + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(texto)
    os.replace(p + ".tmp", p)
    os.chmod(p, 0o600)


def entorno():
    """Minimal environment of the building's processes. It NEVER carries the token."""
    e = {k: v for k, v in os.environ.items() if k in ("HOME", "USER", "LOGNAME", "LANG", "TMPDIR", "PATH", "LC_ALL")}
    e.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    return e


# ---------------------------------------------------------------- requisitos y puerto
def puerto_libre(prefer=None):
    for p in ([prefer] if prefer else []) + [0]:
        if p in PUERTOS_VEDADOS:
            continue
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", p))
            q = s.getsockname()[1]
        except OSError:
            continue
        finally:
            s.close()
        if q not in PUERTOS_VEDADOS:
            return q
    raise SystemExit("cannot find a free port on 127.0.0.1")


def busca_claude():
    c = shutil.which("claude")
    if c:
        return c
    for p in ("~/.local/bin/claude", "~/.claude/local/claude", "/usr/local/bin/claude", "/opt/homebrew/bin/claude"):
        p = os.path.expanduser(p)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def claude_con_sesion(c):
    """Does `claude auth status` say loggedIn? (installs and changes nothing)."""
    try:
        r = subprocess.run([c, "auth", "status"], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL, env=entorno())
        return bool(json.loads(r.stdout or "{}").get("loggedIn"))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False


# ---------------------------------------------------------------- paquete
def instala_paquete():
    """Unpacks the package into ~/logi_edificio/edificio_pyro if missing or changed. Keeps config.json, estado/, sesiones/ and mantenedor/."""
    aqui = os.path.dirname(os.path.abspath(__file__))
    tar = os.path.join(aqui, PAQUETE)
    if not os.path.isfile(tar):
        raise SystemExit("%s is missing next to this module" % PAQUETE)
    with open(tar, "rb") as f:
        h = hashlib.sha256(f.read()).hexdigest()
    if h != PAQUETE_SHA256:
        raise SystemExit("the package is not the expected one (sha256 %s...); installing nothing" % h[:12])
    marca = os.path.join(inst(), ".logi_edificio_marca")
    if lee_json(marca, {}).get("sha256") == h and os.path.isfile(os.path.join(pkg(), "edificio.py")):
        return False
    os.makedirs(inst(), mode=0o700, exist_ok=True)
    os.makedirs(pkg(), exist_ok=True)
    with tarfile.open(tar, "r:gz") as t:
        for m in t.getmembers():
            n = m.name
            if not m.isreg() or n.startswith("/") or ".." in n.split("/") or not n.startswith("edificio_pyro/") or n.count("/") != 1:
                raise SystemExit("package with a disallowed path: " + n)
            with t.extractfile(m) as src:
                dest = os.path.join(inst(), n)
                with open(dest + ".tmp", "wb") as out:
                    shutil.copyfileobj(src, out)
                os.replace(dest + ".tmp", dest)
    escribe600(marca, json.dumps({"sha256": h, "modulo": VERSION_MODULO, "instalado": int(time.time())}))
    return True


CLAUDE_FALSO = r'''#!/usr/bin/env python3
# SIMULATED claude of logi_edificio: no AI, no network. Only to see the building alive when there is no Claude token or session.
# It behaves like Claude in the one way the building relies on: it writes its conversation to ~/.claude/projects/<cwd>/<session-id>.jsonl
# (a line for what you typed, a line for its reply), so messages go delivered -> seen -> answered and the reply shows up in the screen.
import json, os, re, sys, time, uuid
a = sys.argv[1:]
if a[:1] == ["--version"]:
    print("0.0.0-simulated (logi_edificio)")
elif a[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "simulated"}))
else:
    sid = next((a[i + 1] for i, x in enumerate(a[:-1]) if x in ("--session-id", "--resume")), uuid.uuid4().hex)
    carpeta = os.path.join(os.path.expanduser("~"), ".claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", os.getcwd()))
    ruta = os.path.join(carpeta, sid + ".jsonl")
    def apunta(tipo, msg, **extra):
        os.makedirs(carpeta, exist_ok=True)
        d = {"type": tipo, "message": msg, "uuid": uuid.uuid4().hex, "sessionId": sid, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())}
        d.update(extra)
        with open(ruta, "a", encoding="utf-8") as f:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    print("[simulated] director without real Claude: give a token with `logi building start --token-stdin` or run `claude login`.", flush=True)
    for linea in sys.stdin:
        linea = re.sub(r"\x1b\[20[01]~|\^\[\[20[01]~", "", linea).strip()
        if linea in ("/exit", "/quit"):
            break
        if linea:
            print("[simulated] received: " + linea[:200], flush=True)
            apunta("user", {"role": "user", "content": linea}, origin={"kind": "human"}, turnOrigin="human", promptSource="typed")
            time.sleep(0.4)
            apunta("assistant", {"role": "assistant", "content": [{"type": "text", "text": "(simulated reply, no Claude) I received: " + linea[:300]}]})
'''


def escribe_falso():
    p = os.path.join(inst(), "falso", "claude")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(CLAUDE_FALSO.replace("#!/usr/bin/env python3", "#!" + sys.executable, 1))     # the same python that runs logi, not whatever is in PATH
    os.chmod(p, 0o755)
    return p


# ---------------------------------------------------------------- token
def pide_token(args):
    """-> ('token', text) | ('login', None) | ('simulated', None). The token is never printed."""
    t = None
    if "--no-token" in args:
        return "simulated", None
    if "--token-stdin" in args:
        t = sys.stdin.readline().strip()
    if not t:
        t = (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("LOGI_EDIFICIO_TOKEN") or "").strip()
    if not t and os.path.isfile(_token()):
        t = open(_token(), encoding="utf-8").read().strip()
    if not t and sys.stdin.isatty() and sys.stdout.isatty():
        di("The director needs Claude. Paste your Claude token (the one from `claude setup-token`; hidden as you type) and press Enter.")
        di("Empty Enter = use your `claude login` if you already have a session; otherwise the director runs SIMULATED.")
        t = getpass.getpass("Paste your Claude token (hidden): ").strip()
    if t:
        if len(t) < 20 or any(c.isspace() for c in t):
            raise SystemExit("that does not look like a `claude setup-token` token (nothing saved)")
        return "token", t
    c = busca_claude()
    if c and claude_con_sesion(c):
        return "login", None
    return "simulated", None


def guarda_token(t):
    escribe600(_token(), t + "\n")
    enlace = os.path.join(pkg(), "mantenedor", "secretos", "token")      # where the building and the anchor look for it; it is a LINK to the 600 file in ~/.logi_edificio
    os.makedirs(os.path.dirname(enlace), mode=0o700, exist_ok=True)
    if os.path.lexists(enlace):
        os.remove(enlace)
    os.symlink(_token(), enlace)


def enlaza_token_falso():
    """Simulated mode: the building runs the director with its OWN isolated HOME (~/logi_edificio/edificio_pyro/sesiones/home), as it does with a real token,
    so the fake claude never writes into the user's real ~/.claude. The 'token' is a dummy inside ~/logi_edificio; it is never in ~/.logi_edificio/token."""
    falso = os.path.join(inst(), "falso", "token")
    escribe600(falso, "simulated-no-claude\n")
    enlace = os.path.join(pkg(), "mantenedor", "secretos", "token")
    os.makedirs(os.path.dirname(enlace), mode=0o700, exist_ok=True)
    if os.path.lexists(enlace):
        os.remove(enlace)
    os.symlink(falso, enlace)


def quita_enlace_token():
    enlace = os.path.join(pkg(), "mantenedor", "secretos", "token")
    if os.path.islink(enlace):
        os.remove(enlace)


# ---------------------------------------------------------------- building processes (only its own, by saved pid)
def pid_vivo(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def comando(pid):
    try:
        return subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def pids_propios():
    """pids saved by the building (edificio.pid, a2a/a2a.pid, sesiones/*.pid, <n>.ancla.json -> claude pid) whose command lives under ~/logi_edificio."""
    P = pkg()
    cand = []
    for ruta, clave in ((os.path.join(P, "edificio.pid"), "pid"), (os.path.join(P, "a2a", "a2a.pid"), "pid")):
        d = lee_json(ruta, {})
        if isinstance(d, dict) and isinstance(d.get(clave), int):
            cand.append(d[clave])
    sd = os.path.join(P, "sesiones")
    if os.path.isdir(sd):
        for f in os.listdir(sd):
            ruta = os.path.join(sd, f)
            if f.endswith(".pid"):
                try:
                    cand.append(int(open(ruta).read().strip()))
                except (OSError, ValueError):
                    pass
            elif f.endswith(".ancla.json"):
                d = lee_json(ruta, {})
                if isinstance(d, dict) and isinstance(d.get("pid"), int):
                    cand.append(d["pid"])
    for f in ("web.pid", "mirror.pid"):
        d = lee_json(os.path.join(webdir(), f), {})
        if isinstance(d, dict) and isinstance(d.get("pid"), int):
            cand.append(d["pid"])
    ad = os.path.join(P, "estado")
    if os.path.isdir(ad):
        for f in os.listdir(ad):
            if f.endswith(".pid"):
                try:
                    cand.append(int(open(os.path.join(ad, f)).read().strip()))
                except (OSError, ValueError):
                    pass
    vistos, out = set(), []
    for p in cand:
        if p in vistos or p <= 1 or p == os.getpid() or not pid_vivo(p):
            continue
        vistos.add(p)
        c = comando(p)
        if inst() in c:
            out.append((p, c[:90]))
    return out


def edificio(*a, timeout=60):
    return subprocess.run([sys.executable, os.path.join(pkg(), "edificio.py"), *a], cwd=pkg(), env=entorno(), capture_output=True, text=True,
                          timeout=timeout, stdin=subprocess.DEVNULL)


def sala_director(espera=0.0):
    """State of the director card (estado/director.json). Waits up to `espera` s for the anchor to write something other than "arrancando"."""
    t0 = time.time()
    while True:
        d = lee_json(os.path.join(pkg(), "estado", "director.json"), {}) or {}
        if d.get("estado") in ("idle", "busy", "caida") or time.time() - t0 >= espera:
            return d
        time.sleep(0.4)


def director_confiado():
    cf = lee_json(os.path.join(pkg(), "config.json"), {})
    x = ((cf.get("sesiones") or {}).get("fichas") or {}).get("director") if isinstance(cf, dict) else None
    return isinstance(x, dict) and bool(x.get("confianza"))


def texto_director(d):
    if not d:
        return "director: no status card yet"
    if d.get("estado_detalle") == "retenida_swap":
        return "director: HELD by the building's swap guard (%s); it will start by itself once swap drops below 4096 MB" % d.get("que", "")
    return "director: %s%s" % (d.get("estado"), " · " + str(d.get("que"))[:80] if d.get("que") else "")


def corre():
    d = lee_json(os.path.join(pkg(), "edificio.pid"), {})
    return d if isinstance(d, dict) and isinstance(d.get("pid"), int) and pid_vivo(d["pid"]) else None


# ---------------------------------------------------------------- config
def escribe_config(puerto, claude, modo):
    cfgp = os.path.join(pkg(), "config.json")
    cfg = lee_json(cfgp, {})
    cfg = cfg if isinstance(cfg, dict) else {}
    cfg["nombre"] = "local"
    cfg.setdefault("a2a", {})
    cfg["a2a"] = {"activo": True, "puerto": puerto}              # no "ws": nothing goes out to the network
    s = cfg.get("sesiones") if isinstance(cfg.get("sesiones"), dict) else {}
    s.update(cwd=os.path.join(inst(), "trabajo"), claude=claude, tmux=False)
    cfg["sesiones"] = s
    escribe600(cfgp, json.dumps(cfg, indent=1, ensure_ascii=False))


INSTRUCCIONES_DIRECTOR = (
    "You are the director of this LOCAL logi building (the user's computer). This folder is yours. You can talk to other rooms with SendMessage; "
    "the building is not connected to any server. Answer briefly and in the language of whoever writes to you.")


def crea_director(modo):
    """Creates the `director` room if it does not exist (importing edificio.py from the package; same function the crear_sesion order uses)."""
    cf = lee_json(os.path.join(pkg(), "config.json"), {})
    if "director" in ((cf.get("sesiones") or {}).get("fichas") or {}):
        return "already existed"
    carpeta = os.path.join(inst(), "trabajo", "director")
    os.makedirs(carpeta, exist_ok=True)
    codigo = ("import json,sys,edificio\n"
              "d=json.loads(sys.argv[1])\n"
              "print(edificio.crea_sesion(d, None))\n")
    arg = json.dumps({"nombre": "director", "carpeta": "director", "instrucciones": INSTRUCCIONES_DIRECTOR,
                      "tarea": "Greet in one line and wait.", "modelo": "sonnet"})
    r = subprocess.run([sys.executable, "-c", codigo, arg], cwd=pkg(), env=entorno(), capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    if r.returncode:
        raise SystemExit("could not create the director room: " + (r.stderr.strip().splitlines() or ["?"])[-1][:300])
    # Only here, when the room is created: the director is trusted (it may ask the building to open other rooms). Same as
    # `edificio.py sesiones confianza director on`; if the user later turns it off, a new `start` does not turn it back on.
    t = edificio("sesiones", "confianza", "director", "on")
    if t.returncode:
        raise SystemExit("director created, but could not mark it trusted: " + (t.stderr.strip().splitlines() or ["?"])[-1][:300])
    quita_salas_demo()
    return r.stdout.strip()[:200] + " (trusted: it can open other rooms; the package's demo rooms are removed: only the director is left)"


DEMO_SALAS = ("contador", "lector")


def quita_salas_demo():
    """The building package ships two demo rooms (contador, lector: fixed phrases, no AI). A real local building has only the director, so
    right after the director is created they are removed with the building's own order (`borrar_agente`), without editing the package.
    Only demo rooms (not a session, no task) and only here, when the director is first created: a later `start` never touches the user's rooms."""
    codigo = ("import json,sys,edificio\n"
              "for n in sys.argv[1:]:\n"
              "    x = edificio.agentes_def().get(n)\n"
              "    if isinstance(x, dict) and x.get('tipo') != 'sesion' and not x.get('tarea'):\n"
              "        print(edificio.o_borrar({'nombre': n}))\n")
    r = subprocess.run([sys.executable, "-c", codigo, *DEMO_SALAS], cwd=pkg(), env=entorno(), capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    if r.returncode:
        raise SystemExit("could not remove the demo rooms: " + (r.stderr.strip().splitlines() or ["?"])[-1][:300])


# ---------------------------------------------------------------- web screen (own process, own port, loopback, access secret)
WEB_FILE = "percebe_local.py"
ESPEJO_FILE = "espejo_local.py"
BUNDLE_DIR = "percebe_local"


def web_bundled():
    """(dir with the screen files, path of the mirror): next to this module."""
    aqui = os.path.dirname(os.path.abspath(__file__))
    d = os.path.join(aqui, BUNDLE_DIR)
    e = os.path.join(aqui, ESPEJO_FILE)
    if not (os.path.isfile(os.path.join(d, WEB_FILE)) and os.path.isfile(e)):
        raise SystemExit("%s/ or %s is missing next to this module" % (BUNDLE_DIR, ESPEJO_FILE))
    return d, e


def web_puerto_vivo():
    """The web port if its process is ours and answers /ping, else None."""
    d = lee_json(os.path.join(webdir(), "web.pid"), {})
    if not (isinstance(d, dict) and isinstance(d.get("pid"), int) and isinstance(d.get("port"), int) and pid_vivo(d["pid"]) and WEB_FILE in comando(d["pid"])):
        return None
    s = socket.socket()
    s.settimeout(1)
    try:
        return d["port"] if s.connect_ex(("127.0.0.1", d["port"])) == 0 else None
    finally:
        s.close()


def espejo_vivo():
    d = lee_json(os.path.join(webdir(), "mirror.pid"), {})
    return d["pid"] if isinstance(d, dict) and isinstance(d.get("pid"), int) and pid_vivo(d["pid"]) and ESPEJO_FILE in comando(d["pid"]) else None


def asegura_secreto_web():
    """Random access secret, created on the first start, mode 600; never in argv, logs, the environment or the plist."""
    if not (os.path.isfile(_web_token()) and len(open(_web_token()).read().strip()) >= 32):
        escribe600(_web_token(), secrets.token_urlsafe(32) + "\n")


def _copia_si_distinto(origen, destino):
    nuevo = open(origen, "rb").read()
    if os.path.isfile(destino) and open(destino, "rb").read() == nuevo:
        return False
    with open(destino + ".tmp", "wb") as f:
        f.write(nuevo)
    os.replace(destino + ".tmp", destino)
    return True


def _para_pid_guardado(ruta, fichero):
    d = lee_json(ruta, {})
    pid = d.get("pid") if isinstance(d, dict) else None
    if isinstance(pid, int) and pid_vivo(pid) and fichero in comando(pid):     # only the pid it saved, checked by its command
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def arranca_web(evitar):
    """Starts (or keeps) the web screen and its mirror. Returns the screen's port. The files are copied under ~/logi_edificio/web so the processes are
    recognisably ours; everything the screen writes (pins, labels, notes, attachments) lives in ~/logi_edificio/web/cache."""
    asegura_secreto_web()
    W = webdir()
    os.makedirs(W, mode=0o700, exist_ok=True)
    fuente, espejo = web_bundled()
    cambio = False
    for f in sorted(os.listdir(fuente)):
        if os.path.isfile(os.path.join(fuente, f)) and not f.startswith("."):
            cambio |= _copia_si_distinto(os.path.join(fuente, f), os.path.join(W, f))
    cambio |= _copia_si_distinto(espejo, os.path.join(W, ESPEJO_FILE))
    vivo = web_puerto_vivo()
    if vivo and not cambio and espejo_vivo():
        return vivo
    if vivo or espejo_vivo():                                  # a different version (or half a pair) is running: restart both
        _para_pid_guardado(os.path.join(W, "web.pid"), WEB_FILE)
        _para_pid_guardado(os.path.join(W, "mirror.pid"), ESPEJO_FILE)
        time.sleep(0.8)
    for _ in range(5):
        puerto = puerto_libre()
        if puerto not in evitar:
            break
    log = open(os.path.join(W, "web.log"), "a")
    e = entorno()
    e.update(ESPEJO_ESTADO=os.path.join(pkg(), "estado"), ESPEJO_HOME=os.path.join(W, "home"), ESPEJO_CACHE=os.path.join(W, "cache"),
             ESPEJO_CWD=os.path.join(inst(), "trabajo"))
    os.makedirs(os.path.join(W, "home"), mode=0o700, exist_ok=True)
    pe = subprocess.Popen([sys.executable, os.path.join(W, ESPEJO_FILE)], cwd=W, env=e, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    with open(os.path.join(W, "mirror.pid"), "w") as f:
        json.dump({"pid": pe.pid}, f)
    e = entorno()
    e.update(LOGI_ESTADO=os.path.join(pkg(), "estado"), LOGI_PKG=pkg(), LOGI_WEB_SEG=seg(), LOGI_WEB_PIDFILE=os.path.join(W, "web.pid"), PERCEBE_PUERTO=str(puerto),
             PERCEBE_CACHE=os.path.join(W, "cache"), PERCEBE_SESIONES=os.path.join(W, "home", ".claude", "sessions"),
             PERCEBE_PROYECTOS=os.path.join(W, "home", ".claude", "projects"), PERCEBE_RAIZ=os.path.join(W, "raiz"))
    subprocess.Popen([sys.executable, os.path.join(W, WEB_FILE)], cwd=W, env=e, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    for _ in range(80):
        if web_puerto_vivo() == puerto:
            return puerto
        time.sleep(0.1)
    raise SystemExit("the web screen did not start; see %s" % os.path.join(W, "web.log"))


def enlace_web(puerto):
    """A new single-use sign-in link (valid 10 min). The secret itself never goes in a URL; only this nonce does."""
    ruta = os.path.join(seg(), "web_nonce.json")
    lista = [e for e in (lee_json(ruta, []) or []) if isinstance(e, dict) and e.get("exp", 0) > time.time()][-4:]
    n = secrets.token_urlsafe(24)
    lista.append({"n": n, "exp": time.time() + 600})
    escribe600(ruta, json.dumps(lista))
    return "http://127.0.0.1:%d/?n=%s" % (puerto, n)


def abre_navegador(url):
    """Opens the browser unless told not to. LOGI_BUILDING_OPEN_CMD replaces `open`/`xdg-open` (tests). Returns a message."""
    if os.environ.get("LOGI_BUILDING_NO_OPEN"):
        return "not opening a browser (LOGI_BUILDING_NO_OPEN)"
    cmd = os.environ.get("LOGI_BUILDING_OPEN_CMD") or ("open" if sys.platform == "darwin" else "xdg-open")
    exe = shutil.which(cmd) or (cmd if os.path.isfile(cmd) else None)
    if not exe:
        return "could not find `%s` to open a browser; open the link by hand" % cmd
    try:
        subprocess.Popen([exe, url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as x:
        return "could not open a browser (%s); open the link by hand" % x
    return "opened in your browser"


def muestra_web(puerto, args, forzar_abrir=False):
    url = enlace_web(puerto)
    quiere = "--no-open" not in args and (forzar_abrir or "--open" in args or (sys.stdout.isatty() and sys.stdin.isatty()))
    di("WEB SCREEN: http://127.0.0.1:%d/   (loopback only; needs the local access secret in %s, which is NOT a password)" % (puerto, _web_token()))
    if quiere:
        di("  " + abre_navegador(url) + ". If nothing opened, use this single-use link (valid 10 min): " + url)
    else:
        di("  single-use sign-in link (valid 10 min; get another with `logi building open`): " + url)


# ---------------------------------------------------------------- persistencia (opcional)
def plist_texto(puerto):
    py = sys.executable
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>\n<key>Label</key><string>%s</string>\n<key>ProgramArguments</key><array><string>%s</string><string>%s</string><string>servir</string></array>\n'
            '<key>WorkingDirectory</key><string>%s</string>\n<key>RunAtLoad</key><true/>\n<key>KeepAlive</key><true/>\n<key>Nice</key><integer>10</integer>\n'
            '<key>StandardOutPath</key><string>%s</string><key>StandardErrorPath</key><string>%s</string>\n</dict></plist>\n'
            % (ETIQUETA_PLIST, py, os.path.join(pkg(), "edificio.py"), pkg(), os.path.join(pkg(), "edificio.log"), os.path.join(pkg(), "edificio.log")))


def ruta_plist():
    return os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", ETIQUETA_PLIST + ".plist")


def hace_persistente(puerto):
    if sys.platform != "darwin":
        return "persistence: only implemented for macOS (LaunchAgent); nothing was done on this system"
    d = os.path.dirname(ruta_plist())
    if not os.path.isdir(d):
        try:
            os.makedirs(d, mode=0o755)
        except OSError:
            return "persistence: cannot create %s; nothing was done" % d
    if os.stat(d).st_uid != os.getuid() or not os.access(d, os.W_OK):
        return "persistence: %s is not yours (or not writable); the plist was not written. Ask whoever administers this machine to give you the folder." % d
    with open(ruta_plist(), "w", encoding="utf-8") as f:
        f.write(plist_texto(puerto))
    dom = "gui/%d" % os.getuid()
    edificio("supervisor", ETIQUETA_PLIST, "macos", dom, ruta_plist())
    if os.environ.get("LOGI_EDIFICIO_SIN_LAUNCHCTL"):
        return "persistence: plist written to %s and supervisor recorded (launchctl NOT run: LOGI_EDIFICIO_SIN_LAUNCHCTL)" % ruta_plist()
    return "persistence: plist written to %s and supervisor recorded; it loads on the next `start` or at login" % ruta_plist()


# ---------------------------------------------------------------- subcommands
def cmd_start(args):
    t0 = time.time()
    if sys.version_info < (3, 9):
        raise SystemExit("Python 3.9 or newer is required")
    nuevo = instala_paquete()
    di("package %s: %s (sha256 %s...)" % (PAQUETE, "installed in " + pkg() if nuevo else "already in " + pkg(), PAQUETE_SHA256[:12]))
    modo, tok = pide_token(args)
    claude = busca_claude()
    if modo in ("token", "login") and not claude:
        di("Warning: `claude` not found in PATH (install it from claude.ai/code); the director will be SIMULATED.")
        modo = "simulated"
    if modo == "token":
        guarda_token(tok)
        di("token saved in %s (mode 600, outside any repo)" % _token())
    else:
        quita_enlace_token()
    if modo == "simulated":
        claude = escribe_falso()
        enlaza_token_falso()
    cf_act = (lee_json(os.path.join(pkg(), "config.json"), {}) or {}).get("a2a") or {}
    puerto = cf_act.get("puerto") if corre() and isinstance(cf_act.get("puerto"), int) else None     # already running: keep its port
    if not puerto:
        pedido = None
        for i, a in enumerate(args):
            if a == "--port" and i + 1 < len(args) and args[i + 1].isdigit():
                pedido = int(args[i + 1])
        puerto = puerto_libre(pedido)
    modo_previo = (lee_json(_estado(), {}) or {}).get("modo")
    sin_web = "--no-web" in args
    escribe_config(puerto, claude, modo)
    if corre() and modo_previo and modo_previo != modo:
        di("mode changed (%s -> %s): restarting the building; the director comes back with the new Claude" % (modo_previo, modo))
        para_todo()
    r = edificio("arranca")
    linea = (r.stdout.strip() or r.stderr.strip()).splitlines()[-1] if (r.stdout or r.stderr).strip() else "start: no output"
    di("building started" + (" (pid %d)" % corre()["pid"] if corre() else "") if "no hay ninguna" in linea else linea)   # the package says there is no page: now there is one
    if not corre():
        raise SystemExit("the building did not start; see %s" % os.path.join(pkg(), "edificio.log"))
    msg = crea_director(modo)
    wp = None if sin_web else arranca_web({puerto})
    escribe600(_estado(), json.dumps({"puerto": puerto, "web_puerto": wp, "modo": modo, "paquete_sha256": PAQUETE_SHA256, "modulo": VERSION_MODULO, "desde": int(time.time())}))
    if "--persistent" in args:
        di(hace_persistente(puerto))
    di("director: " + msg)
    di(texto_director(sala_director(6)))
    url = "http://127.0.0.1:%d" % puerto
    di("LOCAL BUILDING ready in %.1f s · loopback only · no connection to any server" % (time.time() - t0))
    di("  Local URL (A2A): %s/.well-known/agent-card.json   health: %s/salud" % (url, url))
    if modo == "simulated":
        di("  NOTE: the director is SIMULATED (no Claude). For real Claude: `logi building start --token-stdin` (token from `claude setup-token`) or `claude login`.")
    elif modo == "login":
        di("  The director uses your `claude login`. (On macOS the login lives in the keychain: if you start it over ssh, use a token.)")
    else:
        di("  The director uses your token (isolated: own HOME in %s)." % os.path.join(pkg(), "sesiones", "home"))
    if wp:
        muestra_web(wp, args)
    di("  status: logi building status · open the web screen: logi building open · stop: logi building stop · remove everything: logi building uninstall")


def cmd_status(_a):
    est = lee_json(_estado(), {}) or {}
    if not os.path.isfile(os.path.join(pkg(), "edificio.py")):
        return di("not installed (no %s)" % inst())
    d = corre()
    di("building: %s%s" % ("RUNNING pid %d" % d["pid"] if d else "stopped", " · director mode: %s" % est.get("modo", "?")))
    if est.get("puerto"):
        s = socket.socket()
        s.settimeout(1)
        ok = s.connect_ex(("127.0.0.1", est["puerto"])) == 0
        s.close()
        di("A2A local: http://127.0.0.1:%d (%s)" % (est["puerto"], "responding" if ok else "not responding"))
    di(texto_director(sala_director()) + (" · trusted (can open rooms)" if director_confiado() else " · not trusted (`sesiones confianza director on` to let it open rooms)"))
    wp = web_puerto_vivo()
    di("web screen: %s" % ("http://127.0.0.1:%d/ (responding; `logi building open` gives a single-use sign-in link)" % wp if wp else "not running"))
    di("web access secret: %s" % ("saved (600) in " + _web_token() if os.path.isfile(_web_token()) else "none yet"))
    di("token: %s" % ("saved (600) in " + _token() if os.path.isfile(_token()) else "none"))
    di("persistent: %s" % ("yes, " + ruta_plist() if os.path.isfile(ruta_plist()) else "no"))
    for p, c in pids_propios():
        di("  pid %d  %s" % (p, c))
    if d:
        r = edificio("estado")
        for l in (r.stdout or "").strip().splitlines()[:12]:
            di("  " + l)


def para_todo():
    P = pkg()
    if os.path.isfile(os.path.join(P, "edificio.py")):
        try:
            edificio("para", timeout=40)
        except subprocess.TimeoutExpired:
            pass
    for _ in range(3):
        vivos = pids_propios()
        if not vivos:
            break
        for p, _c in vivos:
            try:
                os.kill(p, signal.SIGTERM)
            except OSError:
                pass
        time.sleep(1.5)
    quedan = pids_propios()
    for p, _c in quedan:
        try:
            os.kill(p, signal.SIGKILL)
        except OSError:
            pass
    return len(pids_propios())


def cmd_open(args):
    wp = web_puerto_vivo()
    if not wp:
        raise SystemExit("the web screen is not running: `logi building start`")
    muestra_web(wp, args, forzar_abrir=True)


def cmd_stop(_a):
    n = para_todo()
    di("stopped" if not n else "%d own processes are still running" % n)


def cmd_uninstall(args):
    n = para_todo()
    if n:
        raise SystemExit("%d own processes did not stop; deleting nothing" % n)
    pl = ruta_plist()
    if os.path.isfile(pl) and ETIQUETA_PLIST in open(pl, encoding="utf-8").read():
        if sys.platform == "darwin" and not os.environ.get("LOGI_EDIFICIO_SIN_LAUNCHCTL"):
            subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), ETIQUETA_PLIST)], capture_output=True, stdin=subprocess.DEVNULL, timeout=30)
        os.remove(pl)
        di("plist removed: " + pl)
    for d, marca in ((inst(), ".logi_edificio_marca"), (seg(), None)):
        if not os.path.isdir(d):
            continue
        if marca and not os.path.isfile(os.path.join(d, marca)):
            di("%s does not carry my mark: leaving it alone" % d)
            continue
        if d == seg() and "--keep-token" in args:
            for f in os.listdir(d):
                if f != "token":
                    ruta = os.path.join(d, f)
                    os.remove(ruta) if os.path.isfile(ruta) else shutil.rmtree(ruta)
            di("kept only the token in " + _token())
            continue
        shutil.rmtree(d)
        di("deleted " + d)
    di("uninstalled. (With `claude login`, Claude Code may have recorded trust of the working folder in ~/.claude.json: that is left untouched.)")


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    cmd = a[0] if a else "--help"
    resto = a[1:]
    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    tabla = {"start": cmd_start, "open": cmd_open, "status": cmd_status, "stop": cmd_stop, "uninstall": cmd_uninstall}
    if cmd not in tabla:
        print(__doc__)
        return 2
    try:
        tabla[cmd](resto)
    except BrokenPipeError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
