#!/usr/bin/env bash
# Install myOS as an always-on service on a Debian or Ubuntu server. From a checkout
# on your laptop:
#
#   ssh root@your-server "MYOS_REPO=$(git remote get-url origin) bash -s" < scripts/server-setup.sh
#
# Safe to run again: it updates the code and restarts the service. myOS listens on
# 127.0.0.1 only. Nothing is exposed until you add a tunnel (docs/sharing.md).
set -euo pipefail

MYOS_USER="${MYOS_USER:-myos}"
MYOS_PORT="${MYOS_PORT:-8766}"  # not 8765, so it never collides with myOS on your own Mac
MYOS_REPO="${MYOS_REPO:-}"
MYOS_BRANCH="${MYOS_BRANCH:-main}"

case "$MYOS_USER" in ""|-*|*[!a-z0-9_-]*) echo "MYOS_USER must be a plain lowercase user name." >&2; exit 1 ;; esac
case "$MYOS_PORT" in ""|*[!0-9]*) echo "MYOS_PORT must be a number." >&2; exit 1 ;; esac
case "$MYOS_BRANCH" in ""|-*|*[!A-Za-z0-9._/-]*) echo "MYOS_BRANCH has characters a branch name should not." >&2; exit 1 ;; esac
case "$MYOS_REPO" in -*) echo "MYOS_REPO must be a git address, not an option." >&2; exit 1 ;; esac

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this as root (or with sudo)." >&2
  exit 1
fi
if [ -z "$MYOS_REPO" ]; then
  echo "Set MYOS_REPO to the git address of your myOS repo (see the top of this script)." >&2
  exit 1
fi
case "$MYOS_REPO" in  # the server has no key for your GitHub account: clone over https
  *@github.com:*) MYOS_REPO="https://github.com/${MYOS_REPO#*@github.com:}" ;;
esac
if ! command -v apt-get >/dev/null; then
  echo "This script supports Debian and Ubuntu. For anything else, follow docs/server.md by hand." >&2
  exit 1
fi

apt-get update -qq
apt-get install -y -qq python3 git curl rsync sudo ca-certificates >/dev/null

if ! id "$MYOS_USER" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "$MYOS_USER"
fi
HOME_DIR="$(getent passwd "$MYOS_USER" | cut -d: -f6)"
chmod 700 "$HOME_DIR"  # your data, keys and Claude login live here
APP_DIR="$HOME_DIR/my-os"

as_user() { runuser -u "$MYOS_USER" -- env HOME="$HOME_DIR" "$@"; }

if [ -d "$APP_DIR/.git" ]; then
  as_user git -C "$APP_DIR" pull --ff-only
else
  as_user git clone --branch "$MYOS_BRANCH" -- "$MYOS_REPO" "$APP_DIR"
fi

# Claude Code, so your own messages can run on your own Claude login.
if ! as_user bash -c 'export PATH="$HOME/.local/bin:$PATH"; command -v claude' >/dev/null; then
  as_user bash -c 'curl -fsSL https://claude.ai/install.sh | bash' || \
    echo "Claude Code did not install. Bots can still run on an API key (HQ, Engines)." >&2
fi

cat > /etc/systemd/system/myos.service <<EOF
[Unit]
Description=myOS
After=network-online.target
Wants=network-online.target

[Service]
User=$MYOS_USER
WorkingDirectory=$APP_DIR
Environment=PATH=$HOME_DIR/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=HOME=$HOME_DIR
Environment=MYOS_DEPLOYMENT=server
ExecStart=/usr/bin/python3 $APP_DIR/dashboard/server.py --port $MYOS_PORT
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable myos >/dev/null 2>&1
systemctl restart myos

up=""
for _ in $(seq 1 30); do
  if curl -fsS -o /dev/null "http://127.0.0.1:$MYOS_PORT/healthz"; then up=1; break; fi
  sleep 1
done
if [ -z "$up" ]; then
  echo "myOS did not start. See why with: journalctl -u myos -n 50" >&2
  exit 1
fi

echo
echo "myOS is running on this server, on 127.0.0.1:$MYOS_PORT."
echo
echo "Next:"
echo "  1. Log in to Claude here, for your own messages (skip it to use only API keys):"
echo "       sudo -u $MYOS_USER -i claude auth login"
echo "  2. On your laptop, from a my-os checkout:"
echo "       scripts/myos-connect.sh root@THIS_SERVER"
echo "  3. To move your data from your Mac:"
echo "       scripts/sync-to-server.sh root@THIS_SERVER"
echo "  4. To invite people, add a tunnel: docs/sharing.md"
