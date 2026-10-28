---
title: Test suite pollutes the real repo — scratch commits, leftover worktrees, and a push to origin/main
status: open
scope: M
owner: null
filed: 2026-10-19
area: tests
verify: test
blocked_by: null
fixed_in: null
---

The pytest suite operates on the repository's own `.git` instead of an isolated throwaway repo, and
at least one path under test reaches `git push`. A plain run — or a push, since the pre-push hook is
`exec make check` — mutates history and can reach origin.

## Measured (observed 2026-10-19)

- `.git/config` carried `user.name = t`, `user.email = t@t`. origin/main's recent commits, including
  `153bf43` ("release: bump to 0.1.94", which published to PyPI), are authored `t <t@t>`. The local
  config was unset this session, but the published-history authorship is already baked in.
- The real checkout's local `main` had accumulated orphan "init" commits authored by `t`, and
  `git worktree list` showed prunable worktrees registered under `/tmp/pytest-of-jcardona/...`.
- Scratch commits ("one" / "base" / "my work") were **pushed to origin/main** and reverted ~9 minutes
  later — the main clobber this session had to recover from with `git revert` + an ff push.
- `.githooks/pre-push` is `exec make check`, so every push runs the full suite in the working
  checkout, re-triggering the pollution at the worst moment.

## Inferred

- A fixture or helper initializes and commits against the repository-under-test's actual `.git`
  (not a temp `GIT_DIR`/worktree), and a code path under test can invoke `git push` with origin
  reachable. That is why running or pushing from a real clone mutates history and can reach origin.

## Done when

- The suite runs entirely against an isolated throwaway repo (its own tmp `GIT_DIR` + worktree,
  origin pointed at a local bare mirror or disabled), such that a full run leaves `git status`, the
  commit graph (`git rev-list --count HEAD`), and `git worktree list` byte-identical to before, and
  no test path can push to the real origin.
- Verified by a regression test: run the suite inside a dirty checkout and assert those three are
  unchanged, and that a mocked/guarded `git push` is never invoked against origin.
