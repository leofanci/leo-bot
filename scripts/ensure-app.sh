#!/bin/sh
# Rebuild LeoBot.app only when needed: the launcher binary is missing, or its
# source (webview.swift / start-server.sh) or icon is newer than it, so the
# ad-hoc signature always covers the bundled files. Called from the
# post-merge/post-checkout/post-rewrite git hooks so a pull that changes the
# app leaves it ready to open. Never fails the git command that called it.
cd "$(dirname "$0")/.." || exit 0
BIN=LeoBot.app/Contents/MacOS/LeoBot
SRC=LeoBot.app/Contents/Resources
# -nt is POSIX since 2024 and in every sh this runs under (macOS /bin/sh is bash or zsh)
# shellcheck disable=SC3013
if [ ! -x "$BIN" ] || [ "$SRC/webview.swift" -nt "$BIN" ] || [ "$SRC/start-server.sh" -nt "$BIN" ] \
        || [ "$SRC/AppIcon.icns" -nt "$BIN" ]; then
    echo "LeoBot.app source changed: rebuilding..."
    sh scripts/build-app.sh || echo "LeoBot.app rebuild failed; run scripts/build-app.sh to see why." >&2
fi
exit 0
