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

# Installed copy: when this checkout is the installed code ($LEOBOT_HOME/app, default
# ~/.leobot/app) and ~/Applications/LeoBot.app exists, rebuild it too.
HOME_DIR="${LEOBOT_HOME:-$HOME/.leobot}"
HERE="$(pwd -P)"
INSTALLED="$(cd "$HOME_DIR/app" 2>/dev/null && pwd -P)"
if [ -d "$HOME/Applications/LeoBot.app" ] && [ -n "$INSTALLED" ] && [ "$HERE" = "$INSTALLED" ]; then
    echo "Rebuilding $HOME/Applications/LeoBot.app..."
    sh scripts/build-app.sh --out "$HOME/Applications" \
        || echo "Installed LeoBot.app rebuild failed; run scripts/build-app.sh --out \"\$HOME/Applications\" to see why." >&2
fi
exit 0
