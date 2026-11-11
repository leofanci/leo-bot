#!/usr/bin/env bash
# Render install.sh with the repo URL filled in. Usage: render-installer.sh OUT
set -euo pipefail

out="${1:-}"
[ -n "$out" ] || { echo "usage: render-installer.sh OUT" >&2; exit 2; }

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src="$root/install.sh"
[ -f "$src" ] || { echo "render-installer: $src not found" >&2; exit 1; }

origin="$(git -C "$root" remote get-url origin)"
case "$origin" in
  *@github.com:*)          path="${origin#*@github.com:}" ;;
  ssh://*@github.com/*)    path="${origin#*@github.com/}" ;;
  https://github.com/*)    path="${origin#https://github.com/}" ;;
  *) echo "render-installer: origin is not on github.com: refusing" >&2; exit 1 ;;
esac
path="${path%/}"
path="${path%.git}"
[[ "$path" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] \
  || { echo "render-installer: cannot read account/repo from origin" >&2; exit 1; }
url="https://github.com/${path}.git"

grep -q '^REPO_URL_DEFAULT="__LEOBOT_REPO_URL__"$' "$src" \
  || { echo "render-installer: placeholder line missing in install.sh" >&2; exit 1; }

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
while IFS= read -r line || [ -n "$line" ]; do
  if [ "$line" = 'REPO_URL_DEFAULT="__LEOBOT_REPO_URL__"' ]; then
    printf 'REPO_URL_DEFAULT="%s"\n' "$url"
  else
    printf '%s\n' "$line"
  fi
done < "$src" > "$tmp"
mv "$tmp" "$out"
trap - EXIT
