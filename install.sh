#!/usr/bin/env bash
# Leo Bot installer for macOS and Debian/Ubuntu. No sudo on macOS.
#
#   curl -fsSL <repo>/releases/latest/download/install.sh | bash
#   bash install.sh [--channel stable|main] [--version vX.Y.Z] [--service|--no-service]
#                   [--no-app] [--claude] [--yes] [--dry-run]
#
# The same settings as environment variables: LEOBOT_CHANNEL, LEOBOT_VERSION,
# LEOBOT_SERVICE (1 or 0), LEOBOT_NO_APP=1, LEOBOT_INSTALL_CLAUDE=1, LEOBOT_YES=1,
# LEOBOT_DRY_RUN=1, LEOBOT_HOME (default ~/.leobot), LEOBOT_REPO_URL, LEOBOT_PORT
# (background service port).
#
# Everything runs from main(), called on the last line, so a download cut short runs nothing.

REPO_URL_DEFAULT="__LEOBOT_REPO_URL__"

say() { printf '%s\n' "$*"; }
warn() { printf 'Warning: %s\n' "$*" >&2; }
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

# One shell word per argument, quoted only when needed, for the `would:` lines.
quote() {
  local out="" a sq="'\\''"
  for a in "$@"; do
    if [[ $a =~ ^[A-Za-z0-9_./:=@%+,-]+$ ]]; then
      out+="$a "
    else
      out+="'${a//\'/$sq}' "
    fi
  done
  printf '%s' "${out% }"
}

# Run a command that changes the machine, or print it under --dry-run.
run() {
  if [ "$DRY_RUN" = 1 ]; then
    say "would: $(quote "$@")"
  else
    "$@"
  fi
}

# Same, for a change described in words (file writes, edits).
plan() {
  [ "$DRY_RUN" = 1 ] && say "would: $*"
  return 0
}

# Normalised vX.Y.Z (ASCII digits only), or failure.
semver_tag() {
  local LC_ALL=C v="v${1#v}"
  [[ $v =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
  printf '%s\n' "$v"
}

# ask QUESTION DEFAULT(y|n): yes/no prompt. Under --yes, or with no terminal at all, the
# default. Piped from curl, stdin is the script, so the answer comes from /dev/tty.
ask() {
  local q="$1" def="$2" hint ans=""
  [ "$def" = y ] && hint="[Y/n]" || hint="[y/N]"
  if [ "$YES" != 1 ]; then
    if [ -t 0 ]; then
      printf '%s %s ' "$q" "$hint" >&2
      read -r ans || ans=""
    elif (: </dev/tty) 2>/dev/null; then
      printf '%s %s ' "$q" "$hint" >/dev/tty
      read -r ans </dev/tty || ans=""
    fi
  fi
  ans="$(printf '%s' "${ans:-$def}" | tr '[:upper:]' '[:lower:]')"
  case "$ans" in y|yes) return 0 ;; *) return 1 ;; esac
}

usage() {
  cat <<'EOF_USAGE'
Install Leo Bot into ~/.leobot (or LEOBOT_HOME).

  install.sh [--channel stable|main] [--version vX.Y.Z] [--service|--no-service]
             [--no-app] [--claude] [--yes] [--dry-run]

  --channel   stable follows releases (default), main follows the latest code
  --version   install this release instead of the newest one
  --service   run Leo Bot as a background service (--no-service: do not)
  --no-app    do not build the Mac app
  --claude    install Claude Code without asking, when it is missing
  --yes       no questions, take the defaults
  --dry-run   only print what would change

Environment: LEOBOT_CHANNEL, LEOBOT_VERSION, LEOBOT_SERVICE (1 or 0), LEOBOT_NO_APP=1,
LEOBOT_INSTALL_CLAUDE=1, LEOBOT_YES=1, LEOBOT_DRY_RUN=1, LEOBOT_HOME, LEOBOT_REPO_URL,
LEOBOT_PORT.
EOF_USAGE
}

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --channel) [ $# -ge 2 ] || die "--channel needs stable or main"; CHANNEL="$2"; CHANNEL_SET=1; shift ;;
      --channel=*) CHANNEL="${1#*=}"; CHANNEL_SET=1 ;;
      --version) [ $# -ge 2 ] || die "--version needs vX.Y.Z"; PIN="$2"; shift ;;
      --version=*) PIN="${1#*=}" ;;
      --service) SERVICE=1 ;;
      --no-service) SERVICE=0 ;;
      --no-app) NO_APP=1 ;;
      --claude) INSTALL_CLAUDE=1 ;;
      --yes|-y) YES=1 ;;
      --dry-run) DRY_RUN=1 ;;
      -h|--help) usage; exit 0 ;;
      *) die "unknown option: $1 (see --help)" ;;
    esac
    shift
  done
  case "$CHANNEL" in stable|main) ;; *) die "--channel must be stable or main, not '$CHANNEL'" ;; esac
  if [ -n "$PIN" ]; then
    PIN="$(semver_tag "$PIN")" || die "--version must look like v1.2.3"
    [ "$CHANNEL" = stable ] || die "--version only works on the stable channel"
  fi
  case "$SERVICE" in ""|1|0) ;; *) die "LEOBOT_SERVICE must be 1 or 0" ;; esac
  case "$INSTALL_CLAUDE" in 1|0) ;; *) die "LEOBOT_INSTALL_CLAUDE must be 1 or 0" ;; esac
  if [ -n "$PORT" ] && ! [[ $PORT =~ ^[0-9]+$ ]]; then die "LEOBOT_PORT must be a number"; fi
}

