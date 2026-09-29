#!/usr/bin/env bash
# The referee. The daemon runs this from the repo root before it pushes, and a
# non-zero exit means nothing pushes.
#
# Repo-owned on purpose: the agent asking to push does not get to choose the
# thing that judges it. Keep it fast and keep it honest — a gate nobody waits
# for gets deleted, and a gate that exits 0 on a broken suite is worse than no
# gate at all, because it hands out confidence nobody earned.
set -euo pipefail

cd "$(dirname "$0")/.."

# Pick the interpreter, in order: an explicit $PYTHON; the repo's own venv
# (uv-managed and editable, so it tracks src/); Jorge's ~/.venv; then whatever
# python3 is on PATH. The repo venv comes before ~/.venv on purpose: a stale
# package install in ~/.venv silently tests old code, which is exactly the false
# "gate passed" this file exists to prevent. The daemon may run this from a
# service unit with a thinner environment, hence the fallbacks.
PY="${PYTHON:-}"
if [[ -z "$PY" ]]; then
    if [[ -x ".venv/bin/python" ]]; then PY=".venv/bin/python"
    elif [[ -x "$HOME/.venv/bin/python" ]]; then PY="$HOME/.venv/bin/python"
    else PY="python3"; fi
fi

echo "gate: $("$PY" --version) · $(git rev-parse --short HEAD)"
"$PY" -m pytest -q
