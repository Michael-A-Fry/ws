#!/usr/bin/env bash
# Re-record the README's terminal screens from docs/tapes/*.tape into docs/assets/.
#
# Runs charmbracelet VHS from its pinned container image, so nothing is installed here and every run uses the same
# VHS, fonts and terminal. ws itself is the wheel built from this checkout, unpacked inside the container. The tapes
# record under a throwaway demo HOME (docs/tapes/demo-home.sh), never your own.
#
#   scripts/record_screens.sh            record every tape
#   scripts/record_screens.sh gate       record one (docs/tapes/gate.tape)
#
# Needs docker and uv. Open every image before you commit it: the PII gate does not read pictures.
set -euo pipefail

# VHS v0.10.0, pinned by digest.
IMAGE="ghcr.io/charmbracelet/vhs:v0.10.0@sha256:e88ed3faa06183a197fd44ded83e706098d9e4038b72da94bcdb9cb9b67e3527"

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

tapes=()
if [ "$#" -eq 0 ]; then
  for t in docs/tapes/*.tape; do
    case "$(basename "$t")" in _*) ;; *) tapes+=("$t") ;; esac
  done
else
  for name in "$@"; do tapes+=("docs/tapes/$name.tape"); done
fi

build="$(mktemp -d)"
trap 'rm -rf "$build"' EXIT
uv build --wheel --quiet --out-dir "$build" >/dev/null
wheel="$(ls "$build"/*.whl)"

for tape in "${tapes[@]}"; do
  echo "recording $tape"
  docker run --rm \
    -v "$root:/ws" -v "$wheel:/tmp/ws.whl:ro" -w /ws \
    --entrypoint bash "$IMAGE" -c '
      set -euo pipefail
      python3 -m zipfile -e /tmp/ws.whl /usr/local/lib/python3.11/dist-packages
      printf "#!/bin/sh\nexec python3 -I -m ws \"\$@\"\n" >/usr/local/bin/ws
      chmod +x /usr/local/bin/ws
      bash docs/tapes/demo-home.sh
      vhs "$1"
    ' _ "$tape"
done
echo "done: docs/assets/"
