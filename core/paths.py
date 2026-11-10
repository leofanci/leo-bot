"""One place for code, home and data folders.

CODE is the app checkout. HOME holds all user data (default ~/.leobot,
override with LEOBOT_HOME). Paths are resolved once at import.
"""
import os
from pathlib import Path
from typing import Mapping

CODE = Path(__file__).resolve().parent.parent


def home_for(env: Mapping[str, str]) -> Path:
    raw = env.get("LEOBOT_HOME", "")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".leobot"


HOME = home_for(os.environ)
DATA = HOME / "data"
RUN = HOME / "run"
LOGS = HOME / "logs"
BACKUPS = HOME / "backups"
SETTINGS = HOME / "leobot.json"

TOKEN_FILE = RUN / "auth-token"
PID_FILE = RUN / "server.pid"
SERVER_LOG = LOGS / "server.log"
UPDATE_LOG = LOGS / "update.log"


def is_installed() -> bool:
    return CODE.resolve() == (HOME / "app").resolve()


def ensure_dirs() -> None:
    for d in (HOME, DATA, RUN, LOGS, BACKUPS):
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(d, 0o700)
