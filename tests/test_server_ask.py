import io, tempfile, unittest
from pathlib import Path
from unittest import mock
import dashboard.server as server
import index

_DB_TMP = None
_PRIOR_DB_PATH = None


def setUpModule():
    # Unpatched paths (db.posts in state_snapshot, db.tree in _handle_ask) read
    # os.db. Point them at an empty throwaway index so a fresh clone, which has
    # no os.db yet, passes and the user's real index is never touched.
    global _DB_TMP, _PRIOR_DB_PATH
    _DB_TMP = tempfile.TemporaryDirectory()
    root = Path(_DB_TMP.name)
    (root / "projects").mkdir()
    index.build(root)
    _PRIOR_DB_PATH = server.db.DB_PATH
    server.db.DB_PATH = root / "database" / "data" / "os.db"


def tearDownModule():
    server.db.DB_PATH = _PRIOR_DB_PATH
    _DB_TMP.cleanup()


class FakeSession:
    _started = False
    _turn_count = 0
    _skill_explicit = False
    _last_skill = None

    def is_fresh(self):
        return not self._started

    def note_skill(self, skill, explicit=False):
        pass

    def ask(self, text, with_web=False, model=None, read_dirs=(), edit_dirs=(), web_domains=()):
        yield ("delta", "Hi ")
        yield ("delta", "there")
        yield ("done", {"result": "Hi there"})


def _bare_handler(sink):
    """A Handler with the socket-backed I/O stubbed out, writing to `sink`."""
    h = server.Handler.__new__(server.Handler)  # bypass __init__/socket
    h.wfile = sink
    h.send_response = h.send_header = h.end_headers = lambda *a, **k: None
    return h


def _captured(body, tree=None):
    """Run _handle_ask with a session that records the prompt + routing args."""
    seen = {}

    class CaptureSession:
        _started = False
        _turn_count = 0
        _last_skill = None
        _skill_explicit = False

        def is_fresh(self):
            return not self._started

        def note_skill(self, skill, explicit=False):
            pass

        def ask(self, text, with_web=False, model=None, read_dirs=(), edit_dirs=(),
                web_domains=(), **tokens):
            seen["text"] = text
            seen["web_domains"] = web_domains
            seen["with_web"] = with_web
            seen["model"] = model
            seen["read_dirs"], seen["edit_dirs"] = tuple(read_dirs), tuple(edit_dirs)
            seen["workspace_dir"] = read_dirs[0] if read_dirs else None
            seen["file_search_mode"] = bool(read_dirs)
            yield ("done", {"result": "ok"})

    h = _bare_handler(io.BytesIO())
    h.server = mock.Mock(server_address=("127.0.0.1", 8765))
    # build_id_registry persists numbering under {server.ROOT}/database/data/ —
    # isolate it so fixture slugs (acme, demo, ...) never leak into the real repo.
    with tempfile.TemporaryDirectory() as tmp, \
         mock.patch.object(server, "get_chat_session", return_value=CaptureSession()), \
         mock.patch.object(server, "ROOT", Path(tmp)), \
         mock.patch.object(server.db, "tree", return_value=tree or []):
        h._handle_ask(body)
    return seen


def _captured_prompt(body, tree=None):
    """Back-compat helper: just the assembled prompt text."""
    return _captured(body, tree)["text"]


class SSE(unittest.TestCase):
    def test_handle_ask_streams_sse(self):
        sink = io.BytesIO()
        h = _bare_handler(sink)
        with mock.patch.object(server, "get_chat_session", return_value=FakeSession()):
            h._handle_ask({"messages": [{"role": "user", "content": "hi"}]})
        out = sink.getvalue().decode()
        self.assertIn('data: {"delta": "Hi "}', out)
        self.assertIn('data: [DONE]', out)
        self.assertLess(out.index('"delta": "Hi "'), out.index("[DONE]"))

    def test_handle_ask_prepends_state_snapshot(self):
        tree = [{"slug": "acme", "kind": "brand", "type": "project",
                 "profiles": [{"slug": "demo", "name": "Demo Brand",
                               "channels": [{"slug": "demo-tiktok", "name": "TikTok",
                                             "platform": "tiktok"}]}]}]
        text = _captured_prompt({"messages": [{"role": "user", "content": "do a thing"}]}, tree)
        self.assertIn("## Current Leo Bot state", text)
        self.assertIn("### acme id=pr1 (brand)", text)
        self.assertIn("## Request\ndo a thing", text)

    def test_handle_ask_includes_client_context(self):
        ctx = "Current view: Profiles · Demo\n## Attached files\n### plan.md\n```\nhello\n```"
        text = _captured_prompt({"messages": [{"role": "user", "content": "summarize"}],
                                 "context": ctx})
        # the client-supplied context (current view + attached file contents)
        # must reach the agent, ahead of the request itself
        self.assertIn("### plan.md", text)
        self.assertIn("Current view: Profiles · Demo", text)
        self.assertLess(text.index("### plan.md"), text.index("## Request\nsummarize"))

    def test_handle_ask_without_context_still_works(self):
        text = _captured_prompt({"messages": [{"role": "user", "content": "hi"}]})
        self.assertIn("## Request\nhi", text)

    def test_unknown_or_unlinked_project_cannot_add_a_directory(self):
        tree = [{"slug": "acme", "kind": "venture", "type": "project",
                 "folders": [], "profiles": []}]
        seen = _captured({
            "messages": [{"role": "user", "content": "inspect the app"}],
            "project_slug": "../../outside",
        }, tree)
        self.assertIsNone(seen["workspace_dir"])
        self.assertNotIn("Linked app workspace", seen["text"])
        self.assertIn("Workspace: HQ", seen["text"])  # unknown project → HQ, no folder
        seen = _captured({
            "messages": [{"role": "user", "content": "inspect the app"}],
            "room_id": "acme/general",
        }, tree)
        self.assertIsNone(seen["workspace_dir"])
        self.assertIn("App workspace unavailable", seen["text"])
        self.assertIn("Do not inspect the Leo Bot source tree", seen["text"])

    def test_tokensave_without_link_spends_no_model_turn(self):
        sink = io.BytesIO()
        h = _bare_handler(sink)

        class NoCallSession(FakeSession):
            def ask(self, *args, **kwargs):
                raise AssertionError("model must not run without a linked folder")

        with mock.patch.object(server, "get_chat_session", return_value=NoCallSession()), \
             mock.patch.object(server.db, "tree", return_value=[]):
            h._handle_ask({
                "messages": [{"role": "user", "content": "/tokensave inspect auth"}],
            })
        out = sink.getvalue().decode()
        self.assertIn("Link a folder to this workspace first", out)
        self.assertIn("[DONE]", out)

