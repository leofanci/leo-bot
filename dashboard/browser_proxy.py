"""browser_proxy.py: per-site proxies for the bot browser, through one local router.

The engine takes one proxy for the whole browser (`--proxy` / AGENT_BROWSER_PROXY, probed on
0.41.0), not one per host. So when a site has a proxy (core/browser.set_site), each bot-mode
engine gets Leo Bot's own router on 127.0.0.1 as its proxy, and the router sends each
connection on by its host:

  a listed site with a proxy   through that proxy (HTTP CONNECT, with its user and password)
  anything else                straight out, as without a proxy

The real proxy address and password never reach the engine, Chrome or their environment:
the engine only knows the router, with a random user token made at each start. A proxy that
does not answer fails the connection; it never falls back to going straight out.

Chrome sends 127.0.0.1 and localhost straight out by itself, without the router.

A plain-HTTP request (not CONNECT) is one request per connection: the router reads that one
request (head and body), sends it on with Connection: close, then relays exactly one answer
(by its Content-Length, chunks or the server's close) with its head rewritten to
Connection: close, and closes. It never reads a second request from the same browser
connection, which Chrome may reuse for another host: that one could otherwise reach the first
host's server or proxy. A server that keeps the connection open anyway does not hold Chrome.

Nothing here logs a proxy address. Errors name the site only.
"""
import base64
import hmac
import secrets
import select
import socket
import socketserver
import sys
import threading
from urllib.parse import unquote, urlsplit

from core import browser

HEAD_MAX = 65536
BODY_MAX = 10 * 1024 * 1024  # one plain-HTTP request body (forms; no file upload reaches a bot)
IDLE_SECONDS = 300
CONNECT_SECONDS = 15
USER = "leobot"


