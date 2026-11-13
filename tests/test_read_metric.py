"""composio.read_metric: a scheduled read holds to the same bar as a bot's own read."""
import unittest
from unittest import mock

from core import composio

CAT = ["instagram", "gmail"]
ARGS = ("instagram", "INSTAGRAM_GET_X", {}, "acme", "product")


def read(*a, grant=None, accounts=None, run=None, **kw):
    grant = {"instagram": "read"} if grant is None else grant
    accounts = {"instagram": ["ca_1"]} if accounts is None else accounts
    with mock.patch.object(composio.integrations, "grant", return_value=grant), \
            mock.patch.object(composio.integrations, "accounts_for", return_value=accounts):
        return composio.read_metric(*a, catalog=CAT, read_only=kw.pop("read_only", lambda s: True),
                                    run=run or (lambda *x: {"data": {"n": 1}}), **kw)


class ReadMetric(unittest.TestCase):
    def test_reads_with_a_read_grant_and_one_account(self):
        seen = []
        out = read(*ARGS, run=lambda *x: seen.append(x) or {"data": {"n": 1}})
        self.assertEqual(out, {"data": {"n": 1}})
        self.assertEqual(seen[0][3], "ca_1")

    def test_rechecks_the_metric_rules(self):
        for args in (("instagram", "INSTAGRAM_CREATE_POST", {}, "acme", "product"),
                     ("instagram", "GMAIL_FETCH_EMAILS", {}, "acme", "product"),
                     ("instagram", "INSTAGRAM_GET_X", {"u": "https://x.example"}, "acme", "product")):
            with self.assertRaises(composio.ComposioError):
                read(*args)
        with self.assertRaises(composio.ComposioError):
            read(*ARGS, read_only=lambda s: False)

    def test_needs_a_grant_and_an_account(self):
        with self.assertRaises(composio.ComposioError):
            read(*ARGS, grant={})
        with self.assertRaises(composio.ComposioError):
            read(*ARGS, accounts={})

    def test_failed_result_raises(self):
        with self.assertRaises(composio.ComposioError) as cm:
            read(*ARGS, run=lambda *x: {"successful": False, "error": "rate limited"})
        self.assertIn("rate limited", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