class RouteSkill(unittest.TestCase):
    def test_plain_chat_routes_nothing(self):
        for msg in ["hi", "what posts do I have?", "rename the project to Acme",
                    "thanks!", "mark draft-001 done"]:
            self.assertIsNone(server._route_skill(msg), msg)

    def test_keywords_suggest_not_route(self):
        cases = {
            "is this idea worth doing?": "problem-validation",
            "help me with positioning": "positioning",
            "fill the left panel with this spec": "workspace",
        }
        for msg, want in cases.items():
            self.assertIsNone(server._explicit_skill(msg), msg)
            self.assertEqual(server._suggest_skill(msg), want, msg)

    def test_explicit_tag_routes_skill(self):
        self.assertEqual(server._explicit_skill("run @gtm-assessment on acme"), "gtm-assessment")
        self.assertEqual(server._explicit_skill("use /problem-validation here"), "problem-validation")
        self.assertEqual(server._explicit_skill("@workspace help"), "workspace")

    def test_unknown_mention_does_not_route(self):
        self.assertIsNone(server._route_skill("email @bob about the @stuff"))

    def test_known_skills_have_files(self):
        for name in server.SKILL_NAMES:
            self.assertTrue(server._load_skill_body(name), f"{name} body empty/missing")

    def test_skills_index_has_name_and_description(self):
        idx = server._skills_index()
        self.assertTrue(idx)
        names = {s["name"] for s in idx}
        self.assertIn("problem-validation", names)
        for s in idx:
            self.assertTrue(s["name"])
            self.assertTrue(s["description"], f"{s['name']}: empty description (broken frontmatter?)")

    def test_every_skill_file_opens_frontmatter(self):
        for f in sorted(server.SKILLS_DIR.glob("*/SKILL.md")):
            self.assertTrue(f.read_text(encoding="utf-8").startswith("---\n"), f"{f.parent.name}: bad opener")

    def test_skill_body_has_no_frontmatter(self):
        for name in server.SKILL_NAMES:
            if name in server._RAIL_COVERED_SKILL_STUBS:
                continue
            head = server._load_skill_body(name).splitlines()[:3]
            self.assertFalse([l for l in head if l.startswith(("name:", "description:")) or l.endswith("---")],
                             f"{name}: frontmatter leaked into body")

    def test_web_token_forces_web(self):
        self.assertTrue(server._needs_web("write copy /web"))
        self.assertFalse(server._needs_web("write copy"))

    def test_current_view_in_context_does_not_force_web(self):
        """buildContext header must not escalate model via WEB_RE `current`."""
        ctx = "Current view: Profiles · Demo\nSection: Demo Brand"
        self.assertFalse(server._needs_web(ctx))
        seen = _captured({"messages": [{"role": "user", "content": "hi"}],
                          "context": ctx})
        self.assertFalse(seen["with_web"])
        self.assertEqual(seen["model"], server.chat_session.CHAT_MODEL)

    def test_workspace_classifier_is_local_and_specific(self):
        self.assertTrue(server._needs_workspace("where is auth handled?"))
        self.assertTrue(server._needs_workspace("review dashboard/server.py"))
        self.assertTrue(server._needs_workspace("look through the linked folder"))
        self.assertTrue(server._needs_workspace("how does my app work?"))
        self.assertTrue(server._needs_workspace("anything", skill="tokensave"))
        self.assertFalse(server._needs_workspace("what positioning memos do we have?"))

    def test_app_context_classifier_covers_gtm_but_skips_admin(self):
        self.assertTrue(server._needs_app_context("what should our brand emphasize?"))
        self.assertTrue(server._needs_app_context("draft launch copy", suggest="launch-plan"))
        self.assertTrue(server._needs_app_context("where is auth handled?"))
        self.assertFalse(server._needs_app_context("rename this project"))
        self.assertFalse(server._needs_app_context("thanks"))


class NeedsWebAndModel(unittest.TestCase):
    def test_web_requires_explicit_tag(self):
        self.assertFalse(server._needs_web("search the web for X"))
        self.assertFalse(server._needs_web("what's the latest on this"))
        self.assertTrue(server._needs_web("look this up /web"))
        self.assertFalse(server._needs_web("rename the project"))

    def test_research_skill_forces_web(self):
        self.assertTrue(server._needs_web("anything", skill="competitor-scan"))
        self.assertFalse(server._needs_web("anything", skill="positioning"))

    def test_model_tiering(self):
        self.assertEqual(server._pick_model(None, False), server.chat_session.CHAT_MODEL)
        self.assertEqual(
            server._pick_model("positioning", False, "help with positioning", explicit=True),
            server.chat_session.ESCALATED_MODEL,
        )
        self.assertEqual(
            server._pick_model("positioning", False, "help with positioning", explicit=False),
            server.chat_session.CHAT_MODEL,
        )
        self.assertEqual(server._pick_model(None, True), server.chat_session.ESCALATED_MODEL)

    def test_read_only_tagged_skill_stays_haiku(self):
        self.assertEqual(
            server._pick_model("positioning", False, "what positioning memos do we have?",
                               explicit=True),
            server.chat_session.CHAT_MODEL,
        )

    def test_continuation_escalates(self):
        sess = type("S", (), {"_skill_explicit": True, "_last_skill": "workspace"})()
        self.assertEqual(
            server._pick_model(None, False, "yes save sec02", sess),
            server.chat_session.ESCALATED_MODEL,
        )

    def test_active_thread_clarifying_answer_stays_escalated(self):
        """A natural clarifying-answer reply mid-skill (not "yes"/"save"/etc)
        must not silently drop to Haiku — that caused a Sonnet→Haiku→Sonnet
        ping-pong that pays the uncached system-prompt cost twice."""
        sess = type("S", (), {"_skill_explicit": True, "_last_skill": "market-sizing"})()
        self.assertEqual(
            server._pick_model(None, False, "the segment is small business owners in Italy", sess),
            server.chat_session.ESCALATED_MODEL,
        )

    def test_active_thread_standalone_read_still_cheap(self):
        """A genuine unrelated read-only query mid-thread still falls back to
        the cheap model — only reads should, not every non-magic-word reply."""
        sess = type("S", (), {"_skill_explicit": True, "_last_skill": "market-sizing"})()
        self.assertEqual(
            server._pick_model(None, False, "what posts do I have", sess),
            server.chat_session.CHAT_MODEL,
        )


