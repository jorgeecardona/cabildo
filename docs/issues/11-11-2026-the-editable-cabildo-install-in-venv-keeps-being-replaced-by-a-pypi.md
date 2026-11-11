---
title: The editable cabildo install in ~/.venv keeps being replaced by a PyPI wheel
status: open
area: install
owner: null
filed: 2026-11-11
priority: high
scope: S
verify: test
---

MEASURED: three times (2026-11-10, 2026-11-11 09:50, 2026-11-11 19:04) ~/.venv/lib/python3.13/site-packages/cabildo became a regular install from the index (INSTALLER=uv, no direct_url.json), version 0.1.168 then 0.1.174. The daemon (ExecStart ~/.venv/bin/python3 -m cabildo.daemon) then ran old code: Grant/Deny popups and improve popups did not show until `VIRTUAL_ENV=~/.venv uv pip install -e .` and a daemon restart.
INFERRED: ~/.bashrc:138 activates ~/.venv in every shell, so any `uv pip install cabildo` or upgrade run by an agent or script in any project lands there. The culprit is not found: no match in chat.db turns around 19:04, none in crm gate scripts.
Done when: the daemon runs from a venv nothing else installs into (service.py unit), or `cabildo doctor` warns when the daemon's cabildo is not the editable checkout; and a week passes with no silent replacement.