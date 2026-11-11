#!/usr/bin/env bash
# Open Leo Bot running on your server, from your laptop, over SSH:
#
#   scripts/leobot-connect.sh user@server [port]
#
# The server stays private: the dashboard reaches your browser through the SSH
# connection only, so the terminal and every owner screen work as if it were local.
# Ctrl-C closes the connection.
set -euo pipefail

TARGET="${1:-}"
PORT="${2:-8766}"
LEOBOT_USER="${LEOBOT_USER:-leobot}"
if [ -z "$TARGET" ]; then
  echo "Usage: scripts/leobot-connect.sh user@server [port]" >&2
  exit 1
fi
case "$LEOBOT_USER" in ""|-*|*[!a-z0-9_-]*) echo "LEOBOT_USER must be a plain lowercase user name." >&2; exit 1 ;; esac

# The login link carries your token: if something else on this laptop answers on this
# port, it must never receive it. (bash's /dev/tcp: a connection that opens means taken.)
if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
  echo "Something on this laptop is already using port $PORT. Stop it and run this again." >&2
  exit 1
fi

# The service writes this as it starts, so give a fresh start a moment.
TOKEN=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
  # $LEOBOT_USER (checked above) expands here on purpose; ~user then expands on the server
  # shellcheck disable=SC2029
  TOKEN="$(ssh "$TARGET" "sudo -n cat ~$LEOBOT_USER/.leobot/run/auth-token 2>/dev/null || cat ~$LEOBOT_USER/.leobot/run/auth-token 2>/dev/null" || true)"
  [ -n "$TOKEN" ] && break
  sleep 3
done
if [ -z "$TOKEN" ]; then
  echo "Could not read the login token on $TARGET. Is the leobot service running?" >&2
  exit 1
fi

URL="http://127.0.0.1:$PORT/?token=$TOKEN"
ssh -N -o ExitOnForwardFailure=yes -L "127.0.0.1:$PORT:127.0.0.1:$PORT" "$TARGET" &
TUNNEL=$!
trap 'kill "$TUNNEL" 2>/dev/null || true' EXIT INT TERM

reached=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if ! kill -0 "$TUNNEL" 2>/dev/null; then
    echo "Could not open the connection to $TARGET." >&2
    exit 1
  fi
  if curl -s -o /dev/null --connect-timeout 1 "http://127.0.0.1:$PORT/healthz"; then reached=1; break; fi
  sleep 1
done
sleep 1
if [ -z "$reached" ] || ! kill -0 "$TUNNEL" 2>/dev/null; then
  echo "Leo Bot did not answer through the connection. Is the service running on $TARGET?" >&2
  exit 1
fi

# The token must not sit in another program's argument list (`ps` shows it to every user
# on this laptop), so the browser opens a private local page that redirects to the link.
LAUNCH="$(mktemp -d)"
chmod 700 "$LAUNCH"
printf '<!doctype html><meta http-equiv="refresh" content="0;url=%s">' "$URL" > "$LAUNCH/open.html"
chmod 600 "$LAUNCH/open.html"
trap 'kill "$TUNNEL" 2>/dev/null || true; rm -rf "$LAUNCH"' EXIT INT TERM
if command -v open >/dev/null; then
  open "$LAUNCH/open.html"
elif command -v xdg-open >/dev/null; then
  xdg-open "$LAUNCH/open.html"
else
  echo "Open this in your browser (it holds your login, keep it private): $URL"
fi

echo "Connected to Leo Bot on $TARGET. Ctrl-C to disconnect; Leo Bot keeps running there."
wait "$TUNNEL"
