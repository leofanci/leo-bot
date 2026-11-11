"""costs.py: the spend report behind /api/costs and the Team view.

Reads assistant replies from saved threads. Replies saved before the cost ledger
(no engine, model, skill, usage or metered field) still count; missing keys
group under "-". Each group row carries a display `label`.
"""
import time

from core import bots, rooms

NONE = "-"
NOT_RECORDED = "not recorded"


def _num(value, kind=float):
    try:
        return kind(value or 0)
    except (TypeError, ValueError):
        return kind(0)


def _key(value):
    return value if isinstance(value, str) and value else NONE


def _add(groups, key, cost, tokens, label=None):
    row = groups.setdefault(key, {"cost": 0.0, "tokens": 0, "turns": 0, "label": key})
    if isinstance(label, str) and label and row["label"] == key:
        row["label"] = label
    row["cost"] += cost
    row["tokens"] += tokens
    row["turns"] += 1


def summarize(ws, records, spaces, days=30, now=None):
    """Estimated spend and tokens in a workspace over `days` (HQ covers every workspace):
    per bot, engine, model, skill and local day, plus the share of input read from
    cache and how many replies ran on an engine that reports no cost."""
    now = time.time() if now is None else now
    since = now - days * 86_400
    per_bot, total = {}, {"cost": 0.0, "tokens": 0, "turns": 0}
    by = {"by_engine": {}, "by_model": {}, "by_skill": {}, "by_day": {}}
    cached = fed = 0
    unmetered = 0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if ws != bots.HQ and rooms.split_id(rooms.room_of_chat(rec, set(), spaces))[0] != ws:
            continue
        for m in rec.get("messages") or []:
            if not isinstance(m, dict) or m.get("role") != "assistant":
                continue
            ts = _num(m.get("ts"))
            if ts < since:
                continue
            cost, tokens = _num(m.get("cost")), _num(m.get("tokens"), int)
            bot = m.get("bot")
            _add(per_bot, bot if isinstance(bot, str) and bot else bots.ROOT_BOT, cost, tokens)
            total["cost"] += cost
            total["tokens"] += tokens
            total["turns"] += 1
            engine = _key(m.get("engine"))
            _add(by["by_engine"], engine, cost, tokens, NOT_RECORDED if engine == NONE else m.get("engine_label"))
            model = _key(m.get("model"))
            _add(by["by_model"], model, cost, tokens, NOT_RECORDED if model == NONE else None)
            skill = _key(m.get("skill"))
            _add(by["by_skill"], skill, cost, tokens, "no skill" if skill == NONE else None)
            _add(by["by_day"], time.strftime("%Y-%m-%d", time.localtime(ts)), cost, tokens)
            usage = m.get("usage")
            if isinstance(usage, dict):
                cached += _num(usage.get("cache_read"), int)
                fed += sum(_num(usage.get(k), int) for k in ("in", "cache_write", "cache_read"))
            if m.get("metered") is False:
                unmetered += 1
    return {"days": days, "bots": per_bot, "total": total, **by,
            "cache_read_share": (cached / fed) if fed else None, "unmetered": unmetered}
