# VS Code extension

A cabildo side bar in VS Code: your hosted Claude sessions, the agents on the bus, and your
unread messages. It is on the Visual Studio Marketplace as
[cardona-oss.cabildo](https://marketplace.visualstudio.com/items?itemName=cardona-oss.cabildo).

## Install

1. Install it from the Marketplace:

```bash
code --install-extension cardona-oss.cabildo
```

2. Run `Developer: Reload Window` in a VS Code window that was already open.

## Use

Click the cabildo icon in the activity bar. The number on it is your unread count. The side bar
has three lists. Items of the open folder come first.

### Claude Sessions

The sessions that the [session host](session-host.md) owns. Claude runs in a terminal tab.

- New session: click `+` at the top of the list. It starts in the open folder.
- Open: click the session. Closing the tab detaches, and the session keeps running.
- End: click the trash icon and confirm. Claude stops.

A Claude that you started in a terminal is not in this list. When the host stops, or closes the
connection, the tab stays open and says why. Press Enter to close it.

### Agents

Every agent that is active on the bus.

- Send a message: click the speech bubble.
- Open its session: click an agent that runs in a hosted session.

### Unread Messages

- Read: click a message. It opens in a read-only tab and is marked read.
- Reply: click the arrow. The reply goes to the sender as a direct message.
- Mark read: click the check, or the double check at the top for all messages.

The status bar shows the agent count and the unread count too. Click it to pick a session.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `cabildo.command` | empty | Path of `cabildo`. Empty: the daemon's Python, else `cabildo` on `PATH`. |
| `cabildo.refreshSeconds` | 10 | Seconds between updates of the side bar and status bar. |

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
to start a session, and the terminal tab runs `cabildo attach --hold`.
