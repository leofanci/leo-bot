"""One place for code, home and data folders.

CODE is the app checkout. HOME holds all user data (default ~/.leobot,
override with LEOBOT_HOME). Paths are resolved once at import, always absolute.

Under unittest LEOBOT_HOME must be set (tests/__init__.py sets a temp folder), so a test run
that skips tests/__init__.py fails at once instead of using the real home folder.
"""
import os
import sys
from pathlib import Path
from typing import Mapping

CODE = Path(__file__).resolve().parent.parent


def home_for(env: Mapping[str, str]) -> Path:
    """$LEOBOT_HOME made absolute (~ expanded, a relative one taken from the current folder,
    so every child process agrees on it), else ~/.leobot."""
    raw = env.get("LEOBOT_HOME", "")
    if raw:
        return Path(os.path.abspath(os.path.expanduser(raw)))
    return Path.home() / ".leobot"


def refuse_real_home_in_tests(env: Mapping[str, str], modules=None) -> None:
    """Raise when tests run without LEOBOT_HOME: they would read and write the real home folder."""
    if "unittest" in (sys.modules if modules is None else modules) and not env.get("LEOBOT_HOME"):
        raise RuntimeError(
            "Leo Bot tests must not use your real home folder, and LEOBOT_HOME is not set. Run them "
            "with `python3 -m unittest discover -t . -s tests` (the -t . makes tests/__init__.py set a "
            "temp home), or set LEOBOT_HOME to a temp folder.")


refuse_real_home_in_tests(os.environ)  # before the export below, which would hide an unset home
HOME = home_for(os.environ)
# Every child (server, indexer, engines, osctl) gets the same absolute home, whatever was given.
os.environ["LEOBOT_HOME"] = str(HOME)
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
