"""leobot update: move an installed checkout (HOME/app) to a newer release without losing data.

Stable follows vX.Y.Z tags on `origin`, main follows origin/main. Every update makes a backup of
DATA first; when the new version does not come up healthy, the previous code is checked out
again and, if the data format changed, the backup is restored (after the current data is saved
too). Every step goes to paths.UPDATE_LOG with a timestamp.

Server start, stop, health and the data restore are leobot.py's own helpers, imported lazily so
the server can use check() and spawn_detached() without loading the command-line module.
"""
import contextlib
import datetime
import fcntl
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from core import migrations, paths, service, version

CHANNELS = ("stable", "main")
DEFAULTS = {"channel": "stable", "checked_at": None, "latest": None, "previous_ref": None}
HEALTH_TIMEOUT = 60  # seconds for the new server to answer with the target version
POLL = 0.5
GIT_TIMEOUT = 300
REMOTE_TIMEOUT = 30
KEEP_PRE = 5
UNIT = "leobot-update"  # systemd transient unit
JOB = "leobot.update"  # launchd job label
LOCK_NAME = "update.lock"

_GITHUB = "https://github.com/"
_TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_PRE = re.compile(r"pre-(v[0-9]+\.[0-9]+\.[0-9]+|main-[0-9a-f]+)-([0-9]{8}-[0-9]{6}-[0-9]{6})\.tar\.gz")


# ------------------------------------------------------------------ settings

def settings() -> dict:
    try:
        raw = json.loads(paths.SETTINGS.read_text())
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    d = {**DEFAULTS, **raw}
    if d["channel"] not in CHANNELS:
        d["channel"] = "stable"
    return d


def save_settings(d: dict) -> None:
    """Merge `d` into leobot.json: temp file, fsync, mode 600, then an atomic rename."""
    try:
        cur = json.loads(paths.SETTINGS.read_text())
    except (OSError, ValueError):
        cur = {}
    if not isinstance(cur, dict):
        cur = {}
    cur.update(d)
    paths.SETTINGS.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=paths.SETTINGS.parent, prefix=".leobot-json-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cur, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, paths.SETTINGS)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ------------------------------------------------------------------- logging

def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _stamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _is_log(stream) -> bool:
    """True when `stream` already writes to the update log (a detached run)."""
    try:
        a = os.fstat(stream.fileno())
        b = os.stat(paths.UPDATE_LOG)
    except (OSError, ValueError, AttributeError, io.UnsupportedOperation):
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _log(msg: str, err: bool = False) -> None:
    line = f"{_now()} {msg}"
    try:
        paths.UPDATE_LOG.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(paths.UPDATE_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, (line + "\n").encode())
        finally:
            os.close(fd)
    except OSError:
        pass
    stream = sys.stderr if err else sys.stdout
    if not _is_log(stream):
        print(line, file=stream, flush=True)


# ----------------------------------------------------------------------- git

