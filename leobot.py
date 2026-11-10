#!/usr/bin/env python3
"""leobot: start, stop and look after Leo Bot. Python standard library only."""
import argparse
import datetime
import fcntl
import hmac
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

from core import datadir, migrations, paths, version

DEFAULT_PORT = 8765
START_WAIT = 20
STOP_WAIT = 10


# ---------------------------------------------------------------- server state

def _pid_record() -> dict:
    try:
        rec = json.loads(paths.PID_FILE.read_text())
        return rec if isinstance(rec, dict) else {}
    except (OSError, ValueError):
        return {}


def _lock_held() -> bool:
    """The server holds an exclusive flock on its pid file for its whole life."""
    try:
        fd = os.open(paths.PID_FILE, os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def status_info() -> dict:
    running = _lock_held()
    rec = _pid_record() if running else {}
    return {"running": running, "pid": _int(rec.get("pid")), "port": _int(rec.get("port")),
            "version": version.current(), "home": str(paths.HOME), "data": str(paths.DATA),
            "installed": paths.is_installed()}


def _token() -> str:
    try:
        return paths.TOKEN_FILE.read_text().strip()
    except OSError:
        return ""


def _proof_ok(port) -> bool:
    """True when the server on `port` proves it holds our token (HMAC over a fresh nonce)."""
    token = _token()
    if not token or not port:
        return False
    nonce = secrets.token_hex(8)
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via HTTP(S)_PROXY
        with opener.open(f"http://127.0.0.1:{port}/healthz?nonce={nonce}", timeout=1) as r:
            body = json.loads(r.read().decode())
    except (OSError, ValueError):
        return False
    want = hmac.new(token.encode(), b"leobot-healthz:" + nonce.encode(), "sha256").hexdigest()
    return isinstance(body, dict) and hmac.compare_digest(str(body.get("proof", "")), want)


def _cmdline(pid) -> str:
    try:
        r = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5)
        return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _wait_stopped(timeout) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not _lock_held():
            return True
        time.sleep(0.1)
    return not _lock_held()


def _url(port) -> str:
    return f"http://127.0.0.1:{port}"


def _login_url(port) -> str:
    token = _token()
    return f"{_url(port)}/?token={token}" if token else _url(port)


# ------------------------------------------------------------------- commands

