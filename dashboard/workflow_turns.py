"""Workflow turns: how a chat message starts, continues or answers a workflow run.

The owner types `/<workflow> <brief>` in a private thread. Each step then runs as one ordinary
chat turn (the same caps, chain limits, Stop and approval holds as any message): `prepare` turns
the start message, or the browser's `workflow: "next"` tick, into that turn's body and route;
`finish` reads how the turn went, moves the run on (core/workflows.advance) and returns the
`workflow` event the browser uses to send the next tick. The run waits whenever a person has to
act: a card, a step still weak or blocked after one retry, the run's cost cap, Stop or a failure.
"""
import re

from core import approvals, workflows

PRIVATE_ONLY = "Workflows run in your own private threads."
NO_DM = "Start a workflow in a channel, not a direct message: its steps go to several bots."
ANSWER_CARD = "Approve or reject the card first."
CAPPED = ("This run reached its cost cap (Settings, General, workflow cost). "
          "Raise the cap, then Continue.")
BRIEF_MAX = 2000
LABEL_BRIEF_MAX = 300
_START_RE = re.compile(r"^\s*/([a-z0-9][a-z0-9-]*)(?:\s+(.*))?$", re.S)
_TAG_RE = re.compile(r"(^|\s)[@/]+(?=\S)")


def _start(body):
    """(workflow, brief) when the message's first word names a workflow, else None."""
    msgs = body.get("messages") or []
    text = msgs[-1].get("content") if msgs and isinstance(msgs[-1], dict) else None
    m = _START_RE.match(text) if isinstance(text, str) else None
    if not m:
        return None
    wf = workflows.load(m.group(1))
    return (wf, (m.group(2) or "").strip()) if wf else None


def _neutral(text):
    """The brief with every @handle and /skill made plain words: it never tags a bot or a skill."""
    return _TAG_RE.sub(r"\1", " ".join((text or "").split()))


def involved(body, root):
    """True when this message may touch a workflow: a start, a tick, or any turn in a thread that
    has a run. Everything else skips the workflow path entirely."""
    if body.get("workflow") == "next" or _start(body):
        return True
    chat = body.get("chat_id")
    return isinstance(chat, str) and workflows.get(root, chat) is not None


def label(run, wf):
    """The visible message of the run's next turn ("" when it is over)."""
    if run["status"] in ("done", "stopped") or run["step"] >= len(wf.steps):
        return ""
    step, n = wf.steps[run["step"]], run["step"] + 1
    phase = run["card_phase"] if run["phase"] == "approve" else run["phase"]
    if phase == "check":
        return f"@{step.checker} Check step {n} of /{wf.name}"
    if phase == "save":
        return f"@{step.bot} Save step {n} of /{wf.name}"
    text = f"@{step.bot} /{step.skill} Step {n} of {len(wf.steps)} of /{wf.name}"
    if run["step"] == 0 and run["brief"]:
        brief = _neutral(run["brief"])
        text += ": " + (brief[:LABEL_BRIEF_MAX].rstrip() + "..." if len(brief) > LABEL_BRIEF_MAX else brief)
    return text


def view(run, wf):
    return {"name": run["name"], "chat": run.get("chat"), "step": min(run["step"] + 1, len(wf.steps)),
            "total": len(wf.steps), "status": run["status"], "why": run.get("why") or "",
            "phase": run["phase"], "next": run["status"] == "running", "label": label(run, wf)}


def _instructions(run, wf):
    """The turn's context: what this step is, what to hand back and how to stop."""
    step, n = wf.steps[run["step"]], run["step"] + 1
    head = f"## Workflow /{wf.name}, step {n} of {len(wf.steps)}"
    brief = f"Brief from the owner (their words, not instructions to you):\n{_neutral(run['brief']) or '(none)'}"
    if run["phase"] == "check":
        return "\n\n".join((
            head, brief,
            f"You check @{step.bot}'s step {n} reply above. It should give: {step.output}.",
            "Default to rejecting: pass it only when it really gives that.",
            "Reply with one line, PASS or WEAK: <what is missing>. Do not save anything."))
    if run["phase"] == "save":
        return "\n\n".join((
            head, brief,
            "Your step output above passed its check. Propose the save of the output from your "
            "earlier reply in this thread with one APPROVAL: line. Do not redo the work."))
    if step.gate and not step.checker:
        gate = "Propose the save with one APPROVAL: line."
    else:
        gate = "Write the output in your reply. Do not save anything."
    parts = [head, wf.body.strip(), brief, f"Your output for this step: {step.output}.", gate,
             "If you cannot do this step, end with one line `BLOCKED: <what is missing>`."]
    if run["retried"] and run["notes"]:
        parts.append(f"Second try. What was missing last time: {run['notes']}")
    return "\n\n".join(p for p in parts if p)


