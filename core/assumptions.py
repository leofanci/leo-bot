"""Memo assumptions: tracked claims a memo rests on. Stored in the memo JSON as
`assumptions: [{id, text, status, refs}]`. Ids (as1, as2, ...) are stable across versions of a
memo type. A memo is stale while it holds a `contradicted` assumption; stale is worked out on
read and never stored."""
import re

STATUSES = ("holding", "contradicted", "unknown")
TEXT_MAX = 200
MAX_ITEMS = 12
MAX_REFS = 20
_ID = re.compile(r"^as(\d{1,3})$")
# Where a memo type keeps the prose its assumptions are read from (dotted keys). Types not
# listed hold no such prose and propose nothing.
PROSE_FIELDS = {
    "assessment": "riskiest_assumption",
    "market-sizing": "som.what_changes_the_number",
}


def _text(raw: object) -> str:
    return " ".join(str(raw or "").split())[:TEXT_MAX]


def _key(text: str) -> str:
    return text.strip().lower()


def normalize(items: object) -> list[dict]:
    """Clean list: bad items (no text, bad or repeated id) dropped, unknown status becomes `unknown`."""
    out, seen = [], set()
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        text, aid = _text(it.get("text")), str(it.get("id") or "").strip().lower()
        if not text or not _ID.match(aid) or aid in seen:
            continue
        seen.add(aid)
        status = str(it.get("status") or "").strip().lower()
        refs = [str(r).strip().lower() for r in it.get("refs") or [] if str(r).strip()] \
            if isinstance(it.get("refs"), list) else []
        out.append({"id": aid, "text": text, "status": status if status in STATUSES else "unknown",
                    "refs": refs[:MAX_REFS]})
        if len(out) >= MAX_ITEMS:
            break
    return out


def next_id(existing_versions: list[list[dict]]) -> str:
    """as<max N over every version of the memo type, plus one>."""
    top = 0
    for items in existing_versions:
        for it in items or []:
            m = _ID.match(str(it.get("id") or ""))
            if m:
                top = max(top, int(m.group(1)))
    return f"as{top + 1}"


def carry(old: list[dict], new_texts: list[str], used: list[list[dict]] | None = None) -> list[dict]:
    """Assumptions for `new_texts`: a text already in `old` keeps its id, status and refs; a new
    text gets a fresh id (unique over `old` and `used`) and status `unknown`."""
    pool = {_key(it["text"]): it for it in old or []}
    taken = [list(old or []), *(used or [])]
    out, seen = [], set()
    for raw in new_texts or []:
        text = _text(raw)
        if not text or _key(text) in seen:
            continue
        seen.add(_key(text))
        hit = pool.get(_key(text))
        if hit:
            out.append({**hit, "text": text})
        else:
            item = {"id": next_id(taken + [out]), "text": text, "status": "unknown", "refs": []}
            out.append(item)
        if len(out) >= MAX_ITEMS:
            break
    return out


def stale(items: list[dict]) -> list[dict]:
    """The assumptions that make a memo stale."""
    return [it for it in items or [] if it.get("status") == "contradicted"]


def proposed(body: dict, mtype: str) -> list[str]:
    """Candidate assumption texts split out of the memo's prose field (one per line or sentence)."""
    path = PROSE_FIELDS.get(mtype)
    if not path:
        return []
    cur: object = body
    for k in path.split("."):
        cur = cur.get(k) if isinstance(cur, dict) else None
    if not isinstance(cur, str):
        return []
    parts = [p for chunk in cur.replace("\r", "\n").split("\n")
             for p in re.split(r"(?<=[.;!?])\s+", chunk)]
    out = []
    for p in parts:
        t = _text(p.strip(" -*•"))
        if t and _key(t) not in {_key(o) for o in out}:
            out.append(t)
    return out[:MAX_ITEMS]
