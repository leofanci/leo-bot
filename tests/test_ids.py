import json
import tempfile
import unittest
from pathlib import Path

from core.ids import (
    IdRegistry,
    bare_slug,
    build_catalog,
    build_id_registry,
    build_project_sections,
    is_canonical_id,
    lk_post,
    lk_prof_brief_spec,
    lk_prof_voice,
    lk_tab_proj,
    mint_post_ids,
    next_activity_id,
    next_milestone_id,
    parse_id,
    resolve_section,
    section_tally,
)


class TestIds(unittest.TestCase):
    def test_composed_parse(self):
        self.assertEqual(parse_id("pr1.sec02")["segments"], ("pr1", "sec02"))
        self.assertTrue(is_canonical_id("pr1.pf2.sec00.po3.br1.fd02"))
        self.assertFalse(is_canonical_id("proj:acme"))
        self.assertFalse(is_canonical_id("not-an-id"))

    def test_canvas_ids(self):
        tree = [{"slug": "demo", "name": "Demo", "profiles": [], "products": []}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cdir = root / "projects" / "demo" / "canvases"
            ldir = root / "projects" / "demo" / "lists"
            cdir.mkdir(parents=True)
            ldir.mkdir(parents=True)
            (cdir / "cv1.json").write_text(json.dumps(
                {"id": "cv1", "title": "A", "blocks": [{"id": "bk1"}, {"id": "bk2"}]}))
            (ldir / "ls1.json").write_text(json.dumps(
                {"id": "ls1", "title": "L", "rows": [{"id": "rw4"}]}))
            reg = build_id_registry(tree, [], root=root)
            for cid in ("pr1.cv1", "pr1.cv1.bk2", "pr1.ls1", "pr1.ls1.rw4"):
                self.assertIn(cid, reg.by_id)
            self.assertIn("pr1.cv1.bk1", reg.by_id)

    def test_registry_project_and_sections(self):
        tree = [{
            "slug": "acme", "name": "Acme", "kind": "venture",
            "profiles": [], "products": [],
        }]
        reg = build_id_registry(tree, [])
        self.assertEqual(reg.get("proj:acme"), "pr1")
        self.assertEqual(reg.get(lk_tab_proj("acme", "validation")), "pr1.sec02")
        self.assertEqual(reg.get(lk_tab_proj("acme", "overview")), "pr1.sec01")

    def test_registry_profile_posts_and_fields(self):
        tree = [{
            "slug": "acme", "name": "Acme",
            "profiles": [{
                "slug": "demo", "name": "Demo", "channels": [],
            }],
            "products": [],
        }]
        posts = [{"id": "draft-001", "profile_slug": "demo", "working_title": "A"}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief_dir = root / "projects" / "acme" / "profiles" / "demo" / "content" / "briefs"
            brief_dir.mkdir(parents=True)
            (brief_dir / "draft-001.json").write_text(
                '{"cover_overlay":"Hook","slide_overlays":[{"slide":1,"overlay":"One"}]}',
                encoding="utf-8",
            )
            reg = build_id_registry(tree, posts, root=root)
            self.assertEqual(reg.get("prof:demo"), "pr1.pf1")
            self.assertEqual(reg.get("post:draft-001"), "pr1.pf1.sec00.po1")
            self.assertEqual(reg.get("brief:post:draft-001"), "pr1.pf1.sec00.po1.br1")
            self.assertEqual(reg.get("fld:brief:draft-001:cover_overlay"), "pr1.pf1.sec00.po1.br1.fd01")
            self.assertEqual(reg.get("fld:brief:draft-001:slide-1"), "pr1.pf1.sec00.po1.br1.fd02")

    def test_registry_profile_and_slot_fields(self):
        tree = [{
            "slug": "acme", "name": "Acme",
            "profiles": [{"slug": "demo", "name": "Demo", "channels": [
                {"slug": "demo-ig", "name": "IG", "platform": "instagram"},
            ]}],
            "products": [],
        }]
        posts = [{"id": "post-001", "profile_slug": "demo", "date": "2026-07-01"}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_dir = root / "projects" / "acme" / "profiles" / "demo" / "content"
            plan_dir.mkdir(parents=True)
            (plan_dir / "plan-2026-07.json").write_text(
                '{"posts":[{"id":"post-001","format":"carousel","working_title":"T","concept":"c"}]}',
                encoding="utf-8",
            )
            reg = build_id_registry(tree, posts, root=root)
            self.assertIsNone(reg.get("fld:prof:demo:name"))
            self.assertIsNone(reg.get("fld:chan:demo-ig:platform"))
            self.assertEqual(reg.get("prof:demo"), "pr1.pf1")
            self.assertEqual(reg.get("tab:prof:demo:setup"), "pr1.pf1.sec01")
            self.assertEqual(reg.get(lk_prof_brief_spec("demo")), "pr1.pf1.sec01.br1")
            self.assertEqual(reg.get(lk_prof_voice("demo")), "pr1.pf1.sec01.vc1")
            self.assertEqual(reg.get("chan:demo-ig"), "pr1.pf1.ch1")
            self.assertEqual(reg.get("sl:post:post-001:working_title"), "pr1.pf1.sec00.po1.sl01")
            self.assertEqual(reg.get("sl:post:post-001:concept"), "pr1.pf1.sec00.po1.sl02")
            self.assertIsNone(reg.get("sl:post:post-001:format"))
            self.assertIsNone(reg.get("sl:post:post-001:channels"))

    def test_registry_multiple_briefs_and_voices(self):
        tree = [{
            "slug": "acme", "name": "Acme",
            "profiles": [{"slug": "demo", "name": "Demo", "channels": []}],
            "products": [],
        }]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_dir = root / "projects" / "acme" / "profiles" / "demo"
            (profile_dir / "brief-specs").mkdir(parents=True)
            (profile_dir / "brief-specs" / "br2.md").write_text("---\nplatforms: tiktok\n---\nx\n", encoding="utf-8")
            (profile_dir / "voices").mkdir(parents=True)
            (profile_dir / "voices" / "vc2.md").write_text("---\nplatforms: all\n---\ny\n", encoding="utf-8")
            reg = build_id_registry(tree, [], root=root)
            self.assertEqual(reg.get(lk_prof_brief_spec("demo", "br1")), "pr1.pf1.sec01.br1")
            self.assertEqual(reg.get(lk_prof_brief_spec("demo", "br2")), "pr1.pf1.sec01.br2")
            self.assertEqual(reg.get(lk_prof_voice("demo", "vc1")), "pr1.pf1.sec01.vc1")
            self.assertEqual(reg.get(lk_prof_voice("demo", "vc2")), "pr1.pf1.sec01.vc2")

    def test_post_channels_bind_to_channel_entity_not_field_id(self):
        tree = [{
            "slug": "acme", "name": "Acme",
            "profiles": [{"slug": "demo", "name": "Demo", "channels": [
                {"slug": "demo-ig", "name": "IG", "platform": "instagram"},
                {"slug": "demo-tt", "name": "TT", "platform": "tiktok"},
            ]}],
            "products": [],
        }]
        posts = [{"id": "post-001", "profile_slug": "demo"}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_dir = root / "projects" / "acme" / "profiles" / "demo" / "content"
            plan_dir.mkdir(parents=True)
            (plan_dir / "plan-2026-07.json").write_text(
                '{"posts":[{"id":"post-001","channels":["demo-ig","demo-tt"],"date":"2026-07-01"}]}',
                encoding="utf-8",
            )
            reg = build_id_registry(tree, posts, root=root)
            self.assertIsNone(reg.get("sl:post:post-001:channels"))
            self.assertEqual(reg.get("post:post-001:ref:chan:demo-ig"), "pr1.pf1.ch1")
            self.assertEqual(reg.get("post:post-001:ref:chan:demo-tt"), "pr1.pf1.ch2")

    def test_registry_gen_prompts_per_slide(self):
        tree = [{
            "slug": "acme", "name": "Acme",
            "profiles": [{"slug": "demo", "name": "Demo", "channels": []}],
            "products": [],
        }]
        posts = [{"id": "post-001", "profile_slug": "demo"}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief_dir = root / "projects" / "acme" / "profiles" / "demo" / "content" / "briefs"
            brief_dir.mkdir(parents=True)
            (brief_dir / "post-001.json").write_text(
                '{"title":"T","gen_prompts":["prompt one","prompt two"]}',
                encoding="utf-8",
            )
            reg = build_id_registry(tree, posts, root=root)
            self.assertEqual(reg.get("brief:post:post-001"), "pr1.pf1.sec00.po1.br1")
            self.assertEqual(reg.get("fld:brief:post-001:gen-prompt-1"), "pr1.pf1.sec00.po1.br1.fd02")
            self.assertEqual(reg.get("fld:brief:post-001:gen-prompt-2"), "pr1.pf1.sec00.po1.br1.fd03")
            self.assertIsNone(reg.get("fld:brief:post-001:gen_prompts"))

    def test_bare_slug_via_registry(self):
        tree = [{"slug": "acme", "profiles": [{"slug": "demo", "channels": []}], "products": []}]
        posts = [{"id": "draft-001", "profile_slug": "demo"}]
        reg = build_id_registry(tree, posts)
        self.assertEqual(bare_slug("pr1.pf1.sec00.po1", reg), "draft-001")
        self.assertEqual(bare_slug("pr1", reg), "acme")

    def test_registry_section_artifacts_cascade(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exp_dir = root / "projects" / "acme" / "strategy" / "experiments"
            exp_dir.mkdir(parents=True)
            (exp_dir / "exp-001-design.json").write_text("{}", encoding="utf-8")
            prod_dir = root / "projects" / "acme" / "products" / "app"
            prod_dir.mkdir(parents=True)
            (prod_dir / "product.md").write_text("# App\n", encoding="utf-8")
            tree = [{"slug": "acme", "profiles": [], "products": []}]
            reg = build_id_registry(tree, [], root=root)
            self.assertEqual(reg.get("exp:proj:acme:exp-001-design"), "pr1.sec03.ex1")
            self.assertEqual(reg.get("prod:app"), "pr1.sec05.pd1")
            exp = reg.resolve("pr1.sec03.ex1")
            self.assertEqual(exp["parent"], "pr1.sec03")
            pd = reg.resolve("pr1.sec05.pd1")
            self.assertEqual(pd["parent"], "pr1.sec05")

    def test_registry_memos_nested_under_section_tab(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memo_dir = root / "projects" / "acme" / "strategy" / "memos"
            memo_dir.mkdir(parents=True)
            (memo_dir / "problem-validation-v1.json").write_text("{}", encoding="utf-8")
            (memo_dir / "positioning-v1.json").write_text("{}", encoding="utf-8")
            (memo_dir / "pricing-v1.json").write_text("{}", encoding="utf-8")
            tree = [{"slug": "acme", "profiles": [], "products": []}]
            reg = build_id_registry(tree, [], root=root)
            self.assertEqual(reg.get("memo:proj:acme:problem-validation-v1"), "pr1.sec02.mm1")
            self.assertEqual(reg.get("memo:proj:acme:positioning-v1"), "pr1.sec04.mm1")
            self.assertEqual(reg.get("memo:proj:acme:pricing-v1"), "pr1.sec04.mm2")
            pos = reg.resolve("pr1.sec04.mm1")
            self.assertEqual(pos["parent"], "pr1.sec04")
            self.assertEqual(pos["ref"]["section"], "pricing")

    def test_resolve_section_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj_dir = root / "projects" / "acme" / "strategy"
            proj_dir.mkdir(parents=True)
            (proj_dir / "intake.md").write_text("# intake\n", encoding="utf-8")
            (proj_dir / "memos").mkdir()
            (proj_dir / "memos" / "problem-validation-v1.json").write_text("{}", encoding="utf-8")
            tree = [{"slug": "acme", "profiles": [], "products": []}]
            reg = build_id_registry(tree, [], root=root)
            pdata = {
                "memos": [{
                    "type": "problem-validation",
                    "version": 1,
                    "status": "proposed",
                    "file_path": "projects/acme/strategy/memos/problem-validation-v1.json",
                }],
            }
            sec = resolve_section("acme", "validation", root, project_data=pdata, registry=reg)
            self.assertEqual(sec["id"], "pr1.sec02")
            self.assertFalse(sec["empty"])
            self.assertEqual(sec["skill"], "problem-validation")

    def test_build_project_sections_and_tally(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tree = [{"slug": "solo", "profiles": [], "products": []}]
            reg = build_id_registry(tree, [], root=root)
            sections = build_project_sections("solo", root, {"memos": [], "experiments": [], "products": [], "features": []}, registry=reg)
            self.assertIn("validation", sections)
            self.assertTrue(sections["validation"]["empty"])
            self.assertEqual(sections["validation"]["id"], "pr1.sec02")
            self.assertEqual(section_tally("solo", "validation", root), "empty")

    def test_file_artifacts_include_md_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj = root / "projects" / "acme"
            intake = proj / "strategy" / "intake.md"
            intake.parent.mkdir(parents=True)
            intake.write_text("# Venture intake\n\n## What it is\n\nB2B SaaS.\n", encoding="utf-8")
            tree = [{"slug": "acme", "profiles": [], "products": []}]
            reg = build_id_registry(tree, [], root=root)
            sec = resolve_section("acme", "validation", root, registry=reg)
            files = [a for a in sec["artifacts"] if a["kind"] == "file"]
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0]["text"], intake.read_text(encoding="utf-8"))
            self.assertEqual(files[0]["id"], "pr1.sec02.doc1")

    def test_resolve_section_technical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj = root / "projects" / "acme"
            technical = proj / "technical.md"
            proj.mkdir(parents=True)
            technical.write_text("# Technical\n\n## Stack\n\nPython.\n", encoding="utf-8")
            tree = [{"slug": "acme", "profiles": [], "products": []}]
            reg = build_id_registry(tree, [], root=root)
            sec = resolve_section("acme", "technical", root, registry=reg)
            self.assertEqual(sec["id"], "pr1.sec06")
            self.assertFalse(sec["empty"])
            files = [a for a in sec["artifacts"] if a["kind"] == "file"]
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0]["text"], technical.read_text(encoding="utf-8"))
            self.assertEqual(section_tally("acme", "technical", root), "technical ✓")

    def test_technical_doc_subsections_get_composed_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj = root / "projects" / "demo"
            proj.mkdir(parents=True)
            (proj / "technical.md").write_text(
                "# Technical\n\n## Stack\n\nPython.\n\n## Architecture\n\nServices.\n",
                encoding="utf-8",
            )
            (proj / "subsections.json").write_text(
                json.dumps({
                    "version": 1,
                    "docs": {
                        "intake": ["What it is"],
                        "technical": ["Stack", "Architecture"],
                        "roadmap": ["Next"],
                    },
                    "validation_tab": ["What it is"],
                }),
                encoding="utf-8",
            )
            tree = [{"slug": "demo", "profiles": [], "products": []}]
            reg = build_id_registry(tree, [], root=root)
            self.assertEqual(reg.get("doc:proj:demo:technical"), "pr1.sec06.doc1")
            self.assertEqual(reg.get("sub:proj:demo:technical:stack"), "pr1.sec06.doc1.ss1")
            self.assertEqual(reg.get("sub:proj:demo:technical:architecture"), "pr1.sec06.doc1.ss2")
            stack = reg.resolve("pr1.sec06.doc1.ss1")
            self.assertEqual(stack["kind"], "subsection")
            self.assertEqual(stack["parent"], "pr1.sec06.doc1")
            self.assertEqual(stack["ref"]["subsection"], "Stack")
            from core.ids import subsection_id_map
            smap = subsection_id_map(reg, "demo")
            self.assertEqual(smap["technical"]["Stack"], "pr1.sec06.doc1.ss1")
            self.assertEqual(smap["technical"]["Architecture"], "pr1.sec06.doc1.ss2")

    def test_mint_post_ids_composed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ids = mint_post_ids(root, "acme", "demo", 2)
            self.assertEqual(ids, ["pr1.pf1.sec00.po1", "pr1.pf1.sec00.po2"])
            self.assertTrue(all(is_canonical_id(i) for i in ids))

    def test_mint_post_ids_persists_across_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = mint_post_ids(root, "acme", "demo", 1)
            second = mint_post_ids(root, "acme", "demo", 1)
            self.assertEqual(first, ["pr1.pf1.sec00.po1"])
            self.assertEqual(second, ["pr1.pf1.sec00.po2"])

    def test_mint_post_ids_never_collide_across_profiles(self):
        # Two profiles minting posts independently must never produce the
        # same id — pr/pf numbers differ, so poN alone is never the whole id.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = mint_post_ids(root, "acme", "profile-a", 3)
            b = mint_post_ids(root, "acme", "profile-b", 3)
            self.assertFalse(set(a) & set(b))

    def test_mint_post_ids_skips_ids_already_baked_in_plan_files(self):
        # Simulates a batch/migrated plan that wrote poN ids straight into a
        # plan file without ever calling mint_post_ids — the persisted
        # registry counter is left behind reality. The next real mint must
        # not reissue a number already sitting on disk.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = root / "projects" / "acme" / "profiles" / "demo" / "content"
            content.mkdir(parents=True)
            baked = {"posts": [{"id": f"pr1.pf1.sec00.po{n}"} for n in (1, 2, 3, 4, 5)]}
            (content / "plan-batch.json").write_text(json.dumps(baked), encoding="utf-8")

            new_id = mint_post_ids(root, "acme", "demo", 1)[0]
            self.assertEqual(new_id, "pr1.pf1.sec00.po6")

    def test_mint_post_ids_stays_ahead_after_backfill(self):
        # Once the floor is crossed once, subsequent mints keep counting up
        # from there rather than re-scanning back down to the old baked max.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = root / "projects" / "acme" / "profiles" / "demo" / "content"
            content.mkdir(parents=True)
            baked = {"posts": [{"id": "pr1.pf1.sec00.po10"}]}
            (content / "plan-batch.json").write_text(json.dumps(baked), encoding="utf-8")

            first = mint_post_ids(root, "acme", "demo", 1)[0]
            second = mint_post_ids(root, "acme", "demo", 1)[0]
            self.assertEqual(first, "pr1.pf1.sec00.po11")
            self.assertEqual(second, "pr1.pf1.sec00.po12")

    def test_generated_stamp_ids(self):
        ms = next_milestone_id(set())
        self.assertTrue(ms.startswith("ms-"))
        act = next_activity_id(set())
        self.assertTrue(act.startswith("act-"))

    def test_live_catalog_from_tree(self):
        tree = [{
            "slug": "acme", "name": "Acme", "kind": "venture",
            "profiles": [{
                "slug": "demo", "name": "Demo", "channels": [
                    {"slug": "demo-tiktok", "platform": "tiktok", "name": "TikTok"},
                ],
            }],
            "products": [],
        }]
        entries = build_catalog(tree)
        ids = {e["id"] for e in entries}
        self.assertIn("pr1", ids)
        self.assertIn("pr1.pf1", ids)
        self.assertIn("pr1.pf1.ch1", ids)
        self.assertIn("pr1.sec01", ids)


if __name__ == "__main__":
    unittest.main()