detect_os() {
  OS=other
  case "$(uname -s)" in
    Darwin) OS=mac ;;
    Linux)
      OS=linux
      if [ -r /etc/os-release ]; then
        local id like
        # shellcheck source=/dev/null
        id="$(. /etc/os-release && printf '%s' "${ID:-}")"
        # shellcheck source=/dev/null
        like="$(. /etc/os-release && printf '%s' "${ID_LIKE:-}")"
        case " $id $like " in *" debian "*|*" ubuntu "*) OS=debian ;; esac
      fi
      ;;
  esac
  if [ "$OS" = linux ] || [ "$OS" = other ]; then
    warn "this system is not macOS, Debian or Ubuntu. Continuing, but nothing will be installed with the package manager."
  fi
}

is_root() { [ "$(id -u)" = 0 ]; }

apt_install() {
  if [ "$YES" = 1 ] || ! [ -t 0 ]; then
    run env DEBIAN_FRONTEND=noninteractive apt-get update
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"
  else
    run apt-get update
    run apt-get install -y "$@"
  fi
}

check_git() {
  if git --version >/dev/null 2>&1; then return 0; fi
  case "$OS" in
    mac)
      run xcode-select --install || true
      die "git comes with Apple's Command Line Tools, which are being installed now. Run this command again when it finishes."
      ;;
    debian)
      if is_root; then
        apt_install git
        [ "$DRY_RUN" = 1 ] || git --version >/dev/null 2>&1 || die "git still does not work after installing it."
      else
        die "git is missing. Install it, then run this command again: sudo apt-get install -y git"
      fi
      ;;
    *) die "git is missing. Install it with your package manager, then run this command again." ;;
  esac
}

python_ok() {
  command -v python3 >/dev/null 2>&1 &&
    python3 -c 'import sys; sys.exit(sys.version_info < (3,10))' >/dev/null 2>&1
}

check_python() {
  if python_ok; then return 0; fi
  local have=""
  command -v python3 >/dev/null 2>&1 && have=" (found $(python3 --version 2>&1))"
  case "$OS" in
    mac)
      die "Leo Bot needs Python 3.10 or newer${have}. Install it from python.org, or with Homebrew: brew install python. Then run this command again."
      ;;
    debian)
      if is_root; then
        apt_install python3
        [ "$DRY_RUN" = 1 ] && return 0
        python_ok || die "Leo Bot needs Python 3.10 or newer, and this system's python3 is older. Upgrade the system, then run this command again."
      else
        die "Leo Bot needs Python 3.10 or newer${have}. Install it, then run this command again: sudo apt-get install -y python3"
      fi
      ;;
    *) die "Leo Bot needs Python 3.10 or newer${have}. Install it with your package manager, then run this command again." ;;
  esac
}

check_claude() {
  if command -v claude >/dev/null 2>&1 || [ -x "$HOME/.local/bin/claude" ]; then return 0; fi
  say "Claude Code is not installed. Bots can also run on an API key (Settings, Engines)."
  if [ "$INSTALL_CLAUDE" = 1 ] || ask "Install Claude Code now with Anthropic's official installer?" n; then
    if [ "$DRY_RUN" = 1 ]; then
      say "would: curl -fsSL https://claude.ai/install.sh | bash"
    elif ! command -v curl >/dev/null 2>&1; then
      warn "curl is missing, so Claude Code was not installed. See the Claude Code docs to install it later."
    elif ! bash -c 'set -o pipefail; curl -fsSL https://claude.ai/install.sh | bash'; then
      warn "Claude Code did not install. Leo Bot works without it; see the Claude Code docs to install it later."
    fi
  else
    say "Skipped Claude Code. Install it later from the Claude Code docs, or use an API key."
  fi
}

