#!/usr/bin/env bash
# Install Leo Bot as an always-on service on a Debian or Ubuntu server. From a checkout
# on your laptop:
#
#   ssh root@your-server "LEOBOT_REPO=$(git remote get-url origin) bash -s" < scripts/server-setup.sh
#
# Safe to run again: it updates the code and restarts the service. Leo Bot listens on
# 127.0.0.1 only. Nothing is exposed until you add a tunnel (docs/sharing.md).
set -euo pipefail

LEOBOT_USER="${LEOBOT_USER:-leobot}"
LEOBOT_PORT="${LEOBOT_PORT:-8766}"  # not 8765, so it never collides with Leo Bot on your own Mac
LEOBOT_REPO="${LEOBOT_REPO:-}"
LEOBOT_BRANCH="${LEOBOT_BRANCH:-main}"

case "$LEOBOT_USER" in ""|-*|*[!a-z0-9_-]*) echo "LEOBOT_USER must be a plain lowercase user name." >&2; exit 1 ;; esac
case "$LEOBOT_PORT" in ""|*[!0-9]*) echo "LEOBOT_PORT must be a number." >&2; exit 1 ;; esac
case "$LEOBOT_BRANCH" in ""|-*|*[!A-Za-z0-9._/-]*) echo "LEOBOT_BRANCH has characters a branch name should not." >&2; exit 1 ;; esac
case "$LEOBOT_REPO" in -*) echo "LEOBOT_REPO must be a git address, not an option." >&2; exit 1 ;; esac

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this as root (or with sudo)." >&2
  exit 1
fi
if [ -z "$LEOBOT_REPO" ]; then
  echo "Set LEOBOT_REPO to the git address of your Leo Bot repo (see the top of this script)." >&2
  exit 1
fi
case "$LEOBOT_REPO" in  # the server has no key for your GitHub account: clone over https
  *@github.com:*) LEOBOT_REPO="https://github.com/${LEOBOT_REPO#*@github.com:}" ;;
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
APP_DIR="$HOME_DIR/leo-bot"

as_user() { runuser -u "$LEOBOT_USER" -- env HOME="$HOME_DIR" "$@"; }

if [ -d "$APP_DIR/.git" ]; then
  as_user git -C "$APP_DIR" pull --ff-only
else
  as_user git clone --branch "$LEOBOT_BRANCH" -- "$LEOBOT_REPO" "$APP_DIR"
fi

# Claude Code, so your own messages can run on your own Claude login.
if ! as_user bash -c 'export PATH="$HOME/.local/bin:$PATH"; command -v claude' >/dev/null; then
  as_user bash -c 'curl -fsSL https://claude.ai/install.sh | bash' || \
    echo "Claude Code did not install. Bots can still run on an API key (Settings, Engines)." >&2
fi

cat > /etc/systemd/system/leobot.service <<EOF
[Unit]
Description=Leo Bot
After=network-online.target
Wants=network-online.target

[Service]
User=$LEOBOT_USER
WorkingDirectory=$APP_DIR
Environment=PATH=$HOME_DIR/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=HOME=$HOME_DIR
Environment=LEOBOT_DEPLOYMENT=server
ExecStart=/usr/bin/python3 $APP_DIR/dashboard/server.py --port $LEOBOT_PORT
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable leobot >/dev/null 2>&1
systemctl restart leobot

up=""
for _ in $(seq 1 30); do
  if curl -fsS -o /dev/null "http://127.0.0.1:$LEOBOT_PORT/healthz"; then up=1; break; fi
  sleep 1
done
if [ -z "$up" ]; then
  echo "Leo Bot did not start. See why with: journalctl -u leobot -n 50" >&2
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
