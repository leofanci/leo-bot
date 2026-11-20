"""self_chat.py: the Slack-style chat with yourself: notes, no bot replies.

One thread per person per workspace, with an id made from both so it is the same
every time. It is a normal saved chat (room `<ws>/dm-self`, shared) so listing,
reactions and the "only its starter sees a DM thread" rule need no special cases.
"""
import time
import uuid

import chat_store

_NS = uuid.UUID("6b1f4c0e-3a52-4d0b-9d4e-5a1c7e9b2f10")
TITLE = "Notes to self"
MAX_CHARS = 8000
MAX_NOTES = 2000  # per thread: every append rewrites the whole file
MAX_TOTAL = 1_000_000  # characters across the thread, same reason


def room_id(ws):
    return f"{ws}/dm-self"


def chat_id(ws, who):
    """`who`: "ceo" for the owner, else the person's id."""
    return str(uuid.uuid5(_NS, f"{ws}|{who}"))


def add(ws, who, text, name=None):
    """Append one note; returns the saved chat record."""
    text = (text or "").strip()
    if not text:
        raise ValueError("write something first")
    if len(text) > MAX_CHARS:
        raise ValueError(f"note too long (max {MAX_CHARS} characters)")
    cid = chat_id(ws, who)
    msg = {"role": "user", "content": text, "ts": time.time(),
           **({"by": who, "by_name": name} if who != "ceo" else {})}
    with chat_store.locked():
        rec = chat_store.load(cid) or {
            "id": cid, "title": TITLE, "created": time.time(), "room": room_id(ws),
            **({"by": who} if who != "ceo" else {}),
            "shared": True, "apps_used": False, "turn_count": 0, "messages": []}
        used = sum(len(str(m.get("content") or "")) for m in rec["messages"] if isinstance(m, dict))
        if len(rec["messages"]) >= MAX_NOTES or used + len(text) > MAX_TOTAL:
            raise ValueError(f"your notes here are full (max {MAX_NOTES} notes, {MAX_TOTAL} characters)")
        rec["messages"].append(msg)
        rec["updated"] = msg["ts"]
        chat_store.save(rec)
    return rec
