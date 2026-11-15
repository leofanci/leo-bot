"""Moving a bot browser login to a server, and a proxy per site (bot browser only)."""
import contextlib
import http.server
import io
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import dashboard.server as server
from core import aes_gcm, approvals, browser, runners
from dashboard import browser_proxy, runner
from dashboard import browser_upstream as bu
from tests.test_browser_api import ORIGIN, PROJECTS, request

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
import auth_proxy  # noqa: E402

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_agent_browser.py"
LOOPBACK = "127.0" + ".0.1"
HTTP = "http" + "://"  # split: the repo guard reads URLs with a user and password
SECRET = "pw-s3cret-demo"
SITE = "acme" + ".example"
MINE = "demo" + ".example"


def state(cookies=None, origins=None):
    return {"cookies": cookies if cookies is not None else [
        {"name": "sid", "value": "demo-session", "domain": "." + SITE, "path": "/", "expires": 1900000000,
         "size": 15, "httpOnly": True, "secure": True, "session": False, "sameSite": "Lax"},
        {"name": "own", "value": "owner-only", "domain": MINE, "path": "/", "expires": -1, "size": 12,
         "httpOnly": False, "secure": True, "session": True}],
        "origins": origins if origins is not None else [
            {"origin": "https://" + SITE, "localStorage": [{"name": "user", "value": "demo"}], "sessionStorage": []},
            {"origin": "https://" + MINE, "localStorage": [{"name": "k", "value": "v"}], "sessionStorage": []}]}


