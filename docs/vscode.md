# VS Code extension

A cabildo side bar in VS Code: your Claude conversations, the selected one's model, context and
cost, the plan limits, the gateway, the spend, and your unread messages. It is on the Visual Studio
Marketplace as
[cardona-oss.cabildo](https://marketplace.visualstudio.com/items?itemName=cardona-oss.cabildo).

## Install

1. Install it from the Marketplace:

```bash
code --install-extension cardona-oss.cabildo
```

2. Run `Developer: Reload Window` in a VS Code window that was already open.

## Use

Click the cabildo icon in the activity bar. The number on it is your unread count. The side bar
has two views: the panel and your unread messages.

### Panel

The panel shows what `cabildo workspace` shows in its left side: the list of
conversations on top, and sections that fold under it. It needs a cabildo that has the `panel`
command.

At the top:

- `Folder`, `Subfolders`, `All`: which conversations the list shows.
- The counts: how many conversations wait for input, work, and run. Click a count to select the
  next conversation in that state.

In the list, the dot of a row is its state:

| Dot | State |
|---|---|
| Amber, with an amber bar at the left of the row | Claude waits for your input |
| Green ring that turns | Claude works |
| Grey, filled | Claude runs and does nothing |
| Grey, hollow | stopped |

A row also shows the age, the agent name, the branch, CPU and memory, the group, the tags and the
notes. It shows the directory when it is not the open folder, and the rooms when the side bar is
420 pixels or wider.

- Select: click a row, or use the arrow keys. A conversation that the
  [session host](session-host.md) runs shows its terminal.
- Open: double click a row, press Enter, or click the terminal icon. A stopped conversation is
  resumed in the session host. Claude runs in a terminal tab.
- Send a message: click the envelope. Only a conversation on the bus has it.
- End: click the cross and confirm. Only a conversation in the session host has it.
- New conversation: click `+` at the top of the view. It starts in the open folder.

Closing a terminal tab detaches, and the session keeps running. When the host stops, or closes
the connection, the tab stays open and says why. Press Enter to close it.

The sections under the list:

- Conversation: the selected one. Its model, how full its context window is, the effort, the
  gateway upstream, the tokens in, out and from the cache, its cost, CPU and memory, and its rooms.
- Plan: the 5-hour and 7-day limits. The bar is the part used. The mark on the bar is the part
  of the window's time that passed. Under it: the pace, the reset time, and the time the limit
  fills when that is before the reset.
- Gateway: each upstream, ready or set aside with the reason, its requests and failovers, and
  its cost today and in the last 7 days (tokens for the subscription).
- Spend: a column per period and a row per model. The bar of a model is its part of the cost
  of 7 days.
- Intents: the daemon's queue and what finished in the last hour. A failed one shows why.
- Jupyter runners: the running ones, with CPU and memory.
- All running: CPU and memory of everything the panel measures.

A folded section shows its main numbers in its heading. A section with nothing to show is left
out. Drag the line above the sections to resize them. Double click it to fit the content.

A bar or a number is amber from 70% and red from 90%. CPU and memory need Linux. The spend
follows `[workspace].spend`: on a subscription it shows only with `spend = "always"`.

### Unread Messages

Each line starts with when the message was sent: the time for today, else the date.

- Read: click a message. It opens in a read-only tab and is marked read.
- Reply: click the arrow. The reply goes to the sender as a direct message.
- Mark read: click the check, or the double check at the top for all messages.

The status bar shows the agent count and the unread count too. Click it to pick a session.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `cabildo.command` | empty | Path of `cabildo`. Empty: the daemon's Python, else `cabildo` on `PATH`. |
| `cabildo.refreshSeconds` | 10 | Seconds between updates of the status bar. |

With Remote SSH, install the extension on the remote host: the sockets are there.

## Publish

The GitLab job `vscode` publishes to the Visual Studio Marketplace. It runs on `main` when
`packages/vscode/` or `.gitlab-ci.yml` changes. Without the variable `VSCE_PAT`, it builds
`cabildo.vsix` and publishes nothing.

The publisher is `cardona-oss` (display name `Cardona OSS`).

Set up the token once:

1. Get a Marketplace token of that publisher. To make a new one in
   [Azure DevOps](https://dev.azure.com): Organization `All accessible organizations`, scope
   Marketplace, `Manage`.
2. Add it in GitLab under Settings, CI/CD, Variables: key `VSCE_PAT`, masked, protected,
   environment `vscode-marketplace`.
3. Run a pipeline on `main`:

```bash
glab ci run --branch main
```

To publish a new version, raise the patch of `version` in `packages/vscode/package.json` and
push. A version that is already on the Marketplace is skipped.

Microsoft retires the kind of token used in step 1. The
[VS Code page](https://code.visualstudio.com/api/working-with-extensions/publishing-extension)
gives 1 December 2026, and the
[Azure DevOps post](https://devblogs.microsoft.com/devops/retirement-of-global-personal-access-tokens-in-azure-devops/)
says it moved to 27 January 2027. After that date this job needs another sign-in. Its sign-in
without a stored token (`vsce publish --oidc`) accepts GitHub Actions, not GitLab.

## Develop

The extension is in `packages/vscode/`. Build and install your copy (needs Node 22 or newer and
pnpm):

```bash
cd packages/vscode
pnpm install
pnpm run package
code --install-extension cabildo.vsix
```

Run the tests:

```bash
pnpm test
```

Check it in a real VS Code window (it opens and closes by itself, and uses a temporary cabildo
home):

```bash
CABILDO_BIN=$HOME/.venv/bin/cabildo pnpm run probe
```

The extension uses two unix sockets: `bus.sock` (the operations `list_agents`, `inbox`, `send`
and `mark_read`) and `host.sock` (the operations `list` and `kill`). It runs `cabildo host new`
to start a session, and the terminal tab runs `cabildo attach --hold`. The panel runs
`cabildo panel`, which prints one JSON line each time something changes. `media/panel.js` draws
it. The records are in `src/cabildo/workspace/panel.py` and mirrored in `src/types.ts`.
