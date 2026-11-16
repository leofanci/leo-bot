"""shell_rules.py: what a bot's shell may run (a bot on Full access, dashboard/engines/access).

Two walls, checked by the taint gate (dashboard/taint.py) the moment a command would run:

  1. Never, whatever the level or the conversation: commands that wreck the machine. A
     recursive delete of the disk root, the home folder or a system folder; formatting or
     erasing a disk; writing raw to a disk device; a fork bomb; killing every process;
     shutting down. A command Leo Bot cannot read (unbalanced quotes) is refused too.
  2. Once the conversation holds outside content or app data (taint.Turn.outside): commands
     that send things off this machine (curl, wget, ssh, scp, rsync, nc, git push, a package
     publish, gh, mail). A page or a mail could tell the bot to ship private data out.

The names in wall 2 are a first check only: a program the bot writes could still open a socket.
So once outside content is in, an allowed command also runs inside the operating system's
sandbox (`sandboxed`, macOS sandbox-exec): no network but this machine's own (localhost), no
DNS, writes only in the bot's edit folders and the temp folders (never a .git hook or config),
and no way to hand work to a process outside it (launchd, Apple events, open, osascript,
crontab). Codex has its own sandbox for this (dashboard/engines/codex.py). A Mac without
sandbox-exec, or another system, keeps wall 2 alone. Before outside content, Full access means
the bot runs commands as the owner does.

A third wall while the run has a browser (its level above off, taint.Turn.browsing): the
browser relay is the only way a bot reaches a browser, so every command runs sandboxed from the
first one, outside content or not, and the sandbox also denies the browser's back doors
(`browser_walls`): the Chrome debug port (each DevToolsActivePort port, and 9222), the engine's
folder ~/.agent-browser (its session sockets and saved logins: no read, write or connect) and
Leo Bot's <data>/run (the engine key, the typed text). Before outside content those ports are
denied one by one; after it every localhost port is, since sandbox-exec cannot deny one port
on top of an allowed localhost (probed on macOS 27: the deny then covers every port). With no
sandbox, a browser level that can send (write, act) refuses the command (and server.py refuses
the turn up front); at Reads only the command runs as before.
"""
import hashlib
import os
import posixpath
import re
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

# Words that run the next word as the command.
_WRAPPERS = {"sudo", "nohup", "env", "command", "exec", "time", "nice", "ionice", "caffeinate", "doas"}
_SEPARATORS = {";", "&&", "||", "|", "&", "\n"}
_SYSTEM = {"/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib64", "/opt", "/private", "/sbin",
           "/usr", "/var", "/system", "/library", "/applications", "/users", "/volumes", "/cores", "/root",
           "/srv", "/mnt", "/proc", "/sys"}
_HOME = re.compile(r"^(~|\$home|\$\{home\})(/.*)?$")
_FORK = re.compile(r":\s*\(\s*\)\s*\{[^}]*:\s*\|\s*:")
_RAW_DISK = re.compile(r">\s*/dev/(r?disk\d|sd[a-z]|nvme\d|hd[a-z]|xvd[a-z]|vd[a-z]|mmcblk\d)")
_SENDS = {"curl", "wget", "ssh", "scp", "sftp", "rsync", "ftp", "telnet", "nc", "ncat", "netcat", "socat",
          "mail", "mailx", "sendmail", "gh", "aria2c", "httpie", "http", "https", "lftp", "smbclient"}
_PUBLISH = {"npm", "pnpm", "yarn", "twine", "cargo", "gem", "poetry", "flit", "docker", "podman"}

NEVER = "This command could wreck the machine, so Leo Bot never runs it."
UNREADABLE = "Leo Bot could not read this command (unbalanced quotes): write it plainly."
NO_SANDBOX = ("This bot's browser can send, and this machine has no sandbox to keep shell commands away from "
              "the browser, so shell commands are off while it browses. Ask the owner to set the browser to "
              "Reads only or Off, or the shell level lower.")
CHROME_DEBUG_PORT = 9222  # Chrome's usual --remote-debugging-port
SENDS_OFF = ("This conversation holds outside content or app data, so commands that send things off "
             "this machine (curl, wget, ssh, scp, rsync, git push, a publish, gh, mail) are off. Do the "
             "local work; tell the owner what to push or send.")


def _segments(command):
    """The command's simple commands, as word lists. ValueError when it cannot be read."""
    flat = re.sub(r"\$\{(\w+)\}", r"$\1", command)  # ${HOME} is $HOME, not a block
    flat = re.sub(r"\$\(|`|[(){}]", " ; ", flat)
    lex = shlex.shlex(flat, posix=True, punctuation_chars=";&|")
    lex.whitespace_split = True
    lex.commenters = ""
    words, out = [], []
    for tok in lex:
        if tok in _SEPARATORS or set(tok) <= set(";&|"):
            if words:
                out.append(words)
            words = []
        else:
            words.append(tok)
    if words:
        out.append(words)
    return out


