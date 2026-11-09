#!/usr/bin/env bash
# Copy your Leo Bot data from this machine to your server, once, when you move:
#
#   scripts/sync-to-server.sh user@server
#
# Copies your projects, chats, team and portfolio (all gitignored), then re-indexes
# and restarts Leo Bot there. Nothing on the server is deleted. Stop using the copy on
# this machine afterwards, or the two will drift apart.
set -euo pipefail

TARGET="${1:-}"
MYOS_USER="${MYOS_USER:-myos}"
if [ -z "$TARGET" ]; then
  echo "Usage: scripts/sync-to-server.sh user@server" >&2
  exit 1
fi
case "$TARGET" in -*|*[[:space:]]*) echo "Target must be user@server, not an option." >&2; exit 1 ;; esac
case "$MYOS_USER" in ""|-*|*[!a-z0-9_-]*) echo "MYOS_USER must be a plain lowercase user name." >&2; exit 1 ;; esac
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE_HOME="$(ssh "$TARGET" "getent passwd $MYOS_USER | cut -d: -f6")"
if [ -z "$REMOTE_HOME" ]; then
  echo "No $MYOS_USER user on $TARGET. Run scripts/server-setup.sh there first." >&2
  exit 1
fi
APP="$REMOTE_HOME/my-os"

ssh "$TARGET" "S=; [ \$(id -u) -eq 0 ] || S='sudo -n'; \$S systemctl stop myos"
# A failed copy must not leave Leo Bot stopped or the copied files open.
PRIVATE="$APP/projects $APP/chats $APP/team $APP/portfolio"
finish() {
  ssh "$TARGET" "S=; [ \$(id -u) -eq 0 ] || S='sudo -n'; \$S chown -R $MYOS_USER: $APP; \$S chmod -R go-rwx $PRIVATE 2>/dev/null; \$S systemctl start myos"
}
trap finish EXIT
for dir in projects chats team portfolio; do
  [ -d "$ROOT/$dir" ] || continue
  rsync -a --rsync-path="sudo -n rsync" "$ROOT/$dir/" "$TARGET:$APP/$dir/" 2>/dev/null \
    || rsync -a "$ROOT/$dir/" "$TARGET:$APP/$dir/"
done
echo "Done. Saved chats continue with a recap of their messages. Linked app folders need linking again on the server."
