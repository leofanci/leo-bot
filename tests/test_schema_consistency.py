"""Cross-module consistency — UI, index, ids, fileops, schemas share one model."""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestSchemaConsistency(unittest.TestCase):
    def test_memo_types_single_source(self):
        from core.project_schemas import MEMO_TYPES, MEMO_SECTION
        import index as index_mod
        import core.ids as ids_mod

        self.assertEqual(set(MEMO_TYPES), set(index_mod.MEMO_TYPES))
        self.assertEqual(set(MEMO_TYPES), set(ids_mod.MEMO_TYPES))
        self.assertEqual(set(MEMO_SECTION.keys()), MEMO_TYPES)

    def test_section_layout_memo_types_from_memo_section(self):
        from core.project_schemas import MEMO_SECTION, MEMO_TYPES, canonical_memo_types_by_section
        from core.ids import PROJECT_SECTION_LAYOUT

        by_sec = canonical_memo_types_by_section()
        seen = set()
        for sec_key, layout in PROJECT_SECTION_LAYOUT.items():
            layout_types = tuple(layout.get("memo_types") or ())
            if sec_key in by_sec:
                self.assertEqual(layout_types, by_sec[sec_key])
            for mtype in layout_types:
                seen.add(mtype)
                self.assertEqual(MEMO_SECTION[mtype], sec_key)
        self.assertEqual(seen, MEMO_TYPES)

    def test_memo_form_fields_match_starter_keys(self):
        from core.project_schemas import (
            MEMO_FIELD_SPECS,
            MEMO_TYPES,
            memo_form_fields,
            memo_starter,
        )

        meta = frozenset({"status", "date", "version"})
        for mtype in MEMO_TYPES:
            starter = set(memo_starter(mtype, 1).keys())
            form_keys = {f["key"] for f in memo_form_fields(mtype)}
            spec_keys = {f["key"] for f in (MEMO_FIELD_SPECS.get(mtype) or MEMO_FIELD_SPECS["_default"])}
            self.assertEqual(form_keys, spec_keys)
            self.assertTrue(form_keys <= starter | meta, f"{mtype}: form has keys not in starter")

    def test_api_schemas_exports_render_metadata(self):
        from core.project_schemas import MEMO_TYPES, schemas_for_api

        api = schemas_for_api()
        self.assertEqual(set(api["memo_types"]), set(MEMO_TYPES))
        self.assertEqual(set(api["memo_section"].keys()), MEMO_TYPES)
        for mtype in MEMO_TYPES:
            self.assertIn(mtype, api["memo_render_order"])
            self.assertIn(mtype, api["memos"])

    def test_os_ids_js_loads_labels_from_api(self):
        from core.project_schemas import MEMO_TYPE_LABELS, schemas_for_api

        js = (ROOT / "dashboard" / "os-ids.js").read_text(encoding="utf-8")
        self.assertIn("function memoTypeLabel", js)
        self.assertIn("function setSchemas", js)
        self.assertNotIn("MEMO_TYPE_LABELS", js)
        self.assertEqual(schemas_for_api()["memo_type_labels"], MEMO_TYPE_LABELS)

    def test_index_normalizes_demo_intake_sections(self):
        # A throwaway workspace: the real data lives in the home folder, which tests never touch.
        import tempfile
        from core.project_schemas import INTAKE_SECTIONS, normalize_workspace_artifacts

        with tempfile.TemporaryDirectory() as tmp:
            demo = Path(tmp) / "projects" / "demo" / "strategy" / "intake.md"
            demo.parent.mkdir(parents=True)
            demo.write_text(f"## {INTAKE_SECTIONS[0]}\nSome notes.\n", encoding="utf-8")
            changed = normalize_workspace_artifacts(tmp)
            after = demo.read_text(encoding="utf-8")
        for sec in INTAKE_SECTIONS:
            self.assertIn(f"## {sec}", after)
        self.assertIn("Some notes.", after)
        self.assertIn("projects/demo/strategy/intake.md", changed)

    def test_http_and_osctl_memo_same_bytes(self):
        from core.project_schemas import dumps_json, normalize_memo_body

        fields = {
            "problem_statement": "Pain",
            "who_has_it": "SMBs",
            "evidence": "line one\nline two",
            "recommendation": "validate",
        }
        a = dumps_json(normalize_memo_body("problem-validation", fields, version=1))
        b = dumps_json(normalize_memo_body("problem-validation", json.loads(a), version=1))
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()