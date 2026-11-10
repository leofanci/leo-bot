"""Update notice and button: owner-only routes, the daily check thread, no network on GET."""
import json
import threading
import unittest
from unittest import mock

from core import paths, updater
from tests import test_server_people as sp

server = sp.server
request = sp.request

KEYS = {"installed", "current", "latest", "available", "notes_url", "updating", "data_dir", "log"}


class UpdateRoutes(sp.Base):
    def setUp(self):
        super().setUp()
        server._UPDATE_STARTED = 0.0
        self.addCleanup(setattr, server, "_UPDATE_STARTED", 0.0)
        for p in (mock.patch.object(paths, "is_installed", return_value=True),
                  mock.patch.object(updater, "is_updating", return_value=False),
                  mock.patch.object(updater, "settings",
                                    return_value={"channel": "stable", "latest": "v9.9.9", "checked_at": "x",
                                                  "previous_ref": None}),
                  mock.patch.object(updater, "release_notes_url", return_value="https://github.com/o/r/releases/tag/v9.9.9"),
                  mock.patch.object(server, "RUNNING_VERSION", "0.1.0")):
            p.start()
            self.addCleanup(p.stop)

    def test_get_update_owner(self):
        with mock.patch.object(updater, "check", side_effect=AssertionError("no network on GET")):
            code, out, _ = request("GET", "/api/update", owner=True)
        self.assertEqual(code, 200, out)
        self.assertEqual(set(out), KEYS)
        self.assertEqual((out["installed"], out["current"], out["latest"], out["available"], out["updating"]),
                         (True, "0.1.0", "v9.9.9", True, False))
        self.assertTrue(out["notes_url"].endswith("v9.9.9"))
        self.assertEqual(out["data_dir"], str(paths.DATA))
        self.assertEqual(out["log"], str(paths.UPDATE_LOG))

    def test_not_available_when_current_is_latest(self):
        with mock.patch.object(updater, "settings", return_value={"channel": "stable", "latest": "v0.1.0"}):
            _, out, _ = request("GET", "/api/update", owner=True)
        self.assertFalse(out["available"])

    def test_updating_follows_the_updater_lock(self):
        with mock.patch.object(updater, "is_updating", return_value=True):
            _, out, _ = request("GET", "/api/update", owner=True)
        self.assertTrue(out["updating"])

    def test_update_routes_forbidden_to_member(self):
        _, member = self.join("acme")
        with mock.patch.object(updater, "spawn_detached") as spawn:
            for method, path, body in (("GET", "/api/update", None), ("POST", "/api/update", {}),
                                       ("POST", "/api/update/open-data", {})):
                with self.subTest(path, method=method):
                    code, out, _ = request(method, path, body, member=member)
                    self.assertEqual(code, 403, out)
                    self.assertNotIn(str(paths.DATA), json.dumps(out))
        spawn.assert_not_called()

    def test_update_routes_forbidden_to_runner_and_relay(self):
        tok = server.os_relay.TOKENS.mint({"ws": "acme", "readonly": False, "apps_used": False,
                                           "engine": "claude-code", "runner_owner": "owner", "shared": False})
        self.addCleanup(server.os_relay.TOKENS.revoke, tok)
        apps_tok = server.apps_relay.TOKENS.mint({"ws": "acme", "bot": "cos", "person": "ceo"})
        self.addCleanup(server.apps_relay.TOKENS.revoke, apps_tok)
        with mock.patch.object(updater, "spawn_detached") as spawn:
            for t in (tok, apps_tok, "runner-token-x"):
                for method in ("GET", "POST"):
                    with self.subTest(method=method, token=t[:6]):
                        code, _, _ = request(method, "/api/update", {} if method == "POST" else None,
                                             headers={"Authorization": f"Bearer {t}", "X-Runner-Token": t})
                        self.assertEqual(code, 403)
        spawn.assert_not_called()

    def test_cross_site_post_refused(self):
        with mock.patch.object(updater, "spawn_detached") as spawn:
            code, _, _ = request("POST", "/api/update", {}, owner=True, origin="https://evil.example")
            self.assertEqual(code, 403)
            code, _, _ = request("POST", "/api/update", {}, owner=True, origin="")
            self.assertEqual(code, 403)
        spawn.assert_not_called()

    def test_post_refused_in_dev_checkout(self):
        with mock.patch.object(paths, "is_installed", return_value=False), \
                mock.patch.object(updater, "spawn_detached") as spawn:
            code, out, _ = request("POST", "/api/update", {}, owner=True)
            _, got, _ = request("GET", "/api/update", owner=True)
        self.assertEqual(code, 409, out)
        self.assertFalse(got["installed"])
        spawn.assert_not_called()

    def test_post_spawns_once(self):
        with mock.patch.object(updater, "spawn_detached") as spawn:
            code, out, _ = request("POST", "/api/update", {}, owner=True)
            self.assertEqual((code, out), (202, {"updating": True}))
            code, _, _ = request("POST", "/api/update", {}, owner=True)
            self.assertEqual(code, 409)
            _, got, _ = request("GET", "/api/update", owner=True)
        self.assertEqual(spawn.call_count, 1)
        self.assertTrue(got["updating"])

    def test_post_refused_while_lock_is_held(self):
        with mock.patch.object(updater, "is_updating", return_value=True), \
                mock.patch.object(updater, "spawn_detached") as spawn:
            code, _, _ = request("POST", "/api/update", {}, owner=True)
        self.assertEqual(code, 409)
        spawn.assert_not_called()

    def test_failed_spawn_clears_the_flag(self):
        with mock.patch.object(updater, "spawn_detached", side_effect=RuntimeError("no")):
            code, out, _ = request("POST", "/api/update", {}, owner=True)
            self.assertEqual(code, 500)
            self.assertIn("error", out)
        with mock.patch.object(updater, "spawn_detached") as spawn:
            code, _, _ = request("POST", "/api/update", {}, owner=True)
        self.assertEqual(code, 202)
        spawn.assert_called_once()

    def test_open_data_uses_the_data_folder_only(self):
        with mock.patch.object(server, "open_local_folder") as opener:
            code, _, _ = request("POST", "/api/update/open-data", {"path": "/etc"}, owner=True)
        self.assertEqual(code, 200)
        opener.assert_called_once_with(str(paths.DATA))


class CheckThread(unittest.TestCase):
    def test_loop_waits_then_checks_daily_and_survives_errors(self):
        sleeps = []
        calls = []

        def check():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")

        with mock.patch.object(updater, "check", side_effect=check):
            server._update_check_loop(sleep=sleeps.append, rounds=3)
        self.assertEqual(sleeps, [60, 86400, 86400])
        self.assertEqual(len(calls), 3)

    def test_thread_only_when_installed(self):
        with mock.patch.object(paths, "is_installed", return_value=False), \
                mock.patch.object(threading, "Thread") as th:
            self.assertIsNone(server._start_update_checker())
        th.assert_not_called()
        with mock.patch.object(paths, "is_installed", return_value=True), \
                mock.patch.object(threading, "Thread") as th:
            server._start_update_checker()
        self.assertTrue(th.call_args.kwargs["daemon"])
        th.return_value.start.assert_called_once()

    def test_main_starts_the_checker(self):
        import inspect
        self.assertIn("_start_update_checker()", inspect.getsource(server.main))


if __name__ == "__main__":
    unittest.main()
