import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import folders
from dashboard import tokensave_bridge as bridge


class TokenSaveBridge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ext = tempfile.TemporaryDirectory()  # linked folders live next to Leo Bot
        self.addCleanup(self.ext.cleanup)
        self.app = Path(self.ext.name).resolve() / "apps" / "acme"
        self.app.mkdir(parents=True)
        project = self.root / "projects" / "acme"
        project.mkdir(parents=True)
        (project / "project.md").write_text(
            f"---\nname: Acme\nlocal_folder: {self.app}\n---\n"
        )
        self.root_patch = mock.patch.object(bridge.fileops, "ROOT", self.root)
        self.root_patch.start()
        self.env_patch = mock.patch.dict(
            bridge.os.environ, {
                "WORKSPACE_ACTIVE_PROJECT": "acme",
                "WORKSPACE_TOKENSAVE_TASK": "find auth",
            }, clear=False,
        )
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        self.root_patch.stop()
        self.tmp.cleanup()

    def _run(self, stdout="graph result"):
        return mock.patch.object(
            bridge.subprocess, "run",
            return_value=subprocess.CompletedProcess([], 0, stdout, "ignored warning"),
        )

    def test_context_is_bounded_and_runs_against_linked_graph(self):
        (self.app / ".tokensave").mkdir()
        (self.app / ".tokensave" / "tokensave.db").touch()
        with mock.patch.object(bridge.shutil, "which", return_value="/bin/tokensave"), \
             self._run() as run:
            self.assertEqual(bridge.context("acme", "find auth"), "graph result")

        self.assertEqual(run.call_count, 2)
        sync_command = run.call_args_list[0].args[0]
        self.assertEqual(sync_command,
                         ["/bin/tokensave", "sync", str(self.app.resolve())])
        command = run.call_args_list[1].args[0]
        self.assertEqual(command[:3], ["/bin/tokensave", "tool", "context"])
        self.assertEqual(command[command.index("--project") + 1], str(self.app.resolve()))
        args = json.loads(command[command.index("--args") + 1])
        self.assertEqual(args["max_nodes"], 10)
        self.assertEqual(args["max_code_lines"], 80)
        self.assertIn(".env", args["path_exclude"])
        self.assertIn("secret", args["path_exclude"])
        self.assertNotIn("path_include", args)

    def test_failed_incremental_sync_never_returns_stale_graph(self):
        (self.app / ".tokensave").mkdir()
        (self.app / ".tokensave" / "tokensave.db").touch()
        failed = subprocess.CompletedProcess([], 2, "", "index locked")
        with mock.patch.object(bridge.shutil, "which", return_value="/bin/tokensave"), \
             mock.patch.object(bridge.subprocess, "run", return_value=failed) as run:
            with self.assertRaisesRegex(bridge.BridgeError, "could not refresh"):
                bridge.context("acme", "find auth")
        self.assertEqual(run.call_count, 1)

    def test_sync_timeout_is_a_clean_failure(self):
        (self.app / ".tokensave").mkdir()
        (self.app / ".tokensave" / "tokensave.db").touch()
        timeout = subprocess.TimeoutExpired(["tokensave", "sync"], 45)
        with mock.patch.object(bridge.shutil, "which", return_value="/bin/tokensave"), \
             mock.patch.object(bridge.subprocess, "run", side_effect=timeout) as run:
            with self.assertRaisesRegex(bridge.BridgeError, "refresh timed out"):
                bridge.context("acme", "find auth")
        self.assertEqual(run.call_count, 1)

    def test_parent_graph_is_rejected_to_avoid_sibling_scope_leakage(self):
        (self.app.parent / ".tokensave").mkdir()
        (self.app.parent / ".tokensave" / "tokensave.db").touch()
        with mock.patch.object(bridge.subprocess, "run") as run:
            with self.assertRaisesRegex(bridge.BridgeError, "tokensave init"):
                bridge.context("acme", "handler")
        run.assert_not_called()

    def test_folder_must_be_linked_and_defaults_to_the_first_indexed(self):
        other = self.app.parent / "docs"
        other.mkdir()
        (other / ".tokensave").mkdir()
        (other / ".tokensave" / "tokensave.db").touch()
        with mock.patch.object(bridge.subprocess, "run") as run:
            with self.assertRaisesRegex(bridge.BridgeError, "not linked"):
                bridge.context("acme", "find auth", folder=str(other))
        run.assert_not_called()
        folders.add("acme", str(other), root=self.root)
        with mock.patch.object(bridge.shutil, "which", return_value="/bin/tokensave"), self._run() as run:
            bridge.context("acme", "find auth")
        self.assertEqual(run.call_args_list[0].args[0], ["/bin/tokensave", "sync", str(other)])
        with mock.patch.object(bridge.subprocess, "run") as run:
            with self.assertRaisesRegex(bridge.BridgeError, "tokensave init"):
                bridge.context("acme", "find auth", folder=str(self.app))

    def test_bridge_rejects_a_different_project_slug(self):
        with self.assertRaisesRegex(bridge.BridgeError, "not the active"):
            bridge.context("another-project", "find auth")

    def test_trusted_server_scope_does_not_depend_on_process_environment(self):
        (self.app / ".tokensave").mkdir()
        (self.app / ".tokensave" / "tokensave.db").touch()
        with mock.patch.dict(bridge.os.environ, {}, clear=True), \
             mock.patch.object(bridge.shutil, "which", return_value="/bin/tokensave"), \
             self._run():
            self.assertEqual(
                bridge.context("acme", "find auth", active_project="acme"),
                "graph result",
            )

    def test_no_index_is_a_clear_error_and_runs_nothing(self):
        with mock.patch.object(bridge.subprocess, "run") as run:
            with self.assertRaisesRegex(bridge.BridgeError, "tokensave init"):
                bridge.context("acme", "find auth")
        run.assert_not_called()

    def test_output_is_capped_and_sibling_project_footer_removed(self):
        (self.app / ".tokensave").mkdir()
        (self.app / ".tokensave" / "tokensave.db").touch()
        raw = "x" * (bridge.MAX_OUTPUT_CHARS + 100)
        with mock.patch.object(bridge.shutil, "which", return_value="/bin/tokensave"), \
             self._run(raw):
            output = bridge.context("acme", "find auth")
        self.assertEqual(output.count("x"), bridge.MAX_OUTPUT_CHARS)
        self.assertTrue(output.endswith("[TokenSave output capped]"))

        cleaned = bridge._clean(
            "answer\n### Other initialized projects\n- /private/sibling"
        )
        self.assertEqual(cleaned, "answer")


if __name__ == "__main__":
    unittest.main()