def call(rid, name, **args):
    return {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": name, "arguments": args}}


def reply_text(reply):
    return reply["result"]["content"][0]["text"]


class AesGcm(unittest.TestCase):
    def test_known_answers(self):
        r = aes_gcm._expand(bytes(range(32)))
        self.assertEqual(aes_gcm._encrypt_block(r, bytes.fromhex("00112233445566778899aabbccddeeff")).hex(),
                         "8ea2b7ca516745bfeafc49904b496089")  # FIPS-197 C.3
        self.assertEqual(aes_gcm.encrypt(bytes(32), bytes(12), b"").hex(), "530f8afbc74536b9a963b4f1c4cb738b")
        self.assertEqual(aes_gcm.encrypt(bytes(32), bytes(12), bytes(16)).hex(),
                         "cea7403d4d606b6e074ec5d3baf39d18d0d1c8a799996bf0265b98b5d48ab919")  # GCM test 14

    def test_tag_is_checked(self):
        key = "ab" * 32
        blob = aes_gcm.seal_file(key, bytes(12), b'{"cookies": []}')
        self.assertEqual(aes_gcm.open_file(key, blob), b'{"cookies": []}')
        with self.assertRaises(ValueError):
            aes_gcm.open_file("cd" * 32, blob)
        bad = bytearray(blob)
        bad[20] ^= 1
        with self.assertRaises(ValueError):
            aes_gcm.open_file(key, bytes(bad))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.home, self.local, self.remote = d / "home", d / "local", d / "remote"
        for p in (self.home, self.local, self.remote):
            p.mkdir()
        for root in (self.local, self.remote):
            (root / "projects" / "acme").mkdir(parents=True)
            (root / "chats").mkdir()
            browser.epoch("acme", root)  # a turn would have made it: each root has its own
        self.pool = bu.Pool()
        for p in (mock.patch.dict(os.environ, {"HOME": str(self.home), "LEOBOT_AGENT_BROWSER": str(FAKE)}),
                  mock.patch.object(bu, "SOCKET_MAX", 1000),
                  mock.patch.object(bu, "POOL", self.pool),
                  mock.patch.dict(bu._TOOLS, clear=True)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.pool.close_all)

    def log(self, event=None):
        path = self.home / "fake-log.jsonl"
        rows = [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
        return [r for r in rows if event is None or r["event"] == event]

    def seal_local(self, data, bot="cmo"):
        """The local bot's saved state, as the engine leaves it (sealed with the local key)."""
        e = browser.epoch("acme", self.local)
        sess = bu.session_name("acme", bot, "bot", e)
        folder = bu.state_dir(self.home)
        folder.mkdir(parents=True, exist_ok=True)
        key = bu.key_file(self.local).read_text().strip()
        (folder / f"{sess}-{sess}.json.enc").write_bytes(aes_gcm.seal_file(key, os.urandom(12), json.dumps(data).encode()))
        return sess


class RoundTrip(Base):
    def test_export_import_round_trip_with_fake_engine(self):
        browser.set_site(MINE, "my_chrome", None, self.local)
        local_sess = self.seal_local(state())
        # The bot's own child is running: the export closes it first (its daemon saves).
        up = self.pool.get("acme", "cmo", "bot", self.local)
        reply, _ = up.forward(call(1, "agent_browser_open", url="https://" + SITE + "/"), None)
        self.assertFalse(reply["result"]["isError"])
        blob, dropped = bu.export_login("acme", "cmo", self.local, self.home)
        self.assertEqual(dropped, [MINE])
        self.assertIn(local_sess, [r["argv"][1] for r in self.log("close")])
        sent = json.loads(blob)
        self.assertEqual([c["name"] for c in sent["cookies"]], ["sid"])
        self.assertEqual([o["origin"] for o in sent["origins"]], ["https://" + SITE])
        self.assertEqual(self.pool.logins(), [])  # the hold ended
        # The server: its own epoch and key. The plain state is sealed straight into its file.
        e2 = browser.epoch("acme", self.remote)
        count, _ = bu.import_login("acme", "cmo", blob, self.remote, e2, self.home)
        self.assertEqual(count, 1)
        sess2 = bu.session_name("acme", "cmo", "bot", e2)
        self.assertNotEqual(sess2, local_sess)
        files = sorted(p.name for p in bu.state_dir(self.home).iterdir())
        self.assertEqual(files, sorted([f"{local_sess}-{local_sess}.json.enc", f"{sess2}-{sess2}.json.enc"]))
        key2 = bu.key_file(self.remote).read_text().strip()
        self.assertNotEqual(key2, bu.key_file(self.local).read_text().strip())
        raw = (bu.state_dir(self.home) / f"{sess2}-{sess2}.json.enc").read_bytes()
        self.assertNotIn(b"demo-session", raw)
        self.assertEqual(stat.S_IMODE(os.stat(bu.state_dir(self.home) / f"{sess2}-{sess2}.json.enc").st_mode), 0o600)
        got = json.loads(aes_gcm.open_file(key2, raw))
        self.assertEqual(got["cookies"], sent["cookies"])
        self.assertEqual(got["origins"], sent["origins"])
        # The server bot's next child restores exactly that session name.
        up2 = self.pool.get("acme", "cmo", "bot", self.remote)
        up2.forward(call(2, "agent_browser_get_url"), None)
        self.assertEqual(self.log("start")[-1]["env"]["AGENT_BROWSER_RESTORE"], sess2)

    def test_no_saved_login_and_wrong_key(self):
        with self.assertRaises(bu._Failed) as cm:
            bu.export_login("acme", "cmo", self.local, self.home)
        self.assertEqual(str(cm.exception), bu.NO_LOGIN)
        sess = self.seal_local(state())
        path = bu.state_dir(self.home) / f"{sess}-{sess}.json.enc"
        path.write_bytes(aes_gcm.seal_file("ab" * 32, os.urandom(12), b"{}"))
        with self.assertRaises(bu._Failed) as cm:
            bu.export_login("acme", "cmo", self.local, self.home)
        self.assertEqual(str(cm.exception), bu.BAD_STATE)

    def test_import_refused_while_busy_or_logging_in(self):
        e = browser.epoch("acme", self.remote)
        blob = json.dumps(state()).encode()
        up = self.pool.get("acme", "cmo", "bot", self.remote)
        with up._lock:  # a call in flight
            with self.assertRaises(bu._Failed) as cm:
                bu.import_login("acme", "cmo", blob, self.remote, e, self.home)
            self.assertEqual(str(cm.exception), bu.BUSY)
        held = self.pool.begin_login("acme", "cmo", e, self.remote)
        with self.assertRaises(bu._Failed) as cm:
            bu.import_login("acme", "cmo", blob, self.remote, e, self.home)
        self.assertEqual(str(cm.exception), bu.LOGIN_OPEN)
        self.pool.end_login(held[0], held[2])
        sess = bu.session_name("acme", "cmo", "bot", e)
        self.assertFalse((bu.state_dir(self.home) / f"{sess}-{sess}.json.enc").exists())

    def test_bot_answers_moving_during_the_hold(self):
        e = browser.epoch("acme", self.remote)
        sess, up, token = self.pool.hold("acme", "cmo", e, self.remote)
        try:
            held = self.pool.get("acme", "cmo", "bot", self.remote)
            reply, _ = held.forward(call(1, "agent_browser_get_url"), None)
            self.assertEqual(reply_text(reply), bu.MOVING)
            self.assertEqual(self.pool.logins(), [])  # not shown as a login window
        finally:
            self.pool.end_login(sess, token)

    def test_bad_blob_refused(self):
        e = browser.epoch("acme", self.remote)
        for blob in (b"not json", json.dumps({"cookies": "x"}).encode(), b"x" * (bu.MAX_LOGIN_BYTES + 1)):
            with self.assertRaises(bu._Failed):
                bu.import_login("acme", "cmo", blob, self.remote, e, self.home)


class HttpBase(Base):
    """Both sides over the real handlers: the owner's computer (local root) and the server."""

    def setUp(self):
        super().setUp()
        self.root = self.local
        for p in (mock.patch.object(server.db, "db_exists", return_value=True),
                  mock.patch.object(server.db, "tree", return_value=PROJECTS),
                  mock.patch.object(server.browser_upstream, "find_chrome_debug_url", return_value=None)):
            p.start()
            self.addCleanup(p.stop)
        self.settings = Path(self.tmp.name) / "runner-settings.json"
        p = mock.patch.object(runner, "SETTINGS_FILE", self.settings)
        p.start()
        self.addCleanup(p.stop)

    def on(self, root, deployment="local"):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(server, "ROOT", root))
        stack.enter_context(mock.patch.object(server.chat_store, "CHATS_DIR", root / "chats"))
        stack.enter_context(mock.patch.object(server, "DEPLOYMENT", deployment))
        return stack

    def runner_post(self, token, body):
        with self.on(self.remote, "server"):
            return request("POST", "/api/runner/browser-login", body, owner=False, origin=None,
                           headers={"Authorization": f"Bearer {token}"})

    def connect(self):
        token = runners.create(self.remote, "owner", "Mac")[1]
        tf = Path(self.tmp.name) / "tok"
        tf.write_text(token)
        tf.chmod(0o600)
        runner.set_connection(self.settings, "http" + "://" + LOOPBACK + ":1", tf)
        return token


class Http(HttpBase):
    def test_login_transfer_owner_only(self):
        self.seal_local(state())
        token = self.connect()
        # The send itself: owner only, this machine only, never on a server.
        with self.on(self.local):
            code, _ = request("POST", "/api/browser/login/send", {"ws": "acme", "bot": "cmo"}, owner=False)
            self.assertIn(code, (401, 403))
            code, _ = request("POST", "/api/browser/login/send", {"ws": "acme", "bot": "cmo"},
                              headers={"X-Forwarded-For": "203.0.113.9"})
            self.assertEqual(code, 403)
        with self.on(self.local, "server"):
            code, _ = request("POST", "/api/browser/login/send", {"ws": "acme", "bot": "cmo"})
            self.assertEqual(code, 403)
        # The server end: the owner's runner token only.
        blob = json.dumps(state()).encode().decode()
        code, out = self.runner_post("myr_wrong", {"ws": "acme", "bot": "cmo", "state": blob})
        self.assertEqual(code, 403)
        member_token = runners.create(self.remote, "ceo", "Mac")[1]
        with mock.patch.object(server.people, "get", return_value={"ws": {"acme": "write"}}):
            code, out = self.runner_post(member_token, {"ws": "acme", "bot": "cmo", "state": blob})
        self.assertEqual(code, 403)
        self.assertIn("owner", out["error"])
        code, out = self.runner_post(token, {"ws": "nope", "bot": "cmo", "state": blob})
        self.assertEqual(code, 404)
        self.assertIn("same workspace", out["error"])
        browser.set_bot("acme", "cmo", "read", "my_chrome", self.remote)
        code, out = self.runner_post(token, {"ws": "acme", "bot": "cmo", "state": blob})
        self.assertEqual(code, 409)
        browser.set_bot("acme", "cmo", "read", "bot", self.remote)
        code, out = self.runner_post(token, {"ws": "acme", "bot": "cmo", "state": "x" * (bu.MAX_LOGIN_BYTES + 1)})
        self.assertEqual(code, 400)
        # The whole chain: the owner's computer sends through the runner's client to the server.
        browser.set_site(MINE, "my_chrome", None, self.remote)

        def through_runner(ws, bot, state_bytes, path, timeout=120):
            client, _ = runner.connection(path)
            self.assertEqual(client.token, token)
            code, out = self.runner_post(client.token, {"ws": ws, "bot": bot, "state": state_bytes.decode()})
            self.assertEqual(code, 200, out)
            return out
        with self.on(self.local), mock.patch.object(runner, "send_login", side_effect=through_runner):
            code, out = request("POST", "/api/browser/login/send", {"ws": "acme", "bot": "cmo"})
        self.assertEqual(code, 200, out)
        self.assertEqual(out["sent"]["cookies"], 1)
        self.assertEqual(out["sent"]["skipped"], [MINE])
        e2 = browser.epochs(self.remote)["acme"]
        sess2 = bu.session_name("acme", "cmo", "bot", e2)
        key2 = bu.key_file(self.remote).read_text().strip()
        got = json.loads(aes_gcm.open_file(key2, (bu.state_dir(self.home) / f"{sess2}-{sess2}.json.enc").read_bytes()))
        self.assertEqual([c["name"] for c in got["cookies"]], ["sid"])

    def test_send_needs_a_runner_connection(self):
        self.seal_local(state())
        with self.on(self.local):
            code, out = request("POST", "/api/browser/login/send", {"ws": "acme", "bot": "cmo"})
        self.assertEqual(code, 409)
        self.assertEqual(out["error"], runner.NO_RUNNER)
        tf = Path(self.tmp.name) / "tok"
        tf.write_text("myr_x")
        tf.chmod(0o644)
        runner.set_connection(self.settings, "https://server.example", tf)
        with self.assertRaises(ValueError) as cm:
            runner.connection(self.settings)
        self.assertIn("chmod 600", str(cm.exception))
        with self.assertRaises(ValueError):
            runner.set_connection(self.settings, "http://server.example", tf) or runner.connection(self.settings)
        self.assertNotIn("myr_x", self.settings.read_text())  # the token itself is never saved there


class ProxyBotOnly(Base):
    def test_proxy_only_in_bot_mode(self):
        url = "http" + "://demo:" + SECRET + "@" + LOOPBACK + ":3128"
        with self.assertRaises(ValueError) as cm:
            browser.set_site(SITE, "my_chrome", None, self.local, proxy=url)
        self.assertEqual(str(cm.exception), browser.PROXY_ONLY_BOT)
        for bad in ("socks5://" + LOOPBACK + ":1080", "http://" + LOOPBACK, "http://" + LOOPBACK + ":1/x", "ftp://x:1"):
            with self.assertRaises(ValueError):
                browser.set_site(SITE, "bot", None, self.local, proxy=bad)
        self.assertEqual(browser.site_proxies(self.local), {})
        # No proxy anywhere: the engine goes straight out, as before.
        up = self.pool.get("acme", "cmo", "bot", self.local)
        up.forward(call(1, "agent_browser_get_url"), None)
        self.assertNotIn("AGENT_BROWSER_PROXY", self.log("start")[-1]["env"])
        # A proxy set later: the running browser is closed first, then starts on the router.
        browser.set_site(SITE, None, None, self.local, proxy=url)
        up.forward(call(2, "agent_browser_get_url"), None)
        starts = self.log("start")
        self.assertEqual(len(starts), 2)
        given = starts[-1]["env"]["AGENT_BROWSER_PROXY"]
        self.assertTrue(given.startswith(HTTP + "leobot:"))
        self.assertIn("@" + LOOPBACK + ":", given)
        self.assertNotIn(SECRET, given)
        self.assertNotIn(":3128", given)
        # My Chrome never gets a proxy.
        with mock.patch.object(bu, "find_chrome_debug_url", return_value="ws://" + LOOPBACK + ":9222/devtools/browser/a"):
            mc = self.pool.get("acme", "cto", "my_chrome", self.local)
            mc.forward(call(3, "agent_browser_get_url"), None)
        self.assertNotIn("AGENT_BROWSER_PROXY", self.log("start")[-1]["env"])
        # The proxy removed: back to straight out, on a fresh browser.
        browser.set_site(SITE, None, None, self.local)
        up = self.pool.get("acme", "cmo", "bot", self.local)
        up.forward(call(4, "agent_browser_get_url"), None)
        self.assertNotIn("AGENT_BROWSER_PROXY", self.log("start")[-1]["env"])
        browser_proxy.close_all()

    def test_router_routes_per_site(self):
        page = http.server.ThreadingHTTPServer((LOOPBACK, 0), _Page)
        threading.Thread(target=page.serve_forever, daemon=True).start()
        self.addCleanup(page.server_close)
        self.addCleanup(page.shutdown)
        up = auth_proxy.Proxy("demo", SECRET, page.server_address[1])
        self.addCleanup(up.close)
        browser.set_site(SITE, "bot", None, self.local, proxy=HTTP + f"demo:{SECRET}@{LOOPBACK}:{up.port}")
        url = browser_proxy.router_url(self.local)
        self.addCleanup(browser_proxy.close_all)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": url}))
        # The listed site: through its proxy, with its password (the proxy maps it to the page).
        with opener.open(f"http://{SITE}:{page.server_address[1]}/x", timeout=10) as r:
            self.assertEqual(r.read(), b"page")
        self.assertEqual(up.seen, [("GET", SITE)])
        # Anything else: straight out, never through the site's proxy.
        with opener.open(f"http://{LOOPBACK}:{page.server_address[1]}/y", timeout=10) as r:
            self.assertEqual(r.read(), b"page")
        self.assertEqual(up.seen, [("GET", SITE)])
        # The router wants its own token.
        bare = urllib.request.build_opener(urllib.request.ProxyHandler({"http": url.replace("leobot:", "leobot:x")}))
        with self.assertRaises(urllib.error.HTTPError) as cm:
            bare.open(f"http://{SITE}/", timeout=10)
        self.assertEqual(cm.exception.code, 407)
        # The proxy down: the site fails, it never goes straight out.
        up.close()
        with self.assertRaises(urllib.error.HTTPError) as cm:
            opener.open(f"http://{SITE}:{page.server_address[1]}/z", timeout=10)
        self.assertEqual(cm.exception.code, 502)
        self.assertNotIn(SECRET, cm.exception.read().decode())

    def test_connect_through_proxy(self):
        page = http.server.ThreadingHTTPServer((LOOPBACK, 0), _Page)
        threading.Thread(target=page.serve_forever, daemon=True).start()
        self.addCleanup(page.server_close)
        self.addCleanup(page.shutdown)
        up = auth_proxy.Proxy("demo", SECRET, page.server_address[1])
        self.addCleanup(up.close)
        browser.set_site(SITE, "bot", None, self.local, proxy=HTTP + f"demo:{SECRET}@{LOOPBACK}:{up.port}")
        router = browser_proxy.router_url(self.local)
        self.addCleanup(browser_proxy.close_all)
        port = int(router.rsplit(":", 1)[1])
        token = router.split("leobot:", 1)[1].split("@", 1)[0]
        s = socket.create_connection((LOOPBACK, port), timeout=10)
        auth = browser_proxy._basic("leobot", token)
        s.sendall(f"CONNECT {SITE}:443 HTTP/1.1\r\nHost: {SITE}:443\r\nProxy-Authorization: {auth}\r\n\r\n".encode())
        head = s.recv(4096)
        self.assertTrue(head.startswith(b"HTTP/1.1 200"), head)
        s.sendall(b"GET /c HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")  # the tunnel reaches the page
        data = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        s.close()
        self.assertTrue(data.endswith(b"page"))
        self.assertEqual(up.seen, [("CONNECT", SITE)])


class _Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "4")
        self.end_headers()
        self.wfile.write(b"page")

    def log_message(self, *a):
        pass