def _program(words):
    """(program name, its arguments), past wrappers and VAR=value prefixes."""
    i = 0
    while i < len(words):
        w = words[i]
        base = posixpath.basename(w).lower()
        if base in _WRAPPERS or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w):
            i += 1
            while base in ("sudo", "doas", "nice", "ionice", "env") and i < len(words) and words[i].startswith("-"):
                i += 1  # its own options
            continue
        return base, words[i + 1:]
    return "", []


def _fatal_target(path):
    p = path.strip().lower()
    if _HOME.match(p):
        rest = (_HOME.match(p).group(2) or "/").rstrip("/*")
        return rest in ("", "/", "/.")
    if not p.startswith("/"):
        return False
    norm = posixpath.normpath(re.sub(r"/+", "/", p))
    norm = norm[:-2] if norm.endswith("/*") else norm
    norm = posixpath.normpath(norm) if norm else "/"
    home = posixpath.normpath(os.path.expanduser("~").lower())
    return norm in _SYSTEM or norm == home or norm.startswith("//")


def _recursive(args):
    for a in args:
        if a == "--":
            break
        if a in ("--recursive", "-R") or (a.startswith("-") and not a.startswith("--") and "r" in a.lower()):
            return True
    return False


def _never(prog, args):
    if prog == "rm" and _recursive(args) and any(_fatal_target(a) for a in args if not a.startswith("-")):
        return True
    if prog in ("chmod", "chown", "chgrp") and _recursive(args) and \
            any(_fatal_target(a) for a in args if not a.startswith("-")):
        return True
    if prog.startswith("mkfs") or prog in ("fdisk", "sfdisk", "parted", "wipefs", "newfs_apfs", "newfs_hfs"):
        return True
    if prog == "diskutil" and args and args[0].lower() in (
            "erasedisk", "erasevolume", "partitiondisk", "zerodisk", "randomdisk", "secureerase", "reformat"):
        return True
    if prog == "dd" and any(a.lower().startswith("of=/dev/") for a in args):
        return True
    if prog in ("shutdown", "reboot", "halt", "poweroff"):
        return True
    if prog in ("kill", "pkill") and "-1" in args:
        return True
    return False


def _sends(prog, args):
    if prog in _SENDS:
        return True
    low = [a.lower() for a in args]
    if prog == "git" and any(a in ("push", "send-email", "request-pull") for a in low):
        return True
    if prog in _PUBLISH and any(a in ("publish", "upload", "push", "login") for a in low):
        return True
    if prog == "open" and any(a.startswith(("http:", "https:", "mailto:")) for a in low):
        return True
    return False


SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# Programs that hand work to a process outside the sandbox: it would run unconfined later.
_HANDOFF_PROGRAMS = ("/usr/bin/open", "/usr/bin/osascript", "/bin/launchctl", "/usr/bin/crontab",
                     "/usr/bin/at", "/usr/bin/batch", "/usr/bin/automator", "/usr/bin/shortcuts")


def _sb_str(path):
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def chrome_debug_ports(home=None, platform=sys.platform):
    """Every port a Chrome DevToolsActivePort names in the standard user data dirs (the ones Leo
    Bot attaches to, dashboard/browser_upstream.CHROME_DIRS), plus 9222. A stale file counts too."""
    from dashboard.browser_upstream import CHROME_DIRS
    home = Path.home() if home is None else Path(home)
    key = "darwin" if platform == "darwin" else "linux" if platform.startswith("linux") else None
    ports = {CHROME_DEBUG_PORT}
    for rel in CHROME_DIRS.get(key, ()):
        try:
            first = (home / rel / "DevToolsActivePort").read_text().splitlines()[0].strip()
        except (OSError, UnicodeDecodeError, ValueError, IndexError):
            continue
        if first.isascii() and first.isdecimal() and 0 < int(first) < 65536:
            ports.add(int(first))
    return sorted(ports)


def browser_walls(root, required=False, home=None, platform=sys.platform):
    """What a run with a browser keeps its shell from (module notes): {"ports", "paths",
    "required"}. `required`: the browser can send, so a command with no sandbox is refused."""
    home = Path.home() if home is None else Path(home)
    paths = sorted({os.path.realpath(str(home / ".agent-browser")), os.path.realpath(str(Path(root) / "run"))})
    return {"ports": chrome_debug_ports(home, platform), "paths": paths, "required": bool(required)}


def _browser_denies(browser):
    lines = []
    for p in browser.get("paths") or ():
        lines.append(f"(deny file-read* file-write* (subpath {_sb_str(p)}))")
        lines.append(f"(deny network-outbound (remote unix-socket (subpath {_sb_str(p)})))")
    return lines


