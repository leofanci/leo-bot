"""Experiment metric source: only read-only app actions are accepted, with the reason when not."""
import json
import unittest

from core import app_rules
from core.project_schemas import METRIC_MAX_WINDOW_DAYS, normalize_experiment_body, normalize_metric
from tests import test_fileops_crud as _crud
import dashboard.fileops as fileops

CAT = ["gmail", "instagram", "apollo", "firecrawl"]


def _tagged(slug):
    return True


def _untagged(slug):
    return False


def _unknown(slug):
    return None


class MetricProblem(unittest.TestCase):
    def check(self, app, action, **kw):
        kw.setdefault("read_only", _tagged)
        return app_rules.metric_problem(app, action, CAT, **kw)

    def test_read_only_action_accepted(self):
        self.assertIsNone(self.check("instagram", "INSTAGRAM_GET_USER_INSIGHTS"))

    def test_write_action_rejected_with_reason(self):
        why = self.check("gmail", "GMAIL_SEND_EMAIL")
        self.assertIn("read", why)

    def test_untagged_action_rejected(self):
        self.assertIn("read-only", self.check("instagram", "INSTAGRAM_GET_USER_INSIGHTS", read_only=_untagged))

    def test_unconfirmable_tag_rejected(self):
        self.assertIn("confirm", self.check("instagram", "INSTAGRAM_GET_USER_INSIGHTS", read_only=_unknown))

    def test_no_tag_lookup_rejected(self):
        self.assertIsNotNone(self.check("instagram", "INSTAGRAM_GET_USER_INSIGHTS", read_only=None))

    def test_action_of_another_app_rejected(self):
        self.assertIn("gmail", self.check("instagram", "GMAIL_FETCH_EMAILS"))

    def test_unknown_app_rejected(self):
        self.assertIsNotNone(self.check("nope", "NOPE_GET_X"))

    def test_blocked_and_paid_toolkits_rejected(self):
        self.assertIsNotNone(self.check("firecrawl", "FIRECRAWL_GET_PAGE"))
        self.assertIsNotNone(self.check("apollo", "APOLLO_GET_PERSON"))
        self.assertIsNotNone(self.check("instagram", "INSTAGRAM_GET_X", paid=frozenset({"instagram"})))

    def test_url_or_big_args_rejected(self):
        self.assertIsNotNone(self.check("instagram", "INSTAGRAM_GET_X", args={"u": "https://evil.example/x"}))
        self.assertIsNotNone(self.check("instagram", "INSTAGRAM_GET_X", args={"q": "x" * 3000}))

    def test_malformed_slug_rejected(self):
        self.assertIsNotNone(self.check("instagram", "instagram get x"))
        self.assertIsNotNone(self.check("instagram", ""))

    def test_agrees_with_decide(self):
        for slug, tk in (("INSTAGRAM_GET_USER_INSIGHTS", "instagram"), ("INSTAGRAM_CREATE_POST", "instagram"),
                         ("GMAIL_FETCH_EMAILS", "gmail"), ("GMAIL_SEND_EMAIL", "gmail"),
                         ("APOLLO_GET_PERSON", "apollo"), ("FIRECRAWL_GET_PAGE", "firecrawl")):
            for tag in (_tagged, _untagged, _unknown):
                verdict, _ = app_rules.decide(slug, {tk: "read"}, CAT, {}, tag)
                ok = app_rules.metric_problem(tk, slug, CAT, read_only=tag) is None
                self.assertEqual(ok, verdict == "read", (slug, tag.__name__))


class NormalizeMetric(unittest.TestCase):
    def test_shape(self):
        m = normalize_metric({"app": "Instagram", "action": "INSTAGRAM_GET_X", "args": {"a": 1},
                              "path": "data.followers", "window_days": "14"})
        self.assertEqual(m, {"app": "instagram", "action": "INSTAGRAM_GET_X", "args": {"a": 1},
                             "path": "data.followers", "window_days": 14})

    def test_defaults(self):
        m = normalize_metric({"app": "instagram", "action": "INSTAGRAM_GET_X", "path": "data.n"})
        self.assertEqual((m["args"], m["window_days"]), ({}, 7))

    def test_bad_values_give_none(self):
        base = {"app": "instagram", "action": "INSTAGRAM_GET_X", "path": "data.n"}
        for bad in ({**base, "path": ""}, {**base, "path": "data..n"}, {**base, "path": "a b"},
                    {**base, "app": ""}, {**base, "window_days": 0},
                    {**base, "window_days": METRIC_MAX_WINDOW_DAYS + 1}, {**base, "args": [1]},
                    {**base, "args": {"x": object()}}, "text", None, {}):
            self.assertIsNone(normalize_metric(bad), bad)

    def test_experiment_body_keeps_valid_metric_only(self):
        m = {"app": "instagram", "action": "INSTAGRAM_GET_X", "path": "data.n"}
        self.assertIn("metric", normalize_experiment_body({"assumption": "a", "metric": m}))
        self.assertNotIn("metric", normalize_experiment_body({"assumption": "a", "metric": {"app": "x"}}))
        self.assertNotIn("metric", normalize_experiment_body({"assumption": "a"}))