def cmd_start(args) -> int:
    info = status_info()
    if info["running"]:
        print(f"Leo Bot is already running at {_url(info['port'] or args.port)}")
        return 0
    paths.ensure_dirs()
    fd = os.open(paths.SERVER_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        log = os.fdopen(fd, "ab")
    except BaseException:
        os.close(fd)
        raise
    with log:
        proc = subprocess.Popen([sys.executable, str(paths.CODE / "dashboard" / "server.py"),
                                 "--port", str(args.port)],
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                cwd=str(paths.CODE), start_new_session=True)
    end = time.monotonic() + START_WAIT
    while time.monotonic() < end:
        if _proof_ok(args.port):
            print(f"Leo Bot is running at {_url(args.port)}")
            return 0
        if proc.poll() is not None:
            break
        time.sleep(0.25)
    if proc.poll() is None:
        proc.terminate()
    print(f"Leo Bot did not start. See {paths.SERVER_LOG}", file=sys.stderr)
    return 1


def cmd_stop(args=None) -> int:
    info = status_info()
    if not info["running"]:
        print("Leo Bot is not running.")
        return 0
    pid = info["pid"]
    if not pid:
        print("Leo Bot looks busy but its pid is unreadable. Not signalling anything.", file=sys.stderr)
        return 1
    if not (_proof_ok(info["port"]) or "dashboard/server.py" in _cmdline(pid)):
        print(f"Pid {pid} does not look like Leo Bot. Not signalling it.", file=sys.stderr)
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        print(f"Not allowed to stop pid {pid}. Stop it as the user that started it.", file=sys.stderr)
        return 1
    if not _wait_stopped(STOP_WAIT):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            print(f"Not allowed to force-stop pid {pid}.", file=sys.stderr)
            return 1
        _wait_stopped(STOP_WAIT)
    if not _lock_held():
        paths.PID_FILE.unlink(missing_ok=True)
        print("Leo Bot stopped.")
        return 0
    print("Leo Bot did not stop.", file=sys.stderr)
    return 1


def _stop_if_running():
    """Stop the server if it runs. Returns the port it ran on, or None when it was not running."""
    info = status_info()
    if not info["running"]:
        return None
    if cmd_stop() != 0:
        raise SystemExit("Could not stop Leo Bot. Nothing was changed.")
    return info["port"] or DEFAULT_PORT


def _restart(port) -> None:
    if port:
        cmd_start(argparse.Namespace(port=port))


def cmd_open(args=None) -> int:
    if not status_info()["running"]:
        rc = cmd_start(argparse.Namespace(port=DEFAULT_PORT))
        if rc:
            return rc
    port = status_info()["port"] or DEFAULT_PORT
    if sys.platform == "darwin":
        app = Path.home() / "Applications" / "LeoBot.app"
        r = subprocess.run(["open", str(app)] if app.exists() else ["open", _login_url(port)])
        if r.returncode != 0:
            print("Could not open Leo Bot. Try: leobot status", file=sys.stderr)
            return 1
    else:
        print(_login_url(port))
    return 0


def _service_state() -> str:
    try:
        from core import service  # Task 7; absent until then
    except ImportError:
        return "not available"
    for name in ("state", "status"):
        fn = getattr(service, name, None)
        if callable(fn):
            try:
                return str(fn())
            except Exception:  # noqa: BLE001
                return "unknown"
    return "unknown"


def cmd_status(args) -> int:
    info = status_info()
    info["service"] = _service_state()
    if args.json:
        print(json.dumps(info))
        return 0
    print(f"running: {'yes' if info['running'] else 'no'}")
    for k in ("pid", "port", "version", "installed", "home", "data", "service"):
        if info.get(k) is not None:
            print(f"{k}: {info[k]}")
    return 0


def cmd_version(args=None) -> int:
    print(version.current())
    return 0


def _mode(p: Path):
    try:
        return p.stat().st_mode & 0o777
    except OSError:
        return None


def _port_free(port) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) != 0


def cmd_doctor(args) -> int:
    lines = []

    def ok(msg):
        lines.append(f"OK   {msg}")

    def fix(msg, cmd):
        lines.append(f"FIX  {msg}. Run: {cmd}")

    if sys.version_info >= (3, 10):
        ok(f"python {sys.version_info.major}.{sys.version_info.minor}")
    else:
        fix("python is older than 3.10", "install Python 3.10 or newer")
    for tool, how in (("git", "install git (Xcode command line tools on macOS)"),
                      ("claude", "install Claude Code and put `claude` on PATH")):
        (ok(f"{tool} on PATH") if shutil.which(tool) else fix(f"{tool} not found", how))
    for label, p in (("home folder", paths.HOME), ("data folder", paths.DATA)):
        m = _mode(p)
        if m is None:
            fix(f"{label} missing", "leobot start")
        elif m == 0o700:
            ok(f"{label} is private")
        else:
            fix(f"{label} is mode {m:o}, not 700", f"chmod 700 {p}")
    rec = migrations.read_marker(paths.DATA)
    if not rec:
        fix("data marker missing", "leobot start")
    else:
        schema = rec.get("schema")
        if schema == migrations.LATEST:
            ok(f"data schema {schema}")
        elif isinstance(schema, int) and schema > migrations.LATEST:
            fix(f"data schema {schema} is newer than this app ({migrations.LATEST})", "leobot update")
        else:
            fix(f"data schema {schema} is older than {migrations.LATEST}", "leobot start")
    journal = datadir.read_journal(paths.DATA)
    if journal is not None:
        if journal.get("_bad"):
            fix(f"the note about an unfinished data move is damaged; its backup is "
                f"{journal.get('backup') or 'a legacy-*.tar.gz in ' + str(paths.BACKUPS)}",
                f"leobot backup --restore <that backup> --yes")
        elif datadir._same_folder(journal["source"], paths.CODE):
            fix("a data move did not finish", "leobot start")
        else:
            fix("a data move did not finish", f"leobot move-data --from {journal['source']}")
    else:
        ok("no unfinished data move")
    sources = [Path(args.source)] if getattr(args, "source", None) else [paths.CODE]
    legacy_home = Path.home() / "leo-bot"
    if not getattr(args, "source", None) and legacy_home.is_dir() and legacy_home.resolve() != paths.CODE.resolve():
        sources.append(legacy_home)
    for src in sources:
        found = datadir.legacy_items(src)
        if found:
            fix(f"old data found in {src} ({', '.join(found)})", f"leobot move-data --from {src}")
        else:
            ok(f"no old data in {src}")
    info = status_info()
    if info["running"]:
        ok(f"port {info['port']} is Leo Bot")
    elif _port_free(DEFAULT_PORT):
        ok(f"port {DEFAULT_PORT} is free")
    else:
        fix(f"port {DEFAULT_PORT} is used by something else", f"leobot start --port {DEFAULT_PORT + 1}")
    try:
        latest = json.loads(paths.SETTINGS.read_text()).get("latest")
    except (OSError, ValueError, AttributeError):
        latest = None
    if isinstance(latest, str) and version.newer(version.current(), latest):
        fix(f"version {latest} is available (you have {version.current()})", "leobot update")
    else:
        ok(f"version {version.current()}")
    print("\n".join(lines))
    return 1 if any(l.startswith("FIX") for l in lines) else 0