def sandbox_profile(writable=(), browser=None, outside=True):
    """The sandbox-exec profile for a command run after outside content (see the module notes).
    `browser` (browser_walls): also keep the command from the browser; with `outside` False the
    profile is that wall alone (no other limit before outside content)."""
    handoff = ["(deny appleevent-send)",
               "(deny process-exec " + " ".join(f"(literal {_sb_str(p)})" for p in _HANDOFF_PROGRAMS) + ")"]
    if browser is not None and not outside:
        ports = [f'(deny network-outbound (remote ip "*:{int(port)}"))' for port in browser.get("ports") or ()]
        return "".join(["(version 1)", "(allow default)"] + ports + handoff + _browser_denies(browser))
    temps = {os.path.realpath(p) for p in (tempfile.gettempdir(), "/tmp", "/private/tmp", "/private/var/folders")
             if p and os.path.isdir(p)}
    dirs = sorted({os.path.realpath(str(d)) for d in writable if d} | temps)
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny network-outbound (remote ip))",
    ]
    if browser is None:  # with a browser no localhost port either: one port cannot be denied here
        lines.append('(allow network-outbound (remote ip "localhost:*"))')
    lines += [
        '(deny network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))',
        *handoff,
        "(deny file-write*)",
        "(allow file-write* " + " ".join(f"(subpath {_sb_str(d)})" for d in dirs)
        + ' (literal "/dev/null") (literal "/dev/tty") (literal "/dev/dtracehelper") (subpath "/dev/fd"))',
        '(deny file-write* (regex #"/\\.git/(hooks|config$)"))',
    ]
    if browser is not None:
        lines += _browser_denies(browser)
    return "".join(lines)


def can_sandbox():
    """This system can run a command inside sandbox-exec."""
    return bool(os.path.exists(SANDBOX_EXEC) and shutil.which(SANDBOX_EXEC))


def _profile_file(profile, profile_dir):
    """The profile written (again, each time) to a file in `profile_dir`, a folder a sandboxed
    command cannot write: the wrapped command stays short enough for an engine's own reviewer
    to judge. None when it cannot be written."""
    try:
        os.makedirs(profile_dir, mode=0o700, exist_ok=True)
        path = os.path.join(profile_dir, hashlib.sha256(profile.encode()).hexdigest()[:24] + ".sb")
        fd, tmp = tempfile.mkstemp(dir=profile_dir, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(profile)
        os.replace(tmp, path)
        return path
    except OSError:
        return None


def sandboxed(command, writable=(), shell="/bin/bash", profile_dir=None, browser=None, outside=True):
    """`command` wrapped to run inside the sandbox, or None when this system has none.
    `profile_dir`: write the profile there and pass its path (a short command) instead of
    the whole profile inline. `browser`, `outside`: see sandbox_profile."""
    if not can_sandbox():
        return None
    profile = sandbox_profile(writable, browser=browser, outside=outside)
    path = _profile_file(profile, profile_dir) if profile_dir else None
    spec = f"-f {shlex.quote(path)}" if path else f"-p {shlex.quote(profile)}"
    return f"{SANDBOX_EXEC} {spec} {shell} -c {shlex.quote(command)}"


def _osctl_only(command):
    """One plain `python3 -m dashboard.osctl ...` call: Leo Bot's own command line, which keeps
    its own rules and writes Leo Bot's data, so it runs outside the sandbox."""
    if re.search(r"[;&|<>`\n]|\$\(", command):
        return False
    try:
        segments = _segments(command)
    except ValueError:
        return False
    if len(segments) != 1:
        return False
    words = segments[0]
    return (len(words) >= 3 and re.fullmatch(r"python3?(\.\d+)?", words[0]) is not None
            and words[1:3] == ["-m", "dashboard.osctl"])


def plan(command, outside=False, writable=(), profile_dir=None, browser=None):
    """(allow, reason, run): `run` is the command to run instead (sandboxed after outside
    content, or from the first command while the run has a browser: `browser`, browser_walls),
    or None to run it as written."""
    allow, reason = decide(command, outside)
    if not allow or _osctl_only(command):
        return allow, reason, None
    if browser is None:
        return (True, "", sandboxed(command, writable, profile_dir=profile_dir)) if outside else (True, "", None)
    run = sandboxed(command, writable, profile_dir=profile_dir, browser=browser, outside=outside)
    if run is None and browser.get("required"):
        return False, NO_SANDBOX, None
    return True, "", run


def decide(command, outside=False):
    """(allow, reason) for one shell command. `outside`: the conversation holds outside
    content or app data."""
    if not isinstance(command, str) or not command.strip():
        return True, ""
    if _FORK.search(command) or _RAW_DISK.search(command.lower()):
        return False, NEVER
    try:
        segments = _segments(command)
    except ValueError:
        return False, UNREADABLE
    progs = [_program(w) for w in segments]
    if any(_never(p, a) for p, a in progs):
        return False, NEVER
    if outside and any(_sends(p, a) for p, a in progs):
        return False, SENDS_OFF
    return True, ""
