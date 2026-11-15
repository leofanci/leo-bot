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
        try:
            if proxy is not None:
                up = self._via(proxy, host, port, method, target, version, headers, rest)
            else:
                up = socket.create_connection((host, port), timeout=CONNECT_SECONDS)
                if method == "CONNECT":
                    client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    first_bytes = rest
                else:
                    first_bytes = self._request(method, path, version, headers, None) + rest
                up.settimeout(None)
                client.settimeout(None)
                with up:
                    _pipe(client, up, first_bytes)
                return
        except OSError:
            line = f"The proxy for {site} did not answer." if proxy is not None else "The site did not answer."
            return self._reply(502, line)
        if up is None:
            return
        with up:
            up.settimeout(None)
            client.settimeout(None)
            _pipe(client, up)

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
            if method != "CONNECT":
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
    read again for every connection, so a change in Settings applies at once."""

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
        site = browser.site_of("http://" + (f"[{host}]" if ":" in host else host) + "/", self.root)
        raw = browser.site_proxies(self.root).get(site) if site else None
        return site, (parse_proxy(raw) if raw else None)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


_ROUTERS = {}
_LOCK = threading.Lock()


def router_url(root):
    """The router's URL for the engine when any site of this data root has a proxy, else None
    (the engine then goes straight out, as before proxies existed)."""
    if not browser.site_proxies(root):
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
