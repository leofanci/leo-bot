"""Workspace logos: sniffed image types only, per workspace, over HTTP."""
import base64
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dashboard.server as server
from core import ws_logos
from tests.test_server_apps import request

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
JPG = b"\xff\xd8\xff\xe0" + b"\0" * 32
WEBP = b"RIFF\0\0\0\0WEBPVP8 " + b"\0" * 32
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_type_from_bytes(self):
        for data, ctype in ((PNG, "image/png"), (JPG, "image/jpeg"), (WEBP, "image/webp")):
            ws_logos.save("acme", data, self.root)
            self.assertEqual(ws_logos.get("acme", self.root), (data, ctype))

    def test_refuses_svg_and_junk(self):
        for bad in (SVG, b"GIF89a....", b"", None):
            with self.assertRaises(ValueError):
                ws_logos.save("acme", bad, self.root)
        self.assertIsNone(ws_logos.get("acme", self.root))

    def test_size_cap(self):
        with self.assertRaises(ValueError):
            ws_logos.save("acme", PNG + b"\0" * ws_logos.MAX_BYTES, self.root)

    def test_replace_keeps_one_file(self):
        ws_logos.save("acme", PNG, self.root)
        ws_logos.save("acme", JPG, self.root)
        files = sorted(p.name for p in (self.root / "team" / "logos").iterdir())
        self.assertEqual(files, ["acme.jpg"])

    def test_private_file(self):
        ws_logos.save("hq", PNG, self.root)
        d = self.root / "team" / "logos"
        self.assertEqual(oct(d.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct((d / "hq.png").stat().st_mode & 0o777), "0o600")

    def test_bad_workspace_ids(self):
        for bad in ("../x", "a/b", ".hidden", "", None, "x" * 200):
            with self.assertRaises(ValueError):
                ws_logos.save(bad, PNG, self.root)
            self.assertIsNone(ws_logos.get(bad, self.root))

    def test_remove_and_version(self):
        self.assertIsNone(ws_logos.version("acme", self.root))
        v = ws_logos.save("acme", PNG, self.root)
        self.assertEqual(ws_logos.version("acme", self.root), v)
        ws_logos.remove("acme", self.root)
        self.assertIsNone(ws_logos.version("acme", self.root))


class Http(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "chats").mkdir()
        for p in (mock.patch.object(server, "ROOT", self.root),
                  mock.patch.object(server.chat_store, "CHATS_DIR", self.root / "chats"),
                  mock.patch.object(server.db, "db_exists", return_value=True),
                  mock.patch.object(server.db, "tree", return_value=[{"slug": "acme", "name": "Acme"}])):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def upload(self, ws, data, **kw):
        return request("POST", "/api/workspace-logo",
                       {"ws": ws, "data": base64.b64encode(data).decode()}, **kw)

    def test_upload_list_serve_remove(self):
        code, out, _, _ = self.upload("acme", PNG)
        self.assertEqual(code, 200)
        code, out, _, _ = request("GET", "/api/workspaces")
        logos = {w["id"]: w["logo"] for w in out["workspaces"]}
        self.assertIsNone(logos["hq"])
        self.assertTrue(logos["acme"])
        code, _, headers, raw = request("GET", "/api/workspace-logo/acme")
        self.assertEqual((code, raw, headers["Content-Type"]), (200, PNG, "image/png"))
        self.assertIn("sandbox", headers["Content-Security-Policy"])
        code, _, _, _ = request("POST", "/api/workspace-logo/remove", {"ws": "acme"})
        self.assertEqual(code, 200)
        code, _, _, _ = request("GET", "/api/workspace-logo/acme")
        self.assertEqual(code, 404)

    def test_hq_logo(self):
        self.assertEqual(self.upload("hq", JPG)[0], 200)
        code, _, headers, _ = request("GET", "/api/workspace-logo/hq")
        self.assertEqual((code, headers["Content-Type"]), (200, "image/jpeg"))

    def test_refusals(self):
        self.assertEqual(self.upload("acme", SVG)[0], 400)
        self.assertEqual(self.upload("nope", PNG)[0], 400)    # not a workspace
        self.assertEqual(self.upload("../x", PNG)[0], 400)
        code, _, _, _ = request("POST", "/api/workspace-logo", {"ws": "acme", "data": "%%%"})
        self.assertEqual(code, 400)
        code, _, _, _ = request("POST", "/api/workspace-logo", {"ws": ["acme"], "data": ""})
        self.assertEqual(code, 400)

    def test_needs_auth(self):
        self.assertEqual(self.upload("acme", PNG, cookie=False)[0], 403)
        self.assertEqual(self.upload("acme", PNG, origin=False)[0], 403)
        ws_logos.save("acme", PNG, self.root)
        self.assertEqual(request("GET", "/api/workspace-logo/acme", cookie=False)[0], 403)

    def test_project_delete_drops_logo(self):
        ws_logos.save("acme", PNG, self.root)
        with mock.patch.object(server.fileops, "delete_project",
                               return_value={"slug": "acme", "deleted": True}):
            code, _, _, _ = request("POST", "/api/project/acme/delete", {})
        self.assertEqual(code, 200)
        self.assertIsNone(ws_logos.get("acme", self.root))


if __name__ == "__main__":
    unittest.main()
