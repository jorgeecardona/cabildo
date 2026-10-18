---
title: Simplify git coordination — guard the irrecoverable (force-push), coach the recoverable (push recipe), isolate by worktree
status: open
scope: L
owner: null
filed: 2026-10-17
area: guard
verify: test
blocked_by: null
fixed_in: null
---

The git-coordination subsystem guards the wrong thing. It blocks *ordinary* pushes whenever a peer
is live (`guard._deny_push`) and serialises them through a daemon queue (`actions.push`), but the
one command that destroys another agent's already-**committed** work — `git push --force` /
`--force-with-lease` / `-f` / `+refspec` / `push --delete` — is denied **nowhere**. Meanwhile git
already solves the concurrent-pusher problem: the second push is rejected non-fast-forward and the
correct recovery is `fetch` + `rebase` + re-push. An autonomous agent is not emotional; it does
whatever its instructions say. So the danger is never the rejection — it is an agent doing a bad
*recovery* (`reset --hard`, `push --force`, blind `pull`) for lack of the right instruction.

The current system is ~1,500 lines (guard.py's push/trunk/file/stage/branch logic, actions.py's
~500-line push path, config.py's push/trunk knobs) built to pre-block a collision that happens in
~2% of pushes (measured: 32 of 1,435 push intents ever queued behind another; deepest queue ever
observed = 3). The minimal system that still protects committed work is ~150–200 lines.

## What is MEASURED

- Push-intent history in the bus DB (`~/.cabildo/chat.db`, actions `push` + legacy `land`): 1,435
  total; **32 (2.2%)** were ever queued behind another push; **max queue depth ever = 3**. Most
  projects never queued two at once.
- `guard.py` has no branch that matches `push --force`/`-f`/`--force-with-lease`/`+<refspec>`/
  `push --delete`. `_deny_push` only denies a *plain* push, and only while a peer is live or
  someone else holds `push:<project>`. Alone, or holding the lock, or `[push].mode = "manual"`, a
  force-push passes.
- The daemon pushes with `--no-verify` by default (`test_daemon_push_skips_the_repos_pre_push_hook`),
  so the repo's own `pre-push` gate is skipped on the daemon path and re-run inside `actions.push`.

## What is INFERRED

- That recipe-on-rejection can replace the push guard + daemon queue for everything except
  force-push. (Inference from the design, not yet measured against a live fleet.)
- That the four shared-checkout conventions (file-lease, blanket-stage, branch-switch,
  destructive-with-peers) evaporate under one-worktree-per-agent. `spawn` already enforces
  isolation; only human-launched sessions can still collide.

## The plan — phased, each phase ships independently

### Phase 0 — Add the missing deny: force-push. Unconditional. (DONE — 966e8e0)
Deny `git push` carrying `--force` / `-f` (incl. bundled short flags) / `--force-with-lease` /
`--delete` / `-d` / a refspec beginning `+`. Denied whether or not a peer is live, because it
destroys committed work even for a lone agent. Opt out per repo like `[destructive]`. ~15 lines in
guard.py + tests. Pure addition, closes the real hole, ships on its own.

Out of band (no code): turn off "allow force push" on origin's protected branches. The server
setting is the actual guarantee; the hook is convenience.

### Phase 1 — Push-failure recipe (PostToolUse) (DONE)
`guard.push_failure_recipe(command, output)` (pure, tested) + wiring in
`hooks/post_tool_use.py`: when `tool_name == "Bash"`, `_is_push(command)`, and the output matches
`non-fast-forward|fetch first|[rejected]|stale info|tip ... is behind`, emit the recipe —
`git fetch origin` → `git rebase origin/<target>` → `git push origin HEAD:<target>`, with the
target parsed from the command (placeholder when bare); on rebase conflict, `--continue` or
`--abort` and coordinate on the bus; never `reset --hard`, `--force`, or a merge `pull`. The
`<target>` is parsed from the push argv. NOT yet done: the same text as standing SessionStart
guidance (optional; the at-the-moment-of-failure injection is the load-bearing half).

### Phase 2 — Collapse the four shared-checkout rules into one
Replace file-lease (+ autoclaim, the shell-write parser, redirect tokenising, apply_patch path
extraction), blanket-stage, branch-switch, and the peers-half of destructive with a single rule:
if a live peer shares your `tree_root` and the argv is a mutating git op, deny with "get a
worktree: `git worktree add ...`" (Claude Code's `EnterWorktree` makes it one call). Plus a
SessionStart notice when a sharer is already present. `claim file:<path>` stays as an advisory
tool; `_deny_file` goes. ~40 lines replacing ~400.

### Phase 3 — Delete the daemon push-queue, the push guard, the trunk guard
Blocked by Phase 1 landing (the recipe must exist first) AND an explicit go-ahead from Jorge,
because it removes `request(action="push")` from the MCP surface. Removes `actions.push` +
`_local_sync` + merged-worktree cleanup + gate/conflict/settings hint helpers (~500 lines),
`guard._deny_push` + `_deny_commit_on_trunk` + commit-dir helpers (~155), the push/trunk config
knobs (~120). Trunk guard goes because it only existed to keep the daemon's local-ff possible — no
daemon, no divergence — and it contradicts the trunk-based workflow anyway. The daemon stays for
`spawn` and `ask`.

Costs to accept (or mitigate): lose the post-push `ls-remote` verification (fold a one-line check
into the recipe), lose automatic merged-worktree cleanup, lose the central intent log of who
pushed when.

### Phase 4 — Shrink destructive guard to the irrecoverable set
Drop `git stash` (recoverable via `stash list`/`pop`); keep `reset --hard`, `clean -f`,
`checkout .`/`checkout -- <path>`, whole-tree `restore`. Remove the peers-branch of the message
once Phase 2 exists.

## Define done

- **Phase 0:** a force-push (`--force`, `-f`, `--force-with-lease`, `+refspec`, `--delete`) is
  denied by `guard.deny_reason` even for a lone agent, and `[destructive].mode = "off"` (or a
  dedicated opt-out) lets it through. Red-then-green tests in `tests/test_guard.py`.
- Later phases define their own done when reached.

## What was deliberately NOT done

- Not touching the bus, presence, `spawn`, or `ask` — only the git-collision machinery.
- Not building the git-level pre-commit/pre-push CLI hook from the earlier plan: it added an
  identity model (human vs agent) and a per-repo installer for the same protection this plan gets
  from a PostToolUse recipe + an unconditional force-push deny, at a fraction of the surface.
- Phase 3's deletions are staged behind Jorge's explicit OK, not done on the strength of this file.

## Provenance

Two model reviews on 2026-10-17: Opus 5.5 on the original git-hook CLI plan (found the daemon
self-deny, the fail-open-at-exit-code hazard, and the human-identity hole), and Fable on
simplification (found the force-push gap and the recipe-replaces-queue argument). This file is the
synthesis; it supersedes the git-hook CLI approach.
