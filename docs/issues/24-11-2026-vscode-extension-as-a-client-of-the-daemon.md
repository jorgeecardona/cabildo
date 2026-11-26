---
title: No VS Code extension, so claims, inbox and live sessions are not visible in the editor
status: in-progress
scope: L
owner: cute-baymax-2219f
filed: 2026-11-24
area: vscode
verify: probe
blocked_by: null
fixed_in: null
---

# No VS Code extension, so claims, inbox and live sessions are not visible in the editor

Goal: a VS Code extension that is a thin client of the cabildo daemon. It shows agents, claims
and the inbox, and it attaches to live Claude sessions in a normal terminal tab.

## Problem

Cabildo state is visible only in the TUI and through MCP tools inside a Claude session. A person
working in VS Code cannot see who holds a claim on the open file, cannot see unread messages, and
cannot open a session the daemon runs.

## Measured

- The repo has no VS Code extension and no VS Code design page. The only mention is a comment in
  `tests/test_push_verification.py:99`.
- `src/cabildo/broker.py`: the daemon serves the SQLite DB over a unix socket (`bus.sock`).
  A request is one JSON line with `op`, `params` and `identity`. A response is one JSON line.
  The operations are the table `bus.OPS`.
- `mods/plugins/cabildo-push/` is a TypeScript plugin for Claude Code, not for VS Code.

## Inferred (not checked)

- `bus.OPS` covers what the extension needs (agents, claims, inbox). Read it and list the gaps.
- The wire format is stable enough for a second client. It was written for the MCP and hook
  clients only.
- Node can talk to a unix socket from the extension host without a native module. Node's `net`
  module supports unix sockets, but check it in a real extension.

## Design

Data first. The extension holds frozen types that mirror the broker responses: `Agent`, `Claim`,
`Message`, `SessionRecord`. Generate them from one schema if `bus.OPS` can export one, so the
types cannot drift from the broker.

1. **Status bar:** agent count and unread inbox count. Click opens the inbox.
2. **Sidebar tree:** agents, claims, rooms.
3. **Editor gutter:** a marker on files that another agent has claimed.
4. **Notification:** a toast for a new direct message and for a denied lock.
5. **Commands:** send a message, release a claim, mark read.
6. **Terminal tab:** a pseudo-terminal whose backend is the daemon session socket. This part
   needs the blocking issue. It shows a live Claude session that the daemon owns.

Items 1 to 5 need only the broker socket and can ship first, as a read-mostly version. Item 6
waits on the blocking issue.

Packaging: a folder `packages/vscode/` in this repo, with its own `package.json`, built with
`vsce`. Publish to the VS Code Marketplace and Open VSX. This is a second release track beside
PyPI, with its own version. Per the global rule, bump only the patch.

## Done when

1. The extension connects to a running daemon and shows the agent list, claims and unread count.
   Verified in a real VS Code window (`probe`).
2. A claim held by another agent shows as a gutter marker on that file.
3. A direct message to the user's session raises a toast within 5 seconds.
4. The extension reconnects after `systemctl --user restart cabildo-daemon.service`.
5. After the blocking issue lands, the terminal tab shows a live session, and typing reaches
   Claude.
6. A test runs the extension's client code against a fake broker socket.

## First slice (done, 25-11-2026)

- Done: `packages/vscode/` with the status bar (agent count, unread count, tooltip) and
  `Cabildo: Attach to Claude Session`. See [the docs page](../vscode.md).
- Probe: `pnpm run probe` in VS Code 1.143.1 read the real daemon, opened a hosted `sh` session
  in a terminal tab, typed a line that the session read, and closed the tab. The session kept
  running. Done-when 5 and 6 are covered with `sh` as the session, not a real `claude`.
- Changed from the design: the terminal tab runs `cabildo attach <id>`. The extension does not
  implement the host's frames in a VS Code pseudo-terminal.
- Probe with a real `claude` (25-11-2026): attach, type and read the reply worked. It found two
  host bugs, fixed in `host.py`: a viewer was dropped 5 seconds after its last key, and a
  second attach at the same size showed a blank screen.
- Added (26-11-2026): a command that starts a session in the open folder, probed with a real
  `claude`. A tab whose connection to the host ends now shows why.
- Added (26-11-2026): the side bar (hosted sessions, agents, unread messages) with send, reply
  and mark read.
- Added (26-11-2026): the GitLab job `vscode` that publishes to the Visual Studio Marketplace
  when `VSCE_PAT` is set, under the existing publisher `JorgeCardona`. Not published yet: this
  project has no token.
- Not done: claims and rooms in the side bar, gutter markers, toasts, releasing a claim, the check
  across a daemon restart, Remote SSH, publishing.

## Not doing here

- Session ownership and the attach socket. That is the blocking issue.
- Editing the SQLite DB from the extension. The extension goes through the broker only.
- Other editors (JetBrains, Neovim). They can reuse the same socket later.
- Cross-machine use. LAN and WAN sync carry messages, not editor state.

## Open questions

- Does `bus.OPS` need a schema export, or do we hand-write the types and test for drift?
- Is a Marketplace publisher account needed now, or only after the read-mostly version works?
- The extension runs on the machine of the editor. For Remote SSH, the daemon socket is on the
  remote host, so check that the extension runs there (a workspace extension), not locally.
