"""Phone access for MOSS Image Studio: a password-protected web app served by the running app, so a phone or
tablet on the same network (or over a WireGuard tunnel) can use this computer's graphics card.

Off by default. When turned on it listens either on this computer only (127.0.0.1) or on all network addresses.
Plain HTTP: use it on a trusted network or through the tunnel, not over the open internet.

This module is only the web server: sign-in, sessions, static files and routing. Everything the phone can see
or do is in moss_phone.Bridge, which runs it against the open window.
"""
import hashlib
import hmac
import json
import mimetypes
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

SESSION_DAYS = 30                  # a phone stays signed in this long
MAX_UPLOAD = 60 * 2**20            # photos from a phone
MAX_BODY = 16 * 2**20              # everything else (a painted area is sent with the request)
STATIC = {"/app.js": "app.js", "/style.css": "style.css", "/manifest.webmanifest": "manifest.webmanifest",
          "/icon-192.png": "icon-192.png", "/icon-512.png": "icon-512.png",
          "/apple-touch-icon.png": "apple-touch-icon.png", "/favicon.ico": "icon-192.png"}


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1).hex()
    return salt, digest


def check_password(password, salt, digest):
    return hmac.compare_digest(hash_password(password, salt)[1], digest)


def _hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


LOGIN = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="MOSS Studio"><meta name="theme-color" content="#16181c">
<link rel="manifest" href="/manifest.webmanifest"><link rel="apple-touch-icon" href="/apple-touch-icon.png">
<title>MOSS Image Studio</title><link rel="stylesheet" href="/style.css"></head>
<body class="login"><main class="login-box"><img src="/icon-192.png" alt="" class="login-icon">
<h1>MOSS Image Studio</h1><p class="muted">Sign in with the phone access password you set on the computer.</p>
<form method="post" action="/login"><label>Password
<input type="password" name="password" autofocus autocomplete="current-password" required></label>
<button class="primary wide">Sign in</button></form><p class="error">{msg}</p></main></body></html>"""


class RemoteServer:
    def __init__(self, bridge, host, port, salt, digest, web_dir, sessions_file=None):
        self.bridge, self.host, self.port = bridge, host, port
        self.salt, self.digest = salt, digest
        self.web_dir = Path(web_dir)
        self.sessions_file = Path(sessions_file) if sessions_file else None
        self.sessions = self._load_sessions()      # sha256(token) -> [csrf, expires]
        self.failures = {}                          # ip -> [times]
        self.lock = threading.Lock()
        self.httpd = None

    def start(self):
        handler = type("Handler", (_Handler,), {"server_app": self})
        self.httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None

    # ---- sessions (kept in a file so phones stay signed in when the app restarts) ----
    def _load_sessions(self):
        try:
            data = json.loads(self.sessions_file.read_text(encoding="utf-8"))
            now = time.time()
            return {k: v for k, v in data.items() if v[1] > now}
        except Exception:
            return {}

    def _save_sessions(self):
        if not self.sessions_file:
            return
        try:
            self.sessions_file.write_text(json.dumps(self.sessions), encoding="utf-8")
        except OSError:
            pass

    def new_session(self):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.lock:
            self.sessions[_hash_token(token)] = [csrf, time.time() + SESSION_DAYS * 86400]
            self._save_sessions()
        return token, csrf

    def session(self, token):
        if not token:
            return None
        key = _hash_token(token)
        with self.lock:
            entry = self.sessions.get(key)
            if entry and entry[1] > time.time():
                return entry
            if self.sessions.pop(key, None):
                self._save_sessions()
            return None

    def end_session(self, token):
        if token:
            with self.lock:
                self.sessions.pop(_hash_token(token), None)
                self._save_sessions()

    def too_many_failures(self, ip):
        now = time.time()
        with self.lock:
            recent = [t for t in self.failures.get(ip, []) if now - t < 600]
            self.failures[ip] = recent
            return len(recent) >= 5

    def failed(self, ip):
        with self.lock:
            self.failures.setdefault(ip, []).append(time.time())


class ApiError(Exception):
    def __init__(self, message, code=400, **extra):
        super().__init__(message)
        self.code, self.extra = code, extra


class _Handler(BaseHTTPRequestHandler):
    server_app = None
    server_version = "MOSS-Image-Studio"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    # ---- helpers ----
    def _send(self, code, body, ctype="text/html; charset=utf-8", headers=None, cache=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache or "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; script-src 'self'; "
                         "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json")

    def _token(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        return cookie["moss_session"].value if "moss_session" in cookie else None

    def _body(self, limit):
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            # read and drop it so the connection stays usable, then refuse
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 1 << 20))
                if not chunk:
                    break
                remaining -= len(chunk)
            raise ApiError("That's too big to send.", 413)
        return self.rfile.read(length) if length else b""

    def _static(self, name):
        path = self.server_app.web_dir / name
        if not path.is_file():
            return self._send(404, "Not found", "text/plain")
        ctype = {".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".webmanifest": "application/manifest+json", ".png": "image/png",
                 ".html": "text/html; charset=utf-8"}.get(path.suffix, "application/octet-stream")
        return self._send(200, path.read_bytes(), ctype, cache="no-cache")

    def _file(self, path, download=False, cache="private, max-age=604800"):
        if not path:
            return self._send(404, "Not found", "text/plain")
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        headers = {}
        if download:
            safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in path.name)
            headers["Content-Disposition"] = f'attachment; filename="{safe}"'
        return self._send(200, path.read_bytes(), ctype, headers, cache=cache)

    # ---- routes ----
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self._get()
        except ApiError as e:
            self._json(e.code, dict(message=str(e), **e.extra))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:                         # never let one request take the server down
            try:
                self._json(500, {"message": f"Something went wrong on the computer: {e}"})
            except Exception:
                pass

    def _get(self):
        url = urlparse(self.path)
        app = self.server_app
        if url.path in STATIC:
            return self._static(STATIC[url.path])
        sess = app.session(self._token())
        if url.path in ("/", "/index.html"):
            if not sess:
                return self._send(200, LOGIN.format(msg=""))
            page = (app.web_dir / "index.html").read_text(encoding="utf-8").replace("{{csrf}}", sess[0])
            return self._send(200, page)
        if not sess:
            return self._json(401, {"message": "Sign in first."})
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        b = app.bridge
        if url.path == "/api/state":
            return self._json(200, b.state())
        if url.path == "/api/options":
            return self._json(200, b.options())
        if url.path == "/api/gallery":
            return self._json(200, b.gallery(q.get("q", ""), int(q.get("offset", 0) or 0),
                                             int(q.get("limit", 60) or 60)))
        if url.path == "/api/info":
            return self._json(200, b.info(q.get("name", "")))
        if url.path == "/api/estimate":
            return self._json(200, b.estimate(q))
        if url.path == "/api/tokens":
            return self._json(200, {"tokens": b.tokens(q.get("text", ""))})
        if url.path == "/api/task":
            return self._json(200, b.task(q.get("id", "")))
        for prefix, kind in (("/pic/", "full"), ("/view/", "view"), ("/thumb/", "thumb")):
            if url.path.startswith(prefix):
                name = unquote(url.path[len(prefix):])
                return self._file(b.picture(name, kind), download="download" in q,
                                  cache="private, no-cache" if kind == "full" else "private, max-age=604800")
        return self._send(404, "Not found", "text/plain")

    def do_POST(self):
        try:
            self._post()
        except ApiError as e:
            self._json(e.code, dict(message=str(e), **e.extra))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._json(500, {"message": f"Something went wrong on the computer: {e}"})
            except Exception:
                pass

    def _post(self):
        url = urlparse(self.path)
        app = self.server_app
        ip = self.client_address[0]
        if url.path == "/login":
            body = self._body(10_000)
            if app.too_many_failures(ip):
                return self._send(429, LOGIN.format(msg="Too many tries. Wait 10 minutes."))
            password = parse_qs(body.decode("utf-8", "replace")).get("password", [""])[0]
            if not check_password(password, app.salt, app.digest):
                app.failed(ip)
                time.sleep(1)
                return self._send(401, LOGIN.format(msg="Wrong password."))
            token, _ = app.new_session()
            return self._send(303, "", headers={"Location": "/", "Set-Cookie":
                              f"moss_session={token}; HttpOnly; SameSite=Strict; Path=/; "
                              f"Max-Age={SESSION_DAYS * 86400}"})
        if url.path == "/logout":
            self._body(10_000)
            app.end_session(self._token())
            return self._send(303, "", headers={"Location": "/", "Set-Cookie": "moss_session=; Max-Age=0; Path=/"})
        sess = app.session(self._token())
        if not sess:
            self._body(MAX_UPLOAD)
            return self._json(401, {"message": "Sign in first."})
        if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), sess[0]):
            self._body(MAX_UPLOAD)
            return self._json(403, {"message": "Please reload the page."})
        b = app.bridge
        if url.path == "/api/upload":
            return self._json(200, b.upload(self._body(MAX_UPLOAD)))
        try:
            params = json.loads(self._body(MAX_BODY).decode("utf-8") or "{}")
            if not isinstance(params, dict):
                raise ValueError
        except ValueError:
            raise ApiError("Bad request.") from None
        routes = {"/api/make": b.make, "/api/cancel": b.cancel, "/api/queue": b.queue_action,
                  "/api/task": b.start_task, "/api/delete": b.delete, "/api/wallpaper": b.wallpaper,
                  "/api/wallpaper-restore": b.wallpaper_restore}
        fn = routes.get(url.path)
        if not fn:
            return self._json(404, {"message": "Not found"})
        return self._json(200, fn(params))
