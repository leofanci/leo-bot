"""rooms.py — Slack-style channels and DMs inside a workspace ("rooms" in code:
"channel" already means a social channel of a profile).

A workspace is HQ (the holding's CEO office) or one project (one company).
Every room id is `<workspace>/<local>`:
  <ws>/general     the workspace's main channel, led by Chief of Staff
  <ws>/t-<name>    topic channel: user-created, members + lead
  <ws>/dm-<bot>    1:1 with one of the workspace's active bots

A project workspace's rooms are bound to that project; nothing in one
workspace is visible from another. Topic channels are stored per workspace
(team/rooms.json for HQ, projects/<slug>/team/rooms.json otherwise).

A topic channel may be private: then only the owner and the people listed in
its `people` (core.people ids) see it. #general is open to everyone in the
workspace. DM threads belong to the person who started them (chat `by`).
A thread an invited person can see is "shared": its bot turns get no file,
shell or app tools, only the osctl relay (dashboard/os_relay.py).

A room costs nothing by itself: untagged messages go to ONE bot (the one that spoke
last, else the lead; or a member whose skill the wording clearly matches), other
members answer only when tagged, and the persona's team list shrinks to them.
"""
import json
import re

from core import bots

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_RESERVED = {"general"}


def split_id(room_id):
    """'acme/t-launch' → ('acme', 't-launch'); malformed → (None, None)."""
    ws, sep, local = str(room_id or "").partition("/")
    return (ws, local) if sep and ws and local else (None, None)


def _load_topics(ws, root):
    try:
        data = json.loads((bots.ws_dir(ws, root) / "rooms.json").read_text())
    except (OSError, ValueError):
        return []
    rows = data.get("topics") if isinstance(data, dict) else None
    return [r for r in rows or [] if isinstance(r, dict) and _NAME_RE.match(str(r.get("name", "")))]


def _save_topics(ws, root, rows):
    bots._atomic_write(bots.ws_dir(ws, root) / "rooms.json", json.dumps({"topics": rows}, indent=2) + "\n")


def list_rooms(team, ws, root=bots.ROOT):
    """A workspace's rooms in sidebar order: general, topics, DMs."""
    active = [b.id for b in team.ordered() if b.active]
    out = [{"id": f"{ws}/general", "kind": "general", "name": "general",
            "members": active, "lead": bots.ROOT_BOT}]
    for t in _load_topics(ws, root):
        members = [m for m in t.get("members") or [] if team.get(m) and team.get(m).active]
        lead = team.resolve(t.get("lead") or bots.ROOT_BOT)
        if lead not in members:
            members.insert(0, lead)
        out.append({"id": f"{ws}/t-{t['name']}", "kind": "topic", "name": t["name"],
                    "members": members, "lead": lead, "private": bool(t.get("private")),
                    "people": [p for p in t.get("people") or [] if isinstance(p, str)]})
    for b in team.ordered():
        if b.active:
            out.append({"id": f"{ws}/dm-{b.id}", "kind": "dm", "name": b.name,
                        "members": [b.id], "lead": b.id})
    return out


def get_room(room_id, team, root=bots.ROOT):
    ws, _ = split_id(room_id)
    if ws != team.ws:
        return None
    return next((r for r in list_rooms(team, ws, root) if r["id"] == room_id), None)


def can_see(room, viewer):
    """`viewer`: {"kind": "owner"} or {"kind": "person", "id": pid}. Private topic
    channels show only to the owner and their listed people."""
    if not room:
        return False
    if (viewer or {}).get("kind") == "owner":
        return True
    if (viewer or {}).get("kind") != "person":
        return False
    return not room.get("private") or viewer.get("id") in (room.get("people") or [])


def chat_visible(rec, room, viewer):
    """A saved thread `rec` listed under `room`: the room must be visible, and a
    DM thread belongs to whoever started it (no `by` = the owner's).

    A person sees a thread only in the room it was started in: a thread whose
    channel was deleted or whose bot was switched off falls back to #general
    (room_of_chat) for the owner, never for people. Nor do they see a thread
    that read a connected app (the owner's mail, calendar...: `apps_used`), or
    one with any turn that ran with full tools (`shared` false: its session
    holds what those turns read)."""
    if not can_see(room, viewer):
        return False
    rec = rec or {}
    if viewer.get("kind") == "person" and (rec.get("room") != room.get("id") or rec.get("apps_used")
                                           or not rec.get("shared")):
        return False
    if room.get("kind") != "dm" and not (split_id(rec.get("room"))[1] or "").startswith("dm-"):
        return True
    starter = rec.get("by") or bots.CEO
    me = bots.CEO if viewer.get("kind") == "owner" else viewer.get("id")
    return starter == me


def save_topic(ws, name, members, lead, root=bots.ROOT, *, replace=None, private=False, people=()):
    """Create a topic channel in workspace `ws`, or update `replace` (its current name).
    `private` + `people` (person ids) limit who sees it; the owner always does."""
    name = (name or "").strip().lower()
    if not _NAME_RE.match(name):
        raise ValueError("channel name: lowercase letters, digits, dashes (max 40)")
    if name in _RESERVED:
        raise ValueError(f"#{name} is already a channel")
    defs = bots.load_defs()
    members = [m for m in dict.fromkeys(members or []) if m in defs]
    if lead not in defs:
        raise ValueError("pick a lead bot")
    if lead not in members:
        members.insert(0, lead)
    rows = _load_topics(ws, root)
    if any(r["name"] == name for r in rows) and name != replace:
        # Same words whether or not the caller can see that channel: a private one's
        # name is not confirmed to someone outside it beyond "taken".
        raise ValueError(f"#{name} is taken: pick another name")
    row = {"name": name, "members": members, "lead": lead}
    if private:
        row["private"] = True
        row["people"] = [p for p in dict.fromkeys(people or []) if isinstance(p, str) and p]
    if replace:
        if not any(r["name"] == replace for r in rows):
            raise ValueError(f"unknown channel: #{replace}")
        rows = [row if r["name"] == replace else r for r in rows]
    else:
        rows.append(row)
    _save_topics(ws, root, rows)
    return f"{ws}/t-{name}"


def forget_person(ws, pid, root=bots.ROOT):
    """Take a removed person off every private channel of `ws` in one write. No re-check of
    the channel's bots: a removal must never stop half way over a bot switched off since."""
    rows = _load_topics(ws, root)
    changed = False
    for r in rows:
        if pid in (r.get("people") or []):
            r["people"] = [x for x in r["people"] if x != pid]
            changed = True
    if changed:
        _save_topics(ws, root, rows)
    return changed


def delete_topic(ws, name, root=bots.ROOT):
    rows = _load_topics(ws, root)
    keep = [r for r in rows if r["name"] != name]
    if len(keep) == len(rows):
        raise ValueError(f"unknown channel: #{name}")
    _save_topics(ws, root, keep)


def room_of_chat(rec, room_ids, workspaces):
    """Room id a saved chat lists under (`room_ids`: rooms that exist).
    Threads whose topic channel was deleted, or from before workspaces
    existed, land in their project's #general, else HQ's #general."""
    rid = rec.get("room") or ""
    if rid in room_ids:
        return rid
    ws, _ = split_id(rid)
    if ws in workspaces:
        return f"{ws}/general"
    if rid.startswith("p-") and rid[2:] in workspaces:  # pre-workspace project room
        return f"{rid[2:]}/general"
    scope = rec.get("scope")
    if scope and scope[0] in workspaces:
        return f"{scope[0]}/general"
    return f"{bots.HQ}/general"
