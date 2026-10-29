---
title: Browser lifecycle is managed by a loose script and hand-edited JSON, not by cabildo
status: open
scope: M
owner: null
filed: 2026-08-29
area: cli
verify: probe
blocked_by: null
fixed_in: null
---

The MCP server that agents browse with is now called `cabildo-browser`, but cabildo does not
manage it. Its configuration lives in `~/.claude.json`, its login refresh is a loose bash
script at `~/.claude/bin/cabildo-browser-login`, and its stray processes are reaped by hand.
Cabildo already owns fleet state on this machine (`cabildo mem`, `cabildo agents`,
`cabildo fleet`); the browser is the one shared resource that sits outside it.

## What is MEASURED

On 2026-08-29, before any change:

- **22** `@playwright/mcp/cli.js` servers and **32** Chrome processes were alive at once, every
  one pointed at the same `--user-data-dir`
  (`~/.cache/ms-playwright-mcp/mcp-chrome-for-testing-3b855a0`). Together they held **2.2 GB**
  RSS. Reaping them moved available RAM from **4.3 GB to 12 GB**.
- Every one of those 22 had a **live `claude` parent**. Nothing was orphaned; the sessions were
  simply never closed. Ages ranged from 18 minutes to 10.5 hours.
- Chrome permits one process per profile directory, so 21 of the 22 were unusable. Across
  **373 transcripts** under `~/.claude/projects`, the string
  `Browser is already in use for .../mcp-chrome-for-testing-3b855a0, use --isolated` appears
  **276 times**.
- `playwright-mcp` 0.0.79 supports `--isolated`, `--storage-state <path>` and
  `--shared-browser-context` (`playwright-mcp --help`).
- With `--isolated`, four browsers launched concurrently, each executing JS, with no contention.
- `cabildo.mcp_server.claim` (line 489) documents its locks as **advisory** — "agents must honor
  them by convention".
- Exporting cookies from the persistent profile to a storage-state file is **refused by the
  auto-mode classifier**. It was attempted twice and blocked both times, so the current
  `storage-state.json` is the empty `{"cookies":[],"origins":[]}`.
- `pkill -f '@playwright/mcp/cli.js'` kills the invoking shell, because the pattern matches that
  shell's own command line. Observed twice, exit code 144. The working form scans `/proc`
  cmdlines and skips `$$`.

## What is INFERRED

- That seeding `--storage-state` from a real logged-in profile will keep agents logged in is
  **untested**. The mechanism is documented and the file is wired up, but it has only ever been
  exercised with an empty state.
- That the 2.2 GB is a steady leak rather than a one-off. It is consistent with sessions being
  left open on a machine that swaps hard, but only one sample was taken.

## What "done" looks like

`cabildo browser` exists and is the only thing anyone touches:

- `cabildo browser status` — how many MCP servers and Chrome processes are alive, their ages and
  RSS, which config each is running, and whether the storage state is empty or stale. Must not
  use `pkill -f`; scan `/proc` as above.
- `cabildo browser reap` — kill browser processes whose parent session is gone, and optionally
  all of them. Reports what it freed. This is the piece `cabildo mem` should be able to call.
- `cabildo browser login` — replaces `~/.claude/bin/cabildo-browser-login`: free the persistent
  profile, open it headed, export storage state on close, chmod 600. Keeping it in cabildo means
  the cookie export runs under the user's own hand, which is what the classifier requires.
- `cabildo browser doctor` — assert the `cabildo-browser` entry in `~/.claude.json` still carries
  `--isolated`, and warn loudly if a `--user-data-dir` has come back.
  Also fail if the entry carries `--headless`: the browser must always run headed (rule set
  by Jorge on 2026-10-24).

`verify: probe` because the load-bearing claim cannot be tested in isolation: closing this means
watching a real agent open a real page **logged in**, from a storage state produced by
`cabildo browser login`, while a second agent browses at the same time.

## Deliberately NOT done

- **Not built on `cabildo claim`/`release`.** They are advisory and require every agent to
  remember to claim. `--isolated` removes the shared resource instead, so there is nothing left
  to arbitrate. Reintroducing a claim here would restore the coordination burden that this
  removed.
- **No second always-on MCP entry** for the persistent profile. Every session would spawn it and
  the contention returns; that is the whole bug.
- **`~/.claude/mcp-servers/playwright/` was not renamed.** It is the npm install directory and is
  referenced by `command` and by the storage-state path; renaming it is churn with no benefit.
- **The 1.8 GB profile was not deleted.** It is the only source of logins until
  `cabildo browser login` runs successfully once.

## Status 2026-10-29

Built: `cabildo browser status`, `reap`, `login`, `doctor` and `serve` (a Python Playwright MCP
server). The needs are in `product/needs/browser-operator.yaml`, each with test proofs. Still
open, because `verify: probe` asks for a real agent logged in from a state made by
`cabildo browser login`, next to a second agent browsing: that run has not happened, and the live
`cabildo-browser` entry in `~/.claude.json` still points at the npm `playwright-mcp`.