def _stamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _confirm(question, yes) -> bool:
    if yes:
        return True
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _tar_data(dest: Path) -> Path:
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with datadir.NoLinkTar.open(dest, "w:gz") as t:
            for child in sorted(paths.DATA.iterdir()):
                t.add(child, arcname=child.name)
        dest.chmod(0o600)
        datadir._fsync_path(dest, strict=True)
        datadir._fsync_path(dest.parent)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return dest


def _unsafe_member(m: tarfile.TarInfo):
    p = Path(m.name)
    if m.name.startswith("/") or p.is_absolute() or ".." in p.parts:
        return f"unsafe path {m.name!r}"
    if not (m.isfile() or m.isdir() or m.issym()):
        return f"unsupported entry {m.name!r}"
    return None


def _check_members(members) -> None:
    for m in members:
        bad = _unsafe_member(m)
        if bad:
            raise ValueError(bad)


def _ensure_dirs(dest: Path, parts) -> Path:
    """Create `parts` under dest one step at a time, refusing to go through any symbolic link."""
    cur = dest
    for part in parts:
        cur = cur / part
        if cur.is_symlink():
            raise ValueError(f"{cur.relative_to(dest)} is a symbolic link; refusing to write through it")
        if not cur.exists():
            cur.mkdir(mode=0o700)
    real_dest = os.path.realpath(dest)
    if os.path.commonpath([real_dest, os.path.realpath(cur)]) != real_dest:
        raise ValueError("path leaves the data folder")
    return cur


def _safe_extract(archive: Path, dest: Path) -> None:
    """Extract directories, regular files and symbolic links only, never writing through a link.
    Links are created as they are and never followed, so a backup that links to a folder outside
    the data restores faithfully."""
    with tarfile.open(archive) as t:
        members = t.getmembers()
        _check_members(members)
        for m in members:
            parts = Path(m.name).parts
            if not parts:
                continue
            parent = _ensure_dirs(dest, parts[:-1])
            target = parent / parts[-1]
            if m.isdir():
                if target.is_symlink():
                    raise ValueError(f"{m.name} is a symbolic link; refusing to write through it")
                if not target.exists():
                    target.mkdir()
                os.chmod(target, (m.mode & 0o755) | 0o700)
            elif m.issym():
                if os.path.lexists(target):
                    raise ValueError(f"{m.name} appears twice")
                os.symlink(m.linkname, target)
            else:
                if os.path.lexists(target):
                    target.unlink()
                src = t.extractfile(m)
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                             (m.mode & 0o755) | 0o600)
                with os.fdopen(fd, "wb") as f:
                    shutil.copyfileobj(src, f)


def _empty_data() -> None:
    for p in paths.DATA.iterdir():
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()


