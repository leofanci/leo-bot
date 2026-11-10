#!/bin/bash
# Started by the Leo Bot app (the Swift main executable) — finds python, sets a
# usable PATH (so the server can spawn `claude`/node), starts the dashboard
# server in the background, and prints "PID <n>" so the app can stop it later.
# Never sources .zprofile (too slow); probes known paths directly.

REPO="$1"
PORT="${2:-8765}"
URL="http://127.0.0.1:$PORT"
# Home folder: the server keeps its token, pid lock and log here (see core/paths.py).
HOME_DIR="${LEOBOT_HOME:-$HOME/.leobot}"
export LEOBOT_HOME="$HOME_DIR"
TOKEN_FILE="$HOME_DIR/run/auth-token"
PIDFILE="$HOME_DIR/run/server.pid"
LOGFILE="$HOME_DIR/logs/server.log"

# Find python3 — probe in order of preference
PYTHON3=""
for _p in \
    "$HOME/anaconda3/bin/python3" \
    "$HOME/miniconda3/bin/python3" \
    "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3" \
    "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3" \
    "/opt/homebrew/bin/python3" \
    "/usr/local/bin/python3" \
    "/usr/bin/python3"; do
    [ -x "$_p" ] && PYTHON3="$_p" && break
done
[ -z "$PYTHON3" ] && PYTHON3="$(command -v python3 2>/dev/null)"

if [ -z "$PYTHON3" ]; then
    osascript -e 'display alert "Leo Bot" message "python3 not found. Install from python.org."' >/dev/null 2>&1
    echo "NOPYTHON"
    exit 1
fi

# Already running? Nothing to do — the app will just connect. Only trust the
# server that wrote THIS checkout's token file: it must answer a random nonce
# with HMAC(token, nonce). Anything else holding the port (or a Leo Bot server from
# another checkout) fails the proof. Python reads the token itself, so it never
# appears in argv or goes over the wire.
is_leobot() {
    "$PYTHON3" - "$URL" "$TOKEN_FILE" <<'PY' 2>/dev/null
import hmac, json, secrets, sys, urllib.request
url, token_file = sys.argv[1], sys.argv[2]
token = open(token_file).read().strip()
nonce = secrets.token_hex(16)
with urllib.request.urlopen(f"{url}/healthz?nonce={nonce}", timeout=1) as r:
    proof = json.load(r).get("proof", "")
want = hmac.new(token.encode(), b"leobot-healthz:" + nonce.encode(), "sha256").hexdigest()
sys.exit(0 if token and hmac.compare_digest(proof, want) else 1)
PY
}
if is_leobot; then
    echo "RUNNING"
    exit 0
fi

# Put claude on PATH so chat features work. Don't assume the newest nvm node has
# it — a `claude` reinstall lands in whichever node was active at the time, which
# may be older than the newest installed version. Prefer the node bin that
# actually contains claude; fall back to the newest node otherwise.
NVM_BIN=""
NVM_CLAUDE=""
for _d in "$HOME"/.nvm/versions/node/*/bin; do
    [ -d "$_d" ] && NVM_BIN="$_d"
    [ -x "$_d/claude" ] && NVM_CLAUDE="$_d"
done
NODE_BIN="${NVM_CLAUDE:-$NVM_BIN}"
export PATH="${NODE_BIN:+$NODE_BIN:}/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"

# Port taken? Stop it only if it is THIS home's stale Leo Bot server (the pid in
# its run/server.pid, still a dashboard/server.py process). A server of another
# home or a foreign program is never killed: tell the user instead.
OWN_PID="$("$PYTHON3" - "$PIDFILE" <<'PY' 2>/dev/null
import json, sys
try:
    print(int(json.load(open(sys.argv[1]))["pid"]))
except Exception:
    pass
PY
)"
for pid in $(/usr/sbin/lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null); do
    if [ -n "$OWN_PID" ] && [ "$pid" = "$OWN_PID" ] \
            && ps -o command= -p "$pid" 2>/dev/null | grep -q "dashboard/server.py"; then
        kill "$pid" 2>/dev/null
        sleep 0.5
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
    else
        osascript -e "display alert \"Leo Bot\" message \"Port $PORT is used by another program. Quit it, then reopen Leo Bot.\"" >/dev/null 2>&1
        echo "PORTBUSY"
        exit 1
    fi
done

cd "$REPO" || { echo "NOREPO"; exit 1; }
# The log holds chat ids and paths: owner-only. Only the log, so the server
# (and the terminal it hosts) still starts with the user's own umask.
# Append: the server opens the same log itself, so never truncate it.
( umask 077; mkdir -p "$HOME_DIR" "$HOME_DIR/logs" "$HOME_DIR/run" )
chmod 700 "$HOME_DIR/logs" "$HOME_DIR/run"
( umask 077; : >> "$LOGFILE" )
chmod 600 "$LOGFILE"
"$PYTHON3" dashboard/server.py --port "$PORT" >> "$LOGFILE" 2>&1 &
echo "PID $!"