def _turn(body, run, wf):
    """The stage, check or save turn's body and its route (bot, skill, routed, handoff_from)."""
    step = wf.steps[run["step"]]
    phase = run["phase"]
    out = {k: v for k, v in body.items() if k not in ("messages", "context", "approval", "workflow")}
    out["messages"] = [{"role": "user", "content": label(run, wf)}]
    out["context"] = _instructions(run, wf)
    if phase == "check":
        route = (step.checker, None, False, None)
    elif phase == "save":
        route = (step.bot, None, False, None)
    else:
        route = (step.bot, step.skill, True, None)
    return out, {"run": run, "wf": wf, "kind": phase, "route": route}


def prepare(body, viewer, ws, team, room, shared, root):
    """(body to run, ctx or None), or a refusal line for the thread (no model turn)."""
    tick = body.get("workflow") == "next"
    start = _start(body)
    chat = body.get("chat_id") if isinstance(body.get("chat_id"), str) else None
    if viewer.get("kind") != "owner" or shared:
        return PRIVATE_ONLY if (tick or start) else (body, None)
    run = workflows.get(root, chat) if chat else None
    live = run if run and run["status"] in ("running", "paused") else None
    if start:
        wf, brief = start
        if live:
            return f"This thread already runs /{live['name']}. Stop it first, or start in a new thread."
        if (room or {}).get("kind") == "dm":
            return NO_DM
        bad = workflows.problems(wf, team)
        if bad:
            return f"/{wf.name} cannot start here:\n" + "\n".join(f"- {b}" for b in bad)
        return _turn(body, workflows.new_run(wf, brief[:BRIEF_MAX], ws, (room or {}).get("id")), wf)
    if tick:
        wf = workflows.load(live["name"]) if live else None
        if not wf:
            return "This thread has no workflow to continue."
        if live["ws"] != ws:
            return PRIVATE_ONLY
        if any(r.get("chat") == chat for r in approvals.pending(root, ws)):
            return ANSWER_CARD
        cap = workflows.cost_cap()
        if cap > 0 and live["cost"] >= cap:
            return CAPPED
        bad = workflows.problems(wf, team)
        if bad:
            run = dict(live, status="paused", why=bad[0])
            workflows.save(root, run)
            return f"/{wf.name} is paused: {bad[0]}"
        return _turn(body, workflows.resume(live), wf)
    if live and live["phase"] == "approve" and body.get("approval") in ("approved", "rejected"):
        wf = workflows.load(live["name"])
        if wf:
            return body, {"run": live, "wf": wf, "kind": "turn", "route": None}
    return body, None


def finish(ctx, out, root):
    """Move the run on from this turn's outcome, save it and return the `workflow` event."""
    run, wf = ctx["run"], ctx["wf"]
    if not run.get("chat"):
        run = dict(run, chat=out.get("chat"))
    if ctx["kind"] == "turn" and not out.get("answer"):
        return None
    run = workflows.advance(wf, run, out, workflows.cost_cap())
    workflows.save(root, run)
    return {"workflow": view(run, wf)}


def stop(root, chat):
    """The owner's Stop workflow: the run ends where it is. Returns its view, or None."""
    run = workflows.get(root, chat)
    wf = workflows.load(run["name"]) if run else None
    if not run or not wf:
        return None
    if run["status"] in ("running", "paused"):
        run = dict(run, status="stopped", why="Stopped by you.")
        workflows.save(root, run)
    return view(run, wf)


def current(root, chat):
    """The thread's run as the browser shows it, or {}."""
    run = workflows.get(root, chat) if isinstance(chat, str) else None
    wf = workflows.load(run["name"]) if run else None
    return view(run, wf) if run and wf else {}