class SetMetric(unittest.TestCase):
    FIELDS = {"app": "instagram", "action": "INSTAGRAM_GET_X", "path": "data.n", "window_days": 7}

    def setUp(self):  # same temp project as the CRUD tests, without re-running their tests
        _crud.CrudTest.setUp(self)
        fileops.create_experiment("acme", {"assumption": "people will pay", "stem": "will-pay"})

    def tearDown(self):
        _crud.CrudTest.tearDown(self)

    def _body(self):
        return json.loads((self.root / "projects/acme/strategy/experiments/will-pay.json").read_text())

    def _set(self, fields=None, **kw):
        kw.setdefault("catalog", CAT)
        kw.setdefault("read_only", _tagged)
        return fileops.set_metric("acme", "will-pay", fields or self.FIELDS, **kw)

    def test_saves_read_only_metric(self):
        out = self._set()
        self.assertEqual(self._body()["metric"]["action"], "INSTAGRAM_GET_X")
        self.assertEqual(out["stem"], "will-pay")

    def test_rejects_write_action_and_saves_nothing(self):
        with self.assertRaises(fileops.ActionError) as cm:
            self._set({**self.FIELDS, "action": "INSTAGRAM_CREATE_POST"})
        self.assertIn("read", str(cm.exception))
        self.assertNotIn("metric", self._body())

    def test_rejects_bad_shape(self):
        with self.assertRaises(fileops.ActionError):
            self._set({**self.FIELDS, "path": "a b"})

    def test_unknown_experiment_raises(self):
        with self.assertRaises(fileops.ActionError):
            fileops.set_metric("acme", "nope", self.FIELDS, catalog=CAT, read_only=_tagged)

    def test_clear_removes_metric(self):
        self._set()
        fileops.set_metric("acme", "will-pay", None, clear=True)
        self.assertNotIn("metric", self._body())

    def test_other_edits_keep_metric(self):
        self._set()
        fileops.update_experiment("acme", "will-pay", {"success_criteria": "10 signups"})
        self.assertEqual(self._body()["metric"]["path"], "data.n")

    def test_update_and_create_cannot_set_metric(self):
        fileops.update_experiment("acme", "will-pay", {"metric": self.FIELDS})
        self.assertNotIn("metric", self._body())
        fileops.create_experiment("acme", {"assumption": "b", "stem": "b", "metric": self.FIELDS})
        other = json.loads((self.root / "projects/acme/strategy/experiments/b.json").read_text())
        self.assertNotIn("metric", other)


class OsctlWalls(unittest.TestCase):
    """set-metric is the owner's: a shared thread, a read-only turn and an Auto job never save it."""
    ARGV = ["set-metric", "--project", "acme", "--stem", "will-pay", "--app", "instagram",
            "--action", "INSTAGRAM_GET_X", "--path", "data.n"]

    def setUp(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        import index
        import dashboard.db as db
        from tests.test_index_projects import write
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for p in (mock.patch.object(fileops, "ROOT", root),
                  mock.patch.object(db, "DB_PATH", root / "database" / "data" / "os.db")):
            p.start()
            self.addCleanup(p.stop)
        write(root / "projects" / "acme" / "project.md", "---\nname: Acme\nkind: venture\n---\n")
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            index.build(root)

    def run_cmd(self, env):
        import contextlib
        import io
        import os
        from unittest import mock
        import dashboard.osctl as osctl
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(fileops, "set_metric") as saved, \
                contextlib.redirect_stdout(out):
            osctl.main(list(self.ARGV))
        return json.loads(out.getvalue().strip().splitlines()[-1]), saved

    def test_owner_turn_reaches_the_save(self):
        out, saved = self.run_cmd({"LEOBOT_WORKSPACE": "acme"})
        self.assertTrue(out["ok"], out)
        saved.assert_called_once()

    def test_shared_thread_refused(self):
        out, saved = self.run_cmd({"LEOBOT_WORKSPACE": "acme", "LEOBOT_SHARED": "1"})
        self.assertFalse(out["ok"])
        saved.assert_not_called()

    def test_read_only_turn_refused(self):
        out, saved = self.run_cmd({"LEOBOT_WORKSPACE": "acme", "LEOBOT_READONLY": "1"})
        self.assertFalse(out["ok"])
        saved.assert_not_called()

    def test_auto_job_waits_for_approval(self):
        out, saved = self.run_cmd({"LEOBOT_WORKSPACE": "acme", "LEOBOT_JOB": "j1", "LEOBOT_JOB_MODE": "auto"})
        self.assertFalse(out["ok"])
        self.assertIn("APPROVAL", out["error"])
        saved.assert_not_called()


if __name__ == "__main__":
    unittest.main()
