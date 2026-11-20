"""A server's owner answers only through the SSH connection unless LEOBOT_OWNER_REMOTE=1."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dashboard.server as server
from tests import test_browser_api as api

VIA_PROXY = {"X-Forwarded-For": "203.0.113.7", "X-Forwarded-Proto": "https"}


class OwnerOnServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "chats").mkdir()
        for p in (mock.patch.object(server, "ROOT", root),
                  mock.patch.object(server.chat_store, "CHATS_DIR", root / "chats"),
                  mock.patch.object(server.db, "db_exists", return_value=True),
                  mock.patch.object(server.db, "tree", return_value=api.PROJECTS),
                  mock.patch.object(server, "DEPLOYMENT", "server"),
                  mock.patch.dict(os.environ, {"LEOBOT_OWNER_REMOTE": ""})):
            p.start()
            self.addCleanup(p.stop)

    def test_owner_through_ssh_works(self):
        self.assertEqual(api.request("GET", "/api/halt")[0], 200)

    def test_owner_through_a_proxy_is_refused(self):
        self.assertEqual(api.request("GET", "/api/halt", headers=VIA_PROXY)[0], 403)
        self.assertEqual(api.request("POST", "/api/halt", {}, headers=VIA_PROXY)[0], 403)

    def test_login_link_through_a_proxy_sets_no_cookie(self):
        code, _ = api.request("GET", "/?token=" + server.AUTH_TOKEN, owner=False, headers=VIA_PROXY)
        self.assertEqual(code, 403)

    def test_opt_in_lets_the_owner_in_remotely(self):
        with mock.patch.dict(os.environ, {"LEOBOT_OWNER_REMOTE": "1"}):
            self.assertEqual(api.request("GET", "/api/halt", headers=VIA_PROXY)[0], 200)

    def test_mac_unchanged(self):
        with mock.patch.object(server, "DEPLOYMENT", "local"):
            self.assertEqual(api.request("GET", "/api/halt", headers=VIA_PROXY)[0], 200)

    def test_https_answers_carry_hsts(self):
        for headers, want in ((VIA_PROXY, True), ({}, False)):
            h = server.Handler.__new__(server.Handler)
            sent = {}
            h.send_header = lambda k, v: sent.__setitem__(k, v)
            h.headers = headers
            h.server = mock.Mock(server_address=("127.0.0.1", api.PORT))
            with mock.patch.object(server.BaseHTTPRequestHandler, "end_headers"):
                h.end_headers()
            self.assertEqual("Strict-Transport-Security" in sent, want)

if __name__ == "__main__":
    unittest.main()
