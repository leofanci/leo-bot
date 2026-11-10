"""Run the server as a per-user background service: a launchd agent on macOS, a systemd
user unit on Linux. No sudo. File locations derive from Path.home() at call time."""
import os
import platform
import plistlib
import subprocess
import sys
from pathlib import Path

from core import paths

LABEL = "leobot.server"
UNIT = "leobot"
DEFAULT_PORT = 8765


def _extra_path() -> str:
    return ":".join([str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin",
                     "/usr/bin", "/bin"])


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{UNIT}.service"


def _kind():
    s = platform.system()
    return "launchd" if s == "Darwin" else "systemd" if s == "Linux" else None


def _file():
    k = _kind()
    return plist_path() if k == "launchd" else unit_path() if k == "systemd" else None


def launchd_plist(python: str, code: Path, port: int, home: Path) -> str:
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [python, str(Path(code) / "dashboard" / "server.py"), "--port", str(port)],
        "WorkingDirectory": str(code),
        "RunAtLoad": True,
        "KeepAlive": True,
        "AbandonProcessGroup": True,
        "StandardOutPath": str(paths.SERVER_LOG),
        "StandardErrorPath": str(paths.SERVER_LOG),
        "EnvironmentVariables": {"LEOBOT_HOME": str(home), "PATH": _extra_path()},
    }).decode()


def _sd_arg(s: str) -> str:
    """One ExecStart argument: double-quoted, backslash and quote escaped, % and $ doubled."""
    s = str(s).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{s}"'


def _sd_env(key: str, value: str) -> str:
    pair = f"{key}={value}"
    if not any(c in pair for c in ' \t"\'\\%$'):
        return f"Environment={pair}"
    pair = pair.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'Environment="{pair}"'


def systemd_unit(python: str, code: Path, port: int, home: Path, server: bool) -> str:
    script = str(Path(code) / "dashboard" / "server.py")
    exec_start = " ".join(_sd_arg(a) for a in (python, script, "--port", str(port)))
    lines = ["[Unit]", "Description=Leo Bot server", "",
             "[Service]",
             f"ExecStart={exec_start}",
             f"WorkingDirectory={str(code).replace('%', '%%')}",
             "Restart=always", "RestartSec=3",
             _sd_env("LEOBOT_HOME", str(home)),
             _sd_env("PATH", _extra_path())]
    if server:
        lines.append("Environment=LEOBOT_DEPLOYMENT=server")
    lines += ["NoNewPrivileges=true", "PrivateTmp=true", "",
              "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


def _run(argv):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(argv, 127, "", str(e))


def _check(r, what):
    if r.returncode != 0:
        raise RuntimeError(f"{what} failed: {(r.stderr or r.stdout or '').strip()}")


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _target() -> str:
    return f"{_domain()}/{LABEL}"


def _ctl(*args):
    return ["systemctl", "--user", *args]


def installed() -> bool:
    f = _file()
    return bool(f and f.is_file())


def _linger_hint() -> str:
    user = os.environ.get("USER", "")
    r = _run(["loginctl", "show-user", user, "-p", "Linger"])
    if r.returncode == 0 and "Linger=no" in (r.stdout or ""):
        return f" To keep it running when you are logged out: loginctl enable-linger {user}"
    return ""


def install(port: int = DEFAULT_PORT, server: bool = False) -> str:
    kind = _kind()
    if not kind:
        raise RuntimeError("Background service is supported on macOS and Linux only.")
    paths.ensure_dirs()
    f = _file()
    f.parent.mkdir(parents=True, exist_ok=True)
    if kind == "launchd":
        f.write_text(launchd_plist(sys.executable, paths.CODE, port, paths.HOME))
        f.chmod(0o644)
        _run(["launchctl", "bootout", _target()])  # already loaded makes bootstrap fail
        _check(_run(["launchctl", "bootstrap", _domain(), str(f)]), "launchctl bootstrap")
        return f"Background service installed (launchd, port {port}). Starts at login."
    f.write_text(systemd_unit(sys.executable, paths.CODE, port, paths.HOME, server))
    f.chmod(0o644)
    _check(_run(_ctl("daemon-reload")), "systemctl daemon-reload")
    _check(_run(_ctl("enable", "--now", UNIT)), "systemctl enable")
    return f"Background service installed (systemd, port {port})." + _linger_hint()


def stop() -> None:
    """Stop so the manager does not respawn it. launchd: unload (the file stays)."""
    kind = _kind()
    if kind == "launchd":
        _run(["launchctl", "bootout", _target()])  # not loaded is fine
    elif kind == "systemd":
        _run(_ctl("stop", UNIT))


def start() -> None:
    kind = _kind()
    if kind == "launchd":
        _run(["launchctl", "bootout", _target()])
        _check(_run(["launchctl", "bootstrap", _domain(), str(plist_path())]), "launchctl bootstrap")
    elif kind == "systemd":
        _check(_run(_ctl("start", UNIT)), "systemctl start")


def restart() -> None:
    kind = _kind()
    if kind == "launchd":
        if _run(["launchctl", "kickstart", "-k", _target()]).returncode != 0:
            start()
    elif kind == "systemd":
        _check(_run(_ctl("restart", UNIT)), "systemctl restart")


def uninstall() -> None:
    kind = _kind()
    f = _file()
    if not f or not f.exists():
        return
    if kind == "launchd":
        _run(["launchctl", "bootout", _target()])
        f.unlink(missing_ok=True)
    else:
        _run(_ctl("disable", "--now", UNIT))
        f.unlink(missing_ok=True)
        _run(_ctl("daemon-reload"))


def status() -> dict:
    kind = _kind()
    if not installed():
        return {"installed": False, "running": False, "kind": None}
    if kind == "launchd":
        r = _run(["launchctl", "print", _target()])
        running = r.returncode == 0 and "state = running" in (r.stdout or "")
    else:
        r = _run(_ctl("is-active", UNIT))
        running = r.returncode == 0 and (r.stdout or "").strip() == "active"
    return {"installed": True, "running": running, "kind": kind}
