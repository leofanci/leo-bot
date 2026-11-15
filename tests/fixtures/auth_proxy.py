#!/usr/bin/env python3
"""A stand-in upstream proxy for tests, on 127.0.0.1 only: it asks for Basic auth and sends every
request, whatever host it names, to one local address (so a made-up host like acme.test reaches
a local fixture page). It records the hosts it was asked for and the auth it got.

    python3 tests/fixtures/auth_proxy.py <port> <user> <password> <target port>
"""
import base64
import select
import socket
import socketserver
import sys
import threading


class Proxy:
    def __init__(self, user, password, target_port, port=0):
        self.expect = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self.target = ("127.0.0.1", target_port)
        self.seen = []  # (method, host) of each request that passed auth
        self.denied = 0
        self.verbose = False
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = self.request.recv(8192)
                    if not chunk:
                        return
                    data += chunk
                head, _, rest = data.partition(b"\r\n\r\n")
                lines = head.decode("latin-1").split("\r\n")
                method, target, version = lines[0].split(" ")
                auth = next((h.split(":", 1)[1].strip() for h in lines[1:]
                             if h.lower().startswith("proxy-authorization:")), "")
                if auth != outer.expect:
                    outer.denied += 1
                    self.request.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                                         b"Proxy-Authenticate: Basic realm=\"t\"\r\nContent-Length: 0\r\n"
                                         b"Connection: close\r\n\r\n")
                    return
                host = target.split(":")[0] if method == "CONNECT" else target.split("/")[2].split(":")[0]
                outer.seen.append((method, host))
                if outer.verbose:
                    print(f"seen {method} {host}", flush=True)
                up = socket.create_connection(outer.target)
                if method == "CONNECT":
                    self.request.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
                    first = rest
                else:
                    path = "/" + target.split("/", 3)[3] if target.count("/") >= 3 else "/"
                    keep = [h for h in lines[1:] if not h.lower().startswith("proxy-")]
                    first = ("\r\n".join([f"{method} {path} {version}"] + keep) + "\r\n\r\n").encode() + rest
                up.sendall(first)
                socks = [self.request, up]
                try:
                    while True:
                        ready, _, _ = select.select(socks, [], [], 30)
                        if not ready:
                            break
                        for s in ready:
                            chunk = s.recv(65536)
                            if not chunk:
                                return
                            (up if s is self.request else self.request).sendall(chunk)
                finally:
                    up.close()

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server(("127.0.0.1", port), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


if __name__ == "__main__":
    port, user, password, target = sys.argv[1:5]
    p = Proxy(user, password, int(target), int(port))
    p.verbose = True
    print(f"proxy on 127.0.0.1:{p.port}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        p.close()
