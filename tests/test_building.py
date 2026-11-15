# Copyright (c) 2026 David Ragel
# SPDX-License-Identifier: BUSL-1.1
# Licensed under the Business Source License 1.1; see LICENSE.
"""Tests of `logi building` (the local building of agents). No network, no real model, no real claude: a temporary HOME and the
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
env.update(HOME=home, PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1")


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
    rc, out = logi("status")
    check(rc == 0 and "RUNNING" in out, "status: RUNNING", out)
    t = time.time()
    rc, out = logi("start", "--no-token")
    cfg2 = json.loads((Path(home) / "logi_edificio" / "edificio_pyro" / "config.json").read_text())
    check(rc == 0 and cfg2["a2a"]["puerto"] == port and list(cfg2["sesiones"]["fichas"]) == ["director"], "start again: same port, still one director", out)
    rc, out = logi("start", "--token-stdin")
    check(rc == 0 or "does not look like" in out, "--token-stdin with empty input does not crash", out)
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
