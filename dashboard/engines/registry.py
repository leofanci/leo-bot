"""registry.py: which engine runs a turn."""
import os
import shutil

from dashboard.engines import config
from dashboard.engines.api import ApiEngine
from dashboard.engines.base import EngineRefused
from dashboard.engines.claude_code import ENGINE_ENV, ENGINE_ID, ClaudeCodeEngine

NO_PEOPLE_ENGINE = ("This workspace has no API key for invited people yet, so bots can't answer here. "
                    "The owner can set one in HQ, Engines.")
NO_SHARED_ENGINE = "{label} can't run in a thread invited people can see. Pick an engine for people in HQ, Engines."


def get(engine_id, root=config.ROOT):
    if engine_id == ENGINE_ID:
        return ClaudeCodeEngine()
    if isinstance(engine_id, str) and engine_id.startswith("api:"):
        provider = config.provider(engine_id[4:], root)
        if provider:
            return ApiEngine(provider, root)
    raise EngineRefused(f"The engine {engine_id!r} is not set up. Check HQ, Engines.")


def default_id(data):
    """Claude Code when it is installed, else the first API provider."""
    if data.get("default"):
        return data["default"]
    if shutil.which("claude") or not data["providers"]:
        return ENGINE_ID
    return f"api:{data['providers'][0]['id']}"


def pick(root, ws, bot, invited=False, shared=False):
    """The engine for one run. An invited person's message never runs on the owner's login."""
    data = config.load(root)
    if invited:
        if not data.get("people"):
            raise EngineRefused(NO_PEOPLE_ENGINE)
        engine = get(data["people"], root)
        if engine.kind != "api":
            raise EngineRefused(NO_PEOPLE_ENGINE)
        return engine
    engine = get(data["bots"].get(f"{ws}/{bot}") or data["workspaces"].get(ws) or default_id(data), root)
    if shared and not engine.shared_safe:
        if not data.get("people"):
            raise EngineRefused(NO_SHARED_ENGINE.format(label=engine.label))
        return get(data["people"], root)
    return engine


def for_job(root=config.ROOT):
    """The engine for a generate.py job: the one its turn ran on, else the default."""
    data = config.load(root)
    wanted = os.environ.get(ENGINE_ENV) or default_id(data)
    if wanted.startswith("runner:"):  # jobs run on the server: never another person's login
        if wanted == "runner:owner":
            wanted = default_id(data)
        elif data.get("people"):
            wanted = data["people"]
        else:
            raise EngineRefused(NO_PEOPLE_ENGINE)
    engine = get(wanted, root)
    if "complete" in engine.supports:
        return engine
    return get(data.get("people") or ENGINE_ID, root)  # a CLI that keeps a shell never runs a job


def statuses(root=config.ROOT):
    """One row per engine for the engines screen."""
    rows = [ClaudeCodeEngine().status()]
    for p in config.load(root)["providers"]:
        rows.append(ApiEngine(p, root).status())
    return rows


def terminal_cmd(root=config.ROOT):
    """The integrated terminal's command: the user's shell unless a CLI is picked."""
    choice = config.load(root).get("terminal") or "shell"
    if choice in ("claude", "codex") and shutil.which(choice):
        return [choice]
    return [os.environ.get("SHELL") or "/bin/sh", "-l"]
