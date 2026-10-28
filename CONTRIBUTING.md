# Contributing to cabildo

Thanks for helping. This is a normal Python package: a stdio MCP server plus
hook scripts, stdlib-only at runtime (the sole runtime dependency is `mcp`). All
state lives in one shared SQLite DB.

## Contributing with an AI agent

Most changes here will be written with a coding agent — that's expected and welcome. To get a good
result from yours:

- **Point it at [`AGENTS.md`](AGENTS.md) first** — the machine-facing contract for this repo: build
  and test commands, the style rules, and the repo-specific gotchas (the `mcp<2` pin, platform
  specifics). Agents read it automatically when started from the repo root.
- **Ship a green `make check`** (lint + format-check + typecheck + test). Have the agent run it and
  fix what it finds *before* opening the MR, not leave it for review.
- **Don't let it bypass the hooks.** The patch version auto-bumps on `src/` changes and CI's
  version-guard enforces it; a `--no-verify` commit fails the pipeline.
- **Keep the diff small and typed**, add a `CHANGELOG.md` entry, and don't reformat unrelated files.

This project follows a [Code of Conduct](CODE_OF_CONDUCT.md). To report a
security issue, see [SECURITY.md](SECURITY.md) rather than filing a public issue.

## Prerequisites

The toolchain (moon, uv) is pinned in `.prototools`. Install it with
[proto](https://moonrepo.dev/proto):

```bash
proto install
```

Python 3.11 or newer is required (see `requires-python` in `pyproject.toml`).

## Setup

```bash
uv sync                      # install dependencies into .venv
python scripts/install.py    # editable install + register the MCP server + wire hooks
make install-hooks           # point git at the tracked .githooks/ (once)
```

`python scripts/install.py --print` is a non-mutating dry run that shows what it
would do. `--client {claude,codex,both}` selects which client(s) to wire
(default `claude`).

## Development loop

```bash
make format    # auto-format + autofix (ruff, line length 100)
make check     # the full CI test stage: lint + format-check + typecheck + test
```

`make check` must pass before you commit. Individual stages:

- `make lint` runs ruff (same as CI).
- `make format-check` verifies formatting without changing files; run
  `make format` to fix.
- `make typecheck` runs pyright in strict mode.
- `make test` runs the pytest suite.

CI runs the full graph with `moon run :ci`. moon owns task ordering, so run
tasks through `make` or `moon run`, never by hand-wiring stages.

## Style

- Formatting and lint are enforced by ruff at line length 100.
- Types are enforced by pyright in strict mode. Prefer explicit types and small,
  well-typed functions, and keep the public API typed.

## Versioning

The version is the `__version__` literal in `src/cabildo/__init__.py`, which
hatchling reads so `pyproject.toml` never disagrees. Do not edit it by hand: the
pre-commit hook auto-bumps it when anything under `src/` changes, and CI's
version-guard enforces that a `src/` change carries a bump. Use `make bump
TYPE=patch|minor|major` only when you need to bump without a `src/` change.

## Changelog

Add an entry under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md) for any
user-visible change. Versioning follows [SemVer](https://semver.org/).

## Commit messages

Write them for a stranger reading the history later: state what changed and why.
The repo uses lowercase, area-prefixed subjects (for example
`guard: directory-prefix file claims`).

## Issues and merge requests

- File issues at
  <https://gitlab.com/cardona-oss/cabildo/-/issues>.
- Open merge requests against `main`. Make sure `make check` passes first.

## Gotchas

- The `mcp` dependency is pinned `<2` on purpose: mcp 2.0 moves `FastMCP`, so an
  unbounded install resolves to a version where the import is gone and the MCP
  tools silently never appear. Relax it only in the same commit that migrates the
  imports.
- Some features rely on platform specifics (`/proc` process identity,
  `notify-send`, `systemd --user`). Run `cabildo doctor` to see what a given OS
  lacks.

## License

By contributing, you agree that your contributions are licensed under the MIT
License, the same terms as the project (see [LICENSE](https://gitlab.com/cardona-oss/cabildo/-/blob/main/LICENSE)).
