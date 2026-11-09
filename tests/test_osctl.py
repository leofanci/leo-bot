import io, json, tempfile, unittest, contextlib
from pathlib import Path
from unittest import mock
import index
from tests.test_index_projects import write
import dashboard.fileops as fileops
import dashboard.db as db
import dashboard.osctl as osctl


def run(argv):
    """Invoke osctl.main, capture the single JSON line it prints."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = osctl.main(argv)
    line = buf.getvalue().strip().splitlines()[-1]
    return code, json.loads(line)


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        fileops.ROOT = self.root
        db.DB_PATH = self.root / "database" / "data" / "os.db"
        # minimal indexable workspace
        write(self.root / "projects" / ".keep", "")
        index.build(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_project_ok(self):
        code, out = run(["create-project", "--slug", "acme", "--name", "Acme"])
        self.assertEqual(code, 0)
        self.assertTrue(out["ok"])
        self.assertEqual(out["slug"], "acme")
        self.assertTrue((self.root / "projects" / "acme" / "project.md").exists())

    def test_create_project_duplicate_errors(self):
        run(["create-project", "--slug", "dup"])
        code, out = run(["create-project", "--slug", "dup"])
        self.assertEqual(code, 1)
        self.assertFalse(out["ok"])
        self.assertIn("already exists", out["error"])

    def test_create_project_with_local_folder(self):
        outside = tempfile.TemporaryDirectory()  # linked folders live next to Leo Bot
        self.addCleanup(outside.cleanup)
        app_dir = Path(outside.name) / "apps" / "acme-app"
        app_dir.mkdir(parents=True)
        code, out = run(["create-project", "--slug", "acme",
                         "--local-folder", str(app_dir)])
        self.assertEqual(code, 0)
        expected = str(app_dir.resolve())
        self.assertEqual([(r["path"], r["access"]) for r in out["folders"]], [(expected, "edit")])
        self.assertEqual([p["folders"][0]["path"] for p in db.tree() if p["slug"] == "acme"], [expected])

    def test_create_profile_and_channel_and_post(self):
        run(["create-project", "--slug", "acme", "--name", "Acme"])
        c, out = run(["create-profile", "--project", "acme",
                      "--slug", "demo", "--name", "Demo"])
        self.assertEqual(c, 0); self.assertTrue(out["ok"])
        self.assertTrue((self.root / "projects" / "acme" / "profiles"
                         / "demo" / "profile.md").exists())

        c, out = run(["create-channel", "--profile", "demo",
                      "--slug", "demo-tiktok", "--platform", "tiktok"])
        self.assertEqual(c, 0); self.assertEqual(out["platform"], "tiktok")

        c, out = run(["add-post", "--profile", "demo",
                      "--working-title", "Idea A", "--channels", "demo-tiktok"])
        self.assertEqual(c, 0); self.assertTrue(out["id"].startswith("pr1.pf1.sec00.po"))
        self.assertEqual(len(db.profile_posts("demo")), 1)

    def test_create_profile_unknown_project_errors(self):
        c, out = run(["create-profile", "--project", "nope", "--slug", "x"])
        self.assertEqual(c, 1); self.assertIn("not found", out["error"])

    def test_activity_and_milestone(self):
        run(["create-project", "--slug", "acme"])
        c, out = run(["create-activity", "--entity", "acme",
                      "--title", "Draft hook", "--type", "task"])
        self.assertEqual(c, 0); self.assertEqual(out["title"], "Draft hook")
        c, out = run(["mark-done", "--entity", "acme", "--title", "Draft hook"])
        self.assertEqual(c, 0); self.assertTrue(out["done"])

        c, out = run(["create-milestone", "--title", "Launch", "--date", "2026-07-01",
                      "--entity", "acme"])
        self.assertEqual(c, 0); self.assertTrue(out["id"].startswith("ms-"))

    def test_create_activity_requires_title(self):
        c, out = run(["create-activity", "--entity", "acme"])
        self.assertEqual(c, 1); self.assertIn("title is required", out["error"])

    def test_update_project(self):
        run(["create-project", "--slug", "acme", "--name", "Acme"])
        c, out = run(["update-project", "--slug", "acme", "--name", "Acme Inc",
                      "--status", "live"])
        self.assertEqual(c, 0); self.assertTrue(out["ok"])
        self.assertEqual(db.project("acme")["entity"]["name"], "Acme Inc")

    def test_update_channel(self):
        run(["create-project", "--slug", "acme"])
        run(["create-profile", "--project", "acme", "--slug", "demo"])
        run(["create-channel", "--profile", "demo", "--slug", "demo-tt", "--platform", "tiktok"])
        c, out = run(["update-channel", "--slug", "demo-tt", "--platform", "instagram"])
        self.assertEqual(c, 0); self.assertTrue(out["ok"])
        self.assertEqual(db.channel("demo-tt")["platform"], "instagram")

    def test_update_milestone(self):
        run(["create-project", "--slug", "acme"])
        _, ms = run(["create-milestone", "--title", "Launch", "--date", "2026-07-01",
                     "--entity", "acme"])
        c, out = run(["update-milestone", "--id", ms["id"], "--title", "Big Launch"])
        self.assertEqual(c, 0); self.assertTrue(out["ok"])

    def test_generate_brief_delegates_to_fileops(self):
        run(["create-project", "--slug", "acme"])
        run(["create-profile", "--project", "acme", "--slug", "demo"])
        run(["create-channel", "--profile", "demo", "--slug", "demo-tiktok", "--platform", "tiktok"])
        _, post = run(["add-post", "--profile", "demo", "--working-title", "Idea A",
                       "--channels", "demo-tiktok"])
        with mock.patch.object(fileops, "generate_brief",
                               return_value={"id": post["id"], "status": "briefed"}) as gen:
            c, out = run(["generate-brief", "--id", post["id"]])
        self.assertEqual(c, 0)
        self.assertTrue(out["ok"])
        gen.assert_called_once_with(post["id"], None, brief_id=None, voice_id=None)

    def test_generate_brief_forwards_explicit_spec_and_voice(self):
        run(["create-project", "--slug", "acme"])
        run(["create-profile", "--project", "acme", "--slug", "demo"])
        run(["create-channel", "--profile", "demo", "--slug", "demo-tiktok", "--platform", "tiktok"])
        _, post = run(["add-post", "--profile", "demo", "--working-title", "Idea A",
                       "--channels", "demo-tiktok"])
        with mock.patch.object(fileops, "generate_brief",
                               return_value={"id": post["id"], "status": "briefed"}) as gen:
            run(["generate-brief", "--id", post["id"], "--spec", "br2", "--voice", "vc2"])
        gen.assert_called_once_with(post["id"], None, brief_id="br2", voice_id="vc2")

    def test_generate_plan_forwards_brief_and_voice_counts(self):
        run(["create-project", "--slug", "acme"])
        run(["create-profile", "--project", "acme", "--slug", "demo"])
        with mock.patch.object(fileops, "run_plan",
                               return_value={"profile_slug": "demo", "stdout": "ok"}) as plan:
            run(["generate-plan", "--profile", "demo", "--period", "2026-07-01 to 2026-07-14",
                 "--brief-counts", "br1:5,br2:2", "--voice-counts", "vc1:7"])
        plan.assert_called_once_with("demo", {
            "period": "2026-07-01 to 2026-07-14",
            "brief_counts": "br1:5,br2:2",
            "voice_counts": "vc1:7",
        })

    def test_generate_plan_delegates_to_fileops(self):
        run(["create-project", "--slug", "acme"])
        run(["create-profile", "--project", "acme", "--slug", "demo"])
        with mock.patch.object(fileops, "run_plan",
                               return_value={"profile_slug": "demo", "stdout": "ok"}) as plan:
            c, out = run(["generate-plan", "--profile", "demo",
                          "--period", "2026-07-01 to 2026-07-14", "--cadence", "3"])
        self.assertEqual(c, 0)
        self.assertTrue(out["ok"])
        plan.assert_called_once_with("demo", {
            "period": "2026-07-01 to 2026-07-14",
            "cadence": "3",
        })

    def test_generate_plan_forwards_dates_flag(self):
        run(["create-project", "--slug", "acme"])
        run(["create-profile", "--project", "acme", "--slug", "demo"])
        with mock.patch.object(fileops, "run_plan",
                               return_value={"profile_slug": "demo", "stdout": "ok"}) as plan:
            run(["generate-plan", "--profile", "demo",
                 "--period", "2026-07-01 to 2026-07-14", "--dates"])
        plan.assert_called_once_with("demo", {
            "period": "2026-07-01 to 2026-07-14",
            "dates": True,
        })

    def test_brief_spec_roundtrip_via_osctl(self):
        run(["create-project", "--slug", "acme"])
        run(["create-profile", "--project", "acme", "--slug", "demo"])
        c, out = run(["update-brief-spec", "--profile", "demo",
                      "--text", "Caption max 120 chars."])
        self.assertEqual(c, 0)
        self.assertTrue(out["ok"])
        c, got = run(["get-brief-spec", "--profile", "demo"])
        self.assertEqual(c, 0)
        self.assertEqual(got["text"].strip(), "Caption max 120 chars.")

    def test_create_and_update_brief_spec_with_id(self):
        run(["create-project", "--slug", "acme"])
        run(["create-profile", "--project", "acme", "--slug", "demo"])
        c, out = run(["create-brief-spec", "--profile", "demo",
                      "--text", "tiktok rules", "--platforms", "all"])
        self.assertEqual(c, 0)
        self.assertEqual(out["brief_id"], "br2")
        c, out2 = run(["update-brief-spec", "--profile", "demo",
                       "--id", "br2", "--text", "updated"])
        self.assertEqual(c, 0)
        self.assertTrue(out2["ok"])
        c, got = run(["get-brief-spec", "--profile", "demo", "--id", "br2"])
        self.assertEqual(got["text"].strip(), "updated")

    def test_create_and_delete_voice(self):
        run(["create-project", "--slug", "acme"])
        run(["create-profile", "--project", "acme", "--slug", "demo"])
        c, out = run(["create-voice", "--profile", "demo",
                      "--text", "faster cuts", "--platforms", "all"])
        self.assertEqual(c, 0)
        self.assertEqual(out["voice_id"], "vc2")
        c, out2 = run(["delete-voice", "--profile", "demo", "--id", "vc2"])
        self.assertEqual(c, 0)
        self.assertTrue(out2["deleted"])

    def test_update_brief_delegates_to_fileops(self):
        run(["create-project", "--slug", "acme"])
        run(["create-profile", "--project", "acme", "--slug", "demo"])
        run(["create-channel", "--profile", "demo", "--slug", "demo-tiktok", "--platform", "tiktok"])
        _, post = run(["add-post", "--profile", "demo", "--working-title", "Idea A",
                       "--channels", "demo-tiktok"])
        with mock.patch.object(fileops, "update_brief",
                               return_value={"id": post["id"], "status": "briefed"}) as upd:
            c, out = run(["update-brief", "--id", post["id"],
                          "--instruction", "punchier hook"])
        self.assertEqual(c, 0)
        self.assertTrue(out["ok"])
        upd.assert_called_once_with(post["id"], "punchier hook", brief_id=None, voice_id=None)

    def test_get_project_includes_sections(self):
        run(["create-project", "--slug", "acme", "--name", "Acme"])
        c, out = run(["get-project", "--slug", "acme"])
        self.assertEqual(c, 0)
        secs = out["project"]["sections"]
        self.assertIn("validation", secs)
        self.assertEqual(secs["validation"]["id"], "pr1.sec02")

    def test_resolve_id_project_section(self):
        run(["create-project", "--slug", "acme", "--name", "Acme"])
        c, out = run(["resolve-id", "--id", "pr1.sec04"])
        self.assertEqual(c, 0)
        self.assertIn("Positioning", out["describe"])
        self.assertEqual(out["section"]["section"], "pricing")
        self.assertTrue(out["section"]["empty"])

    def test_section_artifact_creates_sync_ids(self):
        run(["create-project", "--slug", "acme", "--name", "Acme"])
        c, out = run(["create-intake", "--project", "acme"])
        self.assertEqual(c, 0)
        self.assertTrue(out["ok"])
        self.assertTrue((self.root / "projects" / "acme" / "strategy" / "intake.md").exists())

        c, out = run(["create-memo", "--project", "acme", "--type", "positioning",
                      "--summary", "Test positioning"])
        self.assertEqual(c, 0)
        self.assertTrue(out["id"].startswith("pr1.sec04.mm"))

        c, out = run(["create-experiment", "--project", "acme",
                      "--assumption", "Users will pay"])
        self.assertEqual(c, 0)
        self.assertTrue(out["id"].startswith("pr1.sec03.ex"))

        c, out = run(["create-product", "--project", "acme", "--slug", "app", "--name", "App"])
        self.assertEqual(c, 0)
        self.assertTrue(out["id"].startswith("pr1.sec05.pd"))

        c, out = run(["add-feature", "--product", "app", "--title", "Dark mode"])
        self.assertEqual(c, 0)
        self.assertTrue(".ft" in out["id"])

        _, proj = run(["get-project", "--slug", "acme"])
        secs = proj["project"]["sections"]
        self.assertFalse(secs["validation"]["empty"])
        self.assertFalse(secs["pricing"]["empty"])
        self.assertFalse(secs["experiments"]["empty"])
        self.assertFalse(secs["product"]["empty"])

    def test_create_technical_scaffold(self):
        run(["create-project", "--slug", "acme", "--name", "Acme"])
        c, out = run(["create-technical", "--project", "acme"])
        self.assertEqual(c, 0)
        self.assertTrue(out["ok"])
        path = self.root / "projects" / "acme" / "technical.md"
        self.assertTrue(path.exists())
        _, proj = run(["get-project", "--slug", "acme"])
        sec = proj["project"]["sections"]["technical"]
        self.assertEqual(sec["id"], "pr1.sec06")
        self.assertFalse(sec["empty"])

    def test_add_slide_appends_overlay_and_id(self):
        run(["create-project", "--slug", "acme"])
        run(["create-profile", "--project", "acme", "--slug", "demo"])
        run(["create-channel", "--profile", "demo", "--slug", "demo-tt", "--platform", "tiktok"])
        _, post = run(["add-post", "--profile", "demo", "--working-title", "Idea A",
                       "--channels", "demo-tt"])
        pid = post["id"]
        fileops.write_brief(pid, {
            "id": pid,
            "channels": ["demo-tt"],
            "cover_overlay": "Hook",
            "slide_overlays": [{"slide": 1, "overlay": "One"}],
            "catchy_title": "T",
            "caption": "Intro\n\nWhich?\n",
        }, strict_spec=False)
        c, out = run(["add-slide", "--id", pid, "--overlay", "Show\n2020\nTagline"])
        self.assertEqual(c, 0)
        self.assertEqual(out["slide"], 2)
        self.assertIn(".br1.fd", out["field_id"])
        _, resolved = run(["resolve-id", "--id", out["field_id"]])
        self.assertIn("Show", resolved.get("field", {}).get("value", ""))

    def test_get_subsections_shape_matches_get_project(self):
        run(["create-project", "--slug", "acme"])
        cfg_path = self.root / "projects" / "acme" / "subsections.json"
        self.assertTrue(cfg_path.is_file(), "create-project should persist subsections.json")
        _, flat = run(["get-subsections", "--project", "acme"])
        self.assertEqual(flat["project"], "acme")
        self.assertIn("docs", flat["subsections"])
        self.assertIn("validation_tab", flat["subsections"])
        _, proj = run(["get-project", "--slug", "acme"])
        self.assertEqual(proj["project"]["subsections"], flat["subsections"])
        section_opts = next(f["options"] for f in proj["project"]["feature"] if f["key"] == "section")
        self.assertEqual(section_opts, flat["subsections"]["docs"]["roadmap"])

    def test_feature_form_uses_custom_roadmap_subsections(self):
        run(["create-project", "--slug", "acme"])
        run([
            "update-subsections", "--project", "acme", "--doc", "roadmap",
            "--subsections", "Now,Building,Later",
        ])
        _, proj = run(["get-project", "--slug", "acme"])
        section_opts = next(f["options"] for f in proj["project"]["feature"] if f["key"] == "section")
        self.assertEqual(section_opts, ["Now", "Building", "Later"])
        self.assertEqual(next(f["default"] for f in proj["project"]["feature"] if f["key"] == "section"), "Now")

    def test_update_validation_tab_osctl(self):
        run(["create-project", "--slug", "acme"])
        run(["create-intake", "--project", "acme"])
        c, out = run([
            "update-validation-tab", "--project", "acme",
            "--subsections", "Stage & evidence,Market",
        ])
        self.assertEqual(c, 0)
        self.assertEqual(out["validation_tab"], ["Stage & evidence", "Market"])
        _, proj = run(["get-project", "--slug", "acme"])
        self.assertEqual(
            proj["project"]["subsections"]["validation_tab"],
            ["Stage & evidence", "Market"],
        )

    def test_per_project_technical_subsections(self):
        run(["create-project", "--slug", "acme"])
        run(["create-technical", "--project", "acme"])
        c, out = run([
            "update-subsections", "--project", "acme", "--doc", "technical",
            "--subsections", "Stack,Prompt,Tools",
        ])
        self.assertEqual(c, 0)
        self.assertEqual(out["subsections"]["docs"]["technical"],
                         ["Stack", "Prompt", "Tools"])
        cfg_path = self.root / "projects" / "acme" / "subsections.json"
        self.assertTrue(cfg_path.is_file())
        c, out = run(["update-technical", "--project", "acme", "--text",
                      "# Technical\n\n## Prompt\nSystem v2.\n\n## Stack\nPy.\n"])
        self.assertEqual(c, 0)
        tech = (self.root / "projects" / "acme" / "technical.md").read_text(encoding="utf-8")
        self.assertLess(tech.index("## Stack"), tech.index("## Prompt"))
        self.assertIn("System v2", tech)
        _, proj = run(["get-project", "--slug", "acme"])
        self.assertEqual(proj["project"]["subsections"]["docs"]["technical"],
                         ["Stack", "Prompt", "Tools"])

    def test_revise_post_delegates_to_fileops(self):
        run(["create-project", "--slug", "acme"])
        run(["create-profile", "--project", "acme", "--slug", "demo"])
        run(["create-channel", "--profile", "demo", "--slug", "demo-tiktok", "--platform", "tiktok"])
        _, post = run(["add-post", "--profile", "demo", "--working-title", "Idea A",
                       "--channels", "demo-tiktok"])
        with mock.patch.object(fileops, "revise_post",
                               return_value={"id": post["id"], "is_draft": False}) as rev:
            c, out = run(["revise-post", "--id", post["id"],
                          "--instruction", "punchier title"])
        self.assertEqual(c, 0)
        self.assertTrue(out["ok"])
        rev.assert_called_once_with(post["id"], "punchier title", brief_id=None, voice_id=None)

    def test_update_memo_and_delete_memo(self):
        run(["create-project", "--slug", "acme", "--name", "Acme"])
        c, out = run(["create-memo", "--project", "acme", "--type", "assessment",
                      "--recommendation", "go"])
        self.assertEqual(c, 0)
        c, out = run(["update-memo", "--project", "acme", "--type", "assessment",
                      "--version", "1", "--recommendation", "wait"])
        self.assertEqual(c, 0)
        self.assertTrue(out["ok"])
        c, out = run(["delete-memo", "--project", "acme", "--type", "assessment", "--version", "1"])
        self.assertEqual(c, 0)
        self.assertTrue(out["deleted"])

    def test_delete_experiment(self):
        run(["create-project", "--slug", "acme", "--name", "Acme"])
        run(["create-experiment", "--project", "acme", "--assumption", "people will pay",
             "--stem", "will-pay"])
        c, out = run(["delete-experiment", "--project", "acme", "--stem", "will-pay"])
        self.assertEqual(c, 0)
        self.assertTrue(out["deleted"])

    def test_update_feature_and_delete_feature(self):
        run(["create-project", "--slug", "acme", "--name", "Acme"])
        run(["create-product", "--project", "acme", "--slug", "app", "--name", "Acme App"])
        run(["add-feature", "--product", "app", "--title", "Dark mode"])
        c, out = run(["update-feature", "--product", "app", "--id", "dark-mode",
                      "--priority", "high"])
        self.assertEqual(c, 0)
        self.assertTrue(out["ok"])
        c, out = run(["delete-feature", "--product", "app", "--id", "dark-mode"])
        self.assertEqual(c, 0)
        self.assertTrue(out["deleted"])


if __name__ == "__main__":
    unittest.main()
