"""core/provenance.py: saved content keeps its outside label until the owner trusts it."""
import tempfile
import unittest
from pathlib import Path

from core import provenance


class Labels(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "projects" / "acme" / "memos").mkdir(parents=True)
        (self.root / "projects" / "demo").mkdir(parents=True)
        (self.root / "team" / "memory").mkdir(parents=True)

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def test_a_tainted_request_labels_what_it_changed(self):
        self.write("projects/acme/memos/m1.md", "old")
        before = provenance.scan(self.root, "acme")
        self.write("projects/acme/memos/m1.md", "new text from a mail")
        self.write("projects/acme/team/cos.md", "- remember this")
        self.write("projects/acme/.hidden", "x")
        self.write("projects/acme/os.db", "x")
        got = provenance.mark(self.root, before, provenance.scan(self.root, "acme"), chat="c1")
        self.assertEqual(sorted(got), ["projects/acme/memos/m1.md", "projects/acme/team/cos.md"])
        self.assertEqual({r["path"] for r in provenance.pending(self.root, "acme")}, set(got))
        self.assertEqual(provenance.pending(self.root, "demo"), [])  # another workspace is clean
        self.assertEqual(len(provenance.pending(self.root, "hq")), 2)  # HQ sees every workspace

    def test_hq_memory_counts_for_hq_only(self):
        before = provenance.scan(self.root, "hq")
        self.write("team/memory/cos.md", "- from a page")
        provenance.mark(self.root, before, provenance.scan(self.root, "hq"))
        self.assertEqual(len(provenance.pending(self.root, "hq")), 1)
        self.assertEqual(provenance.pending(self.root, "acme"), [])

    def test_trust_needs_the_version_read(self):
        before = provenance.scan(self.root, "acme")
        self.write("projects/acme/memos/m1.md", "v1")
        provenance.mark(self.root, before, provenance.scan(self.root, "acme"))
        seen = provenance.preview(self.root, "projects/acme/memos/m1.md")
        self.assertEqual(seen["text"], "v1")
        self.write("projects/acme/memos/m1.md", "v2 changed after the owner read it")
        with self.assertRaises(ValueError):
            provenance.trust(self.root, "projects/acme/memos/m1.md", seen["sig"])
        self.assertEqual(len(provenance.pending(self.root, "acme")), 1)
        seen = provenance.preview(self.root, "projects/acme/memos/m1.md")
        provenance.trust(self.root, "projects/acme/memos/m1.md", seen["sig"])
        self.assertEqual(provenance.pending(self.root, "acme"), [])

    def test_only_listed_files_can_be_read_or_trusted(self):
        self.write("projects/acme/secret.md", "x")
        for rel in ("projects/acme/secret.md", "../etc/passwd", ""):
            with self.assertRaises(LookupError):
                provenance.preview(self.root, rel)
            with self.assertRaises(LookupError):
                provenance.trust(self.root, rel, "1:1")

    def test_too_long_to_read_whole_keeps_its_label(self):
        before = provenance.scan(self.root, "acme")
        self.write("projects/acme/memos/big.md", "x" * (provenance.PREVIEW_CHARS + 1))
        provenance.mark(self.root, before, provenance.scan(self.root, "acme"))
        seen = provenance.preview(self.root, "projects/acme/memos/big.md")
        self.assertTrue(seen["cut"])
        with self.assertRaises(ValueError):
            provenance.trust(self.root, "projects/acme/memos/big.md", seen["sig"])

    def test_a_deleted_file_leaves_the_list(self):
        before = provenance.scan(self.root, "acme")
        self.write("projects/acme/memos/m1.md", "v1")
        provenance.mark(self.root, before, provenance.scan(self.root, "acme"))
        (self.root / "projects/acme/memos/m1.md").unlink()
        self.assertEqual(provenance.pending(self.root, "acme"), [])


if __name__ == "__main__":
    unittest.main()
