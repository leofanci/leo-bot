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


HOME_SHARED = ("app", "data")  # the only home folder entries a bot turn may read


def _sort_entry(p: Path, dirs: list, files: list) -> None:
    if p.is_symlink():  # a link: deny the name itself and anything under it
        files.append(p)
        dirs.append(p)
    elif p.is_dir():
        dirs.append(p)
    else:
        files.append(p)


def home_private() -> "tuple[list, list]":
    """Every entry of HOME but app and data, as (folders, files): run, logs, backups and the
    settings even before they exist, plus whatever else is there now (data.pre-restore-*,
    data.partial-*, .app-installing). No bot turn may read any of it."""
    dirs, files = [RUN, LOGS, BACKUPS], [SETTINGS]
    try:
        entries = sorted(HOME.iterdir())
    except OSError:
        entries = []
    for p in entries:
        if p.name not in HOME_SHARED and p not in dirs and p not in files:
            _sort_entry(p, dirs, files)
    return dirs, files


def is_installed() -> bool:
    return CODE.resolve() == (HOME / "app").resolve()


def ensure_dirs() -> None:
    for d in (HOME, DATA, RUN, LOGS, BACKUPS):
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(d, 0o700)
