# Copyright (c) 2026 David Ragel
# SPDX-License-Identifier: BUSL-1.1
# Licensed under the Business Source License 1.1; see LICENSE.
"""A LOCAL BUILDING of agents for `logi`, with one command. Stdlib only, Python 3.9+.

    logi building start [--persistent] [--token-stdin] [--no-token] [--port N]
    logi building status | stop | uninstall [--keep-token]

(`logi edificio ...` also works; or `python3 -m logitration building start|status|stop|uninstall`)

What `start` does (idempotent: running it again duplicates nothing and breaks nothing):
  1. checks requirements (python >= 3.9, `claude` in PATH or says so, a free port);
  2. installs the building package (edificio_pyro_0.9.3.tar.gz next to this file; fixed sha256, verified) under ~/logi_edificio/
     (never in anybody else's building: nothing outside ~/logi_edificio and ~/.logi_edificio);
  3. writes config.json with ONE free port picked automatically, listening ONLY on 127.0.0.1 (local A2A); no connection to any server;
  4. asks for the Claude TOKEN if missing: env var CLAUDE_CODE_OAUTH_TOKEN or LOGI_EDIFICIO_TOKEN, or the file ~/.logi_edificio/token (600), or it
     prompts you (hidden; the one from `claude setup-token`), or --token-stdin; empty Enter = use your `claude login` if there is a session,
     otherwise SIMULATED mode;
  5. starts the building, creates the `director` room (real Claude if there is a token or session; otherwise a FAKE claude, and it says so)
     and prints the local URL.
No LaunchAgent by default; --persistent writes a plist in ~/Library/LaunchAgents only if that directory is yours.
The token: never in argv, logs, the `ps` environment or the plist: a 600 file, which the anchor hands to claude by file descriptor.
No pkill/pgrep: only processes whose pid the building itself saved (and verified with ps) are stopped.
"""
import getpass
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time

VERSION_MODULO = "1.1 (15-11-2026)"
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
import json, sys
a = sys.argv[1:]
if a[:1] == ["--version"]:
    print("0.0.0-simulated (logi_edificio)")
elif a[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "simulated"}))
else:
    print("[simulated] director without real Claude: give a token with `logi building start --token-stdin` or run `claude login`.", flush=True)
    for linea in sys.stdin:
        linea = linea.strip()
        if linea:
            print("[simulated] received: " + linea[:200], flush=True)
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
    return r.stdout.strip()[:200]


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
    cf_act = (lee_json(os.path.join(pkg(), "config.json"), {}) or {}).get("a2a") or {}
    puerto = cf_act.get("puerto") if corre() and isinstance(cf_act.get("puerto"), int) else None     # already running: keep its port
    if not puerto:
        pedido = None
        for i, a in enumerate(args):
            if a == "--port" and i + 1 < len(args) and args[i + 1].isdigit():
                pedido = int(args[i + 1])
        puerto = puerto_libre(pedido)
    modo_previo = (lee_json(_estado(), {}) or {}).get("modo")
    escribe_config(puerto, claude, modo)
    if corre() and modo_previo and modo_previo != modo:
        di("mode changed (%s -> %s): restarting the building; the director comes back with the new Claude" % (modo_previo, modo))
        para_todo()
    r = edificio("arranca")
    di((r.stdout.strip() or r.stderr.strip()).splitlines()[-1] if (r.stdout or r.stderr).strip() else "start: no output")
    if not corre():
        raise SystemExit("the building did not start; see %s" % os.path.join(pkg(), "edificio.log"))
    msg = crea_director(modo)
    escribe600(_estado(), json.dumps({"puerto": puerto, "modo": modo, "paquete_sha256": PAQUETE_SHA256, "modulo": VERSION_MODULO, "desde": int(time.time())}))
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
    di("  status: logi building status · stop: logi building stop · remove everything: logi building uninstall")


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
    di(texto_director(sala_director()))
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
    tabla = {"start": cmd_start, "status": cmd_status, "stop": cmd_stop, "uninstall": cmd_uninstall}
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
