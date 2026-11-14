"""shell_rules.py: what a bot's shell may run (a bot on Full access, dashboard/engines/access).

Two walls, checked by the taint gate (dashboard/taint.py) the moment a command would run:

  1. Never, whatever the level or the conversation: commands that wreck the machine. A
     recursive delete of the disk root, the home folder or a system folder; formatting or
     erasing a disk; writing raw to a disk device; a fork bomb; killing every process;
     shutting down. A command Leo Bot cannot read (unbalanced quotes) is refused too.
  2. Once the conversation holds outside content or app data (taint.Turn.outside): commands
     that send things off this machine (curl, wget, ssh, scp, rsync, nc, git push, a package
     publish, gh, mail). A page or a mail could tell the bot to ship private data out.

This is a best-effort wall on top of the owner's choice, not a sandbox: a program the bot
writes can still open a socket. Full access means the bot runs commands as the owner does.
"""
import os
import posixpath
import re
import shlex

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
