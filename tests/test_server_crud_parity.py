import io, json, shutil, tempfile, unittest
from pathlib import Path
from unittest import mock

import index
from tests.test_index_projects import write
import dashboard.server as server

fileops = server.fileops
db = server.db


def _handler():
    h = server.Handler.__new__(server.Handler)
    h.send_response = h.send_header = h.end_headers = lambda *a, **k: None
    return h


def _get(path):
    h = _handler()
    h.path = path
    h.wfile = io.BytesIO()
    h.do_GET()
    return json.loads(h.wfile.getvalue().decode())


def _post(path, body):
    h = _handler()
    payload = json.dumps(body).encode()
    h.path = path
    h.headers = {"Content-Length": str(len(payload))}
    h.rfile = io.BytesIO(payload)
    h.wfile = io.BytesIO()
    h.do_POST()
    return json.loads(h.wfile.getvalue().decode())


class CrudParityRoutesTest(unittest.TestCase):
    def setUp(self):
        self._prev_root = fileops.ROOT
        self._prev_db_path = db.DB_PATH
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        write(root / "projects" / "acme" / "project.md", "---\nname: Acme\n---")
        fileops.ROOT = root
        db.DB_PATH = root / "database" / "data" / "os.db"
        index.build(root)

    def tearDown(self):
        fileops.ROOT = self._prev_root
        db.DB_PATH = self._prev_db_path
        self.tmp.cleanup()

    def test_memo_update_and_delete_routes(self):
        created = _post("/api/project/acme/memo/new", {"type": "assessment", "recommendation": "go"})
        self.assertTrue(created["ok"])
        upd = _post("/api/project/acme/memo/assessment/1/update", {"recommendation": "wait"})
        self.assertTrue(upd["ok"])
        proj = _get("/api/project/acme")
        memo = next(m for m in proj["memos"] if m["type"] == "assessment" and m["version"] == 1)
        self.assertEqual(memo["body"]["recommendation"], "wait")
        deleted = _post("/api/project/acme/memo/assessment/1/delete", {})
        self.assertTrue(deleted["ok"])
        self.assertTrue(deleted["deleted"])

    def test_experiment_update_and_delete_routes(self):
        created = _post("/api/project/acme/experiment/new",
                         {"assumption": "people will pay", "stem": "will-pay"})
        self.assertTrue(created["ok"])
        upd = _post("/api/project/acme/experiment/will-pay/update", {"success_criteria": "10 signups"})
        self.assertTrue(upd["ok"])
        proj = _get("/api/project/acme")
        exp = next(x for x in proj["experiments"] if x["stem"] == "will-pay")
        self.assertEqual(exp["body"]["success_criteria"], "10 signups")
        deleted = _post("/api/project/acme/experiment/will-pay/delete", {})
        self.assertTrue(deleted["ok"])
        self.assertTrue(deleted["deleted"])

    def test_feature_update_and_delete_routes(self):
        _post("/api/project/acme/product/new", {"slug": "app", "name": "Acme App"})
        created = _post("/api/product/app/feature/new", {"title": "Dark mode"})
        self.assertTrue(created["ok"])
        upd = _post("/api/product/app/feature/dark-mode/update", {"priority": "high"})
        self.assertTrue(upd["ok"])
        deleted = _post("/api/product/app/feature/dark-mode/delete", {})
        self.assertTrue(deleted["ok"])
        self.assertTrue(deleted["deleted"])

    @mock.patch.object(server.sys, "platform", "darwin")
    @mock.patch.object(server.subprocess, "run")
    def test_local_folder_picker_returns_validated_path(self, run_mock):
        folder = Path(self._outside()) / "My App"
        folder.mkdir()
        run_mock.return_value = mock.Mock(returncode=0, stdout=str(folder) + "\n", stderr="")
        out = _post("/api/local-folder/pick", {})
        self.assertTrue(out["ok"])
        self.assertEqual(out["path"], str(folder.resolve()))

    # Opening a linked folder moved to /api/folders/open (tests/test_folder_routes.py).

    def _outside(self):
        """Linked folders live next to myOS, never inside it."""
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        return d


if __name__ == "__main__":
    unittest.main()
