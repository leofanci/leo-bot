"""base.py: the engine interface.

An engine runs one bot turn and yields the event tuples the server consumes:
("delta", text), ("tool", name), ("done", {...}), ("error", text).
The server computes a TurnPolicy per turn; each driver enforces it its own way
and refuses a turn it cannot enforce (EngineRefused), never running with more.
"""
import subprocess
import threading
from dataclasses import dataclass


class EngineRefused(ValueError):
    """This engine cannot run this turn under its policy. The message is shown to the user.
    A ValueError, so an owner's settings call on a missing engine answers 400 with that message."""


class SessionLost(Exception):
    """The engine's own session for this thread is gone (a chat moved to another machine)."""


@dataclass(frozen=True)
class TurnPolicy:
    shared: bool = False           # an invited person can see the thread
    readonly: bool = False         # a consulted teammate: osctl refuses writes
    web: bool = False              # web tools allowed this turn
    web_domains: tuple = ()        # hosts the user typed (fetch only these)
    apps: bool = False             # connected-apps relay this turn
    workspace: str = "hq"          # LEOBOT_WORKSPACE
    read_dirs: tuple = ()          # linked folders this turn may read and search (absolute)
    edit_dirs: tuple = ()          # of those, the ones it may also change (a subset)
    deny_dirs: tuple = ()
    deny_files: tuple = ()
    apps_used: bool = False
    chat: str = None               # LEOBOT_CHAT: the thread (osctl propose-job keys its outbox on it)
    job: str = None                # LEOBOT_JOB: a scheduled job's turn (its id)
    job_mode: str = None           # LEOBOT_JOB_MODE: approval, auto or report


# osctl reads these from its environment; a child never inherits the server's own copy.
TURN_ENV = ("LEOBOT_READONLY", "LEOBOT_CHAT", "LEOBOT_JOB", "LEOBOT_JOB_MODE")


def turn_vars(policy):
    """The osctl variables this turn sets (TURN_ENV), from its policy."""
    return {**({"LEOBOT_READONLY": "1"} if policy.readonly else {}),
            **({"LEOBOT_CHAT": policy.chat} if policy.chat else {}),
            **({"LEOBOT_JOB": policy.job} if policy.job else {}),
            **({"LEOBOT_JOB_MODE": policy.job_mode} if policy.job and policy.job_mode else {})}


@dataclass(frozen=True)
class TurnTokens:
    apps_token: str = None
    apps_port: int = None
    os_token: str = None
    os_port: int = None
    os_url: str = None        # a runner: osctl is on the server, over HTTPS
    runner_token: str = None


def drain(stream):
    """Read a child's stderr as it comes: a full pipe would freeze a chatty child, and the
    turn with it, while we read its stdout. Returns a call that gives the text once it ended."""
    box = []

    def read():
        try:
            with stream:
                box.append(stream.read() or "")
        except (OSError, ValueError):
            pass

    worker = threading.Thread(target=read, daemon=True)
    worker.start()

    def text():
        worker.join(timeout=5)
        return "".join(box)
    return text


def stop_child(proc):
    """A turn abandoned half way (the browser left) must not leave its child running."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


class Engine:
    id = ""
    label = ""
    kind = ""              # "cli" | "api" | "runner"
    shared_safe = False    # lockdown verified live for shared threads
    full_disk = False      # its shell and read tools reach any file (spec section 5a)
    supports = frozenset() # {"web", "apps", "folder_edit", "complete"}

    def resolve_model(self, model):
        """The server asks for a tier name (haiku, sonnet); map it to this engine's model."""
        return model

    def is_fresh(self, session):
        """True when this thread has no session on this engine yet."""
        return self.id not in session.engine_state

    def compact(self, session):
        """Drop this thread's session on this engine, so the next turn starts fresh (the server
        sends the recap of visible messages instead). False: this engine keeps its own size
        down, or its session is not on this machine."""
        session.engine_state.pop(self.id, None)
        return True

    def check(self, policy):
        """Hard lockdown flags: refuse rather than run with more than the policy allows."""
        if policy.shared and not self.shared_safe:
            raise EngineRefused(f"{self.label} cannot run in a thread invited people can see.")
        if policy.apps and "apps" not in self.supports:
            raise EngineRefused(f"{self.label} cannot use connected apps.")
        if policy.edit_dirs and "folder_edit" not in self.supports:
            raise EngineRefused(f"{self.label} cannot change files in linked folders.")
        if policy.apps_used and getattr(self, "full_disk", False):
            # Its own web and file tools cannot be switched off after an app read: mail or
            # calendar text could then be sent anywhere. Claude Code or an API engine can.
            raise EngineRefused(f"This thread read a connected app, so {self.label} cannot continue it "
                                "(its own web tools stay on). Pick Claude Code or an API engine, or start a new thread.")

    def turn(self, session, text, policy, model, tokens):
        raise NotImplementedError

    def complete(self, prompt, stdin_text, model):
        """One-shot text generation with zero tools (generate.py jobs)."""
        raise EngineRefused(f"{self.label} cannot run generation jobs.")

    def complete_metered(self, prompt, stdin_text, model):
        """Like `complete`, plus what it spent: {"text", "model", "usage", "cost", "truncated"}
        (usage and cost None when the engine reports none; truncated True when the reply
        stopped at the token limit). Background jobs use it, so their spend counts against caps."""
        raise EngineRefused(f"{self.label} cannot run generation jobs.")

    def status(self):
        return {"id": self.id, "label": self.label, "ok": False, "detail": ""}
