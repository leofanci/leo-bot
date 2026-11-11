#!/usr/bin/env bash
# Copy your Leo Bot data from this machine to your server, once, when you move:
#
#   scripts/sync-to-server.sh user@server
#
# Copies this machine's data folder (${LEOBOT_HOME:-~/.leobot}/data: projects, chats, team,
# portfolio) to the server's ~/.leobot/data, then restarts Leo Bot there. Nothing on the
# server is deleted. Stop using the copy on this machine afterwards, or the two will drift
# apart.
set -euo pipefail

TARGET="${1:-}"
LEOBOT_USER="${LEOBOT_USER:-leobot}"
if [ -z "$TARGET" ]; then
  echo "Usage: scripts/sync-to-server.sh user@server" >&2
  exit 1
fi
case "$TARGET" in -*|*[[:space:]]*) echo "Target must be user@server, not an option." >&2; exit 1 ;; esac
case "$LEOBOT_USER" in ""|-*|*[!a-z0-9_-]*) echo "LEOBOT_USER must be a plain lowercase user name." >&2; exit 1 ;; esac
LOCAL_DATA="${LEOBOT_HOME:-$HOME/.leobot}/data"
if [ ! -d "$LOCAL_DATA" ]; then
  echo "No data folder at $LOCAL_DATA. Open Leo Bot here once (leobot) so it moves your data there, then run this again." >&2
  exit 1
fi
# the user name is checked above and meant to expand here
# shellcheck disable=SC2029
REMOTE_HOME="$(ssh "$TARGET" "getent passwd $LEOBOT_USER | cut -d: -f6")"
if [ -z "$REMOTE_HOME" ]; then
  echo "No $LEOBOT_USER user on $TARGET. Run scripts/server-setup.sh there first." >&2
  exit 1
fi
REMOTE_DATA="$REMOTE_HOME/.leobot/data"

# Runs a command as the leobot user on the server; $(id -u ...) expands there, not here.
# The user name is checked above and meant to expand here.
remote_service() {
  # shellcheck disable=SC2029
  ssh "$TARGET" "S=; [ \$(id -u) -eq 0 ] || S='sudo -n'; \$S -u $LEOBOT_USER env XDG_RUNTIME_DIR=/run/user/\$(id -u $LEOBOT_USER) systemctl --user $1 leobot" 2>/dev/null \
    || ssh "$TARGET" "XDG_RUNTIME_DIR=/run/user/\$(id -u) systemctl --user $1 leobot"
}

remote_service stop
# A failed copy must not leave Leo Bot stopped or the copied files open.
finish() {
  # shellcheck disable=SC2029
  ssh "$TARGET" "S=; [ \$(id -u) -eq 0 ] || S='sudo -n'; \$S chown -R $LEOBOT_USER: $REMOTE_DATA; \$S chmod -R go-rwx $REMOTE_DATA" || true
  remote_service start
}
trap finish EXIT
# Not copied: the index (this machine's paths; the server rebuilds its own at start) and the
# note of an unfinished data move, which belongs to this machine only.
SKIP=(--exclude=/database/ --exclude=/.leobot-moving)
rsync -a "${SKIP[@]}" --rsync-path="sudo -n rsync" "$LOCAL_DATA/" "$TARGET:$REMOTE_DATA/" 2>/dev/null \
  || rsync -a "${SKIP[@]}" "$LOCAL_DATA/" "$TARGET:$REMOTE_DATA/"
echo "Done. Saved chats continue with a recap of their messages. Linked app folders need linking again on the server."
