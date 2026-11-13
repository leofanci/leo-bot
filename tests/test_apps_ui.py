"""Connected apps UI: pure helpers (run in node) and the guards around app action cards."""
import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "dashboard" / "app.js").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(rf"^(?:async )?function {name}\(.*?\n}}\n", APP_JS, re.S | re.M)
    assert m, f"{name} missing from app.js"
    return m.group(0)


def _run(names, expr, prelude=""):
    script = prelude + "\n".join(_fn(n) for n in names) + f"\nprocess.stdout.write(JSON.stringify({expr}));"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


class Helpers(unittest.TestCase):
    def test_held_rows_and_what_edit_changes(self):
        self.assertEqual(_run(["isHeld"], '[isHeld({kind:"app_action"}), isHeld({kind:"job"}), isHeld({summary:"x"}), isHeld(null)]'),
                         [True, True, False, False])
        self.assertEqual(_run(["heldEditable"],
                              '[heldEditable({kind:"job", job:{fields:{cap:1}}}), heldEditable({kind:"app_action", action:{arguments:{to:"a"}}}), heldEditable({kind:"job"})]'),
                         [{"key": "fields", "values": {"cap": 1}}, {"key": "arguments", "values": {"to": "a"}},
                          {"key": "fields", "values": {}}])

    def test_job_meta_line(self):
        out = _run(["jobMetaText"],
                   '[jobMetaText({bot:"cmo", skill:"positioning", schedule:"daily 09:00", enabled:false}, "Acme", 100),'
                   ' jobMetaText({bot:"cmo", skill:"positioning", schedule:"every 6h", enabled:true, next_due:50,'
                   ' last_run:40, last_status:"waiting", cap:1.5}, "Acme", 100)]')
        self.assertEqual(out[0], "Acme · @cmo /positioning · daily 09:00 · no cap · off · not run yet")
        self.assertTrue(out[1].startswith("Acme · @cmo /positioning · every 6h · $1.5 per 24h · runs at the next check · last "), out[1])
        self.assertTrue(out[1].endswith(": waiting"), out[1])

    def test_grant_label(self):
        self.assertEqual(_run(["grantLabel"], '[grantLabel(undefined), grantLabel("read"), grantLabel("write"), grantLabel("act")]'),
                         ["Off", "Read", "Read + write, asks first", "Acts on its own"])

    def test_batch_card_offers_approve_all(self):
        self.assertEqual(_run(["batchSize", "appCardHtml"],
                              '[batchSize({batch:"b1"}, [{batch:"b1",status:"pending"},{batch:"b1",status:"pending"},{batch:"b2"}]),'
                              ' batchSize({}, [{}]), /Approve all 2/.test(appCardHtml({batch:"b1", preview:{title:"Send", fields:[]}}, 2)),'
                              ' /Approve all/.test(appCardHtml({preview:{title:"Send", fields:[]}}, 1))]',
                              prelude=APP_JS.splitlines()[2] + "\n"),  # const esc
                         [2, 1, True, False])

    def test_app_status_label(self):
        self.assertEqual(_run(["appStatusLabel"],
                              '[appStatusLabel({auth:"managed"}, true), appStatusLabel({auth:"managed"}, false),'
                              ' appStatusLabel({auth:"own_app"}, false), appStatusLabel({auth:"api_key"}, false)]'),
                         ["Connected", "Connect", "Needs setup", "Connect"])
        self.assertEqual(_run(["appStatusLabel"], '[appStatusLabel({}, 1), appStatusLabel({}, 3), appStatusLabel({}, 0)]'),
                         ["Connected", "3 accounts", "Connect"])

    def test_app_logo_is_local_with_letter_fallback(self):
        esc = "function esc(s){return String(s).replace(/[&<>\"']/g, c => '&#' + c.charCodeAt(0) + ';')}\n"
        script = esc + _fn("appLogoHtml") + (
            '\nprocess.stdout.write(JSON.stringify([appLogoHtml("gmail", {name:"Gmail", logo:"https://x.example/g.png"}),'
            ' appLogoHtml("slack", {name:"Slack"}), appLogoHtml("a b", {name:"<i>", logo:"y"})]));')
        with_logo, letter_only, odd = json.loads(subprocess.run(["node", "-e", script], capture_output=True,
                                                                text=True, check=True).stdout)
        self.assertIn('src="/api/integrations/logo/gmail"', with_logo)
        self.assertNotIn("x.example", with_logo)  # never the outside URL
        self.assertIn(">G<img", with_logo)
        self.assertNotIn("<img", letter_only)
        self.assertIn("/api/integrations/logo/a%20b", odd)
        self.assertNotIn("<i>", odd)

    def test_acct_picked(self):
        self.assertEqual(_run(["acctPicked"], '[acctPicked({acme:{gmail:["ca_1"]}}, "acme", "gmail", "ca_1"),'
                                             ' acctPicked({acme:{gmail:["ca_1"]}}, "demo", "gmail", "ca_1"),'
                                             ' acctPicked(undefined, "acme", "gmail", "ca_1")]'),
                         [True, False, False])


class Guards(unittest.TestCase):
    def test_send_cleanup_skips_app_actions(self):
        # Any reply answers a chat approval bar; an app action card stays until clicked.
        self.assertIn('stream.querySelectorAll(".approval-bar:not(.app-card)").forEach(el => el.remove());', APP_JS)
        self.assertNotIn('stream.querySelectorAll(".approval-bar").forEach(el => el.remove());', APP_JS)

    def test_dismiss_skips_app_actions(self):
        body = _fn("renderApprovals")
        self.assertIn("isHeld", body)
        self.assertIn("/api/approvals/dismiss", body)
        card = _fn("wireAppCard")
        self.assertIn('answer("reject")', card)
        self.assertIn("/api/approvals/${encodeURIComponent(a.id)}/${verb}", card)

    def test_integrations_route_and_rail(self):
        self.assertIn("(engines|integrations)", APP_JS)
        self.assertIn('id="ws-settings"', APP_JS)


if __name__ == "__main__":
    unittest.main()
