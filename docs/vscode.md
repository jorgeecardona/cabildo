# VS Code extension

See the agents and your unread count in VS Code, and open a hosted Claude session in a terminal
tab. The extension is in `packages/vscode/`. It is not on a marketplace yet.

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

The status bar shows the agent count and your unread count. Hover over it for the agents and the
newest messages. It shows a disconnect icon when the daemon does not answer.

To start a Claude session in the open folder:

1. Run `Cabildo: New Claude Session in Folder`.

It runs in the [session host](session-host.md) and opens in a terminal tab.

To open a session that runs already:

1. Click the status bar item, or run `Cabildo: Attach to Claude Session`.
2. Pick the session. Sessions in the open folder are listed first.

Closing the tab detaches. The session keeps running. The list has only sessions that the host
owns: a Claude that you started in a terminal is not in it.

When the host stops, or closes the connection, the tab stays open and says why. Press Enter to
close it.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `cabildo.command` | empty | Path of `cabildo`. Empty: the daemon's Python, else `cabildo` on `PATH`. |
| `cabildo.refreshSeconds` | 10 | Seconds between status bar updates. |

With Remote SSH, install the extension on the remote host: the sockets are there.

## Develop

```bash
pnpm test
```

To check it in a real VS Code window (a window opens and closes by itself):

```bash
pnpm run probe
```

The extension reads two unix sockets: `bus.sock` (the operations `list_agents` and `inbox`) and
`host.sock` (the operation `list`). It runs `cabildo host new` to start a session, and the terminal
tab runs `cabildo attach --hold`.
