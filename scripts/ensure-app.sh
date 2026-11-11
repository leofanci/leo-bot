#!/bin/sh
# Rebuild LeoBot.app only when needed: the launcher binary is missing, or the hash of the
# app's sources (scripts/build-app.sh --hash) differs from the one the last build recorded in
# Contents/Resources/source-hash. A checkout that only touches file times rebuilds nothing.
# Called from the post-merge/post-checkout/post-rewrite git hooks and after an update, so a
# change to the app leaves it ready to open. Never fails the git command that called it.
cd "$(dirname "$0")/.." || exit 0
WANT="$(sh scripts/build-app.sh --hash 2>/dev/null)" || WANT=""

# stale APP: true when APP's launcher is missing or was built from other sources.
stale() {
    [ -x "$1/Contents/MacOS/LeoBot" ] || return 0
    [ -n "$WANT" ] || return 0
    [ "$(cat "$1/Contents/Resources/source-hash" 2>/dev/null)" != "$WANT" ]
}

if stale LeoBot.app; then
    echo "LeoBot.app source changed: rebuilding..."
    sh scripts/build-app.sh || echo "LeoBot.app rebuild failed; run scripts/build-app.sh to see why." >&2
fi

# Installed copy: when this checkout is the installed code ($LEOBOT_HOME/app, default
# ~/.leobot/app) and ~/Applications/LeoBot.app exists, rebuild it too when it is stale.
HOME_DIR="${LEOBOT_HOME:-$HOME/.leobot}"
HERE="$(pwd -P)"
INSTALLED="$(cd "$HOME_DIR/app" 2>/dev/null && pwd -P)"
if [ -d "$HOME/Applications/LeoBot.app" ] && [ -n "$INSTALLED" ] && [ "$HERE" = "$INSTALLED" ] \
        && stale "$HOME/Applications/LeoBot.app"; then
    echo "Rebuilding $HOME/Applications/LeoBot.app..."
    sh scripts/build-app.sh --out "$HOME/Applications" \
        || echo "Installed LeoBot.app rebuild failed; run scripts/build-app.sh --out \"\$HOME/Applications\" to see why." >&2
fi
exit 0
