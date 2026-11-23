"""chain.py: bot-to-bot chains inside one chat message.

A bot that receives a HANDOFF may hand off or consult again (dashboard/server.py, the hop loop).
The owner's limits live in Settings, General (core/general.py); 0 means none for both. A workspace
may set its own (team/chain.json in the data folder, {ws: {"hops", "cost"}}): a value there wins,
None inherits Settings.
"""
import json
from pathlib import Path

from core import bots, general, memory

RELPATH = Path("team") / "chain.json"


def _settings():
    try:
        v = general.view()
        return int(v["chain_hops"]), float(v["chain_cost"])
    except Exception:  # noqa: BLE001 - a hand-edited file never breaks a turn
        return 0, 0.0


def _hops(v):
    if v is None or v == "":
        return None
    if not general._whole(v, general.CHAIN_HOPS_MAX):
        raise ValueError(f"hops: a whole number from 0 (no limit) to {general.CHAIN_HOPS_MAX}, or empty")
    return v


def _cost(v):
    if v is None or v == "":
        return None
    if not general._dollars(v, general.CHAIN_COST_MAX):
        raise ValueError(f"cost: dollars from 0 (no cap) to {general.CHAIN_COST_MAX:.0f}, or empty")
    return float(v)


def overrides(root=None):
    """{ws: {"hops": int|None, "cost": float|None}}, only workspaces with a value. A broken file, or a
    bad value in it, reads as none."""
    try:
        data = json.loads((Path(root or bots.ROOT) / RELPATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for ws, row in (data.items() if isinstance(data, dict) else ()):
        if not isinstance(row, dict):
            continue
        try:
            hops = _hops(row.get("hops"))
        except ValueError:
            hops = None
        try:
            cost = _cost(row.get("cost"))
        except ValueError:
            cost = None
        if hops is not None or cost is not None:
            out[ws] = {"hops": hops, "cost": cost}
    return out


def set_override(root, ws, hops, cost):
    """Save this workspace's hops and cost; None or "" inherits Settings. ValueError on a bad value
    (nothing is saved then)."""
    row = {"hops": _hops(hops), "cost": _cost(cost)}
    path = Path(root or bots.ROOT) / RELPATH
    with memory.locked(path):
        data = overrides(root)
        if row["hops"] is None and row["cost"] is None:
            data.pop(ws, None)
        else:
            data[ws] = row
        bots._atomic_write(path, json.dumps(data, indent=2) + "\n")
    return row


def limits(ws=None, root=None):
    """(max_hops, max_cost) for one message in `ws`; (0, 0.0) is no limit. The workspace's own value
    wins over Settings."""
    hops, cost = _settings()
    own = overrides(root).get(ws) if ws else None
    if own:
        hops = own["hops"] if own["hops"] is not None else hops
        cost = own["cost"] if own["cost"] is not None else cost
    return hops, cost


def within_hops(done, max_hops):
    """True when another hop may run after `done` hops."""
    return max_hops <= 0 or done < max_hops


def pingpong(path):
    """True when the last four speakers are the same two bots taking turns."""
    tail = list(path)[-4:]
    if len(tail) < 4:
        return False
    a, b = tail[0], tail[1]
    return a != b and tail == [a, b, a, b]


def missed(runs, saved):
    """{engine_id: how many saved replies of this message came after its last run}.
    `runs`: (engine_id, message_index) for each run on the thread's own session; `saved`: one
    bool per message of the turn, False for a reply the thread record drops (empty)."""
    last = {}
    for eid, i in runs:
        last[eid] = max(i, last.get(eid, -1))
    return {eid: sum(1 for k in saved[i + 1:] if k) for eid, i in last.items()}
