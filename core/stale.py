"""Stale memos: a memo version is stale while it holds a `contradicted` assumption. Worked out
on every read from the memo bodies and the evidence log, never stored."""
from core import assumptions, evidence
from core.ids import lk_evidence, lk_memo


def why(slug: str, mtype: str, version: int, body: object, reg, intake_text: str) -> list[dict]:
    """[{assumption: composed id, text, evidence: composed ev id or None}] for one memo version;
    empty when the memo is not stale. `evidence` is the newest log entry that points at that
    assumption (in this version or one it was carried from)."""
    items = assumptions.stale(assumptions.normalize(body.get("assumptions") if isinstance(body, dict) else None))
    if not items:
        return []
    memo = reg.get(lk_memo(slug, mtype, int(version))) or f"{mtype}-v{version}"
    entries = evidence.entries(intake_text)
    out = []
    for it in items:
        hit = None
        for e in entries:  # later entries win
            ent = reg.resolve(e["ref"]) if e["ref"] else None
            ref = (ent or {}).get("ref") or {}
            if ent and ent["kind"] == "assumption" and ref.get("memo_type") == mtype \
                    and ref.get("assumption") == it["id"] and ref.get("project") == slug:
                hit = e["n"]
        out.append({"assumption": f"{memo}.as{it['id'][2:]}", "text": it["text"],
                    "evidence": reg.get(lk_evidence(slug, hit)) if hit else None})
    return out
