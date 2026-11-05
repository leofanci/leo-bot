"""deployment.py: where this myOS runs, read from its environment.

MYOS_DEPLOYMENT is "server" on a server (set by scripts/server-setup.sh), else this is a Mac.
"""
import os


def mode(env=os.environ):
    return "server" if env.get("MYOS_DEPLOYMENT") == "server" else "mac"
