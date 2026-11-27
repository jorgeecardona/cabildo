# AGENTS.md

Guidance for AI coding agents working in this repo. Humans: read README.md and CHANGELOG.md.

## Overview

cabildo is an IPC bus and chat room for coordinating parallel Claude Code agents: messaging,
presence, and advisory locks with hook-level enforcement. It is a normal Python package (a stdio
MCP server plus hook scripts); all state lives in one shared SQLite DB, single-machine by default.

`cabildo panel` (`workspace/panel.py`) prints what the VS Code side bar (`packages/vscode/`) shows,
one JSON object per line, and reads commands on stdin. It shares `workspace/modeline.py` and
`workspace/routing.py` with the TUI. `packages/vscode/src/types.ts` repeats the dataclasses of
`panel.py`: change both together. Its console view (`src/attach.ts`) speaks the wire format of
`host.py`.

Sessions are client-tagged (`sessions.client`), so Codex can share the same bus: `cabildo codex`
wires it via `codex mcp add` (or prints the `~/.codex/config.toml` block); Codex sessions
identify by `CABILDO_CLIENT=codex` + the owner-pid path in `mcp_server._me`.

Two cross-machine transports exist, both peer-to-peer: LAN pairing (`request("pair")`,
`pair.py` + `lan.Service` in the daemon: PIN pairing, then message sync over mutual TLS on the
local subnet) and `cabildo wan` (`wan.py`: message sync with explicit remote peers over a tunnel);
messages carry a global `guid` for cross-machine dedup.

`cabildo jupyter` (`jupyter.py`, `wsclient.py`) starts detached Jupyter Servers with `uv run` (no
Jupyter dependency in cabildo) and runs cells over the server's kernel WebSocket; the MCP tools are
`jupyter_*`; `jupyter_cells.py` edits a served notebook's cells through the contents API, and
`guard.deny_notebook_write` blocks direct writes to it. State is in `~/.cabildo/jupyter/<name>/`. The daemon runs `jupyter.Supervisor.tick`
every 15 s: it starts `autostart` runners, starts them again when they end, and pauses one over
`memory_max`.

`cabildo browser serve --chrome` drives the person's own Chrome: the extension
(`browser/extension/`) talks to `chrome_host.py` over native messaging, and `chrome_relay.py` shows
it to Playwright as a DevTools WebSocket. `background.js` is the only place that limits an agent to
the `cabildo` tab group and to a list of DevTools commands. Run its end-to-end test with
`CABILDO_CHROME_E2E=1` (it opens a Chromium window).

`cabildo gateway` (`gateway.py`) is a local HTTP proxy for `ANTHROPIC_BASE_URL` that fails over
between upstreams; it runs as a service of the daemon (`gateway.start_service`, like LAN and WAN;
on reload it hands its listening socket to the new process) and is the only part that sends model
traffic to outside hosts. `cabildo gateway enable` writes the base URL into the user's Claude Code
settings so every session uses it. Bedrock keys can be in `~/.config/cabildo/keys.toml` under
`[bedrock.keys]`; an upstream with no `key` uses the one of its own name. The token counts of
each reply go to the table `gateway_usage` (`gateway_usage.py`) under the upstream name. The
`help` MCP tool (`help.py`) serves the pages of `docs/`, which the wheel bundles.

## Build

- Toolchain (moon, uv) is pinned in `.prototools`; `proto install` brings it up.
- `uv sync` installs dependencies. Runtime dependencies: `zeroconf` (mDNS discovery
  for LAN pairing), `textual`, `textual-tty`, `playwright`. There is no `mcp` SDK: the MCP servers
  use `src/cabildo/mcp_stdio.py`.
- `python scripts/install.py` (aka `make install`) does the full editable install: it also
  registers the MCP server and wires the git hooks.
  `--client {claude,codex,opencode,both,all}` selects which client(s) to wire (default `claude`);
  `--print` is a non-mutating dry run.
- `make install-hooks` once, to point git at the tracked `.githooks/` (pre-commit + pre-push).
- `make build` builds the sdist and wheel into `dist/`.

## Test

- `make test` runs the suite (pytest).
- `make check` runs lint + format-check + typecheck + test. Everything in it must pass before
  you commit.
- CI runs the full graph with `moon run :ci` (and `moon run :release` on main). `:ci` also
  covers build, docs, and the version guard.

## Style

- Format and autofix: `make format` (ruff, line length 100).
- Lint: `make lint` (ruff, `select = ["ALL"]` with per-file ignores). Types: `make typecheck`
  (pyright, standard; `reportPrivateUsage = false`).
- Python 3.12+ only.
- Prefer explicit types and small, well-typed functions; keep the public API typed.

## Gotchas

- The version is the `__version__` literal in `src/cabildo/__init__.py`; hatchling reads it so
  `pyproject.toml` never disagrees. The pre-commit hook auto-bumps it when `src/` changes, and
  CI's version-guard enforces that a `src/` change carries a bump. Do not edit versions by hand.
- The MCP servers are `mcp_stdio.McpServer`, written in this repo: stdio only, the `tools`
  capability only, and the `initialize` era of the protocol (`2024-11-05` to `2025-11-25`). It
  answers `server/discover` with "method not found", so a client of the newer era falls back to
  `initialize`. A tool parameter needs a type hint that `mcp_stdio.schema_of` maps (`str`, `int`,
  `float`, `bool`, `Any`, `Literal`, `list`, `dict`, and `| None`); another type fails when the
  tool is defined.
- moon owns task ordering; do not hand-wire CI stages. Run tasks through `make` or `moon run`.
- Some features rely on platform specifics (`/proc` process identity, `notify-send`,
  `systemd --user`); `cabildo doctor` flags what a given OS lacks.