check_repo_url() {
  case "$REPO_URL" in
    https://*|file://*) ;;
    *) die "LEOBOT_REPO_URL must be an https:// address (or file:// for a local mirror): Leo Bot only installs and updates over a secure connection." ;;
  esac
}

# The tag to install: --version if it exists on the remote, else the highest vX.Y.Z.
pick_tag() {
  local refs
  refs="$(git ls-remote --tags --refs "$REPO_URL")" || die "could not read releases from $REPO_URL. Is the network up?"
  if [ -n "$PIN" ]; then
    case $'\n'"$refs"$'\n' in
      *$'\t'"refs/tags/$PIN"$'\n'*) TAG="$PIN"; return 0 ;;
      *) die "release $PIN does not exist." ;;
    esac
  fi
  TAG="$(printf '%s\n' "$refs" | python3 -I -c '
import re, sys
best = None
for line in sys.stdin:
    m = re.fullmatch(r"refs/tags/(v([0-9]+)\.([0-9]+)\.([0-9]+))", line.rstrip("\n").split("\t")[-1])
    if m:
        key = tuple(int(x) for x in m.group(2, 3, 4))
        if best is None or key > best[0]:
            best = (key, m.group(1))
print(best[1] if best else "")')"
  [ -n "$TAG" ] || die "there is no release yet. To install the latest code instead, run again with: --channel main"
}

STAGING=""
cleanup() {
  if [ -n "$STAGING" ] && [ -d "$STAGING" ]; then rm -rf "$STAGING"; fi
}

clone_app() {
  local app="$HOME_DIR/app"
  if [ "$CHANNEL" = main ]; then
    git ls-remote --exit-code --heads "$REPO_URL" refs/heads/main >/dev/null 2>&1 \
      || die "$REPO_URL has no main branch."
    say "Installing Leo Bot from main into $app"
  else
    pick_tag
    say "Installing Leo Bot $TAG into $app"
  fi
  run mkdir -p "$HOME_DIR"
  run chmod 700 "$HOME_DIR"
  # Clone next to the final place, then rename, so a failed clone never looks like an install.
  if [ "$DRY_RUN" = 1 ]; then
    if [ "$CHANNEL" = main ]; then
      run git clone --branch main "$REPO_URL" "$app"
    else
      run git clone "$REPO_URL" "$app"
      run git -C "$app" checkout --detach "refs/tags/$TAG"
    fi
    return 0
  fi
  STAGING="$HOME_DIR/.app-installing"
  rm -rf "$STAGING"
  if [ "$CHANNEL" = main ]; then
    git clone --quiet --branch main "$REPO_URL" "$STAGING" || die "could not download Leo Bot from $REPO_URL."
  else
    git clone --quiet "$REPO_URL" "$STAGING" || die "could not download Leo Bot from $REPO_URL."
    git -C "$STAGING" -c advice.detachedHead=false checkout --quiet --detach "refs/tags/$TAG" \
      || die "could not check out $TAG."
  fi
  mv "$STAGING" "$app"
  STAGING=""
}

# HOME/data with the marker, run/, logs/, backups/ (mode 700), and the channel in leobot.json.
# Done by the app's own code so the marker and settings formats live in one place. Folders and
# marker only: moving old data and migrations happen at the first server start, under its lock.
# A channel already saved is kept unless --channel was given.
setup_home() {
  if [ "$DRY_RUN" = 1 ]; then
    plan "create $(quote "$HOME_DIR")/{data,run,logs,backups} (mode 700) and the data marker"
    plan "save channel $CHANNEL in $(quote "$HOME_DIR/leobot.json")"
    return 0
  fi
  python3 -I - "$HOME_DIR/app" "$CHANNEL" "$CHANNEL_SET" <<'PY' || die "could not set up $HOME_DIR."
import sys
sys.path.insert(0, sys.argv[1])
from core import datadir, paths, updater
datadir.init_home()
if sys.argv[3] == "1" or not paths.SETTINGS.exists():
    updater.save_settings({"channel": sys.argv[2]})
PY
}

# First line after the shebang of every wrapper this script writes; how it knows its own.
WRAPPER_MARK="# leobot wrapper (installed by install.sh)"

