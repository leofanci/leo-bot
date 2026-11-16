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
the bot runs commands as the owner does, but for Leo Bot's own code (the code wall, below).

The code wall, on every level, before and after outside content (`code_walls`): no bot command
changes Leo Bot's own code (REPO), the Python that runs osctl (`_python_dirs`) or the turns'
working folder (core.paths.TURN_CWD, where `python3 -m dashboard.osctl` starts), nor renames a
folder above them. So every allowed command but the plain osctl form runs sandboxed from the
first one: before outside content with the internet, writes and localhost as before, plus the
code denies and the hand-off denies (a hand-off would run the command outside the sandbox);
after outside content the write limit above plus the code denies. With no sandbox, a command
whose parsed target writes there (`writes_code`: a word or redirect target of a program that
can write) is refused with CODE_OFF. That check reads the command only: a program the bot
writes could still change the code, a limit the docs state.

A third wall while the run has a browser (its level above off, taint.Turn.browsing): the
browser relay is the only way a bot reaches a browser, so every command runs sandboxed from the
first one, outside content or not, and the sandbox also denies the browser's back doors
(`browser_walls`): every localhost port from the first command (a Chrome debug port, the bot
browser's own CDP port, whatever port they use), the engine's folder ~/.agent-browser (its
session sockets and saved logins: no read, write or connect), Leo Bot's <data>/run (the engine
key, the typed text) and writes to <data>/team (approvals, browser settings, the sandbox
profiles), Leo Bot's own code (REPO) and the Python that runs osctl (sys.prefix, sys.base_prefix,
the folder of sys.executable, its site-packages folders). Writes go only to the edit and temp folders from the first
command, and no folder above a walled one can be renamed (each ancestor up to the home folder
or / is denied as itself: a rename otherwise moves the folder out from under its path rule).
Before outside content the internet stays open. With no sandbox, a browser level that can send (write, act) refuses the command (and
server.py refuses the turn up front); at Reads only the command runs as before.

Every profile Leo Bot writes to a file also denies writes to that file's folder, and the
rename of each folder above it, so no command it wraps can swap the profile of the next one.

