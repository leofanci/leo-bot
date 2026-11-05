"""deployment.py: where this myOS runs, read from its environment.

MYOS_DEPLOYMENT is "server" on a server (set by scripts/server-setup.sh), else this is a Mac.
MYOS_CLOUD_URL is reserved for a hosted myOS service. Unset, which is the normal case, there
is no Cloud: no screen, no sign-in and no request. Nothing here opens a connection.
"""
import os
from urllib.parse import urlsplit


def mode(env=os.environ):
    return "server" if env.get("MYOS_DEPLOYMENT") == "server" else "mac"


def cloud_url(env=os.environ):
    """The service's plain https address, or "" for anything else, so a typo can't half-enable it."""
    value = (env.get("MYOS_CLOUD_URL") or "").strip().rstrip("/")
    try:
        parts = urlsplit(value)
        clean = parts.scheme == "https" and parts.hostname and not (parts.username or parts.password)
        _ = parts.port  # raises on a bad port
    except ValueError:
        return ""
    return value if clean and not (parts.query or parts.fragment) else ""


def view(env=os.environ):
    """For the settings screen: the mode, and only whether a service is set, never its address."""
    return {"deployment": mode(env), "cloud": bool(cloud_url(env))}
