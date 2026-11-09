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

    def check(self, policy):
        """Hard lockdown flags: refuse rather than run with more than the policy allows."""
        if policy.shared and not self.shared_safe:
            raise EngineRefused(f"{self.label} cannot run in a thread invited people can see.")
        if policy.apps and "apps" not in self.supports:
            raise EngineRefused(f"{self.label} cannot use connected apps.")
        if policy.edit_dirs and "folder_edit" not in self.supports:
            raise EngineRefused(f"{self.label} cannot change files in linked folders.")

    def turn(self, session, text, policy, model, tokens):
        raise NotImplementedError

    def complete(self, prompt, stdin_text, model):
        """One-shot text generation with zero tools (generate.py jobs)."""
        raise EngineRefused(f"{self.label} cannot run generation jobs.")

    def status(self):
        return {"id": self.id, "label": self.label, "ok": False, "detail": ""}
