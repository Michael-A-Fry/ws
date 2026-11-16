#!/bin/sh
# Vale for the pre-commit hook: syncs the style package only when it is missing, then lints the files given.
# The package under .github/vale/ is git-ignored, so a fresh clone (and CI's prek step, which runs before CI's own
# `vale sync`) has none; without the sync, vale fails on every file. When the package is there, no network is used.
set -eu
if [ ! -d .github/vale/Microsoft ]; then
  vale sync >/dev/null
fi
exec vale "$@"