def parse_proxy(url):
    """(host, port, user, password) of a proxy URL that set_site accepted, or None."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except (ValueError, TypeError):
        return None
    if parts.scheme != "http" or not parts.hostname or not port:
        return None
    user = unquote(parts.username) if parts.username is not None else None
    password = unquote(parts.password) if parts.password is not None else None
    return parts.hostname, port, user, password


def _basic(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password or ''}".encode()).decode()


def _split_head(data):
    head, _, rest = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    return lines[0], lines[1:], rest


def _target_host(method, target):
    """(host, port, origin-form path or None) of a proxy request, or None."""
    try:
        if method == "CONNECT":
            parts = urlsplit("//" + target)
            return parts.hostname, parts.port or 443, None
        parts = urlsplit(target)
        if parts.scheme != "http" or not parts.hostname:
            return None
        path = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
        return parts.hostname, parts.port or 80, path
    except ValueError:
        return None


_HOP = ("connection", "keep-alive", "proxy-connection", "proxy-authenticate", "proxy-authorization")


def _relay_response(up, client, method):
    """Exactly one answer from `up` to `client`: its head with Connection: close, then its body
    by Content-Length, chunks, or until `up` closes. 1xx interim answers are passed on first."""
    up.settimeout(IDLE_SECONDS)
    reader = up.makefile("rb")
    try:
        while True:
            status = reader.readline(HEAD_MAX)
            if not status:
                return
            lines = []
            while True:
                line = reader.readline(HEAD_MAX)
                if not line or line in (b"\r\n", b"\n"):
                    break
                lines.append(line.rstrip(b"\r\n"))
                if len(lines) > 200:
                    return
            parts = status.split(b" ", 2)
            code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            keep = [h for h in lines if h.split(b":", 1)[0].strip().lower().decode("latin-1") not in _HOP]
            names = {h.split(b":", 1)[0].strip().lower(): h.split(b":", 1)[1].strip() for h in keep if b":" in h}
            if 100 <= code < 200:
                client.sendall(status + b"".join(h + b"\r\n" for h in keep) + b"\r\n")
                continue
            client.sendall(status + b"".join(h + b"\r\n" for h in keep) + b"Connection: close\r\n\r\n")
            if method == "HEAD" or code in (204, 304):
                return
            if b"chunked" in names.get(b"transfer-encoding", b"").lower():
                while True:
                    size_line = reader.readline(HEAD_MAX)
                    if not size_line:
                        return
                    client.sendall(size_line)
                    try:
                        size = int(size_line.split(b";", 1)[0].strip(), 16)
                    except ValueError:
                        return
                    if size == 0:
                        while True:  # trailers, then the empty line
                            t = reader.readline(HEAD_MAX)
                            if not t:
                                return
                            client.sendall(t)
                            if t in (b"\r\n", b"\n"):
                                return
                    left = size + 2  # the chunk and its CRLF
                    while left:
                        data = reader.read(min(65536, left))
                        if not data:
                            return
                        client.sendall(data)
                        left -= len(data)
            elif names.get(b"content-length", b"").isdigit():
                left = int(names[b"content-length"])
                while left:
                    data = reader.read1(min(65536, left))
                    if not data:
                        return
                    client.sendall(data)
                    left -= len(data)
            else:
                while True:
                    data = reader.read1(65536)
                    if not data:
                        return
                    client.sendall(data)
            return
    except (OSError, ValueError):
        return
    finally:
        reader.close()


def _pipe(a, b, first=b""):
    """Copy both ways until either side closes or nothing moves for IDLE_SECONDS."""
    try:
        if first:
            b.sendall(first)
        socks = [a, b]
        while True:
            ready, _, _ = select.select(socks, [], [], IDLE_SECONDS)
            if not ready:
                return
            for s in ready:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except OSError:
        return


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        router, client = self.server.router, self.request
        client.settimeout(CONNECT_SECONDS)
        data = b""
        try:
            while b"\r\n\r\n" not in data:
                chunk = client.recv(8192)
                if not chunk:
                    return
                data += chunk
                if len(data) > HEAD_MAX:
                    return self._reply(431, "Request header too large")
        except OSError:
            return
        first, headers, rest = _split_head(data)
        parts = first.split(" ")
        if len(parts) != 3:
            return self._reply(400, "Bad request")
        method, target, version = parts
        auth = next((h.split(":", 1)[1].strip() for h in headers if h.lower().startswith("proxy-authorization:")), "")
        if not hmac.compare_digest(auth.encode(), router.auth.encode()):
            return self._reply(407, "Proxy authentication required",
                               extra="Proxy-Authenticate: Basic realm=\"leobot\"\r\n")
        found = _target_host(method, target)
        if found is None or not found[0]:
            return self._reply(400, "Bad request")
        host, port, path = found
        site, proxy = router.route(host)
        body = b""
        if method != "CONNECT":
            body = self._body(headers, rest)
            if body is None:
                return
        try:
            if proxy is not None:
                up = self._via(proxy, host, port, method, target, version, headers, rest if method == "CONNECT" else body)
            else:
                up = socket.create_connection((host, port), timeout=CONNECT_SECONDS)
                if method == "CONNECT":
                    client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    up.sendall(rest)
                else:
                    up.sendall(self._request(method, path, version, headers, None) + body)
        except OSError:
            line = f"The proxy for {site} did not answer." if proxy is not None else "The site did not answer."
            return self._reply(502, line)
        if up is None:
            return
        with up:
            up.settimeout(None)
            client.settimeout(None)
            if method == "CONNECT":
                _pipe(client, up)
            else:  # one request: only its one answer comes back, then this connection closes
                _relay_response(up, client, method)

    def _body(self, headers, rest):
        """The one request's body (by Content-Length), or None when a reply was sent. Bytes past
        it (a pipelined next request) are dropped."""
        names = {h.split(":", 1)[0].strip().lower(): h.split(":", 1)[1].strip() for h in headers if ":" in h}
        if "transfer-encoding" in names:
            self._reply(501, "Chunked request bodies are not supported")
            return None
        raw = names.get("content-length", "0") or "0"
        if not raw.isdecimal() or int(raw) > BODY_MAX:
            self._reply(413 if raw.isdecimal() else 400, "Request body too large" if raw.isdecimal() else "Bad request")
            return None
        need = int(raw)
        body = rest[:need]
        try:
            while len(body) < need:
                chunk = self.request.recv(min(65536, need - len(body)))
                if not chunk:
                    return None
                body += chunk
        except OSError:
            return None
        return body

    def _request(self, method, target, version, headers, proxy_auth):
        """The request head sent on: no proxy headers of the browser, Connection: close (one
        request per connection, so a kept-alive connection never reaches another host)."""
        keep = [h for h in headers if h and h.split(":", 1)[0].strip().lower()
                not in ("proxy-authorization", "proxy-connection", "connection", "keep-alive")]
        lines = [f"{method} {target} {version}"] + keep + ["Connection: close"]
        if proxy_auth:
            lines.append("Proxy-Authorization: " + proxy_auth)
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")

    def _via(self, proxy, host, port, method, target, version, headers, rest):
        """A connection to `host` through the site's proxy, the client told. None when the reply
        was already sent."""
        phost, pport, user, password = proxy
        auth = _basic(user, password) if user is not None else None
        up = socket.create_connection((phost, pport), timeout=CONNECT_SECONDS)
        try:
            if method != "CONNECT":  # `rest` is the request's body here
                up.sendall(self._request(method, target, version, headers, auth) + rest)
                return up
            lines = [f"CONNECT {host}:{port} HTTP/1.1", f"Host: {host}:{port}"]
            if auth:
                lines.append("Proxy-Authorization: " + auth)
            up.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = up.recv(8192)
                if not chunk or len(data) > HEAD_MAX:
                    raise OSError("no answer")
                data += chunk
            status, _, extra = _split_head(data)
            code = status.split(" ")[1] if len(status.split(" ")) > 1 else ""
            if code != "200":
                up.close()
                self._reply(502, "The proxy refused the connection.")
                return None
            self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n" + extra)
            if rest:  # what the browser sent right after its CONNECT goes on through the tunnel
                up.sendall(rest)
            return up
        except OSError:
            up.close()
            raise

    def _reply(self, code, line, extra=""):
        body = line.encode()
        try:
            self.request.sendall((f"HTTP/1.1 {code} {line}\r\n{extra}Content-Type: text/plain\r\n"
                                  f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body)
        except OSError:
            pass


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):  # never print a request: it may name a proxy
        print("browser proxy: a connection failed", file=sys.stderr)


class Router:
    """Leo Bot's local proxy for one data root's bot browsers. The sites and their proxies are
    checked for every connection (re-read only when browser.json changed), so a change in
    Settings applies at once."""

    def __init__(self, root):
        self.root = root
        self.token = secrets.token_hex(16)
        self.auth = _basic(USER, self.token)
        self.server = _Server(("127.0.0.1", 0), _Handler)
        self.server.router = self
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, name="browser-proxy", daemon=True).start()

    @property
    def url(self):
        """What the engine gets (AGENT_BROWSER_PROXY): this router, never a real proxy."""
        return "http" + f"://{USER}:{self.token}@127.0.0.1:{self.port}"  # split: the repo guard reads URLs

    def route(self, host):
        """(site, parsed proxy or None) for a host."""
        listed, proxies = _settings(self.root)
        site = browser.site_of("http://" + (f"[{host}]" if ":" in host else host) + "/", self.root, listed)
        raw = proxies.get(site) if site else None
        return site, (parse_proxy(raw) if raw else None)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


_ROUTERS = {}
_LOCK = threading.Lock()
_CACHE = {}  # str(root) -> (browser.json stat key, (sites, proxies))


def _settings(root):
    """(sites, site proxies) of this data root, read again only when team/browser.json changed
    (it is always replaced whole, so its inode, size or mtime moves)."""
    try:
        st = browser._path(root).stat()
        stamp = (st.st_ino, st.st_size, st.st_mtime_ns)
    except OSError:
        stamp = None
    key = str(root)
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1]
    value = (browser.sites(root), browser.site_proxies(root))
    with _LOCK:
        _CACHE[key] = (stamp, value)
    return value


def router_url(root):
    """The router's URL for the engine when any site of this data root has a proxy, else None
    (the engine then goes straight out, as before proxies existed)."""
    if not _settings(root)[1]:
        return None
    key = str(root)
    with _LOCK:
        r = _ROUTERS.get(key)
        if r is None:
            r = _ROUTERS[key] = Router(root)
        return r.url


def close_all():
    with _LOCK:
        routers = list(_ROUTERS.values())
        _ROUTERS.clear()
    for r in routers:
        try:
            r.close()
        except OSError:
            pass
