# VS Code extension

A cabildo side bar in VS Code: your hosted Claude sessions, the agents on the bus, and your
unread messages. The extension is in `packages/vscode/`. It is not on a marketplace yet.

## Install

1. Build the package (needs Node 22 or newer and pnpm):

```bash
cd packages/vscode
pnpm install
pnpm run package
```

2. Install it:

```bash
code --install-extension cabildo.vsix
```

3. Run `Developer: Reload Window` in a VS Code window that was already open.

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

## Develop

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
