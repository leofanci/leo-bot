#!/usr/bin/env bash
# Install Leo Bot as an always-on service on a Debian or Ubuntu server. From a checkout
# on your laptop:
#
#   ssh root@your-server "LEOBOT_REPO_URL=$(git remote get-url origin) bash -s" < scripts/server-setup.sh
#
# Root prepares a `leobot` user. Leo Bot then installs itself into that user's ~/.leobot with
# install.sh and runs as a systemd user unit of that user, so it can stop, start and update
# its own service without sudo. Safe to run again: it updates the code and restarts the
# service. Leo Bot listens on 127.0.0.1 only. Nothing is exposed until you add a tunnel
# (docs/sharing.md).
#
# Settings: LEOBOT_REPO_URL (LEOBOT_REPO still works), LEOBOT_USER, LEOBOT_PORT,
# LEOBOT_CHANNEL (stable, the default, or main).
set -euo pipefail

LEOBOT_USER="${LEOBOT_USER:-leobot}"
LEOBOT_PORT="${LEOBOT_PORT:-8766}"  # not 8765, so it never collides with Leo Bot on your own Mac
LEOBOT_REPO="${LEOBOT_REPO:-}"      # old name of LEOBOT_REPO_URL
LEOBOT_REPO_URL="${LEOBOT_REPO_URL:-$LEOBOT_REPO}"
LEOBOT_CHANNEL="${LEOBOT_CHANNEL:-stable}"

case "$LEOBOT_USER" in ""|-*|*[!a-z0-9_-]*) echo "LEOBOT_USER must be a plain lowercase user name." >&2; exit 1 ;; esac
case "$LEOBOT_PORT" in ""|*[!0-9]*) echo "LEOBOT_PORT must be a number." >&2; exit 1 ;; esac
case "$LEOBOT_CHANNEL" in stable|main) ;; *) echo "LEOBOT_CHANNEL must be stable or main." >&2; exit 1 ;; esac
case "$LEOBOT_REPO_URL" in -*) echo "LEOBOT_REPO_URL must be a git address, not an option." >&2; exit 1 ;; esac

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this as root (or with sudo)." >&2
  exit 1
fi
if [ -z "$LEOBOT_REPO_URL" ]; then
  echo "Set LEOBOT_REPO_URL to the git address of your Leo Bot repo (see the top of this script)." >&2
  exit 1
fi
case "$LEOBOT_REPO_URL" in  # the server has no key for your GitHub account: clone over https
  *@github.com:*) LEOBOT_REPO_URL="https://github.com/${LEOBOT_REPO_URL#*@github.com:}" ;;
esac
if ! command -v apt-get >/dev/null; then
  echo "This script supports Debian and Ubuntu. For anything else, follow docs/server.md by hand." >&2
  exit 1
fi

apt-get update -qq
apt-get install -y -qq python3 git curl rsync sudo ca-certificates >/dev/null

if ! id "$LEOBOT_USER" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "$LEOBOT_USER"
fi
HOME_DIR="$(getent passwd "$LEOBOT_USER" | cut -d: -f6)"
chmod 700 "$HOME_DIR"  # your data, keys and Claude login live here
cd "$HOME_DIR"  # not root's folder, which the leobot user cannot read
USER_ID="$(id -u "$LEOBOT_USER")"
RUNTIME_DIR="/run/user/$USER_ID"
LEOBOT_CLI="$HOME_DIR/.leobot/app/leobot.py"

# Keep the user's service running after logout and across reboots.
loginctl enable-linger "$LEOBOT_USER"
for _ in $(seq 1 20); do [ -d "$RUNTIME_DIR" ] && break; sleep 1; done
if [ ! -d "$RUNTIME_DIR" ]; then
  echo "The user session for $LEOBOT_USER did not start ($RUNTIME_DIR is missing)." >&2
  exit 1
fi

