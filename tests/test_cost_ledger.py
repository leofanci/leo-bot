"""Harness upgrade phase 1: token parts, per-reply ledger fields, caps wiring, cost report."""
import unittest

from dashboard.engines import api, claude_code, codex, prices


class EngineUsage(unittest.TestCase):
    def test_claude_code_parts(self):
        _, p = claude_code.parse_event({"type": "result", "total_cost_usd": 0.01, "usage": {
            "input_tokens": 10, "cache_creation_input_tokens": 200,
            "cache_read_input_tokens": 1000, "output_tokens": 90}})
        self.assertEqual(p["usage"], {"in": 10, "cache_write": 200, "cache_read": 1000, "out": 90})
        self.assertEqual(p["tokens"], 1300)

    def test_codex_cached_split(self):
        (_, p), = codex.parse_event({"type": "turn.completed", "usage": {
            "input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 5}})
        self.assertEqual(p["usage"], {"in": 40, "cache_write": 0, "cache_read": 60, "out": 5})
        self.assertEqual(p["tokens"], 105)

    def test_openai_compatible_cached_split(self):
        eng = api.ApiEngine.__new__(api.ApiEngine)
        _, p = eng._done(["x"], {"prompt_tokens": 50, "completion_tokens": 7, "cached_tokens": 20}, "some-model")
        self.assertEqual(p["usage"], {"in": 30, "cache_write": 0, "cache_read": 20, "out": 7})
        self.assertEqual(p["tokens"], 57)

    def test_anthropic_api_parts_and_estimate(self):
        eng = api.ApiEngine.__new__(api.ApiEngine)
        _, p = eng._done(["x"], {"input_tokens": 1_000_000, "output_tokens": 0}, "claude-haiku-5-5")
        self.assertEqual(p["usage"], {"in": 1_000_000, "cache_write": 0, "cache_read": 0, "out": 0})
        self.assertAlmostEqual(p["cost"], 0.10)

    def test_estimate(self):
        u = {"in": 1_000_000, "cache_write": 0, "cache_read": 0, "out": 0}
        self.assertAlmostEqual(prices.estimate("claude-haiku-5-5", u), 0.10)
        self.assertIsNone(prices.estimate("unknown-model", u))
        self.assertIsNone(prices.estimate("claude-haiku-5-5", None))
        self.assertIs(api.PRICES, prices.PRICES)


if __name__ == "__main__":
    unittest.main()
