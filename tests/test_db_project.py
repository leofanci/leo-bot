import json, tempfile, unittest
from pathlib import Path
import index
from tests.test_index_projects import write

class TestProject(unittest.TestCase):
    def _db(self, tmp):
        root = Path(tmp)
        proj = root / "projects" / "acme"
        write(proj / "project.md", "---\nname: Acme\nkind: venture\npriority: primary\nlocal_folder: /tmp/acme-app\n---")
        write(proj / "strategy" / "memos" / "problem-validation-v1.json",
              json.dumps({"status": "approved", "created_at": "2026-06-01"}))
        prof = proj / "profiles" / "demo"
        write(prof / "profile.md", "---\nname: Demo\n---")
        write(prof / "channels" / "demo-tiktok" / "channel.md", "---\nplatform: tiktok\n---")
        write(prof / "content" / "plan-x.json", json.dumps({"posts": [
            {"id": "p1", "status": "planned", "channels": ["demo-tiktok"]}]}))
        write(proj / "products" / "app" / "product.md", "---\nname: App\ntype: app\n---")
        write(proj / "products" / "app" / "roadmap.md", "## Now\n- [ ] Editor")
        index.build(root)
        import dashboard.db as db
        db.DB_PATH = root / "database" / "data" / "os.db"
        return db

    def test_project_aggregates_all_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            p = db.project("acme")
            self.assertEqual(p["entity"]["slug"], "acme")
            self.assertNotIn("local_folder", p["entity"])  # folders live in core.folders
            self.assertEqual([x["slug"] for x in p["profiles"]], ["demo"])
            self.assertEqual([x["slug"] for x in p["products"]], ["app"])
            self.assertEqual(len(p["memos"]), 1)
            self.assertTrue(len(p["features"]) >= 1)
            self.assertIsNone(db.project("nonexistent"))

    def test_profile_posts_carry_channels(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            posts = db.profile_posts("demo")
            self.assertEqual(len(posts), 1)
            self.assertEqual(posts[0]["channels"], ["demo-tiktok"])

if __name__ == "__main__":
    unittest.main()