def cmd_backup(args) -> int:
    paths.ensure_dirs()
    if not args.restore:
        dest = _tar_data(paths.BACKUPS / f"manual-{_stamp()}.tar.gz")
        print(f"Backup saved: {dest}")
        return 0
    src = Path(args.restore)
    try:
        with tarfile.open(src) as t:
            _check_members(t.getmembers())
    except (OSError, tarfile.TarError, ValueError) as e:
        print(f"Cannot restore {src}: {e}. Nothing was changed.", file=sys.stderr)
        return 1
    if paths.DATA.is_symlink():
        print(f"{paths.DATA} is a symbolic link to {os.readlink(paths.DATA)}. Restore will not replace "
              f"a linked data folder. Restore by hand into the target. Nothing was changed.", file=sys.stderr)
        return 1
    if not _confirm(f"Replace your data with {src.name}? Current data is saved first.", args.yes):
        print("Cancelled.", file=sys.stderr)
        return 1
    port = _stop_if_running()
    snap = None
    kept = None
    done = False
    try:
        stamp = _stamp()
        real_data = paths.DATA.resolve()
        if real_data == src.resolve() or real_data in src.resolve().parents:
            kept = paths.BACKUPS / f"restore-source-{stamp}.tar.gz"  # the restore would delete it
            shutil.copy2(src, kept)
            src = kept
        else:
            kept = None
        snap = _tar_data(paths.BACKUPS / f"pre-restore-{stamp}.tar.gz")
        aside = paths.HOME / f"data.pre-restore-{stamp}"
        os.rename(paths.DATA, aside)
        try:
            paths.DATA.mkdir(mode=0o700)
            _safe_extract(src, paths.DATA)
            os.chmod(paths.DATA, 0o700)
        except BaseException as e:
            shutil.rmtree(paths.DATA, ignore_errors=True)
            try:
                os.rename(aside, paths.DATA)
                back = "Your previous data is back."
            except OSError:
                back = f"Could not put it back. Your data is in {aside} and in {snap}."
            print(f"Restore failed ({e}). {back} Snapshot: {snap}"
                  + (f" Restore source kept: {kept}" if kept else ""), file=sys.stderr)
            if not isinstance(e, Exception):
                raise
            return 1
        done = True
        notes = []
        try:
            shutil.rmtree(aside)
        except OSError as e:
            notes.append(f"Could not delete {aside} ({e}); remove it by hand.")
        if kept:
            kept.unlink(missing_ok=True)
        try:
            _reindex()
        except Exception as e:  # noqa: BLE001
            notes.append(f"Re-index failed ({e}). Start Leo Bot to rebuild it.")
        print(f"Restored. Previous data saved: {snap}")
        for n in notes:
            print(n, file=sys.stderr)
        return 0
    except Exception as e:
        if done:
            raise
        print(f"Restore failed ({e}). Nothing was replaced."
              + (f" Snapshot: {snap}" if snap else ""), file=sys.stderr)
        return 1
    finally:
        _restart(port)


def _reindex() -> None:
    subprocess.run([sys.executable, str(paths.CODE / "index.py"), str(paths.DATA)],
                   capture_output=True, text=True)


def cmd_move_data(args) -> int:
    port = _stop_if_running()
    try:
        res = datadir.move_legacy(Path(args.source), replace=args.replace)
        if res.refused:
            print(res.refused, file=sys.stderr)
            return 1
        if res.moved:
            print(f"Moved: {', '.join(res.moved)}")
            if res.backup:
                print(f"Backup: {res.backup}")
        else:
            print("Nothing to move.")
        _reindex()
        return 0
    finally:
        _restart(port)


def _rm(p: Path) -> None:
    if p.is_symlink() or p.is_file():
        p.unlink(missing_ok=True)
    elif p.is_dir():
        shutil.rmtree(p)


_HOME_ENTRIES = {"app", "data", "run", "logs", "backups", "leobot.json", ".DS_Store"}


