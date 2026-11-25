"""Phone access for MOSS Image Studio: a small password-protected web page served by the app.

Off by default. When turned on it listens either on this PC only (127.0.0.1) or on all network
addresses (for phones on the same network or over a WireGuard tunnel). Plain HTTP: use it on a
trusted network or through the tunnel, not over the open internet.

The server never touches the window directly; it talks to the app through a small `bridge`:
  bridge.state()         -> dict for /api/status (built on the window thread)
  bridge.submit(params)  -> queues a picture request
  bridge.picture(name, thumb) -> Path of a recent picture or None
  bridge.options()       -> models, shapes and sizes for the form
"""
import hashlib
import hmac
import json
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SESSION_HOURS = 12


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1).hex()
    return salt, digest


def check_password(password, salt, digest):
    return hmac.compare_digest(hash_password(password, salt)[1], digest)


PAGE_LOGIN = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,
initial-scale=1"><title>MOSS Image Studio</title><link rel="stylesheet" href="/style.css"></head><body>
<main><h1>MOSS Image Studio</h1><form method="post" action="/login"><label>Password
<input type="password" name="password" autofocus autocomplete="current-password"></label>
<button>Sign in</button></form><p class="msg">{msg}</p></main></body></html>"""

PAGE_APP = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,
initial-scale=1"><title>MOSS Image Studio</title><link rel="stylesheet" href="/style.css"></head>
<body data-csrf="{csrf}"><main><h1>MOSS Image Studio</h1>
<label>What do you want to see?<textarea id="prompt" rows="5"></textarea></label>
<div class="row"><label>Model<select id="model"></select></label>
<label>Shape<select id="shape"></select></label><label>Size<select id="tier"></select></label></div>
<button id="go">Make picture</button><p id="status" class="msg"></p>
<h2>Recent pictures</h2><div id="recent"></div>
<form method="post" action="/logout"><button class="quiet">Sign out</button></form></main>
<script src="/app.js"></script></body></html>"""

STYLE = """:root{color-scheme:light dark;--bg:#f3f4f6;--card:#fff;--fg:#1d1f23;--muted:#666;--accent:#0067c0}
@media (prefers-color-scheme:dark){:root{--bg:#1f2023;--card:#2b2d31;--fg:#e8e8e8;--muted:#a0a4ab;--accent:#4c8dff}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px system-ui,Segoe UI,sans-serif}
main{max-width:720px;margin:0 auto;padding:16px}h1{font-size:1.3rem}h2{font-size:1.05rem;margin-top:24px}
label{display:block;margin:10px 0;color:var(--muted)}textarea,select,input{display:block;width:100%;box-sizing:border-box;
margin-top:4px;padding:10px;border-radius:8px;border:1px solid #888;background:var(--card);color:var(--fg);font-size:16px}
.row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}button{background:var(--accent);color:#fff;border:0;
border-radius:8px;padding:12px 18px;font-size:16px;margin-top:8px}button.quiet{background:transparent;color:var(--muted);
padding:8px 0}.msg{color:var(--muted)}#recent{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));
gap:8px}#recent a{display:block}#recent img{width:100%;border-radius:6px;display:block}#recent span{font-size:12px;
color:var(--muted)}@media (max-width:480px){.row{grid-template-columns:1fr}}"""

SCRIPT = """const csrf=document.body.dataset.csrf;const $=id=>document.getElementById(id);
function fill(sel,items){sel.innerHTML='';for(const [v,t] of items){const o=document.createElement('option');
o.value=v;o.textContent=t;sel.appendChild(o);}}
async function options(){const r=await fetch('/api/options');const d=await r.json();fill($('model'),d.models);
fill($('shape'),d.shapes);fill($('tier'),d.tiers);if(d.model)$('model').value=d.model;}
async function status(){try{const r=await fetch('/api/status');if(r.status==401){location.reload();return;}
const d=await r.json();$('status').textContent=d.status+(d.waiting?` (${d.waiting} waiting)`:'');
const box=$('recent');box.innerHTML='';for(const p of d.recent){const a=document.createElement('a');
a.href='/pic/'+encodeURIComponent(p.name);a.target='_blank';const img=document.createElement('img');
img.src='/pic/'+encodeURIComponent(p.name)+'?thumb=1';img.alt=p.prompt;img.loading='lazy';const s=document.createElement('span');
s.textContent=p.prompt.slice(0,80);a.appendChild(img);a.appendChild(s);box.appendChild(a);}}catch(e){}}
$('go').onclick=async()=>{const prompt=$('prompt').value.trim();if(!prompt){$('status').textContent='Type a prompt first.';return;}
$('go').disabled=true;const r=await fetch('/api/make',{method:'POST',headers:{'Content-Type':'application/json',
'X-CSRF-Token':csrf},body:JSON.stringify({prompt,model:$('model').value,shape:$('shape').value,tier:+$('tier').value})});
const d=await r.json();$('status').textContent=d.message||'';$('go').disabled=false;status();};
options();status();setInterval(status,4000);"""


