# Copyright (c) 2026 David Ragel
# SPDX-License-Identifier: BUSL-1.1
# Licensed under the Business Source License 1.1; see LICENSE.
"""Local web screen of the `logi building` (stdlib only, Python 3.9+). Started by `logi building start`; do not run it by hand.

Listens ONLY on 127.0.0.1. Every page and API call needs the local access secret (a cookie derived from ~/.logi_edificio/web_token);
the cookie is handed out by a single-use sign-in link made by `logi building open`. It is a local secret, not a user password.
It reads the building's state files and, to send a message to a room, appends one line to that room's mailbox (the building's own file contract).

Environment (set by `logi building start`): LOGI_WEB_PKG (the building package dir), LOGI_WEB_SEG (~/.logi_edificio), LOGI_WEB_PORT.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

PKG = os.environ.get("LOGI_WEB_PKG", "")
SEG = os.environ.get("LOGI_WEB_SEG", "")
PORT = int(os.environ.get("LOGI_WEB_PORT", "0") or 0)
ESTADO = os.path.join(PKG, "estado")
NOMBRE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\^\[\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|[\x00-\x08\x0b-\x1f\x7f]")
COOKIE = "lb"
DELIV = {"entregado": "delivered", "visto": "seen", "contestado": "answered", "retenido": "held"}   # states written by the room anchor
_envios = []


def lee_json(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def lineas(p, ultimas=200):
    out = []
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for l in f.readlines()[-ultimas:]:
                try:
                    x = json.loads(l)
                    if isinstance(x, dict):
                        out.append(x)
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def secreto():
    try:
        return open(os.path.join(SEG, "web_token"), encoding="utf-8").read().strip()
    except OSError:
        return ""


SECRETO = secreto()
GALLETA = hmac.new(SECRETO.encode(), b"logi-building-cookie-v1", hashlib.sha256).hexdigest() if SECRETO else ""


def consume_enlace(n):
    """A sign-in link is single use and expires (written by `logi building open` into ~/.logi_edificio/web_nonce.json)."""
    ruta = os.path.join(SEG, "web_nonce.json")
    lista = lee_json(ruta, [])
    ahora = time.time()
    ok, resto = False, []
    for e in lista if isinstance(lista, list) else []:
        if not isinstance(e, dict) or e.get("exp", 0) < ahora:
            continue
        if hmac.compare_digest(str(e.get("n", "")), n):
            ok = True
        else:
            resto.append(e)
    if ok or len(resto) != len(lista if isinstance(lista, list) else []):
        fd = os.open(ruta + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(resto, f)
        os.replace(ruta + ".tmp", ruta)
    return ok


def salas():
    pl = lee_json(os.path.join(ESTADO, "plano.json"), {}) or {}
    dire = pl.get("directivos") or {}
    ag = lee_json(os.path.join(ESTADO, "agentes.json"), {}) or {}
    lista = []
    for sala, n in sorted(dire.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 99):
        lista.append((n, int(sala) if str(sala).isdigit() else None))
    vistos = {n for n, _ in lista}
    lista += [(n, None) for n in ag if n not in vistos]
    return pl, [(n, s) for n, s in lista if NOMBRE.match(str(n))]


def estado():
    pl, lista = salas()
    emb = pl.get("emblemas") or {}
    rooms = []
    for n, sala in lista:
        e = lee_json(os.path.join(ESTADO, n + ".json"), {}) or {}
        rooms.append({"name": n, "sala": sala if sala is not None else e.get("sala"), "emblem": emb.get(n, ""), "estado": e.get("estado", "?"),
                      "detalle": e.get("estado_detalle", ""), "que": str(e.get("que", ""))[:160], "tool": e.get("herramienta", ""),
                      "question": str(e.get("pregunta", ""))[:160], "swap": (e.get("retenida") or {}).get("swap_mb"),
                      "kind": e.get("tipo", "demo")})
    est = lee_json(os.path.join(SEG, "estado.json"), {}) or {}
    return {"mode": est.get("modo", "?"), "a2a_port": est.get("puerto"), "labels": pl.get("rotulos") or {}, "rooms": rooms, "t": time.time()}


def charla(n):
    msgs = []
    for m in lineas(os.path.join(ESTADO, n + ".charla.jsonl"), 80):
        msgs.append({"who": str(m.get("quien", n))[:40], "text": str(m.get("texto", ""))[:4000], "t": m.get("t", 0), "mine": False})
    hechos = {m for m in (x.get("id") for x in lineas(os.path.join(ESTADO, n + ".buzon.jsonl.hecho"), 400)) if m}
    ent = {}
    for x in lineas(os.path.join(ESTADO, n + ".entrega.jsonl"), 400):
        ent[x.get("id")] = x.get("estado", "")
    for m in lineas(os.path.join(ESTADO, n + ".buzon.jsonl"), 80):
        if m.get("de") != "usuario":
            continue
        ide = m.get("id")
        msgs.append({"who": "you", "text": str(m.get("texto", ""))[:4000], "t": m.get("t", 0), "mine": True,
                     "delivery": "answered" if ide in hechos else DELIV.get(ent.get(ide), str(ent.get(ide) or "queued"))})
    msgs.sort(key=lambda m: m["t"])
    pant = ""
    try:
        with open(os.path.join(PKG, "sesiones", n + ".pantalla.txt"), encoding="utf-8", errors="replace") as f:
            pant = ANSI.sub("", "".join(f.readlines()[-25:]))[-3000:]
    except OSError:
        pass
    return {"name": n, "messages": msgs[-80:], "screen": pant}


def envia(n, texto):
    ahora = time.time()
    _envios[:] = [t for t in _envios if ahora - t < 60]
    if len(_envios) >= 30:
        return 429, {"error": "too many messages in a minute"}
    _envios.append(ahora)
    texto = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", texto).strip()[:1500]
    if not texto:
        return 400, {"error": "empty message"}
    # The building's own mailbox contract (estado/<room>.buzon.jsonl, one JSON line; same fields as edificio._buzon). A message "de": "usuario" is typed into
    # the room as coming from a person. (`edificio.py orden mensaje` would force "agente", i.e. a message from another room, which the room treats as data.)
    linea = json.dumps({"id": secrets.token_hex(6), "de": "usuario", "texto": texto, "t": time.time()}, ensure_ascii=False) + "\n"
    try:
        with open(os.path.join(ESTADO, n + ".buzon.jsonl"), "a", encoding="utf-8") as f:
            f.write(linea)
    except OSError as x:
        return 502, {"ok": False, "error": "could not write the mailbox: %s" % x}
    return 200, {"ok": True, "result": "message in the mailbox of " + n}


class H(BaseHTTPRequestHandler):
    server_version = "logi-building-web"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="application/json", extra=()):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text") or ctype == "application/json" else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _host_ok(self):
        return (self.headers.get("Host") or "") in ("127.0.0.1:%d" % PORT, "localhost:%d" % PORT)

    def _cookie_ok(self):
        if not GALLETA:
            return False
        for parte in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = parte.strip().partition("=")
            if k == COOKIE and hmac.compare_digest(v, GALLETA):
                return True
        return False

    def _no(self):
        self._send(401, "<!doctype html><meta charset=utf-8><title>Access needed</title><body style='font:16px system-ui;max-width:34em;margin:3em auto;padding:0 1em'>"
                        "<h1>Access needed</h1><p>This is the local logi building screen. Run <code>logi building open</code> in a terminal on this computer "
                        "to get a single-use sign-in link.</p></body>", "text/html")

    def do_GET(self):
        u = urlsplit(self.path)
        if not self._host_ok():
            return self._send(421, {"error": "bad host"})
        q = parse_qs(u.query)
        if u.path == "/ping":
            return self._send(200, {"ok": True})
        if u.path == "/" and "n" in q:
            if consume_enlace(q["n"][0]):
                return self._send(302, b"", "text/plain", [("Location", "/"), ("Set-Cookie", "%s=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=604800" % (COOKIE, GALLETA))])
            return self._no()
        if not self._cookie_ok():
            return self._no()
        if u.path == "/":
            return self._send(200, PAGINA, "text/html")
        if u.path == "/api/state":
            return self._send(200, estado())
        m = re.fullmatch(r"/api/room/([A-Za-z0-9_-]{1,40})", u.path)
        if m and m.group(1) in [n for n, _ in salas()[1]]:
            return self._send(200, charla(m.group(1)))
        self._send(404, {"error": "not found"})

    do_HEAD = do_GET

    def do_POST(self):
        if not self._host_ok():
            return self._send(421, {"error": "bad host"})
        if not self._cookie_ok():
            return self._no()
        org = self.headers.get("Origin")
        if org not in ("http://127.0.0.1:%d" % PORT, "http://localhost:%d" % PORT) or self.headers.get("X-Requested-With") != "logi":
            return self._send(403, {"error": "bad origin"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 8192:
                return self._send(413, {"error": "too large"})
            d = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        if self.path == "/api/send" and isinstance(d, dict) and isinstance(d.get("room"), str) and isinstance(d.get("text"), str):
            if d["room"] not in [x for x, _ in salas()[1]]:
                return self._send(404, {"error": "no such room"})
            c, r = envia(d["room"], d["text"])
            return self._send(c, r)
        self._send(404, {"error": "not found"})


PAGINA = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Local building</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--ink:#1c2230;--mut:#69728a;--line:#dfe3ea;--acc:#2f6fed;--ok:#1d9a5b;--warn:#c47a00;--bad:#c2372f;--mine:#e3edff}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#10141c;--card:#181e29;--ink:#e6e9f0;--mut:#8f99b0;--line:#2a3243;--acc:#6b9bff;--ok:#43c38a;--warn:#e0a63a;--bad:#ef6a62;--mine:#1f3358}}
:root[data-theme=dark]{--bg:#10141c;--card:#181e29;--ink:#e6e9f0;--mut:#8f99b0;--line:#2a3243;--acc:#6b9bff;--ok:#43c38a;--warn:#e0a63a;--bad:#ef6a62;--mine:#1f3358}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
header{display:flex;flex-wrap:wrap;gap:10px 16px;align-items:center;padding:14px 16px;border-bottom:1px solid var(--line);background:var(--card)}
h1{font-size:18px;margin:0}.badge{font-size:12px;padding:2px 9px;border-radius:99px;border:1px solid var(--line);color:var(--mut)}
.badge.sim{color:var(--warn);border-color:var(--warn)}.badge.real{color:var(--ok);border-color:var(--ok)}
main{display:grid;grid-template-columns:minmax(260px,380px) 1fr;gap:16px;padding:16px;max-width:1100px;margin:0 auto}
@media (max-width:760px){main{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
.floor{display:grid;grid-template-columns:70px repeat(3,1fr);gap:8px;align-items:stretch;margin-bottom:8px}
.lab{font-size:12px;color:var(--mut);align-self:center}
.room{border:1px dashed var(--line);border-radius:10px;min-height:74px;padding:6px;font-size:12px;display:flex;flex-direction:column;justify-content:center;align-items:center;text-align:center;background:transparent;color:var(--ink);font-family:inherit}
button.room{cursor:pointer;border-style:solid;background:var(--bg)}button.room.sel{outline:2px solid var(--acc)}
.room .em{font-size:22px}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--mut);margin-right:4px}
.busy .dot{background:var(--ok);animation:p 1.2s ease-in-out infinite}.held .dot{background:var(--warn)}.down .dot{background:var(--bad)}
@keyframes p{50%{transform:scale(1.8);opacity:.4}}@media (prefers-reduced-motion:reduce){.busy .dot{animation:none}}
.note{border-left:4px solid var(--warn);background:var(--bg);padding:8px 12px;border-radius:6px;margin:0 0 12px;font-size:14px}
#chat{height:300px;overflow:auto;border:1px solid var(--line);border-radius:8px;padding:8px;background:var(--bg)}
.m{max-width:85%;margin:0 0 8px;padding:6px 10px;border-radius:10px;background:var(--card);border:1px solid var(--line);white-space:pre-wrap;word-break:break-word}
.m.mine{margin-left:auto;background:var(--mine)}.m small{display:block;color:var(--mut);font-size:11px}
form{display:flex;gap:8px;margin-top:10px}input[type=text]{flex:1;padding:9px 11px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);font:inherit}
form button{padding:9px 16px;border:0;border-radius:8px;background:var(--acc);color:#fff;font:inherit;cursor:pointer}form button:disabled{opacity:.5}
pre{margin:8px 0 0;max-height:150px;overflow:auto;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px;font-size:12px;white-space:pre-wrap}
details summary{cursor:pointer;color:var(--mut);font-size:13px;margin-top:10px}.mut{color:var(--mut)}h2{font-size:16px;margin:0 0 4px}
</style></head><body>
<header><h1>Local building</h1><span id="mode" class="badge">…</span><span class="badge">127.0.0.1 only · no server connection</span></header>
<main>
<section class="card"><h2>Floor plan</h2><div class="mut" style="font-size:13px;margin-bottom:10px">Click a room.</div><div id="plan"></div></section>
<section class="card"><h2 id="rt">Room</h2><div id="rs" class="mut" style="margin-bottom:10px"></div><div id="note"></div>
<div id="chat" aria-live="polite"></div>
<form id="f"><input id="txt" type="text" maxlength="1500" placeholder="Write a message to this room" autocomplete="off"><button id="go">Send</button></form>
<div id="fb" class="mut" style="font-size:13px;margin-top:6px"></div>
<details><summary>Terminal view of the room</summary><pre id="scr">(nothing yet)</pre></details></section>
</main>
<script>
var hp=(location.hash||'#director').slice(1).split('/'),sel=hp[0]||'director',S=null,last='';if(hp[1]=='light'||hp[1]=='dark')document.documentElement.setAttribute('data-theme',hp[1]);   /* #room/dark forces a theme */
function $(i){return document.getElementById(i)}
function el(t,c,x){var e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}
function get(u){return fetch(u,{credentials:'same-origin'}).then(function(r){if(r.status==401){location.reload();throw 0}return r.json()})}
function cls(r){return r.detalle=='retenida_swap'?'held':r.estado=='trabaja'||r.estado=='busy'?'busy':r.estado=='caida'?'down':''}
function human(r){if(r.detalle=='retenida_swap')return 'held by the swap guard';return ({trabaja:'working',busy:'working',idle:'idle',espera:'asking you',descansa:'resting',caida:'down'})[r.estado]||r.estado}
function plan(){var p=$('plan');p.textContent='';var max=0;S.rooms.forEach(function(r){if(r.sala!=null&&r.sala>max)max=r.sala});
 var rows=Math.max(3,Math.ceil((max+1)/3)),by={};S.rooms.forEach(function(r){if(r.sala!=null)by[r.sala]=r});
 for(var i=0;i<rows;i++){var f=el('div','floor');f.appendChild(el('div','lab',S.labels[String(i*3)]||'Floor '+(i+1)));
  for(var j=0;j<3;j++){var r=by[i*3+j];if(!r){f.appendChild(el('div','room mut','empty'));continue}
   var b=el('button','room '+cls(r)+(r.name==sel?' sel':''));b.type='button';b.appendChild(el('span','em',r.emblem||'🏠'));
   var n=el('span',null);n.appendChild(el('span','dot'));n.appendChild(document.createTextNode(r.name));b.appendChild(n);b.appendChild(el('span','mut',human(r)));
   b.onclick=(function(x){return function(){sel=x.name;location.hash=sel+(hp[1]?'/'+hp[1]:'');last='';plan();room()}})(r);f.appendChild(b)}
  p.appendChild(f)}}
function room(){var r=S.rooms.filter(function(x){return x.name==sel})[0];if(!r&&S.rooms.length){r=S.rooms[0];sel=r.name}if(!r)return;
 $('rt').textContent=(r.emblem?r.emblem+' ':'')+r.name;$('rs').textContent=human(r)+(r.que&&r.detalle!='retenida_swap'?' · '+r.que:'')+(r.tool?' ('+r.tool+')':'')+(r.question?' · \u201c'+r.question+'\u201d':'');
 var n=$('note');n.textContent='';
 if(r.detalle=='retenida_swap'){var d=el('div','note');d.appendChild(el('b',null,'This room is held on purpose. '));d.appendChild(document.createTextNode('The building’s swap guard keeps it stopped because this computer is using '+(r.swap||'too much')+' MB of swap (limit 4096 MB). It starts by itself when swap drops. Messages you send now wait in its mailbox.'));n.appendChild(d)}
 else if(r.kind=='demo'){var d2=el('div','note');d2.textContent='Demo room: it only notes your message, it does not answer.';n.appendChild(d2)}
 else if(S.mode=='simulated'){var d3=el('div','note');d3.textContent='Simulated director (no Claude): it only echoes in its terminal view. For real Claude: logi building start --token-stdin';n.appendChild(d3)}
 get('/api/room/'+encodeURIComponent(sel)).then(function(c){var k=JSON.stringify(c);if(k==last)return;last=k;var b=$('chat'),bottom=b.scrollTop+b.clientHeight>=b.scrollHeight-30;b.textContent='';
  if(!c.messages.length)b.appendChild(el('div','mut','No messages yet.'));
  c.messages.forEach(function(m){var d=el('div','m'+(m.mine?' mine':''),m.text);var s=el('small',null,(m.mine?'you':m.who)+' · '+new Date(m.t*1000).toLocaleTimeString()+(m.mine?' · '+m.delivery:''));d.insertBefore(s,d.firstChild);b.appendChild(d)});
  if(bottom)b.scrollTop=b.scrollHeight;$('scr').textContent=c.screen||'(nothing yet)'})}
function tick(){get('/api/state').then(function(s){S=s;var m=$('mode');m.textContent=s.mode=='simulated'?'simulated director':s.mode=='token'?'director: your Claude token':s.mode=='login'?'director: your claude login':s.mode;m.className='badge '+(s.mode=='simulated'?'sim':'real');plan();room()})}
$('f').onsubmit=function(e){e.preventDefault();var t=$('txt').value.trim();if(!t)return;$('go').disabled=true;$('fb').textContent='Sending…';
 fetch('/api/send',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Requested-With':'logi'},body:JSON.stringify({room:sel,text:t})}).then(function(r){return r.json()}).then(function(j){
  $('fb').textContent=j.ok?'Sent: '+j.result:'Not sent: '+(j.error||j.result);if(j.ok)$('txt').value='';last='';room()}).catch(function(){$('fb').textContent='Not sent'}).then(function(){$('go').disabled=false})};
tick();setInterval(tick,2000);
</script></body></html>
"""


def main():
    if not (PKG and SEG and PORT and SECRETO):
        sys.exit("started by `logi building start` only (missing environment or access secret)")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    srv.daemon_threads = True
    ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web.pid")
    with open(ruta, "w") as f:
        json.dump({"pid": os.getpid(), "port": PORT}, f)
    try:
        srv.serve_forever()
    finally:
        try:
            os.remove(ruta)
        except OSError:
            pass


if __name__ == "__main__":
    main()