def _git(code: Path, *args, timeout=GIT_TIMEOUT) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
    try:
        return subprocess.run(["git", "-C", str(code), *args], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(args, 127, "", str(e))


def _why(r) -> str:
    lines = (r.stderr or r.stdout or "").strip().splitlines()
    return lines[-1] if lines else f"exit {r.returncode}"


def _head(code: Path) -> "str | None":
    r = _git(code, "rev-parse", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else None


def _commit(code: Path, ref: str) -> "str | None":
    r = _git(code, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    sha = r.stdout.strip()
    return sha if r.returncode == 0 and _SHA.fullmatch(sha) else None


def _version_at(code: Path, commit: str) -> "str | None":
    r = _git(code, "show", f"{commit}:VERSION")
    v = r.stdout.strip() if r.returncode == 0 else ""
    return v or None


def _version_file(code: Path) -> str:
    try:
        return (Path(code) / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def _origin_url(code: Path) -> "str | None":
    r = _git(code, "remote", "get-url", "origin")
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def origin_problem(code: Path) -> "str | None":
    """Updates come only from the configured origin, over https:// (file:// for local mirrors)."""
    url = _origin_url(code)
    if not url:
        return "The app folder has no `origin` to update from."
    if not url.startswith(("https://", "file://")):
        return ("The app folder's origin is not an https:// address, "
                "and Leo Bot only updates over https:// for safety.")
    return None


def release_notes_url(code: "Path | None" = None, tag: "str | None" = None) -> "str | None":
    url = _origin_url(Path(code or paths.CODE)) or ""
    if not url.startswith(_GITHUB):
        return None
    m = re.fullmatch(r"([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?", url[len(_GITHUB):])
    if not m:
        return None
    base = f"{_GITHUB}{m.group(1)}/{m.group(2)}"
    return f"{base}/releases/tag/{tag}" if tag else f"{base}/commits/main"


def _remote_latest_tag(code: Path) -> "str | None":
    r = _git(code, "ls-remote", "--tags", "origin", timeout=REMOTE_TIMEOUT)
    return version.latest_tag(r.stdout) if r.returncode == 0 else None


def _remote_has_tag(code: Path, tag: str) -> "bool | None":
    """True/False, or None when origin cannot be reached."""
    r = _git(code, "ls-remote", "--tags", "origin", f"refs/tags/{tag}", timeout=REMOTE_TIMEOUT)
    if r.returncode != 0:
        return None
    return any(line.split("\t")[-1].strip() == f"refs/tags/{tag}" for line in r.stdout.splitlines())


def _remote_main(code: Path) -> "str | None":
    r = _git(code, "ls-remote", "origin", "refs/heads/main", timeout=REMOTE_TIMEOUT)
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.strip() == "refs/heads/main" and _SHA.fullmatch(sha.strip()):
            return sha.strip()
    return None


def _tag_of(target: str) -> "str | None":
    t = (target or "").strip()
    if not t.startswith("v"):
        t = "v" + t
    return t if _TAG.fullmatch(t) and version.parse(t) else None


# --------------------------------------------------------------------- check

def check(code: "Path | None" = None) -> dict:
    """Compare this checkout with origin. Never raises on network failure (`latest` is None).
    Stores `checked_at`, and `latest` when known, in leobot.json."""
    code = Path(code or paths.CODE)
    channel = settings()["channel"]
    current = _version_file(code)
    res = {"current": current, "latest": None, "available": False, "channel": channel, "notes_url": None}
    try:
        if origin_problem(code) is None:
            if channel == "main":
                sha, head = _remote_main(code), _head(code)
                if sha:
                    res.update(latest=sha, available=bool(head) and sha != head,
                               notes_url=release_notes_url(code))
            else:
                tag = _remote_latest_tag(code)
                if tag:
                    res.update(latest=tag, available=version.newer(current, tag),
                               notes_url=release_notes_url(code, tag))
        upd = {"checked_at": _now()}
        if res["latest"] is not None:
            upd["latest"] = res["latest"]
        save_settings(upd)
    except OSError:
        pass
    return res


def refuse_reason(code: Path) -> "str | None":
    if not paths.is_installed():
        return "This is a development checkout. Update it with git."
    r = _git(code, "status", "--porcelain")
    if r.returncode != 0:
        return f"The app folder {code} is not a readable git checkout ({_why(r)})."
    changed = [line[3:] for line in r.stdout.splitlines() if line.strip()]
    if changed:
        more = f" and {len(changed) - 5} more" if len(changed) > 5 else ""
        return (f"The app folder has local changes: {', '.join(changed[:5])}{more}. "
                f"Leo Bot will not overwrite them. Undo them, then run: leobot update")
    return None


# ---------------------------------------------------------------- the lock

def _lock_path() -> Path:
    return paths.RUN / LOCK_NAME


@contextlib.contextmanager
def _update_lock():
    """Yields True when this process holds the update lock, False when another update runs."""
    paths.RUN.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(_lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def is_updating() -> bool:
    try:
        fd = os.open(_lock_path(), os.O_RDONLY)
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


# ------------------------------------------------------------ server helpers

def _cli():
    """leobot.py's helpers: the running __main__ when that is leobot.py, else an import."""
    main = sys.modules.get("__main__")
    if Path(getattr(main, "__file__", "") or "").name == "leobot.py" and hasattr(main, "_launch"):
        return main
    import leobot
    return leobot


def _service_installed(cli) -> bool:
    try:
        return bool(cli.service.installed())
    except Exception:  # noqa: BLE001
        return False


def _service_port(cli):
    try:
        sp = cli.service.port() if _service_installed(cli) else None
    except Exception:  # noqa: BLE001
        return None
    return sp if isinstance(sp, int) and not isinstance(sp, bool) else None


def _stop_server(cli):
    """Stop the server. With a service installed the service is always stopped, even when the
    server is not up this instant, since KeepAlive / Restart=always would bring it back in the
    middle of a backup or checkout. Returns the port it ran on, or None when it was not
    running. Raises SystemExit when it cannot be stopped."""
    if _service_installed(cli):
        info = cli.status_info()
        if cli.cmd_stop() != 0:
            raise SystemExit("Could not stop Leo Bot. Nothing was changed.")
        return (info.get("port") or cli.DEFAULT_PORT) if info.get("running") else None
    return cli._stop_if_running()


def _wait_healthy(cli, port, want, proc=None) -> bool:
    """Poll healthz (with proof) until it reports `want` (any version when None)."""
    end = time.monotonic() + HEALTH_TIMEOUT
    while True:
        try:
            info = cli.status_info()
        except Exception:  # noqa: BLE001
            info = {}
        body = cli._healthz(info.get("port") or port)
        if body is not None and (want is None or body.get("version") == want):
            return True
        if proc is not None and proc.poll() is not None:
            return False
        if time.monotonic() >= end:
            return False
        time.sleep(POLL)


def _start(cli, port, want):
    try:
        proc = cli._launch(port)
    except Exception as e:  # noqa: BLE001
        _log(f"Could not start Leo Bot ({e}).", err=True)
        return False, None
    return _wait_healthy(cli, port, want, proc), proc


def _stop(cli, proc=None) -> bool:
    try:
        _stop_server(cli)
        ok = True
    except SystemExit as e:
        _log(str(e), err=True)
        ok = False
    except Exception as e:  # noqa: BLE001
        _log(f"Could not stop Leo Bot ({e}).", err=True)
        ok = False
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    return ok


def _schema():
    return migrations.read_marker(paths.DATA).get("schema")


def _data_changed(before: dict) -> bool:
    """True when the data may no longer suit the previous code: a migration step was under
    way (even one cut short) or the marker differs from the one at backup time."""
    now = migrations.read_marker(paths.DATA)
    return "migrating" in now or now != before


def _archive_schema(archive: Path):
    try:
        with tarfile.open(archive) as t:
            f = t.extractfile(migrations.MARKER)
            rec = json.loads(f.read().decode()) if f else {}
    except (OSError, KeyError, ValueError, tarfile.TarError):
        return None
    return rec.get("schema") if isinstance(rec, dict) else None


def _confirm(question: str, yes: bool) -> bool:
    if yes:
        return True
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _prune() -> None:
    """Keep the newest KEEP_PRE update backups (pre-<version>-<time>). Nothing else is touched."""
    found = []
    for p in paths.BACKUPS.iterdir():
        m = _PRE.fullmatch(p.name)
        if m and p.is_file() and not p.is_symlink():
            found.append((m.group(2), p.name, p))
    found.sort(reverse=True)
    for _, name, p in found[KEEP_PRE:]:
        try:
            p.unlink()
            _log(f"Removed old update backup {name}")
        except OSError as e:
            _log(f"Could not remove old update backup {name} ({e})", err=True)


def _ensure_app(code: Path) -> None:
    """On macOS, rebuild ~/Applications/LeoBot.app when its sources changed."""
    if sys.platform != "darwin":
        return
    if not (Path.home() / "Applications" / "LeoBot.app").exists():
        return
    script = Path(code) / "scripts" / "ensure-app.sh"
    if not script.is_file():
        return
    _log("Rebuilding the Mac app if it changed")
    try:
        r = subprocess.run(["/bin/sh", str(script)], cwd=str(code), capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=600)
    except (OSError, subprocess.SubprocessError) as e:
        _log(f"Mac app rebuild did not run ({e}). Run scripts/build-app.sh by hand.", err=True)
        return
    if r.returncode != 0:
        _log(f"Mac app rebuild failed ({_why(r)}). Run scripts/build-app.sh by hand.", err=True)


# --------------------------------------------------------------------- apply

def apply(target: "str | None" = None, *, channel: "str | None" = None, yes: bool = True) -> int:
    """`leobot update`. Returns 0 when updated or already up to date, 1 otherwise."""
    code = paths.CODE
    reason = refuse_reason(code)
    if reason:
        _log(reason, err=True)
        return 1
    with _update_lock() as mine:
        if not mine:
            _log("An update is already running. See " + str(paths.UPDATE_LOG), err=True)
            return 1
        return _apply(_cli(), code, target, channel, yes)


def _apply(cli, code: Path, target, channel, yes) -> int:
    problem = origin_problem(code)
    if problem:
        _log(problem, err=True)
        return 1
    channel = channel or settings()["channel"]
    if channel not in CHANNELS:
        _log(f"Unknown channel {channel!r}. Use stable or main.", err=True)
        return 1
    current = _version_file(code)
    head = _head(code)
    if not head:
        _log("Could not read the current version of the app folder.", err=True)
        return 1

    # 2-3. resolve the target
    if target is not None:
        tag = _tag_of(target)
        if not tag:
            _log(f"{target!r} is not a release version like v1.2.3.", err=True)
            return 1
        found = _remote_has_tag(code, tag)
        if found is None:
            _log("Could not reach origin to look for that version. Nothing was changed.", err=True)
            return 1
        if not found:
            _log(f"There is no release {tag}. Nothing was changed.", err=True)
            return 1
        if version.parse(tag) == version.parse(current):
            _log(f"Leo Bot is up to date ({current}).")
            return 0
    elif channel == "stable":
        tag = _remote_latest_tag(code)
        if not tag:
            _log("Could not reach origin to check for updates. Nothing was changed.", err=True)
            return 1
        if not version.newer(current, tag):
            _log(f"Leo Bot is up to date ({current}).")
            return 0
    else:
        tag = None
        sha = _remote_main(code)
        if not sha:
            _log("Could not reach origin to check for updates. Nothing was changed.", err=True)
            return 1
        if sha == head:
            _log(f"Leo Bot is up to date ({current}, main).")
            return 0

    # 6a. download first: a failure here leaves the running server alone
    _log("Downloading the update from origin")
    fetch = ["fetch", "--quiet", "--tags", "--force", "--no-recurse-submodules", "origin"]
    if tag is None:
        fetch.append("+refs/heads/main:refs/remotes/origin/main")
    r = _git(code, *fetch)
    if r.returncode != 0:
        _log(f"Could not download the update ({_why(r)}). Leo Bot stays on {current}.", err=True)
        return 1
    if tag is not None:
        if not (_TAG.fullmatch(tag) and version.parse(tag)):  # stable only ever checks out vX.Y.Z
            _log(f"Refusing to check out {tag!r}: not a release tag.", err=True)
            return 1
        ref = f"refs/tags/{tag}^{{commit}}"
        commit = _commit(code, f"refs/tags/{tag}")
        label = tag
    else:
        commit = _commit(code, "refs/remotes/origin/main")
        ref = commit
        label = f"main-{commit[:12]}" if commit else "main"
    if not commit:
        _log(f"The download did not contain {label}. Leo Bot stays on {current}.", err=True)
        return 1
    want = _version_at(code, commit)
    if not want:
        _log(f"{label} has no VERSION file. Leo Bot stays on {current}.", err=True)
        return 1
    if not _confirm(f"Update Leo Bot {current} to {want} ({label})? Your data is backed up first.", yes):
        _log("Update cancelled.", err=True)
        return 1
    _log(f"Updating Leo Bot {current} to {want} ({label})")
    old_prev = settings()["previous_ref"]
    try:  # remembered before anything is stopped or changed
        save_settings({"previous_ref": head})
    except OSError as e:
        _log(f"Could not save the settings ({e}). Nothing was changed.", err=True)
        return 1

    # 5. stop, then 4. back up (stopped first, so the backup is consistent)
    _log("Stopping Leo Bot")
    try:
        port = _stop_server(cli)
    except SystemExit as e:
        _log(f"{e}", err=True)
        _restore_prev(old_prev)
        return 1
    resume = port is not None or _service_installed(cli)
    port = port or _service_port(cli) or cli.DEFAULT_PORT
    step = "Preparing the home folder"
    try:
        paths.ensure_dirs()
        step = "Reading the data marker"
        marker_before = migrations.read_marker(paths.DATA)
        step = "Backing up your data"
        _log(step)
        pre = cli._tar_data(paths.BACKUPS / f"pre-{label}-{_stamp()}.tar.gz")
        _log(f"Backup saved: {pre}")
        step = "Removing old update backups"
        _prune()
    except Exception as e:  # noqa: BLE001
        _log(f"{step} failed ({e}). Nothing was changed.", err=True)
        _restore_prev(old_prev)
        if resume:
            _start(cli, port, current)
        return 1

    # 6b. check out; from here on any failure goes back to the previous version
    ctx = dict(label=label, head=head, current=current, pre=pre, marker_before=marker_before, port=port,
               old_prev=old_prev)
    proc = None
    try:
        _log(f"Checking out {label}")
        r = _git(code, "checkout", "--quiet", "--detach", ref)
        if r.returncode != 0:
            return _recover(cli, code, proc=None, why=f"Could not check out {label} ({_why(r)})", **ctx)
        # 7. Mac app, 8. start (the server migrates the data before serving), 9. health
        _ensure_app(code)
        _log(f"Starting Leo Bot {want}")
        ok, proc = _start(cli, port, want)
    except Exception as e:  # noqa: BLE001
        return _recover(cli, code, proc=proc, why=f"The update failed ({e})", **ctx)
    if not ok:
        return _recover(cli, code, proc=proc,
                        why=f"Leo Bot {want} did not answer with its version within {HEALTH_TIMEOUT} s",
                        **ctx)
    _log(f"Leo Bot is updated to {want}. Backup from before the update: {pre}")
    return 0


def _restore_prev(old_prev) -> None:
    """Put back the previous_ref from before an update or rollback that did not happen."""
    try:
        save_settings({"previous_ref": old_prev})
    except OSError as e:
        _log(f"Could not restore previous_ref in {paths.SETTINGS} ({e}).", err=True)


def _checkout_back(code: Path, head: str) -> bool:
    r = _git(code, "checkout", "--quiet", "--force", "--detach", head)
    if r.returncode != 0:
        _log(f"Could not check out the previous version ({_why(r)}).", err=True)
        return False
    return True


def _recover(cli, code, *, label, head, current, pre, marker_before, port, proc, why, old_prev) -> int:
    """Step 10: stop the new server, put the data and the previous code back, start it."""
    _log(f"{why}. Going back to {current}.", err=True)
    if not _stop(cli, proc):
        _log(f"Could not stop the new server, so nothing else was changed. Stop it, then run: "
             f"leobot update --rollback. Backup from before the update: {pre}", err=True)
        return 1
    failed = None
    if _data_changed(marker_before):
        _log("The data format changed: saving the current data, then restoring the backup "
             "from before the update")
        try:
            failed = cli._replace_data(pre, paths.BACKUPS / f"failed-{label}-{_stamp()}.tar.gz")
        except cli.RestoreFailed as e:
            # The data stays in the newer format, so the older code must not open it.
            _log(f"{e}", err=True)
            if not e.data_back:
                _log(f"Leo Bot is stopped and stays on {label}. First put your data back with: "
                     f"{cli._put_back_command(e.aside)}", err=True)
            _log(f"Leo Bot is stopped and stays on {label}, so the older version never opens the "
                 f"newer data. To go back by hand, run: leobot backup --restore {pre} --yes ; then: "
                 f"git -C {code} checkout --force --detach {head} ; then: leobot start", err=True)
            _log(f"Backup from before the update: {pre}", err=True)
            return 1
    if not _checkout_back(code, head):
        if failed:
            _log(f"Leo Bot is stopped: your data is back to how it was before the update, but the "
                 f"newer code is still checked out. Run: git -C {code} checkout --force --detach "
                 f"{head} ; then: leobot start", err=True)
        else:
            _log(f"Leo Bot is stopped. Run: git -C {code} checkout --force --detach {head} ; "
                 f"then: leobot start", err=True)
        _log(f"Backup from before the update: {pre}", err=True)
        if failed:
            _log(f"Data at the time of the failed update: {failed}", err=True)
        return 1
    _restore_prev(old_prev)  # the update did not happen
    if failed:
        try:
            cli._reindex()
        except Exception as e:  # noqa: BLE001
            _log(f"Re-index failed ({e}). Start Leo Bot to rebuild it.", err=True)
    _ensure_app(code)
    ok, _ = _start(cli, port, current)
    if ok:
        _log(f"Leo Bot {current} is running again.", err=True)
    else:
        _log(f"Leo Bot {current} did not start. See {paths.SERVER_LOG}", err=True)
    _log(f"Backup from before the update: {pre}", err=True)
    if failed:
        _log(f"Data at the time of the failed update: {failed}", err=True)
    else:
        _log("The data format did not change, so your data was kept as it is.", err=True)
    return 1


# ------------------------------------------------------------------ rollback

def _release_tags(code: Path, below) -> list:
    """(parsed, tag) for local vX.Y.Z tags lower than `below`."""
    r = _git(code, "tag", "--list", "v*")
    if r.returncode != 0 or below is None:
        return []
    out = []
    for t in r.stdout.split():
        v = version.parse(t) if _TAG.fullmatch(t) else None
        if v and v < below:
            out.append((v, t))
    return out


def _previous_release(code: Path, current: str) -> "str | None":
    """Highest local vX.Y.Z tag lower than `current`."""
    tags = _release_tags(code, version.parse(current))
    return max(tags)[1] if tags else None


def _rollback_target(code: Path, current: str, head: str):
    """(commit, ref) to go back to, or (None, None). previous_ref wins when it is a release
    below the current one (stable) or simply set (main); else the highest lower release tag."""
    s = settings()
    prev = s["previous_ref"]
    prev_commit = _commit(code, prev) if isinstance(prev, str) and _SHA.fullmatch(prev) else None
    if prev_commit and prev_commit != head:
        if s["channel"] == "main":
            return prev_commit, prev_commit
        for _, t in sorted(_release_tags(code, version.parse(current)), reverse=True):
            if _commit(code, f"refs/tags/{t}") == prev_commit:
                return prev_commit, f"refs/tags/{t}^{{commit}}"
    tag = _previous_release(code, current)
    commit = _commit(code, f"refs/tags/{tag}") if tag else None
    if commit and commit != head:
        return commit, f"refs/tags/{tag}^{{commit}}"
    if prev_commit and prev_commit != head:
        if s["channel"] == "main":
            return prev_commit, prev_commit
        pv, cv = version.parse(_version_at(code, prev_commit) or ""), version.parse(current)
        if pv is not None and cv is not None and pv < cv:  # stable never "rolls back" forward
            return prev_commit, prev_commit
    return None, None


def _newest_pre(labels) -> "Path | None":
    found = []
    for p in paths.BACKUPS.glob("pre-*.tar.gz"):
        m = _PRE.fullmatch(p.name)
        if m and m.group(1) in labels and p.is_file():
            found.append((m.group(2), p))
    return max(found)[1] if found else None


def rollback(yes: bool) -> int:
    """`leobot update --rollback`: back to the previous release (see _rollback_target)."""
    code = paths.CODE
    reason = refuse_reason(code)
    if reason:
        _log(reason, err=True)
        return 1
    with _update_lock() as mine:
        if not mine:
            _log("An update is already running. See " + str(paths.UPDATE_LOG), err=True)
            return 1
        return _rollback(_cli(), code, yes)


def _rollback(cli, code: Path, yes: bool) -> int:
    current = _version_file(code)
    head = _head(code)
    if not head:
        _log("Could not read the current version of the app folder.", err=True)
        return 1
    commit, ref = _rollback_target(code, current, head)
    if not commit:
        _log("There is no earlier version to go back to.", err=True)
        return 1
    old_version = _version_at(code, commit)
    old = old_version or commit[:12]
    pre = _newest_pre({f"v{current}", f"main-{head[:12]}"})
    pre_schema = _archive_schema(pre) if pre else None
    restore = pre is not None and pre_schema is not None and pre_schema != _schema()
    if restore:
        q = (f"Going back to {old} needs the data from before the update ({pre.name}), since the "
             f"older version cannot read the newer data. Your current data is saved first. "
             f"Continue?")
        if not _confirm(q, yes):
            _log("Rollback cancelled.", err=True)
            return 1
    elif pre is None:
        _log(f"No backup from before {current} was found, so the data is kept as it is.", err=True)
    _log(f"Rolling back Leo Bot {current} to {old}")
    old_prev = settings()["previous_ref"]
    try:
        save_settings({"previous_ref": head})
    except OSError as e:
        _log(f"Could not save the settings ({e}). Nothing was changed.", err=True)
        return 1
    try:
        port = _stop_server(cli)
    except SystemExit as e:
        _log(f"{e}", err=True)
        _restore_prev(old_prev)
        return 1
    resume = port is not None or _service_installed(cli)
    port = port or _service_port(cli) or cli.DEFAULT_PORT
    back = dict(cli=cli, code=code, head=head, current=current, port=port, old_prev=old_prev)
    snap = None
    proc = None
    try:
        paths.ensure_dirs()
        r = _git(code, "checkout", "--quiet", "--detach", ref)
        if r.returncode != 0:
            _log(f"Could not check out {old} ({_why(r)}). Leo Bot stays on {current}.", err=True)
            return _back_to_current(start=resume, **back)
        if restore:
            try:
                snap = cli._replace_data(pre, paths.BACKUPS / f"rollback-v{current}-{_stamp()}.tar.gz")
            except cli.RestoreFailed as e:
                _log(f"{e} Going back to {current}.", err=True)
                if not e.data_back:
                    return _back_to_current(start=False, put_back=e.aside, **back)
                return _back_to_current(start=True, **back)
            try:
                cli._reindex()
            except Exception as e:  # noqa: BLE001
                _log(f"Re-index failed ({e}). Start Leo Bot to rebuild it.", err=True)
        _ensure_app(code)
        ok, proc = _start(cli, port, old_version)
    except Exception as e:  # noqa: BLE001
        _log(f"The rollback failed ({e}).", err=True)
        ok = False
    if ok:
        if snap:
            _log(f"Your data from {current} was saved: {snap}")
        _log(f"Leo Bot is back on {old}.")
        return 0
    # The older version did not come up: go back to the current code (and its data).
    _log(f"Leo Bot {old} did not start (see {paths.SERVER_LOG}). Going back to {current}.", err=True)
    if not _stop(cli, proc):
        _log(f"Could not stop Leo Bot {old}, so nothing else was changed. Stop it, then run: "
             f"git -C {code} checkout --force --detach {head}"
             + (f" ; then: leobot backup --restore {snap} --yes" if snap else "")
             + " ; then: leobot start", err=True)
        return 1
    if snap:
        try:
            cli._replace_data(snap, paths.BACKUPS / f"rollback-failed-{old}-{_stamp()}.tar.gz")
            _log(f"Your data from {current} is back (from {snap}).", err=True)
        except cli.RestoreFailed as e:
            _log(f"{e}", err=True)
            if not e.data_back:
                return _back_to_current(start=False, put_back=e.aside, **back)
            _log(f"Your data from {current} could not be put back; it is saved in {snap}. Restore "
                 f"it with: leobot backup --restore {snap} --yes", err=True)
    return _back_to_current(start=True, **back)


def _back_to_current(*, cli, code: Path, head: str, current: str, port, old_prev, start: bool,
                     put_back=None) -> int:
    """A rollback that did not happen: check the current code out again and, unless told not
    to, start it. `put_back`: DATA is missing and the old folder is there; never start then."""
    _restore_prev(old_prev)
    if not _checkout_back(code, head):
        _log(f"Leo Bot is stopped. Run: git -C {code} checkout --force --detach {head}"
             + (f" ; then: {cli._put_back_command(put_back)}" if put_back else "")
             + " ; then: leobot start", err=True)
        return 1
    _ensure_app(code)
    if put_back:
        _log(f"Leo Bot is stopped, since your data folder is missing. Put it back with: "
             f"{cli._put_back_command(put_back)} ; then: leobot start", err=True)
        return 1
    if start:
        ok, _ = _start(cli, port, current)
        _log(f"Leo Bot {current} is running again." if ok
             else f"Leo Bot {current} did not start. See {paths.SERVER_LOG}", err=True)
    return 1


# ------------------------------------------------------------------ detached

def spawn_detached() -> None:
    """Start `leobot update --yes` outside the server's process tree, so stopping the server
    does not stop the update. Output goes to the update log. Raises RuntimeError on failure."""
    if is_updating():
        raise RuntimeError("An update is already running.")
    paths.ensure_dirs()
    log = str(paths.UPDATE_LOG)
    home = str(paths.HOME)
    cmd = [sys.executable, str(paths.CODE / "leobot.py"), "update", "--yes"]
    kind = service.kind() if service.installed() else None
    if kind == "systemd":
        argv = ["systemd-run", "--user", "--unit", UNIT, "--collect", "--quiet",
                f"--setenv=LEOBOT_HOME={home}",
                f"--property=StandardOutput=append:{log}", f"--property=StandardError=append:{log}",
                *cmd]
        _must(argv, "systemd-run")
    elif kind == "launchd":
        _run(["launchctl", "remove", JOB])  # a finished job from last time; not there is fine
        # A submitted job is restarted when it exits, so the job removes itself when done.
        line = " ".join(shlex.quote(a) for a in ["/usr/bin/env", f"LEOBOT_HOME={home}", *cmd])
        script = f"{line} >> {shlex.quote(log)} 2>&1; /bin/launchctl remove {JOB}"
        _must(["launchctl", "submit", "-l", JOB, "--", "/bin/sh", "-c", script], "launchctl submit")
    else:
        fd = os.open(paths.UPDATE_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=fd, stderr=fd, cwd=str(paths.CODE),
                             env={**os.environ, "LEOBOT_HOME": home}, start_new_session=True)
        except OSError as e:
            raise RuntimeError(f"could not start the update ({e})") from e
        finally:
            os.close(fd)


def _run(argv) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(argv, 127, "", str(e))


def _must(argv, what) -> None:
    r = _run(argv)
    if r.returncode != 0:
        raise RuntimeError(f"{what} failed: {_why(r)}")
