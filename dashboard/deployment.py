"""deployment.py: where this myOS runs, read from its environment.

MYOS_DEPLOYMENT is "server" on a server (set by scripts/server-setup.sh), else this is a Mac.
MYOS_CLOUD_URL is reserved for the hosted myOS service, which does not exist yet. For now, set
or unset, there is no Cloud screen, sign-in or request. Nothing here opens a connection.
"""
import os
import re

_ADDRESS = re.compile(r"https://[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::([0-9]{1,5}))?(?:/[a-z0-9._~/-]*)?", re.I)


def mode(env=os.environ):
    return "server" if env.get("MYOS_DEPLOYMENT") == "server" else "mac"


def cloud_url(env=os.environ):
    """The service's plain https address, or "" for anything else, so a typo can't half-enable it."""
    value = (env.get("MYOS_CLOUD_URL") or "").strip().rstrip("/")
    match = _ADDRESS.fullmatch(value)
    return value if match and 0 < int(match[1] or 443) < 65536 else ""


def view(env=os.environ):
    """For the settings screen: the mode, and only whether a service is set, never its address."""
    return {"deployment": mode(env), "cloud": bool(cloud_url(env))}
