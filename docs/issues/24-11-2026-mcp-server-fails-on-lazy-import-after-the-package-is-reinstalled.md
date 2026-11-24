---
title: The MCP server fails with "cannot import name 'sessions_ops'" after the package is reinstalled under it
status: open
area: mcp
owner: null
filed: 2026-11-24
fixed_in: null
priority: low
scope: S
verify: test
---

MEASURED: `mcp__cabildo__sessions_rename` (and any other tool that does `from . import sessions_ops` inside the function, src/cabildo/mcp_server.py lines 1739, 1759, 1778, 1803) failed in a session with:

`cannot import name 'sessions_ops' from 'cabildo' (/home/jcardona/.venv/lib/python3.15/site-packages/cabildo/__init__.py)`

The file `src/cabildo/sessions_ops.py` exists (added 2026-11-19, bf1d1c1), and a fresh `python -c "import cabildo"` in the same venv loads `src/cabildo/__init__.py` (0.1.235, editable). The `site-packages/cabildo/` folder today holds only `_bundled_docs` and `_bundled_mods` (changed 2026-11-22 23:41) and has no `__init__.py`. The `cabildo-mcp` process of that session started 2026-11-22 00:04, before that change.

LIKELY (not proven): the server imported `cabildo` from a copy in site-packages, the package was reinstalled while the server ran, and each lazy `from . import x` then looks in the old, now changed, package folder. Tools that import at module top keep working, so only some tools break and only in old sessions. A restart of the server fixes it.

Fix to consider: import `sessions_ops` and the other helpers at the top of `mcp_server.py`, so the server fails at start and not at the first call. A test starts the server module and checks that every tool's imports resolve.

Done when: a test (tests/test_mcp_imports.py) imports `cabildo.mcp_server` and fails if any function body in it has a relative `from . import` that is not also available at the top of the module.