as_user() {
  runuser -u "$LEOBOT_USER" -- env HOME="$HOME_DIR" XDG_RUNTIME_DIR="$RUNTIME_DIR" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=$RUNTIME_DIR/bus" "$@" </dev/null
}

# An older setup ran Leo Bot as a system unit on the same port. Remove it before the user
# service starts.
if [ -e /etc/systemd/system/leobot.service ]; then
  systemctl disable --now leobot >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/leobot.service
  systemctl daemon-reload
fi

# The installer comes from the repo itself: stdin is this script, so there is no local copy.
# Clone as the leobot user into a private temp folder, at the latest release (or main).
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
chown "$LEOBOT_USER": "$WORK"
chmod 700 "$WORK"
as_user git clone --quiet -- "$LEOBOT_REPO_URL" "$WORK/src"
if [ "$LEOBOT_CHANNEL" = main ]; then
  as_user git -C "$WORK/src" checkout --quiet main
else
  TAG="$(as_user git -C "$WORK/src" tag --list 'v[0-9]*.[0-9]*.[0-9]*' --sort=-v:refname | head -n 1)"
  if [ -z "$TAG" ]; then
    echo "There is no release yet. To install the latest code instead, run again with LEOBOT_CHANNEL=main." >&2
    exit 1
  fi
  as_user git -C "$WORK/src" checkout --quiet "$TAG"
fi

# No service yet: the old folder's data must move first, while nothing is running.
# --claude: install Claude Code for the leobot user without asking (--yes alone would skip it).
as_user env LEOBOT_REPO_URL="$LEOBOT_REPO_URL" LEOBOT_PORT="$LEOBOT_PORT" LEOBOT_DEPLOYMENT=server \
  bash "$WORK/src/install.sh" --channel "$LEOBOT_CHANNEL" --no-service --no-app --claude --yes

OLD_DIR="$HOME_DIR/leo-bot"
if [ -d "$OLD_DIR" ]; then
  as_user python3 "$LEOBOT_CLI" stop >/dev/null 2>&1 || true
  if ! as_user python3 "$LEOBOT_CLI" move-data --from "$OLD_DIR"; then
    echo "Old data in $OLD_DIR was not moved. Fix that first, then run this script again:" >&2
    echo "  sudo -iu $LEOBOT_USER $HOME_DIR/.local/bin/leobot doctor" >&2
    exit 1
  fi
fi

as_user python3 "$LEOBOT_CLI" service install --port "$LEOBOT_PORT" --server

up=""
for _ in $(seq 1 30); do
  if curl -fsS -o /dev/null "http://127.0.0.1:$LEOBOT_PORT/healthz"; then up=1; break; fi
  sleep 1
done
if [ -z "$up" ]; then
  echo "Leo Bot did not start. See why with: sudo -iu $LEOBOT_USER $HOME_DIR/.local/bin/leobot doctor" >&2
  exit 1
fi

echo
echo "Leo Bot is running on this server, on 127.0.0.1:$LEOBOT_PORT."
echo
echo "Next:"
echo "  1. Log in to Claude here, for your own messages (skip it to use only API keys):"
echo "       sudo -u $LEOBOT_USER -i claude auth login"
echo "  2. On your laptop, from a leo-bot checkout:"
echo "       scripts/leobot-connect.sh root@THIS_SERVER"
echo "  3. To move your data from your Mac:"
echo "       scripts/sync-to-server.sh root@THIS_SERVER"
echo "  4. To invite people, add a tunnel: docs/sharing.md"
echo "  5. To update later, from the Leo Bot Settings screen, or on this server:"
echo "       sudo -iu $LEOBOT_USER $HOME_DIR/.local/bin/leobot update"
if [ -d "$OLD_DIR" ]; then
  echo
  echo "The old folder $OLD_DIR is left in place. Once you have checked your data,"
  echo "delete it yourself: rm -rf $OLD_DIR"
fi
