"""chain.py: bot-to-bot chains inside one chat message.

A bot that receives a HANDOFF may hand off or consult again (dashboard/server.py, the hop loop).
The owner's limits live in Settings, General (core/general.py); 0 means none for both.
"""
from core import general


def limits():
    """(max_hops, max_cost) for one message; (0, 0.0) is no limit. A broken settings file reads as none."""
    try:
        v = general.view()
        return int(v["chain_hops"]), float(v["chain_cost"])
    except Exception:  # noqa: BLE001 - a hand-edited file never breaks a turn
        return 0, 0.0


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
