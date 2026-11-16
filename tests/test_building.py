# Copyright (c) 2026 David Ragel
# SPDX-License-Identifier: BUSL-1.1
# Licensed under the Business Source License 1.1; see LICENSE.
"""Tests of `logi building` (the local building of agents and its web screen). No network, no real model, no real claude: a temporary HOME and the
fake claude of `--no-token`. Starts the building, checks that it listens only on 127.0.0.1, that repeating `start` duplicates
nothing, then stops and uninstalls and checks that nothing is left. Run: python tests/test_building.py"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
R = {"ok": 0, "fail": 0}


def check(cond, name, detail=""):
    R["ok" if cond else "fail"] += 1
    print(("ok    " if cond else "FAIL  ") + name + ((" - " + str(detail)[:160]) if detail and not cond else ""))


home = tempfile.mkdtemp(prefix="logi_building_test_")
env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CODE_OAUTH_TOKEN", "LOGI_EDIFICIO_TOKEN")}
env.pop("LOGI_BUILDING_NO_OPEN", None)
env.update(LOGI_BUILDING_NO_OPEN="1", HOME=home, PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1")


def logi(*a):
    r = subprocess.run([sys.executable, "-m", "logitration", "building", *a], env=env, capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL)
    return r.returncode, r.stdout + r.stderr


def get(url):
    try:
        return urllib.request.urlopen(url, timeout=5).status
    except Exception as e:
        return str(e)


try:
    rc, out = logi("--help")
    check(rc == 0 and "start" in out and "uninstall" in out, "--help lists start/status/stop/uninstall", out)
    rc, out = logi("arranca")
    check(rc == 2, "Spanish subcommands are gone", out)
    rc, out = logi("start", "--no-token")
    check(rc == 0 and "SIMULATED" in out, "start --no-token: the building starts, the director is SIMULATED", out)
    st = json.loads((Path(home) / ".logi_edificio" / "estado.json").read_text())
    port = st["puerto"]
    check(get("http://127.0.0.1:%d/salud" % port) == 200, "health answers on 127.0.0.1:%d" % port)
    card = json.load(urllib.request.urlopen("http://127.0.0.1:%d/.well-known/agent-card.json" % port, timeout=5))
    check(isinstance(card, dict) and "name" in card, "the A2A card is served")
    cfg = json.loads((Path(home) / "logi_edificio" / "edificio_pyro" / "config.json").read_text())
    check(list(cfg["a2a"]) == ["activo", "puerto"] and "director" in cfg["sesiones"]["fichas"], "config: no server connection, one director room", cfg["a2a"])
    check(cfg["sesiones"]["fichas"]["director"].get("confianza") is True, "the director is created trusted (it can open other rooms)", cfg["sesiones"]["fichas"]["director"])
    rc, out = logi("status")
    check(rc == 0 and "RUNNING" in out, "status: RUNNING", out)
    check("director" in out and "· trusted" in out, "status shows the director as trusted", out)
    t = time.time()
    rc, out = logi("start", "--no-token")
    cfg2 = json.loads((Path(home) / "logi_edificio" / "edificio_pyro" / "config.json").read_text())
    check(rc == 0 and cfg2["a2a"]["puerto"] == port and list(cfg2["sesiones"]["fichas"]) == ["director"], "start again: same port, still one director", out)
    pk = Path(home) / "logi_edificio" / "edificio_pyro"
    subprocess.run([sys.executable, "edificio.py", "sesiones", "confianza", "director", "off"], cwd=str(pk), env=env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    rc, out = logi("start", "--no-token")
    cfg3 = json.loads((pk / "config.json").read_text())
    check(rc == 0 and not cfg3["sesiones"]["fichas"]["director"].get("confianza"), "start again does not turn trust back on if the user removed it", cfg3["sesiones"]["fichas"]["director"])
    rc, out = logi("status")
    check("not trusted" in out, "status shows the director as not trusted", out)
    subprocess.run([sys.executable, "edificio.py", "sesiones", "confianza", "director", "on"], cwd=str(pk), env=env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    rc, out = logi("start", "--token-stdin")
    check(rc == 0 or "does not look like" in out, "--token-stdin with empty input does not crash", out)
    # ---- web screen (the building drawn as a street building): loopback, access secret, single-use link, Origin/Host/X-Clave checks, message to the director
    import re
    import urllib.error
    wp = st["web_puerto"]
    base = "http://127.0.0.1:%d" % wp

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    def web(path, cookie=None, method="GET", data=None, headers=None):
        req = urllib.request.Request(base + path, data=data, method=method, headers=headers or {})
        if cookie:
            req.add_header("Cookie", cookie)
        try:
            r = urllib.request.build_opener(_NoRedirect).open(req, timeout=5)
            return r.status, r.read().decode(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), e.headers

    check(web("/")[0] == 401 and web("/estado.json")[0] == 401, "web: no cookie -> 401")
    check(web("/ping")[0] == 200, "web: /ping answers without the cookie (liveness only)")
    rc, out = logi("open", "--no-open")
    link = re.search(r"http://127\.0\.0\.1:%d/\?n=\S+" % wp, out)
    check(rc == 0 and bool(link), "open --no-open prints a single-use link", out)
    secret = (Path(home) / ".logi_edificio" / "web_token").read_text().strip()
    c, _, h = web(link.group(0)[len(base):])
    cookie = h.get("Set-Cookie", "").split(";")[0]
    check(c == 302 and "HttpOnly" in h.get("Set-Cookie", "") and "SameSite=Strict" in h.get("Set-Cookie", "") and secret not in cookie, "link: 302 + HttpOnly SameSite=Strict cookie, not the secret", h.get("Set-Cookie"))
    check(web(link.group(0)[len(base):])[0] == 401, "the link works only once")
    check(oct((Path(home) / ".logi_edificio" / "web_token").stat().st_mode & 0o777) == "0o600", "web_token is mode 600")
    c, body, _ = web("/", cookie)
    check(c == 200 and "<title>Local building</title>" in body and "tú" in body, "web: with the cookie the building screen loads", body[:100])
    c, body, _ = web("/estado.json", cookie)
    est = json.loads(body) if c == 200 else {}
    names = [a["nombre"] for a in est.get("agentes", [])]
    check(c == 200 and "contador" in names and "lector" in names and est.get("plano", {}).get("0") == "director", "web: /estado.json lists the demo rooms and the director's room is on the floor plan", names)
    check(web("/estado.json", cookie, headers={"Host": "evil.example"})[0] == 421, "web: foreign Host -> 421")
    sid = next((a["sid"] for a in est.get("agentes", []) if a["nombre"] == "contador"), "")
    clave = (Path(home) / "logi_edificio" / "web" / "cache" / "clave").read_text().strip()
    msg = json.dumps({"sid": sid, "texto": "hello from the test", "autor": "user"}).encode()
    check(web("/habla", cookie, "POST", msg, {"Origin": base})[0] == 403, "web: POST without the page key -> 403")
    check(web("/habla", cookie, "POST", msg, {"X-Clave": clave, "Origin": "http://evil.example"})[0] == 403, "web: POST with a foreign Origin -> 403")
    c, body, _ = web("/habla", cookie, "POST", msg, {"X-Clave": clave, "Origin": base})
    check(c == 200 and json.loads(body).get("r"), "web: POST from the page -> 200 and a message id", body)
    for _ in range(30):
        c, body, _ = web("/charla?sid=" + sid, cookie)
        ch = json.loads(body) if c == 200 else {}
        if any(e.get("estado") in ("visto", "contestado") for e in ch.get("envios", [])):
            break
        time.sleep(0.5)
    check(any(e.get("estado") in ("visto", "contestado") for e in ch.get("envios", [])), "web: the message reaches the room (seen or answered)", ch.get("envios"))
    buz = (Path(home) / "logi_edificio" / "edificio_pyro" / "estado" / "contador.buzon.jsonl").read_text()
    check("hello from the test" in buz, "web: the message is in the room's mailbox")
    rc, out = logi("stop")
    time.sleep(0.5)
    check(rc == 0 and "stopped" in out and get("http://127.0.0.1:%d/salud" % port) != 200, "stop: the port is free", out)
    rc, out = logi("uninstall")
    check(rc == 0 and not (Path(home) / "logi_edificio").exists() and not (Path(home) / ".logi_edificio").exists(), "uninstall: nothing left in the home", out)
finally:
    logi("uninstall")
    import shutil
    shutil.rmtree(home, ignore_errors=True)

print(f"\nchecks: {R['ok'] + R['fail']}   failures: {R['fail']}")
sys.exit(1 if R["fail"] else 0)
