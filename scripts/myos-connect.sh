#!/usr/bin/env bash
# Open myOS running on your server, from your laptop, over SSH:
#
#   scripts/myos-connect.sh user@server [port]
#
# The server stays private: the dashboard reaches your browser through the SSH
# connection only, so the terminal and every owner screen work as if it were local.
# Ctrl-C closes the connection.
set -euo pipefail

TARGET="${1:-}"
PORT="${2:-8766}"
MYOS_USER="${MYOS_USER:-myos}"
if [ -z "$TARGET" ]; then
  echo "Usage: scripts/myos-connect.sh user@server [port]" >&2
  exit 1
fi

TOKEN="$(ssh "$TARGET" "sudo -n cat ~$MYOS_USER/my-os/dashboard/.auth-token 2>/dev/null || cat ~$MYOS_USER/my-os/dashboard/.auth-token")"
if [ -z "$TOKEN" ]; then
  echo "Could not read the login token on $TARGET. Is the myos service running?" >&2
  exit 1
fi

URL="http://127.0.0.1:$PORT/?token=$TOKEN"
ssh -N -o ExitOnForwardFailure=yes -L "127.0.0.1:$PORT:127.0.0.1:$PORT" "$TARGET" &
TUNNEL=$!
trap 'kill "$TUNNEL" 2>/dev/null || true' EXIT INT TERM

for _ in 1 2 3 4 5 6 7 8 9 10; do
  if ! kill -0 "$TUNNEL" 2>/dev/null; then
    echo "Could not open the connection. Is something on this laptop already using port $PORT?" >&2
    exit 1
  fi
  if curl -s -o /dev/null --connect-timeout 1 "http://127.0.0.1:$PORT/"; then break; fi
  sleep 1
done

if command -v open >/dev/null; then
  open "$URL"
elif command -v xdg-open >/dev/null; then
  xdg-open "$URL"
else
  echo "Open this in your browser (it holds your login, keep it private): $URL"
fi

echo "Connected to myOS on $TARGET. Ctrl-C to disconnect; myOS keeps running there."
wait "$TUNNEL"