class RemoteServer:
    def __init__(self, bridge, host, port, salt, digest):
        self.bridge, self.host, self.port = bridge, host, port
        self.salt, self.digest = salt, digest
        self.sessions = {}            # token -> (csrf, expires)
        self.failures = {}            # ip -> [times]
        self.lock = threading.Lock()
        self.httpd = None

    def start(self):
        server = self
        handler = type("Handler", (_Handler,), {"server_app": server})
        self.httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None

    # sessions
    def new_session(self):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.lock:
            self.sessions[token] = (csrf, time.time() + SESSION_HOURS * 3600)
        return token, csrf

    def session(self, token):
        with self.lock:
            entry = self.sessions.get(token or "")
            if entry and entry[1] > time.time():
                return entry
            self.sessions.pop(token or "", None)
            return None

    def too_many_failures(self, ip):
        now = time.time()
        with self.lock:
            recent = [t for t in self.failures.get(ip, []) if now - t < 600]
            self.failures[ip] = recent
            return len(recent) >= 5

    def failed(self, ip):
        with self.lock:
            self.failures.setdefault(ip, []).append(time.time())


class _Handler(BaseHTTPRequestHandler):
    server_app = None
    server_version = "MOSS-Image-Studio"
    sys_version = ""

    def log_message(self, *args):
        pass

    # helpers
    def _send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self'; style-src 'self'; "
                                                    "script-src 'self'; form-action 'self'; frame-ancestors 'none'")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json")

    def _token(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        return cookie["moss_session"].value if "moss_session" in cookie else None

    def _session(self):
        return self.server_app.session(self._token())

    def _body(self, limit=200_000):
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise ValueError("request too large")
        return self.rfile.read(length)

    # routes
    def do_GET(self):
        url = urlparse(self.path)
        app = self.server_app
        if url.path == "/style.css":
            return self._send(200, STYLE, "text/css")
        if url.path == "/app.js":
            return self._send(200, SCRIPT, "application/javascript")
        sess = self._session()
        if url.path == "/":
            if not sess:
                return self._send(200, PAGE_LOGIN.format(msg=""))
            return self._send(200, PAGE_APP.format(csrf=sess[0]))
        if not sess:
            return self._json(401, {"message": "Sign in first."})
        if url.path == "/api/status":
            return self._json(200, app.bridge.state())
        if url.path == "/api/options":
            return self._json(200, app.bridge.options())
        if url.path.startswith("/pic/"):
            from urllib.parse import unquote
            name = unquote(url.path[len("/pic/"):])
            path = app.bridge.picture(name, "thumb" in parse_qs(url.query))
            if not path:
                return self._send(404, "Not found", "text/plain")
            ctype = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
            return self._send(200, path.read_bytes(), ctype)
        return self._send(404, "Not found", "text/plain")

    def do_POST(self):
        url = urlparse(self.path)
        app = self.server_app
        ip = self.client_address[0]
        try:
            body = self._body()
        except ValueError:
            return self._send(413, "Too large", "text/plain")
        if url.path == "/login":
            if app.too_many_failures(ip):
                return self._send(429, PAGE_LOGIN.format(msg="Too many tries. Wait 10 minutes."))
            password = parse_qs(body.decode("utf-8", "replace")).get("password", [""])[0]
            if not check_password(password, app.salt, app.digest):
                app.failed(ip)
                time.sleep(1)
                return self._send(401, PAGE_LOGIN.format(msg="Wrong password."))
            token, _ = app.new_session()
            return self._send(303, "", headers={"Location": "/", "Set-Cookie":
                              f"moss_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_HOURS * 3600}"})
        if url.path == "/logout":
            with app.lock:
                app.sessions.pop(self._token() or "", None)
            return self._send(303, "", headers={"Location": "/", "Set-Cookie": "moss_session=; Max-Age=0; Path=/"})
        sess = self._session()
        if not sess:
            return self._json(401, {"message": "Sign in first."})
        if self.headers.get("X-CSRF-Token") != sess[0]:
            return self._json(403, {"message": "Please reload the page."})
        if url.path == "/api/make":
            try:
                params = json.loads(body.decode("utf-8"))
            except Exception:
                return self._json(400, {"message": "Bad request."})
            prompt = str(params.get("prompt", "")).strip()[:4000]
            if not prompt:
                return self._json(400, {"message": "Type a prompt first."})
            message = app.bridge.submit(dict(prompt=prompt, model=str(params.get("model", "")),
                                             shape=str(params.get("shape", "")), tier=int(params.get("tier", 0))))
            return self._json(200, {"message": message})
        return self._json(404, {"message": "Not found"})
