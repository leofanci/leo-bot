"""deployment.py: where this Leo Bot runs, read from its environment.

LEOBOT_DEPLOYMENT is "server" on a server (set by scripts/server-setup.sh), else this is a Mac.
"""
import os


def mode(env=os.environ):
    # An install made before the rename still carries the old variable.
    return "server" if "server" in (env.get("LEOBOT_DEPLOYMENT"), env.get("MYOS_DEPLOYMENT")) else "mac"