class ProjectScopedChatSession(unittest.TestCase):
    def test_switching_projects_starts_a_fresh_session(self):
        old_chat, old_scope = server._CHAT, server._CHAT_SCOPE
        first = mock.Mock()
        second = mock.Mock()
        third = mock.Mock()
        try:
            server._CHAT = None
            server._CHAT_SCOPE = None
            with mock.patch.object(server.chat_session, "ChatSession",
                                   side_effect=[first, second, third]) as make:
                # Scope is the workspace: an old (slug, path) scope is that slug.
                self.assertIs(server.get_chat_session(("acme", "/app-a")), first)
                self.assertIs(server.get_chat_session(("acme",)), first)
                self.assertIs(server.get_chat_session(("other",)), second)
                self.assertIs(server.get_chat_session(None), third)
            self.assertEqual(make.call_count, 3)
            first.close.assert_called_once_with()
            second.close.assert_called_once_with()
            self.assertIsNone(server._CHAT_SCOPE)
        finally:
            server._CHAT = old_chat
            server._CHAT_SCOPE = old_scope


class SkillRouting(unittest.TestCase):
    def test_cheap_turn_no_skill_no_web_cheap_model(self):
        seen = _captured({"messages": [{"role": "user", "content": "what posts do I have?"}]})
        self.assertFalse(seen["with_web"])
        self.assertEqual(seen["model"], server.chat_session.CHAT_MODEL)
        self.assertNotIn("## Active skill:", seen["text"])

    def test_tagged_turn_injects_skill_and_escalates(self):
        seen = _captured({"messages": [{"role": "user", "content": "/problem-validation validate this idea"}]})
        self.assertIn("## Active skill: problem-validation", seen["text"])
        self.assertEqual(seen["model"], server.chat_session.ESCALATED_MODEL)

    def test_untagged_read_gets_no_hint_and_no_skill(self):
        seen = _captured({"messages": [{"role": "user", "content": "help me with positioning"}]})
        self.assertNotIn("Skill hint", seen["text"])
        self.assertNotIn("## Active skill:", seen["text"])
        self.assertEqual(seen["model"], server.chat_session.CHAT_MODEL)

    def test_untagged_write_runs_the_model(self):
        sink = io.BytesIO()
        h = _bare_handler(sink)

        class S(FakeSession):
            _started = True
            _turn_count = 1

        with mock.patch.object(server, "get_chat_session", return_value=S()):
            h._handle_ask({"messages": [{"role": "user", "content": "fill all the tabs"}]})
        out = sink.getvalue().decode()
        self.assertNotIn("tag who should do it", out)
        self.assertIn('"delta": "Hi "', out)
        self.assertIn("[DONE]", out)
        self.assertFalse(hasattr(server, "_tag_gate_message"))


class ComposeTurnPrompt(unittest.TestCase):
    def setUp(self):
        # compose_turn_prompt -> state_snapshot persists numbering under
        # {server.ROOT}/database/data/ — isolate so fixture slugs (acme)
        # never leak into the real repo's registry file.
        self.tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(server, "ROOT", Path(self.tmp.name))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmp.cleanup()

    def _sess(self, *, started=False, turn_count=0, last_skill=None):
        class S:
            pass
        s = S()
        s._started = started
        s._turn_count = turn_count
        s._last_skill = last_skill
        s.is_fresh = lambda: not s._started
        return s

    def test_fresh_includes_snapshot(self):
        tree = [{"slug": "acme", "kind": "brand", "type": "project", "profiles": []}]
        text = server.compose_turn_prompt(
            user_msg="hi", context="", skill=None,
            session=self._sess(), projects=tree,
        )
        self.assertIn("## Current Leo Bot state", text)
        self.assertNotIn("resumed session", text)

    def test_resume_continuation_keeps_skill_stub(self):
        s = self._sess(started=True, turn_count=2, last_skill="workspace")
        s._skill_explicit = True
        tree = [{"slug": "acme", "kind": "brand", "type": "project", "profiles": []}]
        text = server.compose_turn_prompt(
            user_msg="yes save it", context="", skill=None,
            session=s, projects=tree,
        )
        self.assertNotIn("## Current Leo Bot state", text)
        self.assertIn("resumed session", text)
        self.assertIn("continuing", text)
        self.assertNotIn("Tab map + write gate", text)

    def test_resume_new_explicit_tag_gets_full_body(self):
        text = server.compose_turn_prompt(
            user_msg="now pricing", context="", skill="pricing-strategy",
            session=self._sess(started=True, turn_count=1, last_skill="positioning"),
            projects=[], explicit=True,
        )
        self.assertIn("## Active skill: pricing-strategy", text)
        self.assertNotIn("continuing", text)