def _home_problem():
    """Why paths.HOME must not be deleted, or None."""
    h = paths.HOME.resolve()
    user = Path.home().resolve()
    code = paths.CODE.resolve()
    if h == Path(h.anchor) or h == user or h in user.parents:
        return f"{h} is or contains your own home folder"
    if (h == code or h in code.parents) and code != (h / "app").resolve():
        return f"{h} is or contains the Leo Bot code folder"
    if not any((h / n).exists() for n in ("data", "run", "leobot.json")):
        return f"{h} does not look like a Leo Bot home"
    extra = sorted(p.name for p in h.iterdir()
                   if p.name not in _HOME_ENTRIES and not p.name.startswith("data.pre-restore-"))
    if extra:
        return f"{h} holds other things ({', '.join(extra[:5])})"
    return None


def _is_checkout(p: Path) -> bool:
    try:
        if p.resolve() == paths.CODE.resolve():
            return True
        return (p / "dashboard" / "server.py").is_file() and (p / "leobot.py").is_file() and (p / ".git").exists()
    except OSError:
        return False


def _is_ours(link: Path) -> bool:
    """Our launcher: a symlink to leobot.py in the app/code folder, or a script naming leobot.py."""
    try:
        if link.is_symlink():
            tgt = link.resolve()
            return tgt.name == "leobot.py" and tgt.parent in (paths.CODE.resolve(), (paths.HOME / "app").resolve())
        return link.is_file() and "leobot.py" in link.read_text(errors="replace")
    except OSError:
        return False


def cmd_uninstall(args) -> int:
    home = paths.HOME.resolve()
    what = "the app, the command and the launcher" + (f" AND all your data in {home}" if args.data else " (data is kept)")
    if args.data:
        problem = _home_problem()
        if problem:
            print(f"Refusing to delete everything: {problem}.", file=sys.stderr)
            return 1
    if not _confirm(f"Uninstall Leo Bot: remove {what}?", args.yes):
        print("Cancelled.", file=sys.stderr)
        return 1
    try:  # first, so a service cannot bring the server back
        from core import service  # Task 7; absent until then
        service.uninstall()
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"Could not remove the background service ({e}). Continuing.", file=sys.stderr)
    user = Path.home().resolve()
    if home == user or home in user.parents:
        print(f"Refusing: {home} is or contains your own home folder.", file=sys.stderr)
        return 1
    _stop_if_running()
    link = Path.home() / ".local" / "bin" / "leobot"
    if _is_ours(link):
        _rm(link)
    app = paths.HOME / "app"
    if app.exists() or app.is_symlink():
        if _is_checkout(app):
            _rm(app)
        else:
            print(f"Left {app} alone: it does not look like a Leo Bot checkout.", file=sys.stderr)
    _rm(Path.home() / "Applications" / "LeoBot.app")
    if args.data:
        _rm(paths.HOME)
    print("Leo Bot uninstalled." + (f" Removed {home}." if args.data else f" Your data is still in {paths.DATA}."))
    return 0


# ----------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="leobot", description="Start and look after Leo Bot.")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("open", help="start if needed, then open Leo Bot (default)")
    p = sub.add_parser("start", help="start the server")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub.add_parser("stop", help="stop the server")
    p = sub.add_parser("status", help="show server and folders")
    p.add_argument("--json", action="store_true")
    sub.add_parser("version", help="print the version")
    p = sub.add_parser("doctor", help="check the setup")
    p.add_argument("--from", dest="source", help="also look for old data in this folder")
    p = sub.add_parser("backup", help="back up or restore your data")
    p.add_argument("--restore", metavar="FILE")
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("move-data", help="move old data into the home folder")
    p.add_argument("--from", dest="source", required=True)
    p.add_argument("--replace", action="store_true")
    p = sub.add_parser("uninstall", help="remove Leo Bot")
    p.add_argument("--data", action="store_true", help="also delete all your data")
    p.add_argument("--yes", action="store_true")
    return ap


COMMANDS = {"open": cmd_open, "start": cmd_start, "stop": cmd_stop, "status": cmd_status,
            "version": cmd_version, "doctor": cmd_doctor, "backup": cmd_backup,
            "move-data": cmd_move_data, "uninstall": cmd_uninstall}


def main(argv=None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    return COMMANDS[args.cmd or "open"](args)


if __name__ == "__main__":
    sys.exit(main())
