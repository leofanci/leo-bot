#!/usr/bin/env bash
# Cut a release. Usage: release.sh X.Y.Z [--dry-run]
set -euo pipefail

usage() { echo "usage: release.sh X.Y.Z [--dry-run]" >&2; exit 2; }
ver=""
dry=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) dry=1 ;;
    -*) usage ;;
    *) [ -z "$ver" ] || usage; ver="$arg" ;;
  esac
done
[ -n "$ver" ] || usage
[[ "$ver" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "release: $ver is not X.Y.Z" >&2; exit 1; }

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

step() { if [ "$dry" = 1 ]; then echo "[dry-run] $*"; else echo "==> $*"; fi; }

[ "$(git rev-parse --abbrev-ref HEAD)" = "main" ] || { echo "release: not on main" >&2; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "release: working tree not clean" >&2; exit 1; }

cur="$(tr -d '[:space:]' < VERSION)"
newest="$(printf '%s\n%s\n' "$cur" "$ver" | sort -t. -k1,1n -k2,2n -k3,3n | tail -1)"
if [ "$ver" = "$cur" ] || [ "$newest" != "$ver" ]; then
  echo "release: $ver must be greater than current $cur" >&2
  exit 1
fi

git fetch origin main
head="$(git rev-parse HEAD)"
[ "$head" = "$(git rev-parse origin/main)" ] || { echo "release: HEAD is not origin/main" >&2; exit 1; }

concls="$(gh run list --branch main --commit "$head" --json conclusion --jq '.[].conclusion')"
[ -n "$concls" ] || { echo "release: no CI run found for $head" >&2; exit 1; }
if [ "$(printf '%s\n' "$concls" | grep -vc '^success$')" != 0 ]; then
  echo "release: CI runs for $head are not all success" >&2
  exit 1
fi

git rev-parse -q --verify "refs/tags/v$ver" >/dev/null && { echo "release: tag v$ver exists locally" >&2; exit 1; }
if git ls-remote --tags origin "refs/tags/v$ver" | grep -q .; then
  echo "release: tag v$ver exists on origin" >&2
  exit 1
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
step "render installer"
"$root/scripts/render-installer.sh" "$tmp/install.sh"
echo "installer repo line: $(grep '^REPO_URL_DEFAULT=' "$tmp/install.sh")"

step "write VERSION $ver"
step "commit Release v$ver"
step "tag v$ver"
step "push commit and tag"
step "gh release create v$ver with install.sh"
if [ "$dry" = 0 ]; then
  printf '%s\n' "$ver" > VERSION
  git add VERSION
  git commit -m "Release v$ver"
  git tag "v$ver"
  git push --atomic origin main "v$ver"
  if ! gh release create "v$ver" --generate-notes "$tmp/install.sh#install.sh"; then
    echo "release: tag v$ver is pushed but the GitHub Release failed. Recover with:" >&2
    echo "  scripts/render-installer.sh /tmp/install.sh && gh release create v$ver --generate-notes /tmp/install.sh#install.sh" >&2
    exit 1
  fi
fi
