# cabildo for VS Code

A cabildo side bar: your Claude conversations and what they use. Needs a running
[cabildo](https://gitlab.com/cardona-oss/cabildo) daemon.

- Conversations: which one waits for your input, works, runs or is stopped. Open one, start one
  in the open folder, send it a message, or end it.
- Console: Claude's terminal inside a cabildo view, a tab per session, with its model, context
  and cost.
- The selected conversation: model, context used, tokens, cost, CPU and memory.
- Plan limits, gateway upstreams, spend per model, the daemon's intents and Jupyter runners.
- Unread Messages: read, reply, mark read.
- Status bar: agent count and unread count.

Install and settings: [docs/vscode.md](https://gitlab.com/cardona-oss/cabildo/-/blob/main/docs/vscode.md).
