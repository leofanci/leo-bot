#!/usr/bin/env bash
# Cut a release. Usage: release.sh X.Y.Z [--dry-run]
#
# main only changes through pull requests, so a release takes two runs when VERSION is lower:
#   1. release.sh X.Y.Z  opens a pull request that writes X.Y.Z into VERSION (branch release-vX.Y.Z).
#   2. Merge it once CI is green, update main, then run release.sh X.Y.Z again: VERSION now says
#      X.Y.Z, so HEAD is tagged vX.Y.Z and the GitHub Release is published.
# The second step alone is the first release of the version already in VERSION. A lower
# version, or a tag that exists (locally or on origin), is always refused.
set -euo pipefail

# What releasing $2 means when VERSION says $1: bump (a pull request that writes VERSION),
# tag (tag HEAD as it is), or refuse.
version_action() {
  local cur="$1" ver="$2" newest
  if [ "$ver" = "$cur" ]; then
    echo tag
    return 0
  fi
  newest="$(printf '%s\n%s\n' "$cur" "$ver" | sort -t. -k1,1n -k2,2n -k3,3n | tail -1)"
  if [ "$newest" = "$ver" ]; then echo bump; else echo refuse; fi
}

usage() { echo "usage: release.sh X.Y.Z [--dry-run]" >&2; exit 2; }

main() {
  local ver="" dry=0 arg root cur action head concls tmp branch
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
  action="$(version_action "$cur" "$ver")"
  if [ "$action" = refuse ]; then
    echo "release: $ver is lower than the current version $cur" >&2
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

  if git rev-parse -q --verify "refs/tags/v$ver" >/dev/null; then
    echo "release: tag v$ver exists locally" >&2
    exit 1
  fi
  if git ls-remote --tags origin "refs/tags/v$ver" | grep -q .; then
    echo "release: tag v$ver exists on origin" >&2
    exit 1
  fi

  if [ "$action" = bump ]; then
    branch="release-v$ver"
    if git rev-parse -q --verify "refs/heads/$branch" >/dev/null \
        || git ls-remote --heads origin "refs/heads/$branch" | grep -q .; then
      echo "release: branch $branch exists already (an open release pull request?)" >&2
      exit 1
    fi
    step "create branch $branch"
    step "write VERSION $ver"
    step "commit Release v$ver"
    step "push $branch and open a pull request"
    step "after it merges with CI green: update main, then run scripts/release.sh $ver again"
    if [ "$dry" = 0 ]; then
      git switch -c "$branch"
      printf '%s\n' "$ver" > VERSION
      git add VERSION
      git commit -m "Release v$ver"
      git push -u origin "$branch"
      gh pr create --base main --head "$branch" --title "Release v$ver" \
        --body "Sets VERSION to $ver. After this merges with CI green, run: scripts/release.sh $ver"
      git switch main
      echo "Opened the release pull request. Merge it once CI is green, then run: git pull && scripts/release.sh $ver"
    fi
    return 0
  fi

  tmp="$(mktemp -d)"
  # shellcheck disable=SC2064  # expand now: tmp is local to main
  trap "rm -rf '$tmp'" EXIT
  step "render installer"
  "$root/scripts/render-installer.sh" "$tmp/install.sh"
  echo "installer repo line: $(grep '^REPO_URL_DEFAULT=' "$tmp/install.sh")"

  step "VERSION is $ver: tag v$ver at HEAD"
  step "push tag"
  step "gh release create v$ver with install.sh"
  if [ "$dry" = 0 ]; then
    git tag "v$ver"
    git push origin "v$ver"
    if ! gh release create "v$ver" --generate-notes "$tmp/install.sh#install.sh"; then
      echo "release: tag v$ver is pushed but the GitHub Release failed. Recover with:" >&2
      echo "  scripts/render-installer.sh /tmp/install.sh && gh release create v$ver --generate-notes /tmp/install.sh#install.sh" >&2
      exit 1
    fi
  fi
}

# Sourced (by the tests, for version_action): define only.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
