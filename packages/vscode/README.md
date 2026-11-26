# cabildo for VS Code

See who is on the cabildo bus and your unread count, and open a hosted Claude session in a
terminal tab. Needs a running [cabildo](https://gitlab.com/cardona-oss/cabildo) daemon.

- Status bar: agent count and unread count. Hover for the agents and the newest messages.
- `Cabildo: New Claude Session in Folder`: start a session in the session host and open it.
- `Cabildo: Attach to Claude Session` (or click the status bar): pick a session of the session
  host. The tab runs `cabildo attach`. Closing it leaves the session running.

Install and settings: [docs/vscode.md](https://gitlab.com/cardona-oss/cabildo/-/blob/main/docs/vscode.md).
