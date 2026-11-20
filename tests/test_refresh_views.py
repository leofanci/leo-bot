"""refreshViews (app.js): after a bot's turn, redraw only the page that is on screen."""
import json
import subprocess
import unittest
from pathlib import Path

JS = Path(__file__).resolve().parent.parent / "dashboard" / "app.js"

# Every view refreshViews may draw, stubbed to record its name.
DRAWS = ("renderContent", "renderTimeline", "renderDay", "renderOperations", "renderTeam",
         "renderApprovals", "renderSettings", "renderFolders", "renderFiles", "renderProjectSection",
         "renderProfile", "renderProfileSetup", "renderChannelGuidelines", "renderCanvasList",
         "renderNeeds", "requestCanvasReload")


def drawn(view):
    src = JS.read_text(encoding="utf-8")
    start = src.index("async function refreshViews(")
    fn = src[start:src.index("\n}\n", start) + 2]
    stubs = "".join(f"function {d}(){{ calls.push({json.dumps(d)}); }}\n" for d in DRAWS)
    script = ("const calls = [];\n" + stubs
              + "async function renderRail(){}\nconst onProfileList = () => true;\n"
              + f"let STATE = {{ view: {json.dumps(view)} }};\n" + fn
              + "\nrefreshViews().then(() => process.stdout.write(JSON.stringify(calls)));")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True, timeout=10)
    return json.loads(out.stdout)


class RefreshViews(unittest.TestCase):
    def test_known_views_redraw_themselves(self):
        self.assertEqual(drawn("team"), ["renderTeam"])
        self.assertEqual(drawn("needs"), ["renderNeeds"])
        self.assertEqual(drawn("canvases"), ["renderCanvasList"])

    def test_open_canvas_reloads_in_place(self):
        # It used to fall through to the Today page under the canvas header.
        self.assertEqual(drawn("canvas"), ["requestCanvasReload"])

    def test_forms_and_picture_page_are_left_alone(self):
        # parseRoute sets "route" before a page that keeps no view of its own (forms, #/avatar).
        self.assertEqual(drawn("route"), [])


if __name__ == "__main__":
    unittest.main()