class StateSnapshot(unittest.TestCase):
    def setUp(self):
        # build_id_registry now persists numbering to {root}/database/data/
        # id_registry.json — isolate ROOT so fixture slugs (acme, solo, p2)
        # never leak into the real repo's registry file.
        self.tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(server, "ROOT", Path(self.tmp.name))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmp.cleanup()

    def test_empty_tree(self):
        out = server.state_snapshot([])
        self.assertTrue(out.startswith("## Current Leo Bot state"))
        self.assertIn("(no projects yet)", out)

    def test_nested_outline(self):
        tree = [{"slug": "acme", "kind": "brand", "type": "project",
                 "profiles": [{"slug": "demo", "name": "Demo Brand",
                               "channels": [
                                   {"slug": "demo-tiktok", "name": "TikTok", "platform": "tiktok"},
                                   {"slug": "demo-ig", "name": "Instagram", "platform": "instagram"},
                               ]}]}]
        out = server.state_snapshot(tree)
        self.assertIn("id=pr1", out)
        self.assertIn("id=pr1.sec02", out)
        self.assertIn("id=pr1.pf1", out)
        self.assertIn("id=pr1.pf1.ch1", out)
        self.assertIn("id=pr1.pf1.ch2", out)

    def test_project_no_profiles_and_profile_no_channels(self):
        tree = [{"slug": "solo", "type": "project", "profiles": []},
                {"slug": "p2", "kind": "venture", "type": "project",
                 "profiles": [{"slug": "prof", "name": "Prof", "channels": []}]}]
        out = server.state_snapshot(tree)
        self.assertIn("id=pr1", out)
        self.assertIn("id=pr2.pf1", out)
        self.assertNotIn("channel", out)



# ---- linked folders on chat turns (core.folders) --------------------------- #
from core import folders as _folders  # noqa: E402
from dashboard.engines.claude_code import ClaudeCodeEngine as _CC  # noqa: E402


class _FakeEngine:
    label = "Fake"
    shared_safe = True

    def __init__(self, id="claude-code", kind="cli", supports=_CC.supports, full_disk=False):
        self.id, self.kind, self.supports, self.full_disk = id, kind, frozenset(supports), full_disk

    def resolve_model(self, model):
        return model

    def is_fresh(self, session):
        return True


