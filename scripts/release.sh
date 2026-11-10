#!/usr/bin/env bash
# Cut a release. Usage: release.sh X.Y.Z [--dry-run]
set -euo pipefail

ver="${1:-}"
dry=0
[ "${2:-}" = "--dry-run" ] && dry=1
[ -n "$ver" ] || { echo "usage: release.sh X.Y.Z [--dry-run]" >&2; exit 2; }
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

concl="$(gh run list --branch main --limit 1 --json conclusion --jq '.[0].conclusion')"
[ "$concl" = "success" ] || { echo "release: latest CI run on main is '$concl', need success" >&2; exit 1; }

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
  git push origin main "v$ver"
  gh release create "v$ver" --generate-notes "$tmp/install.sh#install.sh"
fi
