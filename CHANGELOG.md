# Changelog

All notable changes to cabildo are documented here. Versioning is [SemVer](https://semver.org/).

## [Unreleased]

## [0.1.7] - 2026-09-12

### Added

- **macOS process identity (#2, #11)** — `context.py`'s `_ppid`/`_argv` gain a
  `ps`-backed darwin backend (`ps -o ppid=`, `ps -ww -o command=`) behind a
  `sys.platform` check, so the MCP server can resolve its owning `claude` session
  on macOS, where there is no `/proc`. The Linux `/proc` fast path is byte-for-byte
  unchanged. `resolve_claude_pid`/`_is_claude`/`_exe_id` operate on the returned
  argv and are unchanged. Darwin path is unit-tested against mocked `ps` output;
  not yet run on a real Mac. (Bulk process enumeration in `procs.py` stays
  Linux-only — see #3 for its memory backends.)

## [0.1.6] - 2026-09-12

### Added

- **macOS desktop notifications (#4)** — `desktop.py` gains a darwin backend
  behind a `sys.platform` check (the Linux `notify-send`/D-Bus path is unchanged):
  `available()` returns true when `osascript` is present, passive notifications use
  `osascript display notification`, and the `request(action="ask")` button
  round-trip uses `terminal-notifier` (or an `osascript` dialog fallback). Without
  it, urgent-to-human prompts silently no-op'd on macOS. Darwin path is unit-tested
  against mocked `osascript`/`terminal-notifier` output; not yet run on a real Mac.

## [0.1.5] - 2026-09-12

### Added

- **`cabildo doctor`** — one command that verifies the whole install and flags
  what won't work on this machine. It checks the six hooks are wired in
  `~/.claude/settings.json`, that the `cabildo` bus server is registered (user or
  project scope) and the per-repo `issues` server is wired where a `docs/issues/`
  exists, that the SQLite bus is reachable and in WAL, and whether a daemon is
  live. Crucially it also flags the platform features cabildo leans on that a
  given OS may lack — `/proc` process identity, `notify-send`, and
  `systemd --user` — so the macOS gaps surface at check time instead of as an
  agent that mysteriously never joins the bus. Exits non-zero only on a genuine
  break; platform gaps and a missing (optional) daemon are warnings.

## [0.1.4] - 2026-09-12

### Fixed

- **`show_issue` no longer overruns `SHOW_LIMIT`.** The truncation footer embeds
  the issue's absolute path, whose length is unbounded, so a long path pushed the
  output past the cap (a red suite on macOS, where pytest temp dirs live under
  `/private/var/folders/...`). The footer's rendered length, path included, is
  now budgeted against `SHOW_LIMIT`, so body + footer stays within the cap
  regardless of path length. (#1)

## [0.1.3] - 2026-09-12

### Added

- **`cabildo viewer`** — a read-only local web view of the bus (agents, locks,
  messages) served on `http://127.0.0.1:8787`, refreshing every 2s. Stdlib
  `http.server` only, binds to localhost, needs nothing running. It is the open
  counterpart of the hosted dashboard: the same glanceable view of your fleet,
  with no account and no server.

### Changed

- **The hosted backend split out.** The App Engine bus, the browser dashboard,
  and the machine runner moved to a separate private repo, `cabildo-backend`.
  cabildo is now self-contained: the local SQLite bus, the two MCP servers, the
  hooks, the `watch` TUI, and the new `viewer` all work with no network and no
  account. `cabildo sync` remains here as the optional one-way client that
  mirrors the local bus up to a hosted instance.
- **Repo aligned to the standard OSS template**: proto/moon-driven GitLab CI
  (one `moon run :ci`, `:release` on main via PyPI OIDC Trusted Publishing),
  `pre-commit`/`pre-push` git hooks, version-bump guards (`scripts/bump-version`,
  `check-version-bump`, `ci-version-guard`), ruff + pyright config, mkdocs +
  Pages, and `uv.lock`. `__version__` is now mirrored from `pyproject` and
  asserted by a test.

## [0.1.2]

- Baseline release on PyPI: the coordination bus (direct/topic messaging,
  presence, advisory locks), the `issues` MCP server, hook-level lock
  enforcement, the daemon (intent queue + desktop notifications), and
  `cabildo sync`.