The one command that runs outside the sandbox, a plain `python3 -m dashboard.osctl ...`, is
rewritten to run Leo Bot's own osctl only: this server's own Python (sys.executable) in
isolated mode (-I: no current folder, no PYTHONPATH, no user site on sys.path) with the repo
put on sys.path explicitly, from the repo folder. A bot that writes dashboard/osctl.py into its
edit folder never gets that file run outside the sandbox. (-P needs Python 3.11; Leo Bot
supports 3.10.)
"""
import fnmatch
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
BROWSER_SHELL_OFF = ("This did not run: {bot}'s browser is set to {browser} (Settings, Browser) and its shell "
                     "to {shell} (Team), and on {engine} Leo Bot cannot keep that shell away from the browser, "
                     "so a command could send past your approvals. Set the browser to Reads only or Off, or "
                     "the shell to a level without commands, then ask again.")
NO_SANDBOX = ("This bot's browser can send, and this machine has no sandbox to keep shell commands away from "
              "the browser, so shell commands are off while it browses. Ask the owner to set the browser to "
              "Reads only or Off, or the shell level lower.")
CODE_OFF = "Leo Bot's own code and the Python that runs it are never changed by a bot. Work in your edit folder."
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
REPO = Path(__file__).resolve().parent.parent  # Leo Bot's own code: the osctl it runs
_OSCTL_PREFIX = re.compile(r"^\s*python3?(?:\.\d+)?\s+-m\s+dashboard\.osctl(?=\s|$)")
_OSCTL_RUN = ("import runpy, sys; sys.path.insert(0, {repo}); "
              "runpy.run_module('dashboard.osctl', run_name='__main__', alter_sys=True)")
# Programs that hand work to a process outside the sandbox: it would run unconfined later.
_HANDOFF_PROGRAMS = ("/usr/bin/open", "/usr/bin/osascript", "/bin/launchctl", "/usr/bin/crontab",
                     "/usr/bin/at", "/usr/bin/batch", "/usr/bin/automator", "/usr/bin/shortcuts")


def _sb_str(path):
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _python_dirs():
    """Where the Python that runs osctl lives: sys.prefix, sys.base_prefix, the folder of
    sys.executable, and its site-packages folders (site.getsitepackages, plus the user one when
    there is one), as real paths: a site-packages linked from outside the prefix counts too."""
    import site
    dirs = [sys.prefix, sys.base_prefix, os.path.dirname(os.path.realpath(sys.executable))]
    try:
        dirs += list(site.getsitepackages())
    except (AttributeError, OSError):  # some virtualenvs have no getsitepackages
        pass
    try:
        user = site.getusersitepackages()
    except (AttributeError, OSError):
        user = None
    if isinstance(user, str) and user:
        dirs.append(user)
    return sorted({os.path.realpath(d) for d in dirs if isinstance(d, str) and d})


def _ancestors(path, home):
    """The folders above `path`, up to and excluding the home folder and /: a deny on each stops
    a rename of a parent from moving a walled folder out from under its path rule."""
    out = []
    stop = {"/", os.path.realpath(str(home)), os.path.realpath(os.path.expanduser("~"))}
    cur = os.path.dirname(path)
    while cur and cur not in stop and cur != os.path.dirname(cur):
        out.append(cur)
        cur = os.path.dirname(cur)
    return out


# Files the owner's shell or git reads at its next start: a write there would run a program later,
# as the owner, outside every sandbox. Each file is denied as itself (literal), each folder whole.
STARTUP_FILES = (".zshrc", ".zshenv", ".zprofile", ".zlogin", ".bashrc", ".bash_profile", ".bash_login", ".profile",
                 ".gitconfig")
STARTUP_DIRS = ("Library/LaunchAgents", ".config/git", ".config/fish")
_WALLS_CACHE = {}


def _unfollowed(path):
    """`path` with its folders made real but its last part kept: the name of a link itself."""
    path = os.path.abspath(str(path))
    return os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))


def _link_chain(path):
    """`path` and every link hop down to the real file, each as `_unfollowed` names it."""
    out, cur = [], _unfollowed(path)
    for _ in range(40):  # a link loop ends here
        out.append(cur)
        if not os.path.islink(cur):
            break
        try:
            cur = _unfollowed(os.path.join(os.path.dirname(cur), os.readlink(cur)))
        except OSError:
            break
    return out


def code_walls(home=None):
    """What no bot command may write, on every level (module notes): {"write_paths" (Leo Bot's
    code, the Python that runs osctl, the turns' working folder, STARTUP_DIRS: sorted real paths,
    no write under them), "literals" (sys.executable and each link hop to the real binary,
    STARTUP_FILES and where they link: no write on each as itself; the folder that holds them stays
    usable, a Homebrew bin folder for one), "ancestors" (each folder above all of them, denied as
    itself: no rename), "home"}. The folders are built once per process for each (REPO, TURN_CWD,
    home, Python and its real binary); the link chains are read again on every call, so an upgrade
    that moves a link, or a startup file that becomes one, is walled at once."""
    from core import paths
    home = Path.home() if home is None else Path(home)
    key = (str(REPO), str(paths.TURN_CWD), str(home), sys.executable, os.path.realpath(sys.executable),
           _python_dirs)
    writes = _WALLS_CACHE.get(key)
    if writes is None:
        writes = _WALLS_CACHE[key] = sorted({
            os.path.realpath(str(REPO)), *_python_dirs(), os.path.realpath(str(paths.TURN_CWD)),
            *(os.path.realpath(str(home / d)) for d in STARTUP_DIRS)})
    writes = list(writes)
    literals = sorted({*_link_chain(sys.executable), *(p for n in STARTUP_FILES for p in _link_chain(home / n))})
    ancestors = sorted({a for p in writes + literals for a in _ancestors(p, home)})
    return {"write_paths": writes, "literals": literals, "ancestors": ancestors, "home": os.path.realpath(str(home))}


def _fold(path):
    """A path as the disk compares it: macOS disks ignore letter case."""
    return path.lower() if sys.platform == "darwin" else path


def in_code(path, walls=None):
    """`path` is under a code wall "write_paths" (followed to its real path), or names one of its
    "literals" (the name itself or where it leads) (code_walls)."""
    walls = walls or code_walls()
    real = _fold(os.path.realpath(str(path)))
    literals = {_fold(p) for p in walls.get("literals") or ()}
    if real in literals or _fold(_unfollowed(path)) in literals:
        return True
    return any(real == w or real.startswith(w + "/")
               for w in (_fold(p).rstrip("/") for p in walls["write_paths"]))


def holds_code(folder, walls=None):
    """`folder` is, holds or sits inside a code wall: never an edit folder or a writable root."""
    walls = walls or code_walls()
    real = _fold(os.path.realpath(str(folder))).rstrip("/") or "/"
    inner = [_fold(p) for p in [*walls["write_paths"], *(walls.get("literals") or ())]]
    return in_code(folder, walls) or any(p == real or p.startswith(real.rstrip("/") + "/") for p in inner)


def browser_walls(root, required=False, home=None, profile_dir=None):
    """What a run with a browser keeps its shell from (module notes): {"paths" (no read, write
    or socket), "write_paths" (no write), "ancestors" (no write on the folder itself: no rename),
    "home", "required"}. `required`: the browser can send, so a command with no sandbox is
    refused. Every localhost port is denied as well, and writes go only to the edit and temp
    folders (sandbox_profile)."""
    home = Path.home() if home is None else Path(home)
    real = lambda p: os.path.realpath(str(p))  # noqa: E731 - one short helper
    agent, run, team, data = real(home / ".agent-browser"), real(Path(root) / "run"), real(Path(root) / "team"), real(root)
    pdir = real(profile_dir) if profile_dir else None
    code = code_walls(home)
    writes = sorted({team, *code["write_paths"]} | ({pdir} if pdir else set()))
    walled = [team, run, agent, data] + ([pdir] if pdir else [])
    ancestors = sorted({a for p in walled for a in _ancestors(p, home)} | set(code["ancestors"]))
    return {"paths": sorted({agent, run}), "write_paths": writes, "literals": code["literals"],
            "ancestors": ancestors, "home": real(home), "required": bool(required)}


def _browser_denies(browser):
    lines = ['(deny network-outbound (remote ip "localhost:*"))']
    for p in browser.get("paths") or ():
        lines.append(f"(deny file-read* file-write* (subpath {_sb_str(p)}))")
        lines.append(f"(deny network-outbound (remote unix-socket (subpath {_sb_str(p)})))")
    return lines + _write_denies(browser)


def _write_denies(walls):
    """No write under each of `walls` "write_paths", and no write on each "ancestors" folder
    itself (no rename): the one deny builder of the code wall and the browser wall."""
    return ([f"(deny file-write* (subpath {_sb_str(p)}))" for p in walls.get("write_paths") or ()]
            + [f"(deny file-write* (literal {_sb_str(p)}))"
               for p in [*(walls.get("literals") or ()), *(walls.get("ancestors") or ())]])


def _own_denies(profile_dir, home=None):
    """The folder the profile file is written to: never writable from inside it, nor any folder
    above it renamed."""
    if not profile_dir:
        return []
    pdir = os.path.realpath(str(profile_dir))
    return [f"(deny file-write* (subpath {_sb_str(pdir)}))"] + [
        f"(deny file-write* (literal {_sb_str(a)}))" for a in _ancestors(pdir, Path.home() if home is None else home)]


def _write_limit(writable):
    """Writes only in the edit folders and the temp folders (never a .git hook or config)."""
    temps = {os.path.realpath(p) for p in (tempfile.gettempdir(), "/tmp", "/private/tmp", "/private/var/folders")
             if p and os.path.isdir(p)}
    dirs = sorted({os.path.realpath(str(d)) for d in writable if d} | temps)
    return ["(deny file-write*)",
            "(allow file-write* " + " ".join(f"(subpath {_sb_str(d)})" for d in dirs)
            + ' (literal "/dev/null") (literal "/dev/tty") (literal "/dev/dtracehelper") (subpath "/dev/fd"))',
            '(deny file-write* (regex #"/\\.git/(hooks|config$)"))']


def sandbox_profile(writable=(), browser=None, outside=True, profile_dir=None, home=None, code=None):
    """The sandbox-exec profile for a bot command (see the module notes). After outside content:
    no internet, writes only in `writable` and the temp folders. `browser` (browser_walls): also
    keep the command from the browser, write-limited from the first command; with `outside`
    False the internet stays (only localhost is denied). Neither: the command runs as the owner
    would, but for the code wall. `code` (code_walls, built when not given) is denied on every
    profile; browser_walls already holds it. `profile_dir`: where the profile file goes, denied
    to writes with every folder above it. The denies come last, so an edit folder that holds a
    walled path never opens it."""
    handoff = ["(deny appleevent-send)",
               "(deny process-exec " + " ".join(f"(literal {_sb_str(p)})" for p in _HANDOFF_PROGRAMS) + ")"]
    home = (browser or {}).get("home") if home is None else home
    lines = ["(version 1)", "(allow default)"]
    if outside:
        lines.append("(deny network-outbound (remote ip))")
        if browser is None:  # with a browser no localhost port either
            lines.append('(allow network-outbound (remote ip "localhost:*"))')
        lines.append('(deny network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))')
    lines += handoff
    if browser is not None or outside:
        lines += _write_limit(writable)
    if browser is not None:
        lines += _browser_denies(browser)
    else:
        lines += _write_denies(code_walls(home) if code is None else code)
    return "".join(lines + _own_denies(profile_dir, home))


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
    profile = sandbox_profile(writable, browser=browser, outside=outside, profile_dir=profile_dir)
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


def osctl_command(command):
    """A plain `python3 -m dashboard.osctl <args>` as Leo Bot's own osctl only (module notes),
    or None when it is not that plain form. The arguments keep their shell text."""
    m = _OSCTL_PREFIX.match(command)
    if m is None:
        return None
    code = _OSCTL_RUN.format(repo=repr(str(REPO)))
    return f"cd {shlex.quote(str(REPO))} && {shlex.quote(sys.executable)} -I -c {shlex.quote(code)}{command[m.end():]}"


def plan(command, outside=False, writable=(), profile_dir=None, browser=None, cwd=None):
    """(allow, reason, run): `run` is the command to run instead (sandboxed from the first
    command for the code wall, write-limited and offline after outside content, kept from the
    browser while the run has one: `browser`, browser_walls), or None to run it as written: no
    sandbox here, or an empty command. `cwd`: where it runs (default the first edit folder), for
    the no-sandbox code check."""
    allow, reason = decide(command, outside)
    if not allow:
        return allow, reason, None
    if not isinstance(command, str) or not command.strip():
        return True, "", None
    if _osctl_only(command):
        own = osctl_command(command)
        if own is not None:
            return True, "", own
    run = sandboxed(command, writable, profile_dir=profile_dir, browser=browser, outside=outside)
    if run is None and browser is not None and browser.get("required"):
        return False, NO_SANDBOX, None
    # The engine's own folder for the command (cwd) and the first edit folder: an engine shell
    # can keep a `cd` from an earlier command, so both are checked.
    where = {c for c in (cwd, str(writable[0]) if writable else None) if c} or {None}
    if run is None and any(writes_code(command, c) for c in where):
        return False, CODE_OFF, None
    return True, "", run


# Programs that only write through a redirect: their own words are never a write target. `git`
# counts only for a read-only subcommand, `find` only without an action that writes or runs.
_READ_ONLY = {"cat", "less", "head", "tail", "ls", "grep", "rg", "find", "wc", "stat", "file", "diff", "git",
              "cd", "pushd", "pwd", "echo", "printf"}
_GIT_READ = {"status", "log", "diff", "show", "blame"}
_FIND_WRITES = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"}


def _read_only(prog, args):
    if prog == "git":
        i = 0
        while i < len(args):
            a = args[i]
            if a in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
                i += 2
                continue
            if a.startswith("-"):
                i += 1
                continue
            return a in _GIT_READ
        return False
    if prog == "find":
        return not any(a in _FIND_WRITES for a in args)
    return prog in _READ_ONLY


def _words_and_redirects(args):
    """(words, redirect targets) of one simple command's arguments: `> f`, `>>f`, `2> f`, `&> f`
    and `x>f` all name f; `2>&1` names none."""
    words, targets, pending = [], [], False
    for a in args:
        if pending:
            targets.append(a)
            pending = False
            continue
        if ">" not in a:
            words.append(a.lstrip("<"))
            continue
        before, _, after = a.partition(">")
        before = before.rstrip("0123456789&")
        if before:
            words.append(before)
        after = after.lstrip(">|")
        if after.startswith("&"):  # a file descriptor copy
            continue
        if after:
            targets.append(after)
        else:
            pending = True
    return words, targets


def _expand(word, cwd):
    """`word` as an absolute path (~ and $HOME expanded, relative to `cwd`), links not followed,
    or None when it cannot be known: another variable, or a relative word with no folder."""
    home = os.path.expanduser("~")
    if word == "~" or word.startswith("~/"):
        word = home + word[1:]
    elif word == "$HOME" or word.startswith("$HOME/"):
        word = home + word[5:]
    if not word or "$" in word:
        return None
    if not word.startswith("/"):
        if not cwd:
            return None
        word = os.path.join(cwd, word)
    return os.path.normpath(word)


def _resolve(word, cwd):
    """`word` as a real path (`_expand`, links followed), or None when it cannot be known."""
    path = _expand(word, cwd)
    return None if path is None else os.path.realpath(path)


_GLOB = re.compile(r"[*?\[]")
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish"}
_OUTPUT_OPT = re.compile(r"^(?:--output(?:-file)?|--out|-o)(?:=|$)|^-o(?=.)")


def _glob_walled(word, cwd, walls):
    """A glob word (* ? [) that could name a code wall: it matches a walled path, one of its
    literals or a folder above them, or its fixed part already lies inside a wall."""
    path = _expand(word, cwd)
    if path is None:
        return False
    i = _GLOB.search(path).start()
    head = path[:i].rsplit("/", 1)[0]
    pattern = _fold((os.path.realpath(head) if head else "").rstrip("/") + path[len(head):])
    if head and in_code(head, walls):
        return True
    for target in [*walls["write_paths"], *(walls.get("literals") or ()), *walls["ancestors"]]:
        cur = _fold(target)
        while cur and cur != "/":
            if fnmatch.fnmatchcase(cur, pattern):
                return True
            cur = os.path.dirname(cur)
    return False


def _git_config_outside(args):
    """`git config --global|--system|--file ...` (or `git config` under a -c/-C prefix): a write to a
    git config outside the repo, which the owner's git reads later (STARTUP_FILES)."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in ("-C", "-c") else 1
    if i >= len(args) or args[i] != "config":
        return False
    rest = args[i + 1:]
    reads = {"--get", "--get-all", "--get-regexp", "--list", "-l", "--show-origin", "--show-scope"}
    return (any(a in ("--global", "--system", "--file", "-f") or a.startswith("--file=") for a in rest)
            and not any(a in reads for a in rest))


def _script_of(prog, args):
    """The command string a shell (`bash -c "..."`, `sh -lc`, `eval ...`) runs, or None."""
    if prog == "eval":
        return " ".join(args)
    if prog in _SHELLS:
        for i, a in enumerate(args):
            if a.startswith("-") and not a.startswith("--") and "c" in a[1:]:
                return args[i + 1] if i + 1 < len(args) else ""
    return None


def writes_code(command, cwd=None, _depth=0):
    """The command, as written, names a write under the code wall (code_walls): a word or a
    redirect target of a program that can write lands under a walled path, on one of its literals
    or on a folder above them. A redirect there always counts, so does an output option
    (`-o`, `--output=`) of a read-only program. A shell's `-c` string and `eval` are read again,
    a glob counts when it could match a wall, and a path attached to an option (`-oPATH`) is read
    from its first / or ~. A command that cannot be read counts too. This reads the command only,
    and one command at a time (module notes): it is the check for a system with no sandbox."""
    if _depth > 8:
        return True
    try:
        segments = _segments(command)
    except ValueError:
        return True
    walls = code_walls()
    ancestors = {_fold(p) for p in walls["ancestors"]}

    def walled(word):
        if _GLOB.search(word):
            return _glob_walled(word, cwd, walls)
        path = _expand(word, cwd)
        if path is None:
            return False
        return (_fold(os.path.realpath(path)) in ancestors or _fold(_unfollowed(path)) in ancestors
                or in_code(path, walls))

    def candidates(w):
        out = [w]
        if "=" in w:
            out.append(w.split("=", 1)[1])
        if w.startswith("-"):
            cut = [i for i in (w.find("/"), w.find("~")) if i > 0]
            if cut:
                out.append(w[min(cut):])
            m = re.match(r"^-[A-Za-z]+(?=[.~/])(.+)$", w)  # -o../x, -o./x: the path after the letters
            if m:
                out.append(m.group(1))
        return out

    for seg in segments:
        prog, args = _program(seg)
        script = _script_of(prog, args)
        if script is not None and writes_code(script, cwd, _depth + 1):
            return True
        words, targets = _words_and_redirects(args)
        if any(walled(t) for t in targets):
            return True
        if prog in ("cd", "pushd"):
            dest = next((w for w in words if not w.startswith("-")), "~")
            cwd = _resolve(dest, cwd) or cwd
        if prog == "git" and _git_config_outside(args):
            return True
        if _read_only(prog, args):
            if prog != "git":  # ls -o, grep -o, find -o, rg -o only read
                continue
            for i, w in enumerate(words):
                if _OUTPUT_OPT.match(w):
                    value = re.sub(r"^(?:--output(?:-file)?|--out|-o)=?", "", w)
                    if value and walled(value):
                        return True
                    if not value and i + 1 < len(words) and walled(words[i + 1]):
                        return True
            continue
        for w in words:
            if any(walled(c) for c in candidates(w)):
                return True
    return False


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
