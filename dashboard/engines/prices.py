"""prices.py: list prices and the cost estimate for engines that report tokens but no cost.

Usage is the four token parts every engine's done payload carries:
{"in", "cache_write", "cache_read", "out"}. `in` is input not read from cache. An optional
"cache_write_1h" says how many of the cache writes kept the hour-long cache (Claude Code's).
"""

# USD per million tokens (input, output, cache read). Cache writes cost input * 1.25, or
# input * 2 for the hour-long cache.
PRICES = {"claude-haiku-5-5": (0.10, 0.50, 0.01), "claude-sonnet-5-5": (2.0, 10.0, 0.20),
          "claude-opus-5-5": (4.0, 20.0, 0.20)}
# Tier names the server asks Claude Code for (it maps them to the newest model of that tier).
TIERS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
CACHE_WRITE_FACTOR = 1.25
CACHE_WRITE_1H_FACTOR = 2.0
# A model not on the list (a Codex login's default, an API provider's own model): priced as the
# costliest one, so a cap counted on it never lets more through than it says.
UNKNOWN = max(PRICES.values(), key=lambda p: (p[1], p[0]))


def usage(fresh=0, cache_write=0, cache_read=0, out=0):
    """The usage dict, with every part a non-negative int."""
    return {"in": max(0, int(fresh or 0)), "cache_write": max(0, int(cache_write or 0)),
            "cache_read": max(0, int(cache_read or 0)), "out": max(0, int(out or 0))}


def estimate(model, parts, unknown_ok=False):
    """Estimated USD for one reply, or None when usage is missing or the model is unpriced.
    `unknown_ok`: an unpriced model is counted at the UNKNOWN (upper bound) price instead."""
    price = PRICES.get(TIERS.get(model, model) or "") or (UNKNOWN if unknown_ok else None)
    if not price or not isinstance(parts, dict):
        return None
    p_in, p_out, p_cache = price
    writes = int(parts.get("cache_write") or 0)
    hour = min(writes, max(0, int(parts.get("cache_write_1h") or 0)))
    return ((int(parts.get("in") or 0) * p_in
             + (writes - hour) * p_in * CACHE_WRITE_FACTOR + hour * p_in * CACHE_WRITE_1H_FACTOR
             + int(parts.get("cache_read") or 0) * p_cache
             + int(parts.get("out") or 0) * p_out) / 1_000_000)