class TurnFolders(unittest.TestCase):
    """Which folders a turn gets, and what it may change there."""

    def setUp(self):
        root, ext = tempfile.TemporaryDirectory(), tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.addCleanup(ext.cleanup)
        self.root = Path(root.name).resolve()
        ext = Path(ext.name).resolve()
        self.a, self.b, self.d = ext / "app-a", ext / "docs-b", ext / "demo-app"
        for d in (self.a, self.b, self.d):
            d.mkdir()
        for slug in ("acme", "demo"):
            (self.root / "projects" / slug).mkdir(parents=True)
            (self.root / "projects" / slug / "project.md").write_text(f"---\nname: {slug}\nkind: venture\n---\n")
        (self.root / "team").mkdir()
        _folders.add("acme", str(self.a), _folders.EDIT, self.root)
        _folders.add("acme", str(self.b), _folders.READ, self.root)
        _folders.add("demo", str(self.d), _folders.EDIT, self.root)
        self.reply = "ok"

    def tree(self):
        return [{"slug": s, "kind": "venture", "type": "project", "profiles": [],
                 "folders": _folders.load(s, self.root)["folders"]} for s in ("acme", "demo")]

    def turn(self, msg, room="acme/general", engine=None, shared=False, explicit_thread=False,
             runner=False, apps=False, session_id=None, ctx_error=None, recap=None):
        seen = {"runs": [], "ctx": []}
        test = self

        def record(self_, text, with_web=False, model=None, read_dirs=(), edit_dirs=(), web_domains=(), **kw):
            seen["runs"].append({"text": text, "model": model, "web": with_web, "read_dirs": tuple(read_dirs),
                                 "edit_dirs": tuple(edit_dirs), "deny_dirs": tuple(getattr(self_, "deny_dirs", ())),
                                 "who": type(self_).__name__})
            yield ("delta", test.reply if len(seen["runs"]) == 1 else "ok")
            yield ("done", {"result": "ok"})

        class Sess:
            _started = False
            _turn_count = 0
            _last_skill = "folder-work" if explicit_thread else None
            _skill_explicit = explicit_thread
            ask = record

            def is_fresh(self):
                return True

            def note_skill(self, skill, explicit=False):
                pass

        sess = Sess()
        if session_id:
            sess.session_id = session_id
        if recap:
            sess.pending_recap = recap
        seen["sess"] = sess
        seen["approvals"] = mock.Mock(return_value={"id": "ap1"})
        h = _bare_handler(io.BytesIO())
        h.server = mock.Mock(server_address=("127.0.0.1", 8765))
        patches = [mock.patch.object(server, "get_chat_session", return_value=sess),
                   mock.patch.object(server, "ROOT", self.root),
                   mock.patch.object(server.fileops, "ROOT", self.root),
                   mock.patch.object(server.db, "tree", return_value=self.tree()),
                   mock.patch.object(server.chat_session.ChatSession, "ask", record),
                   mock.patch.object(server.tokensave_bridge, "context",
                                     side_effect=ctx_error or (lambda *a, **k: seen["ctx"].append(k) or "graph")),
                   mock.patch.object(server.chat_store, "CHATS_DIR", self.root / "chats"),
                   mock.patch.object(server.chat_store, "record_turn"),
                   mock.patch.object(server.approvals, "add", seen["approvals"])]
        if engine is not None:
            patches.append(mock.patch.object(server.engine_registry, "pick", return_value=engine))
        if shared:
            patches.append(mock.patch.object(server, "_shared_room", return_value=True))
        if runner:
            patches.append(mock.patch.object(server.runners.HUB, "online", return_value=True))
        if apps:
            patches += [mock.patch.object(server.integrations, "api_key", return_value="k"),
                        mock.patch.object(server, "_needs_apps", return_value=True)]
        for p in patches:
            p.start()
        try:
            h._handle_ask({"messages": [{"role": "user", "content": msg}], "room_id": room})
        finally:
            for p in reversed(patches):
                p.stop()
        seen["out"] = h.wfile.getvalue().decode()
        seen["lead"] = seen["runs"][0] if seen["runs"] else None
        return seen

    def test_untagged_turn_reads_all_folders_and_edits_none(self):
        lead = self.turn("where is auth handled?")["lead"]
        self.assertEqual(lead["read_dirs"], (str(self.a), str(self.b)))
        self.assertEqual(lead["edit_dirs"], ())
        self.assertIn("tag", lead["text"].split("## Linked folders", 1)[1].lower())

    def test_tagged_dm_skill_and_continuation_can_edit(self):
        for name, kw in (("bot tag", {"msg": "@cto fix the login redirect"}),
                         ("skill tag", {"msg": "/folder-work tidy the readme"}),
                         ("dm", {"msg": "tidy the readme", "room": "acme/dm-cto"}),
                         ("continuation", {"msg": "yes go ahead", "explicit_thread": True})):
            with self.subTest(name):
                lead = self.turn(**kw)["lead"]
                self.assertEqual(lead["edit_dirs"], (str(self.a),))
                self.assertEqual(lead["read_dirs"], (str(self.a), str(self.b)))

    def test_untagged_save_word_runs_but_edits_nothing(self):
        seen = self.turn("update the readme with the new steps")
        self.assertNotIn("tag who should do it", seen["out"])
        self.assertEqual(seen["lead"]["edit_dirs"], ())

    def test_read_folder_never_in_edit_dirs(self):
        lead = self.turn("@cto refactor the docs folder")["lead"]
        self.assertNotIn(str(self.b), lead["edit_dirs"])

    def test_app_turn_and_apps_used_thread_drop_edit(self):
        lead = self.turn("@cto check my inbox and fix the login", apps=True)["lead"]
        self.assertEqual(lead["edit_dirs"], ())

    def test_web_turn_keeps_edit(self):
        # A fetched page could carry instructions: accepted (spec), fetches reach only typed hosts.
        lead = self.turn("@cto /web fix the login like docs.example.com says")["lead"]
        self.assertTrue(lead["web"])
        self.assertEqual(lead["edit_dirs"], (str(self.a),))

    def test_shared_and_runner_turns_get_no_folders(self):
        for name, kw in (("shared", {"shared": True}), ("runner", {"runner": True})):
            with self.subTest(name):
                lead = self.turn("@cto fix the login", **kw)["lead"]
                self.assertEqual((lead["read_dirs"], lead["edit_dirs"]), ((), ()))
                self.assertNotIn(str(self.a), lead["text"])
                self.assertNotIn("## Linked folders", lead["text"])

    def test_codex_and_acp_never_get_edit_dirs(self):
        for eid in ("codex", "acp"):
            with self.subTest(eid):
                seen = self.turn("@cto fix the login", engine=_FakeEngine(eid, supports=(), full_disk=True))
                lead = seen["lead"]
                self.assertEqual(lead["edit_dirs"], ())
                block = lead["text"].split("## Linked folders", 1)[1]
                self.assertIn(f"{self.a}", block)
                self.assertNotIn("(edit)", block)

    def test_engines_refuse_edit_dirs_they_cannot_enforce(self):
        from dashboard.engines.base import Engine, EngineRefused, TurnPolicy
        eng = Engine()
        eng.label = "X"
        with self.assertRaises(EngineRefused):
            eng.check(TurnPolicy(read_dirs=("/w/a",), edit_dirs=("/w/a",)))
        eng.check(TurnPolicy(read_dirs=("/w/a",)))

    def test_consulted_teammate_reads_only(self):
        seen = self.turn("@cmo @cto how should the login page read?")
        mate = seen["runs"][1]
        self.assertEqual(mate["who"], "ChatSession")
        self.assertEqual(mate["read_dirs"], (str(self.a), str(self.b)))
        self.assertEqual(mate["edit_dirs"], ())
        self.assertIn("## Linked folders", mate["text"])

    def test_followup_keeps_the_leads_folders(self):
        self.reply = "Let me check.\nCONSULT @cmo: what tone?"
        seen = self.turn("@cto fix the login copy")
        self.assertEqual(len(seen["runs"]), 3)
        lead, follow = seen["runs"][0], seen["runs"][2]
        self.assertEqual((follow["read_dirs"], follow["edit_dirs"]), (lead["read_dirs"], lead["edit_dirs"]))
        self.assertEqual(lead["edit_dirs"], (str(self.a),))

    def test_other_workspace_folder_in_deny_dirs_unless_also_linked(self):
        lead = self.turn("where is auth handled?")["lead"]
        self.assertIn(str(self.d), lead["deny_dirs"])
        _folders.add("acme", str(self.d), _folders.READ, self.root)
        lead = self.turn("where is auth handled?")["lead"]
        self.assertNotIn(str(self.d), lead["deny_dirs"])
        self.assertIn(str(self.d), lead["read_dirs"])

    def test_hq_gets_its_own_folders_and_no_other_workspace_denies(self):
        hq = self.root.parent / (self.root.name + "-hq")
        hq.mkdir()
        self.addCleanup(lambda: __import__("shutil").rmtree(hq, ignore_errors=True))
        _folders.add("hq", str(hq), _folders.EDIT, self.root)
        lead = self.turn("@cos tidy the notes", room="hq/general")["lead"]
        self.assertEqual(lead["read_dirs"], (str(hq),))
        self.assertEqual(lead["edit_dirs"], (str(hq),))
        self.assertNotIn(str(self.a), lead["deny_dirs"])

    def test_layer_removed_nested_folder_denied(self):
        inner = self.a / "vendor"
        inner.mkdir()
        with self.assertRaises(_folders.FolderError):  # linking one inside another is refused now
            _folders.add("acme", str(inner), _folders.EDIT, self.root)
        # An older store may still hold such a pair: the inner one stays walled off.
        data = _folders.load("acme", self.root)
        data["folders"].append({"id": "f3", "path": str(inner), "name": "vendor", "access": "edit"})
        _folders.save("acme", data, self.root)
        row = data["folders"][-1]
        _folders.set_limit("acme", "bots", "cto", {"f1": "edit"}, self.root)
        lead = self.turn("@cto fix the login")["lead"]
        self.assertEqual(lead["read_dirs"], (str(self.a),))
        self.assertIn(str(inner), lead["deny_dirs"])
        self.assertTrue(row["id"])

    def test_channel_layer_limits_and_dm_skips_it(self):
        _folders.set_limit("acme", "rooms", "general", {"f2": "read"}, self.root)
        lead = self.turn("@cto fix the login")["lead"]
        self.assertEqual((lead["read_dirs"], lead["edit_dirs"]), ((str(self.b),), ()))
        lead = self.turn("fix the login", room="acme/dm-cto")["lead"]
        self.assertEqual(lead["edit_dirs"], (str(self.a),))

    def test_stale_folder_skipped_and_chat_still_answers(self):
        self.b.rmdir()
        seen = self.turn("where is auth handled?")
        self.assertEqual(seen["lead"]["read_dirs"], (str(self.a),))
        self.assertIn("[DONE]", seen["out"])

    def test_block_lists_folders_and_is_absent_with_none(self):
        text = self.turn("hello there")["lead"]["text"]
        block = text.split("## Linked folders", 1)[1]
        self.assertIn(f"app-a: {self.a}", block)
        self.assertIn(f"docs-b: {self.b}", block)
        self.assertIn("read-only", block)
        self.assertIn("CLAUDE.md", block)
        self.assertIn("AGENTS.md", block)
        _folders.remove("acme", "f1", self.root)
        _folders.remove("acme", "f2", self.root)
        self.assertNotIn("## Linked folders", self.turn("hello there")["lead"]["text"])

    def test_tokensave_stops_only_for_tokensave_without_index(self):
        seen = self.turn("/tokensave how does login work")
        self.assertEqual(seen["runs"], [])
        self.assertIn("tokensave init", seen["out"])
        _folders.remove("acme", "f1", self.root)
        _folders.remove("acme", "f2", self.root)
        seen = self.turn("/tokensave how does login work")
        self.assertEqual(seen["runs"], [])
        self.assertIn("Link a folder", seen["out"])

    def test_tokensave_uses_the_first_indexed_folder(self):
        (self.b / ".tokensave").mkdir()
        (self.b / ".tokensave" / "tokensave.db").write_text("")
        with mock.patch.object(server.shutil, "which", return_value="/bin/tokensave"):
            seen = self.turn("/tokensave how does login work")
            self.assertEqual(seen["ctx"][0]["folder"], str(self.b))
            self.assertTrue(seen["ctx"][0]["include_code"])
            self.assertIn("graph", seen["lead"]["text"])
            # A shared thread gets no outline at all: an invited person's words never steer a
            # read of the owner's code.
            seen = self.turn("/tokensave how does login work", shared=True)
            self.assertEqual(seen["ctx"], [])
            self.assertEqual(seen["runs"], [])
            self.assertIn("not available in a thread invited people can see", seen["out"])
            self.assertNotIn(str(self.b), seen["out"])

    def test_fix_wording_never_pulls_another_bots_mention_to_cto(self):
        team = server.bots.team(self.root, "acme", self.tree()[0])
        sess = type("S", (), {"_skill_explicit": False, "_last_skill": None, "_last_bot": None})()
        for msg, bot in (("@cmo fix the typo in the launch post", "cmo"),
                         ("@gtm fix the date of the webinar", "gtm"),
                         ("@cos implement the plan we agreed", "cos")):
            got = server._route_bot(team, msg, sess)
            self.assertEqual(got[0], bot, msg)
            self.assertNotEqual(got[1], "folder-work", msg)
        self.assertEqual(server._route_bot(team, "@cto fix the login", sess)[:2], ("cto", "folder-work"))
        self.assertIsNone(server._suggest_skill("fix the typo in the launch post"))

    def test_runner_turn_gets_no_source_code_or_error_detail(self):
        (self.b / ".tokensave").mkdir()
        (self.b / ".tokensave" / "tokensave.db").write_text("")
        with mock.patch.object(server.shutil, "which", return_value="/bin/tokensave"):
            seen = self.turn("/tokensave how does login work", runner=True)
            self.assertEqual(seen["ctx"], [])  # a person's own Mac reads none of the owner's code
            seen = self.turn("/tokensave how does login work", runner=True,
                             ctx_error=server.tokensave_bridge.BridgeError(f"bad index at {self.b}"))
        self.assertNotIn(str(self.b), seen["out"])

    def test_symlinked_stored_path_never_forks_the_thread(self):
        link = self.root.parent / (self.root.name + "-link")
        link.symlink_to(self.a)
        self.addCleanup(link.unlink)
        _folders.save("acme", {"folders": [{"id": "f1", "path": str(link), "access": "edit"}]}, self.root)
        linked = set(_folders.all_paths("acme", self.root))
        used = {r["path"] for r in _folders.for_turn("acme", None, "cto", self.root)}
        self.assertFalse(used - linked)

    def test_a_bot_limited_to_no_folders_is_told_so_not_to_link_one(self):
        _folders.set_limit("acme", "bots", "cto", {}, self.root)
        lead = self.turn("@cto where is the auth handler in the code?")["lead"]
        self.assertEqual(lead["read_dirs"], ())
        self.assertIn("limited", lead["text"])
        self.assertNotIn("ask the owner to link", lead["text"])
        # Every folder stale: that is said, not a limit.
        _folders.set_limit("acme", "bots", "cto", None, self.root)
        self.a.rmdir()
        self.b.rmdir()
        lead = self.turn("@cto where is the auth handler in the code?")["lead"]
        self.assertIn("no usable linked folder", lead["text"])
        self.assertNotIn("limited", lead["text"])

    def test_recap_goes_before_the_prompt_once(self):
        seen = self.turn("hello there", recap="RECAP-OF-EARLIER-THREAD")
        self.assertTrue(seen["lead"]["text"].startswith("RECAP-OF-EARLIER-THREAD"))
        self.assertFalse(seen["sess"].pending_recap)

    def test_tokensave_tag_runs_on_the_cheap_model_with_the_graph(self):
        (self.b / ".tokensave").mkdir()
        (self.b / ".tokensave" / "tokensave.db").write_text("")
        with mock.patch.object(server.shutil, "which", return_value="/bin/tokensave"):
            seen = self.turn("/tokensave where is auth handled?")
        lead = seen["lead"]
        self.assertEqual(lead["model"], server.chat_session.CHAT_MODEL)
        self.assertIn("## Active skill: tokensave", lead["text"])
        self.assertIn("already refreshed and queried", lead["text"])
        self.assertEqual(seen["ctx"][0]["active_project"], "acme")

    def test_tokensave_refresh_failure_spends_no_model_turn_or_stale_context(self):
        (self.b / ".tokensave").mkdir()
        (self.b / ".tokensave" / "tokensave.db").write_text("")
        with mock.patch.object(server.shutil, "which", return_value="/bin/tokensave"):
            seen = self.turn("/tokensave inspect auth", ctx_error=server.tokensave_bridge.BridgeError("index locked"))
        self.assertEqual(seen["runs"], [])
        self.assertIn("index locked", seen["out"])
        self.assertIn("No stale context was used", seen["out"])

    def test_cto_edit_turn_runs_with_no_index_on_the_strong_model(self):
        lead = self.turn("@cto fix the login redirect")["lead"]
        self.assertEqual(lead["model"], server.chat_session.ESCALATED_MODEL)
        self.assertIn("folder-work", lead["text"])

    def test_edit_word_picks_strong_model_only_when_editing(self):
        pick = server._pick_model
        self.assertEqual(pick(None, False, "fix the login", None, editing=True), server.chat_session.ESCALATED_MODEL)
        self.assertEqual(pick(None, False, "fix the login", None, editing=False), server.chat_session.CHAT_MODEL)
        self.assertEqual(pick("tokensave", False, "rename the handler", None, editing=True),
                         server.chat_session.ESCALATED_MODEL)
        self.assertEqual(pick(None, False, "how does it work", None, editing=True), server.chat_session.CHAT_MODEL)
        # The edit words never reach the tag gate.
        self.assertIsNone(server._WRITE_TURN_RE.search("fix the login"))

    def test_untagged_private_turn_may_ask_approval(self):
        self.reply = "Done.\nAPPROVAL: save it"
        seen = self.turn("tidy the readme please", room="acme/dm-cto", session_id="c1")
        self.assertEqual(seen["lead"]["edit_dirs"], (str(self.a),))  # a DM turn may edit
        seen["approvals"].assert_called_once()  # any private turn may propose a save
        seen = self.turn("/folder-work fix the readme", session_id="c1")
        seen["approvals"].assert_called_once()


