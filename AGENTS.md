# AGENTS.md

Guidance for AI coding agents working in this repo. Humans: read README.md and CHANGELOG.md.

## Overview

cabildo is an IPC bus and chat room for coordinating parallel Claude Code agents: messaging, presence, and advisory locks with hook-level enforcement. It is a normal Python package (a stdio MCP server plus hook scripts); all state lives in one shared SQLite DB, single-machine by default. Sessions are client-tagged (`sessions.client`), so Codex can share the same bus — `cabildo codex` wires it via `codex mcp add` (or prints the `~/.codex/config.toml` block); Codex sessions identify by `CABILDO_CLIENT=codex` + the owner-pid path in `mcp_server._me`. Two optional, opt-in cross-machine transports exist: `cabildo sync` (one-way mirror to the hosted App Engine dashboard) and `cabildo lan` (`lan.py` — peer-to-peer message sync over the local subnet, nothing leaving the LAN; messages carry a global `guid` for cross-machine dedup, and `origin='lan'` rows are excluded from the cloud uplink).

## Build

- Toolchain (moon, uv) is pinned in `.prototools`; `proto install` brings it up.
- `uv sync` installs dependencies. The package is stdlib-only at runtime; the only runtime dependency is `mcp`.
- `python scripts/install.py` (aka `make install`) does the full editable install: it also registers the MCP server and wires the git hooks. `--client {claude,codex,both}` selects which client(s) to wire (default `claude`); `--print` is a non-mutating dry run.
- `make install-hooks` once, to point git at the tracked `.githooks/` (pre-commit + pre-push).
- `make build` builds the sdist and wheel into `dist/`.

## Test

- `make test` runs the suite (pytest).
- `make check` runs lint + typecheck + test. Everything in it must pass before you commit.
- CI runs the full graph with `moon run :ci` (and `moon run :release` on main). `:ci` also covers build, docs, and the version guard.

## Style

- Format and autofix: `make format` (ruff, line length 100).
- Lint: `make lint` (ruff). Types: `make typecheck` (pyright, strict).
- Prefer explicit types and small, well-typed functions; keep the public API typed.

## Gotchas

- Version lives only in `pyproject.toml`. The pre-commit hook auto-bumps it when `src/` changes, and CI's version-guard enforces that a `src/` change carries a bump. Do not edit versions by hand.
- The `mcp` dependency is pinned `<2` on purpose: mcp 2.0 moves `FastMCP`, so an unbounded install resolves to a version where the import is gone and the MCP tools silently never appear. Relax it only in the same commit that migrates the imports.
- moon owns task ordering; do not hand-wire CI stages. Run tasks through `make` or `moon run`.
- Some features rely on platform specifics (`/proc` process identity, `notify-send`, `systemd --user`); `cabildo doctor` flags what a given OS lacks.
