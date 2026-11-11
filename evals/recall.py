"""Offline recall eval: known-answer queries against one memory file, through core.memory.recall
(the FTS5 index, or the substring scan when SQLite lacks FTS5). Answers the Phase 2 vector gate:
local embeddings are worth adding only if keyword recall misses more than `max_miss` of the
queries. Deterministic, no model call.
"""
import json
import tempfile
from pathlib import Path

CASES = Path(__file__).parent / "cases" / "recall.json"
WS, BOT = "acme", "cmo"


def load(path=CASES):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def run(data=None, fts=None):
    """{"rows": [(query, kind, hit)], "miss": rate, "by_kind": {kind: (hits, total)}}.
    `fts` False forces the substring scan (the no-FTS5 fallback)."""
    from core import memory
    data = data or load()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = root / "projects" / WS / "team" / f"{BOT}.md"
        path.parent.mkdir(parents=True)
        path.write_text("".join(f"{ln}\n" for ln in data["entries"]), encoding="utf-8")
        prior = memory.HAS_FTS5
        if fts is not None:
            memory.HAS_FTS5 = bool(fts) and prior
        try:
            rows = []
            for c in data["queries"]:
                got = memory.recall(BOT, c["q"], WS, root, limit=data["k"])
                hit = any(w.lower() in ln.lower() for ln in got for w in c["want"])
                rows.append((c["q"], c["kind"], hit))
        finally:
            memory.HAS_FTS5 = prior
    by_kind = {}
    for _, kind, hit in rows:
        h, t = by_kind.get(kind, (0, 0))
        by_kind[kind] = (h + hit, t + 1)
    miss = sum(not hit for *_, hit in rows) / len(rows) if rows else 0.0
    return {"rows": rows, "miss": miss, "by_kind": by_kind}


def scorecard(result, max_miss):
    lines = [f"recall@k  miss {result['miss']:.0%} (vector gate opens above {max_miss:.0%})"]
    for kind, (h, t) in sorted(result["by_kind"].items()):
        lines.append(f"  {kind:<10} {h}/{t}")
    lines += [f"  miss: {q}" for q, _, hit in result["rows"] if not hit]
    return "\n".join(lines)
