"""registry.py: which engine runs a turn."""
import os
import pwd
import shutil

from dashboard import deployment
from dashboard.engines import config
from dashboard.engines.acp import AcpEngine
from dashboard.engines.api import ApiEngine
from dashboard.engines import prices
from dashboard.engines.base import EngineRefused
from dashboard.engines.claude_code import CHAT_MODEL, ENGINE_ENV, ENGINE_ID, ESCALATED_MODEL, ClaudeCodeEngine
from dashboard.engines.codex import ENGINE_ID as CODEX_ID, CodexEngine

NO_PEOPLE_ENGINE = ("This workspace has no API key for invited people yet, so bots can't answer here. "
                    "The owner can pick one in Settings, Bots.")
NO_SHARED_ENGINE = "{label} can't run in a thread invited people can see. Pick an engine for invited people in Settings, Bots."


def on_server():
    return deployment.mode() == "server"


def get(engine_id, root=config.ROOT):
    if engine_id == ENGINE_ID:
        return ClaudeCodeEngine()
    if engine_id == CODEX_ID or (isinstance(engine_id, str) and engine_id.startswith("acp:")):
        if on_server():  # their tools can reach any file the server's user can
            raise EngineRefused("This engine can read any file on the machine, so it only runs on your own Mac.")
        if engine_id == CODEX_ID:
            return CodexEngine()
        agent = next((a for a in config.load(root)["acp_agents"] if a["id"] == engine_id[4:]), None)
        if agent:
            return AcpEngine(agent)
    if isinstance(engine_id, str) and engine_id.startswith("api:"):
        provider = config.provider(engine_id[4:], root)
        if provider:
            return ApiEngine(provider, root)
    raise EngineRefused(f"The engine {engine_id!r} is not set up. Check Settings, Engines.")


def default_id(data):
    """Claude Code when it is installed, else the first API provider."""
    if data.get("default"):
        return data["default"]
    if shutil.which("claude") or not data["providers"]:
        return ENGINE_ID
    return f"api:{data['providers'][0]['id']}"


def _people_engine(data, root, refusal=NO_PEOPLE_ENGINE):
    """The API key for invited people. Nothing else ever answers for them."""
    if not data.get("people"):
        raise EngineRefused(refusal)
    engine = get(data["people"], root)
    if engine.kind != "api":
        raise EngineRefused(refusal)
    return engine


def pick(root, ws, bot, invited=False, shared=False, apps=False):
    """The engine for one run. An invited person's message never runs on the owner's login.
    A full-disk engine runs only the owner's private threads, with no connected apps."""
    data = config.load(root)
    if invited:
        return _people_engine(data, root)
    engine = get(data["bots"].get(f"{ws}/{bot}") or data["workspaces"].get(ws) or default_id(data), root)
    if shared and not engine.shared_safe:
        return _people_engine(data, root, NO_SHARED_ENGINE.format(label=engine.label))
    if apps and "apps" not in engine.supports:
        fallback = data.get("default") if data.get("default") not in (None, "", engine.id) else ENGINE_ID
        other = get(fallback, root)
        if "apps" not in other.supports:
            raise EngineRefused(f"{engine.label} can't use connected apps. Tag a bot on another engine.")
        return other
    return engine


def metered(engine):
    """Whether every run on `engine` reports its cost or can have it estimated: Claude Code,
    or an API provider whose fast and strong models are both priced. A scheduled job needs one."""
    if engine.id == ENGINE_ID:
        return True
    return engine.kind == "api" and all(engine.resolve_model(m) in prices.PRICES
                                        for m in (CHAT_MODEL, ESCALATED_MODEL))


def for_job(root=config.ROOT):
    """The engine for a generate.py job: the one its turn ran on, else the default."""
    data = config.load(root)
    wanted = os.environ.get(ENGINE_ENV) or default_id(data)
    if wanted.startswith("runner:"):  # jobs run on the server: never another person's login
        if wanted != "runner:owner":
            return _people_engine(data, root)
        wanted = default_id(data)
    engine = get(wanted, root)
    if "complete" in engine.supports:
        return engine
    # An owner's turn on a CLI that keeps a shell: a job never runs on one.
    return _people_engine(data, root) if data.get("people") else get(ENGINE_ID, root)


def statuses(root=config.ROOT):
    """One row per engine for the engines screen."""
    data = config.load(root)
    engines = [ClaudeCodeEngine()]
    if not on_server():
        engines.append(CodexEngine())
        engines += [AcpEngine(a) for a in data["acp_agents"]]
    engines += [ApiEngine(p, root) for p in data["providers"]]
    # folder_edit: whether bots on it may change files in linked folders (Codex and ACP: never).
    return [{**e.status(), "folder_edit": "folder_edit" in e.supports} for e in engines]


def terminal_cmd(root=config.ROOT):
    """The integrated terminal's command: the user's shell unless a CLI is picked."""
    choice = config.load(root).get("terminal") or "shell"
    if choice in ("claude", "codex") and shutil.which(choice):
        return [choice]
    return [os.environ.get("SHELL") or pwd.getpwuid(os.getuid()).pw_shell or "/bin/sh", "-l"]  # a service has no $SHELL