# ~/.local/bin/leobot: a small wrapper, never written through a symlink.
install_wrapper() {
  local bin="$HOME/.local/bin" wrapper="$HOME/.local/bin/leobot" body tmp
  body="#!/bin/sh"$'\n'"$WRAPPER_MARK"$'\n'"# \`leobot uninstall\` removes it."$'\n'
  if [ "$CUSTOM_HOME" = 1 ]; then
    body+="export LEOBOT_HOME=$(quote "$HOME_DIR")"$'\n'
  fi
  body+="exec python3 $(quote "$HOME_DIR/app/leobot.py") \"\$@\""$'\n'
  if { [ -e "$wrapper" ] || [ -L "$wrapper" ]; } && ! wrapper_is_ours "$wrapper"; then
    warn "$wrapper already exists and is not Leo Bot's, so it was left alone. Run Leo Bot with: python3 $(quote "$HOME_DIR/app/leobot.py")"
    WRAPPER_OK=0
    return 0
  fi
  if [ "$DRY_RUN" = 1 ]; then
    run mkdir -p "$bin"
    plan "write $(quote "$wrapper") (mode 755):"
    printf '%s' "$body" | sed 's/^/    /'
  else
    mkdir -p "$bin"
    tmp="$(mktemp "$bin/.leobot.XXXXXX")"
    printf '%s' "$body" >"$tmp"
    chmod 755 "$tmp"
    mv -f "$tmp" "$wrapper"
  fi
  WRAPPER_OK=1
}

wrapper_is_ours() {
  local f="$1"
  if [ -L "$f" ]; then
    case "$(readlink "$f")" in *leobot.py) return 0 ;; *) return 1 ;; esac
  fi
  [ -f "$f" ] && grep -qxF "$WRAPPER_MARK" "$f" && grep -q '^exec python3 .*leobot\.py' "$f"
}

on_path() {
  case ":$PATH:" in *":$HOME/.local/bin:"*|*":$HOME/.local/bin/:"*) return 0 ;; *) return 1 ;; esac
}

rc_file() {
  case "$(basename "${SHELL:-}")" in
    zsh) printf '%s\n' "$HOME/.zshrc" ;;
    bash) if [ "$OS" = mac ]; then printf '%s\n' "$HOME/.bash_profile"; else printf '%s\n' "$HOME/.bashrc"; fi ;;
    *) printf '%s\n' "$HOME/.profile" ;;
  esac
}

add_to_path() {
  on_path && return 0
  local rc line
  rc="$(rc_file)"
  # Literal $HOME and $PATH: they expand when the shell starts.
  # shellcheck disable=SC2016
  line='export PATH="$HOME/.local/bin:$PATH"'
  if [ -f "$rc" ] && grep -qxF "$line" "$rc"; then
    return 0
  fi
  if [ "$DRY_RUN" = 1 ]; then
    plan "append $(quote "$line") to $(quote "$rc")"
    return 0
  fi
  printf '\n# Added by the Leo Bot installer\n%s\n' "$line" >>"$rc"
  say "Added ~/.local/bin to your PATH in $rc. New terminals pick it up."
}

build_app() {
  [ "$OS" = mac ] || return 0
  [ "$NO_APP" = 1 ] && return 0
  if ! command -v swiftc >/dev/null 2>&1 || ! xcode-select -p >/dev/null 2>&1; then
    say "Skipped the Mac app: swiftc is not installed. Leo Bot works in the browser."
    return 0
  fi
  if [ "$DRY_RUN" = 1 ]; then
    run "$HOME_DIR/app/scripts/build-app.sh" --out "$HOME/Applications"
  elif ! "$HOME_DIR/app/scripts/build-app.sh" --out "$HOME/Applications"; then
    warn "the Mac app did not build. Leo Bot works in the browser."
  fi
}

