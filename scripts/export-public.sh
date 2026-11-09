#!/bin/sh
set -eu

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "Refusing export: commit or discard tracked changes first." >&2
  exit 1
fi

output=${1:-/tmp/LeoBot-public.zip}
git archive --format=zip --output="$output" HEAD
echo "Wrote tracked files only: $output"