class SavedThreadFolders(unittest.TestCase):
    """A saved thread keeps its context across folder changes unless a folder it used is gone."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.chats = Path(tmp.name)
        p = mock.patch.object(server.chat_store, "CHATS_DIR", self.chats)
        p.start()
        self.addCleanup(p.stop)
        old = (server._CHAT, server._CHAT_SCOPE)
        server._CHAT = server._CHAT_SCOPE = None

        def restore():
            server._CHAT, server._CHAT_SCOPE = old
        self.addCleanup(restore)

    def id(self, alias):
        """Chat ids are uuids (chat_store): one per alias in a test."""
        self._ids = getattr(self, "_ids", {})
        return self._ids.setdefault(alias, str(__import__("uuid").uuid4()))

    def saved(self, scope, folder_paths=(), cid="c1"):
        rec = {"id": self.id(cid), "title": "t", "created": 1, "updated": 1, "scope": scope, "started": True,
               "turn_count": 2, "room": "acme/general", "engines": {},
               "messages": [{"role": "user", "content": "earlier message", "ts": 1},
                            {"role": "assistant", "content": "earlier answer", "ts": 2, "bot": "cto"}],
               "folder_paths": list(folder_paths)}
        server.chat_store.save(rec)
        return rec

    def test_old_scope_keeps_its_context(self):
        self.saved(["acme", "/app-a"])
        sess = server.get_chat_session(("acme",), self.id("c1"), linked={"/app-a"})
        self.assertEqual(sess.session_id, self.id("c1"))
        self.assertTrue(sess._started)
        self.assertFalse(getattr(sess, "pending_recap", None))

    def test_adding_a_folder_keeps_the_session(self):
        self.saved(["acme"], ["/w/a"])
        sess = server.get_chat_session(("acme",), self.id("c1"), linked={"/w/a", "/w/b"})
        self.assertEqual(sess.session_id, self.id("c1"))
        self.assertEqual(sess.folder_paths, {"/w/a"})

    def test_changing_access_or_a_layer_keeps_the_session(self):
        self.saved(["acme"], ["/w/a"])
        first = server.get_chat_session(("acme",), self.id("c1"), linked={"/w/a"})
        # Same active thread, next turn: access or a layer changed, the paths did not.
        second = server.get_chat_session(("acme",), self.id("c1"), linked={"/w/a"})
        self.assertIs(second, first)

    def test_unlinking_a_used_folder_starts_fresh_with_a_recap(self):
        self.saved(["acme"], ["/w/a", "/w/b"])
        second = server.get_chat_session(("acme",), self.id("c1"), linked={"/w/a"})
        self.assertNotEqual(second.session_id, self.id("c1"))
        self.assertIn("earlier message", second.pending_recap)
        self.assertTrue(server.chat_store.load(self.id("c1")))  # the old chat stays saved
        # The active session, too: a used folder unlinked mid-thread.
        first = server.get_chat_session(("acme",), self.id("c1"), linked={"/w/a", "/w/b"})
        self.assertEqual(first.session_id, self.id("c1"))
        again = server.get_chat_session(("acme",), self.id("c1"), linked={"/w/b"})
        self.assertIsNot(again, first)
        self.assertIn("earlier message", again.pending_recap)

    def test_hq_thread_with_folders_never_moves_to_a_company(self):
        self.saved(None, ["/w/hq-notes"])
        sess = server.get_chat_session(("acme",), self.id("c1"), linked=set())
        self.assertNotEqual(sess.session_id, self.id("c1"))
        # The active HQ session that used a folder is not adopted either.
        hq = server.get_chat_session(None, self.id("c1"), linked={"/w/hq-notes"})
        self.assertEqual(hq.session_id, self.id("c1"))
        moved = server.get_chat_session(("acme",), self.id("c1"), linked=set())
        self.assertIsNot(moved, hq)
        # An HQ thread that never had folders still adopts its first project.
        self.saved(None, [], cid="c2")
        self.assertEqual(server.get_chat_session(("acme",), self.id("c2"), linked=set()).session_id, self.id("c2"))

    def test_person_slot_follows_the_same_rules(self):
        self.saved(["acme", "/app-a"])
        self.assertEqual(server.person_chat_session("p1", ("acme",), self.id("c1"), linked={"/app-a"}).session_id,
                         self.id("c1"))
        self.saved(["acme"], ["/w/a"], cid="c3")
        sess = server.person_chat_session("p1", ("acme",), self.id("c3"), linked=set())
        self.assertNotEqual(sess.session_id, self.id("c3"))
        server.stop_person_chat("p1")
        server._PERSON_CHATS.pop("p1", None)

    def test_ask_records_folder_paths_and_restore_reads_them(self):
        class Eng:
            id, label, kind, supports, full_disk = "fake", "Fake", "cli", frozenset({"folder_edit"}), False

            def is_fresh(self, s):
                return True

            def turn(self, session, text, policy, model, tokens):
                yield ("done", {})
        sess = server.chat_session.ChatSession(repo_dir=".", rail="r")
        sess.engine = Eng()
        list(sess.ask("hi", read_dirs=("/w/a",), edit_dirs=("/w/a",)))
        list(sess.ask("hi again"))
        self.assertEqual(sess.folder_paths, {"/w/a"})
        server.chat_store.record_turn(sess, ("acme",), "hi", "ok", room="acme/general")
        rec = server.chat_store.load(sess.session_id)
        self.assertEqual(rec["folder_paths"], ["/w/a"])
        self.assertEqual(rec["scope"], ["acme"])
        self.assertFalse(rec["shared"])
        back = server.chat_session.ChatSession.restore(".", "r", rec)
        self.assertEqual(back.folder_paths, {"/w/a"})

    def test_thread_with_a_folder_turn_hidden_from_a_person_who_joins_later(self):
        from core import rooms
        sess = server.chat_session.ChatSession(repo_dir=".", rail="r")
        sess.folder_paths = {"/w/a"}
        server.chat_store.record_turn(sess, ("acme",), "hi", "ok", room="acme/general", shared=False)
        rec = server.chat_store.load(sess.session_id)
        room = {"id": "acme/general", "kind": "general"}
        self.assertTrue(rooms.chat_visible(rec, room, {"kind": "owner"}))
        self.assertFalse(rooms.chat_visible(rec, room, {"kind": "person", "id": "p1"}))

    def test_person_never_gets_folder_paths(self):
        rec = self.saved(["acme", "/app-a"], ["/w/a"])
        out = server._for_person(rec)
        self.assertNotIn("folder_paths", out)
        self.assertNotIn("scope", out)
        self.assertNotIn("/w/a", str(out))
        for row in server.chat_store.list_meta():
            self.assertNotIn("folder_paths", row)

    def test_an_old_scope_path_counts_as_used(self):
        self.saved(["acme", "/app-a"])
        self.assertEqual(server.get_chat_session(("acme",), self.id("c1"), linked={"/app-a"}).session_id, self.id("c1"))
        server._CHAT = server._CHAT_SCOPE = None
        fresh = server.get_chat_session(("acme",), self.id("c1"), linked=set())
        self.assertNotEqual(fresh.session_id, self.id("c1"))
        self.assertIn("earlier message", fresh.pending_recap)

    def test_an_unresolved_old_scope_path_matches_its_resolved_link(self):
        import os
        self.saved(["acme", "/tmp"])
        sess = server.get_chat_session(("acme",), self.id("c1"), linked={os.path.realpath("/tmp")})
        self.assertEqual(sess.session_id, self.id("c1"))

    def test_scope_slug_reads_both_shapes(self):
        self.assertEqual(server._scope_slug(["acme", "/app"]), "acme")
        self.assertEqual(server._scope_slug(("acme",)), "acme")
        self.assertIsNone(server._scope_slug(None))
        self.assertIsNone(server._scope_slug([]))


if __name__ == "__main__":
    unittest.main()


CHAT = "0123abcd-0000-4000-8000-0123456789ab"


class SkillLoadTests(unittest.TestCase):
    """A skill the bot loads with `osctl get-skill` stays active for the thread."""

    def make_session(self, root, load=None):
        from core import skill_loads

        class LoadingSession(FakeSession):
            session_id = CHAT
            _last_bot = None

            def note_bot(self, bot_id):
                pass

            def ask(self, text, **kw):
                if load:  # what osctl get-skill does inside the run
                    skill_loads.record(root, CHAT, load)
                yield ("delta", "ok")
                yield ("done", {"result": "ok"})
        return LoadingSession()

    def turn(self, sess, root, msg="what should our positioning be?"):
        h = _bare_handler(io.BytesIO())
        h.server = mock.Mock(server_address=("127.0.0.1", 8765))
        with mock.patch.object(server, "get_chat_session", return_value=sess), \
             mock.patch.object(server, "ROOT", root), \
             mock.patch.object(server.chat_store, "CHATS_DIR", root / "chats"), \
             mock.patch.object(server.db, "tree", return_value=[]):
            h._handle_ask({"messages": [{"role": "user", "content": msg}]})

    def test_loaded_skill_becomes_active_and_marker_clears(self):
        from core import skill_loads
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sess = self.make_session(root, load="positioning")
            self.turn(sess, root)
            self.assertEqual(sess._last_skill, "positioning")
            self.assertTrue(sess._skill_explicit)
            self.assertEqual(skill_loads.loaded(root, CHAT), [])
            self.assertFalse((root / "team" / "skill_loads" / f"{CHAT}.json").exists())
        model, reason = server._pick_model_reason(None, False, "draft it", sess)
        self.assertEqual((model, reason), (server.chat_session.ESCALATED_MODEL, "skill-thread"))
        model, _ = server._pick_model_reason(None, False, "what is X?", sess)
        self.assertEqual(model, server.chat_session.CHAT_MODEL)

    def test_no_load_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = self.make_session(Path(tmp))
            self.turn(sess, Path(tmp))
        self.assertIsNone(sess._last_skill)
        self.assertFalse(sess._skill_explicit)
        self.assertEqual(server._pick_model_reason(None, False, "draft it", sess)[0],
                         server.chat_session.CHAT_MODEL)

    def test_adopt_without_session_id_is_a_no_op(self):
        sess = FakeSession()
        self.assertIsNone(server._adopt_loaded_skill(sess))
        self.assertIsNone(sess._last_skill)