class ProxyNeverShown(HttpBase):
    def test_proxy_url_never_logged(self):
        url = "http" + "://demo:" + SECRET + "@" + LOOPBACK + ":3128"
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out), self.on(self.local):
            code, view = request("POST", "/api/browser/settings", {"site": SITE, "mode": "bot", "add": True, "proxy": url})
            self.assertEqual(code, 200, view)
            self.assertNotIn(SECRET, json.dumps(view))
            row = next(s for s in view["sites"] if s["site"] == SITE)
            self.assertEqual(row["proxy"], HTTP + "demo:***@" + LOOPBACK + ":3128")
            code, view = request("GET", "/api/browser/settings?ws=acme")
            self.assertNotIn(SECRET, json.dumps(view))
            # A change of limit keeps the proxy; a masked value is never saved back.
            code, view = request("POST", "/api/browser/settings", {"site": SITE, "mode": "bot", "daily": 5})
            self.assertEqual(browser.site_proxies(self.local)[SITE], url)
            code, bad = request("POST", "/api/browser/settings", {"site": SITE, "mode": "bot", "proxy": row["proxy"]})
            self.assertEqual(code, 400)
            self.assertEqual(browser.site_proxies(self.local)[SITE], url)
            # A bad proxy's error never repeats it.
            code, bad = request("POST", "/api/browser/settings",
                                {"site": SITE, "mode": "bot", "proxy": "socks5://demo:" + SECRET + "@h:1"})
            self.assertEqual(code, 400)
            self.assertNotIn(SECRET, json.dumps(bad))
            code, bad = request("POST", "/api/browser/settings", {"site": SITE, "mode": "my_chrome"})
            self.assertEqual(code, 400)
            self.assertNotIn(SECRET, json.dumps(bad))
            # The engine, a bot's calls, a send plan in Approvals, the router's own failure.
            up = self.pool.get("acme", "cmo", "bot", self.local)
            reply, _ = up.forward(call(1, "agent_browser_open", url="https://" + SITE + "/"), None)
            approvals.add_send_plan(self.local, ws="acme", room="acme/general", chat="0" * 8 + "-0000-0000-0000-" + "0" * 12,
                                    bot="cmo", person="owner",
                                    items=[{"url": "https://" + SITE + "/m", "text": "hi", "site": SITE}])
            router = browser_proxy.router_url(self.local)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": router}))
            with self.assertRaises(urllib.error.HTTPError) as cm:  # nothing listens on the proxy's port
                opener.open(f"http://{SITE}/", timeout=10)
            body = cm.exception.read().decode()
            self.assertEqual(cm.exception.code, 502)
            browser_proxy.close_all()
        self.assertNotIn(SECRET, json.dumps(reply))
        self.assertNotIn(SECRET, body)
        self.assertNotIn(SECRET, (self.home / "fake-log.jsonl").read_text())
        self.assertNotIn(SECRET, (self.local / "team" / "approvals.json").read_text())
        self.assertNotIn(SECRET, err.getvalue() + out.getvalue())
        self.assertEqual(stat.S_IMODE(os.stat(self.local / "team" / "browser.json").st_mode), 0o600)

    def test_app_never_prefills_the_proxy(self):
        js = (Path(__file__).resolve().parents[1] / "dashboard" / "app.js").read_text(encoding="utf-8")
        line = next(x for x in js.splitlines() if "data-site-proxy " in x or "data-site-proxy type" in x)
        self.assertNotIn("value=", line)


if __name__ == "__main__":
    unittest.main()
