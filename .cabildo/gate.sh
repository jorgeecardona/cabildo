#!/usr/bin/env bash
# The referee. The daemon runs this from the repo root before it pushes, and a
# non-zero exit means nothing lands.
#
# Repo-owned on purpose: the agent asking to land does not get to choose the
# thing that judges it. Keep it fast and keep it honest — a gate nobody waits
# for gets deleted, and a gate that exits 0 on a broken suite is worse than no
# gate at all, because it hands out confidence nobody earned.
set -euo pipefail

cd "$(dirname "$0")/.."

# Jorge's venv when it's there, otherwise whatever python3 is on PATH — the
# daemon may run this from a systemd unit with a thinner environment.
PY="${PYTHON:-}"
if [[ -z "$PY" ]]; then
    if [[ -x "$HOME/.venv/bin/python" ]]; then PY="$HOME/.venv/bin/python"; else PY="python3"; fi
fi

echo "gate: $("$PY" --version) · $(git rev-parse --short HEAD)"
"$PY" -m pytest -q