install_service() {
  local want="$SERVICE" def=n server=0
  if [ "${LEOBOT_DEPLOYMENT:-}" = server ] ||
     { [ "$OS" != mac ] && [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; }; then
    server=1
  fi
  if [ "$server" = 1 ]; then def=y; fi
  if [ -z "$want" ]; then
    if ask "Run Leo Bot as a background service that starts on its own?" "$def"; then want=1; else want=0; fi
  fi
  if [ "$want" != 1 ]; then
    say "No background service. Turn it on later with: leobot service install"
    return 0
  fi
  local cmd=(python3 "$HOME_DIR/app/leobot.py" service install)
  if [ -n "$PORT" ]; then cmd+=(--port "$PORT"); fi
  if [ "$server" = 1 ]; then cmd+=(--server); fi
  if ! run "${cmd[@]}"; then
    warn "the background service did not start. Try again later with: leobot service install"
  fi
}

main() {
  set -euo pipefail

  CHANNEL="${LEOBOT_CHANNEL:-stable}"
  CHANNEL_SET=0
  [ -z "${LEOBOT_CHANNEL:-}" ] || CHANNEL_SET=1
  PIN="${LEOBOT_VERSION:-}"
  SERVICE="${LEOBOT_SERVICE:-}"
  NO_APP="${LEOBOT_NO_APP:-0}"
  INSTALL_CLAUDE="${LEOBOT_INSTALL_CLAUDE:-0}"
  YES="${LEOBOT_YES:-0}"
  DRY_RUN="${LEOBOT_DRY_RUN:-0}"
  PORT="${LEOBOT_PORT:-}"
  TAG=""
  WRAPPER_OK=0
  WRAPPER_DONE=0
  parse_args "$@"

  case "$REPO_URL_DEFAULT" in https://*) REPO_URL="${LEOBOT_REPO_URL:-$REPO_URL_DEFAULT}" ;; *) REPO_URL="${LEOBOT_REPO_URL:-}" ;; esac
  [ -n "$REPO_URL" ] || die "this copy of install.sh has no download address. Download install.sh from the latest release, or set LEOBOT_REPO_URL."
  check_repo_url

  [ -n "${HOME:-}" ] || die "HOME is not set."
  CUSTOM_HOME=0
  HOME_DIR="${LEOBOT_HOME:-}"
  case "$HOME_DIR" in \~) HOME_DIR="$HOME" ;; \~/*) HOME_DIR="$HOME/${HOME_DIR#\~/}" ;; esac
  if [ -z "$HOME_DIR" ]; then
    HOME_DIR="$HOME/.leobot"
  else
    case "$HOME_DIR" in /*) ;; *) HOME_DIR="$PWD/$HOME_DIR" ;; esac
    HOME_DIR="${HOME_DIR%/}"
    [ "$HOME_DIR" = "$HOME/.leobot" ] || CUSTOM_HOME=1
  fi
  export LEOBOT_HOME="$HOME_DIR"
  export GIT_TERMINAL_PROMPT=0  # a wrong or private URL fails instead of asking for a password
  [ "$DRY_RUN" = 1 ] && say "Dry run: nothing on this machine changes."

  trap cleanup EXIT

  detect_os
  check_git
  check_python
  check_claude

  local updated=0
  if [ -e "$HOME_DIR/app/.git" ]; then
    # A first install cut short after the clone: finish its steps before the update takes over.
    if [ ! -e "$HOME_DIR/data/.leobot-data" ]; then
      say "Finishing the setup of $HOME_DIR first."
      setup_home
    fi
    if ! [ -e "$HOME/.local/bin/leobot" ] && ! [ -L "$HOME/.local/bin/leobot" ]; then
      install_wrapper
      WRAPPER_DONE=1
    fi
    if [ "$OS" = mac ] && [ "$NO_APP" != 1 ] && ! [ -d "$HOME/Applications/LeoBot.app" ]; then
      build_app
    fi
    say "Leo Bot is already installed in $HOME_DIR/app: updating it."
    local cmd=(python3 "$HOME_DIR/app/leobot.py" update --yes)
    if [ "$CHANNEL_SET" = 1 ]; then cmd+=(--channel "$CHANNEL"); fi
    if [ -n "$PIN" ]; then cmd+=(--version "$PIN"); fi
    run "${cmd[@]}" || die "the update did not finish. See the messages above, or run: leobot doctor"
    updated=1
  elif [ -e "$HOME_DIR/app" ] || [ -L "$HOME_DIR/app" ]; then
    die "$HOME_DIR/app exists but is not a Leo Bot install. Move it away, then run this command again."
  else
    clone_app
    setup_home
  fi

  [ "$WRAPPER_DONE" = 1 ] || install_wrapper
  add_to_path
  if [ "$updated" = 0 ]; then
    build_app
    install_service
  elif [ "$SERVICE" = 1 ]; then
    install_service
  fi

  say ""
  if [ "$DRY_RUN" = 1 ]; then
    say "Leo Bot would be installed in $HOME_DIR."
  else
    say "Leo Bot is installed in $HOME_DIR."
  fi
  if [ "$WRAPPER_OK" != 1 ]; then
    say "Run: python3 $(quote "$HOME_DIR/app/leobot.py")"
  elif on_path; then
    say "Run: leobot"
  else
    say "Run: $(quote "$HOME/.local/bin/leobot")"
  fi
}

main "$@"
