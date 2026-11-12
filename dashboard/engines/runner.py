"""runner.py: the engine that sends a turn to the sender's own Mac (core/runners.py).

A runner turn always runs under the shared policy: its one Leo Bot tool is osctl over
HTTPS back to this server. The runner's Mac is not the workspace, and the prompt
must never be able to reach that person's files, so no file or shell tools.
"""
from core import runners
from dashboard.engines.base import Engine


class _Handle:
    def __init__(self, hub, turn):
        self.hub, self.turn, self.stopped = hub, turn, False

    def poll(self):
        return 0 if self.stopped else None

    def terminate(self):
        self.stopped = True
        self.hub.cancel(self.turn)


class RunnerEngine(Engine):
    kind = "runner"
    shared_safe = True
    supports = frozenset({"web"})

    def __init__(self, owner, name=None, hub=None):
        self.owner = owner
        self.id = f"runner:{owner}"
        self.label = f"{name}'s Mac" if name else "your Mac"
        self.hub = hub or runners.HUB

    def compact(self, session):
        return False  # the session lives on that Mac, keyed by the chat id

    def turn(self, session, text, policy, model, tokens):
        session.engine_state.setdefault(self.id, {})
        job = {"chat": session.session_id, "rail": session.rail, "text": text, "model": model,
               "recap": getattr(session, "recap", None) or "", "os_token": tokens.os_token,
               "policy": {"web": policy.web, "web_domains": list(policy.web_domains),
                          "readonly": policy.readonly, "apps_used": policy.apps_used,
                          "workspace": policy.workspace}}
        turn = self.hub.submit(self.owner, job)
        handle = _Handle(self.hub, turn)
        session._proc = handle
        try:
            yield from self.hub.wait_events(turn)
        finally:
            session._proc = None

    def status(self):
        on = self.hub.online(self.owner)
        return {"id": self.id, "label": self.label, "kind": self.kind, "installed": True, "ok": on,
                "detail": "online" if on else "offline"}
