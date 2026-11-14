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
"""
import hashlib
import os
import posixpath
import re
import shlex
import shutil
import tempfile

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


def sandbox_profile(writable=()):
    """The sandbox-exec profile for a command run after outside content (see the module notes)."""
    temps = {os.path.realpath(p) for p in (tempfile.gettempdir(), "/tmp", "/private/tmp", "/private/var/folders")
             if p and os.path.isdir(p)}
    dirs = sorted({os.path.realpath(str(d)) for d in writable if d} | temps)
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny network-outbound (remote ip))",
        '(allow network-outbound (remote ip "localhost:*"))',
        '(deny network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))',
        "(deny appleevent-send)",
        "(deny process-exec " + " ".join(f"(literal {_sb_str(p)})" for p in _HANDOFF_PROGRAMS) + ")",
        "(deny file-write*)",
        "(allow file-write* " + " ".join(f"(subpath {_sb_str(d)})" for d in dirs)
        + ' (literal "/dev/null") (literal "/dev/tty") (literal "/dev/dtracehelper") (subpath "/dev/fd"))',
        '(deny file-write* (regex #"/\\.git/(hooks|config$)"))',
    ]
    return "".join(lines)


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


def sandboxed(command, writable=(), shell="/bin/bash", profile_dir=None):
    """`command` wrapped to run inside the sandbox, or None when this system has none.
    `profile_dir`: write the profile there and pass its path (a short command) instead of
    the whole profile inline."""
    if not (os.path.exists(SANDBOX_EXEC) and shutil.which(SANDBOX_EXEC)):
        return None
    profile = sandbox_profile(writable)
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


def plan(command, outside=False, writable=(), profile_dir=None):
    """(allow, reason, run): `run` is the command to run instead (sandboxed after outside
    content), or None to run it as written."""
    allow, reason = decide(command, outside)
    if not allow or not outside or _osctl_only(command):
        return allow, reason, None
    return True, "", sandboxed(command, writable, profile_dir=profile_dir)


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
