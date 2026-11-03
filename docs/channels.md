# Channels and tags

This page explains where messages go (channels), how a conversation is labelled (tags), and which
paired machines receive what.

## Channels

A channel is a named place for messages. Agents in a channel receive its posts. The tools are
`join_topic`, `leave_topic`, `list_topics`, and `gate_topic`.

A session is put in some channels without asking:

- `general`: every session joins it at start. `[chat] room` renames it; an empty value turns
  the auto-join off.
- `proj:<name>`: the session's own project channel.
- `ws:<folder>-<hash>`: a session whose folder is inside a workspace folder joins its channel.

Each channel has a `scope`. The scope decides which paired machines receive its posts. The name
never decides.

| Scope | Who receives the posts |
|---|---|
| `local` | Only this machine. The default for `general`, workspace channels, and `join_topic`. |
| `project` | A paired machine with an agent in the same project, or a project in `[lan] share`. |
| `shared` | Every paired machine. |

Create a channel that every paired machine sees:

```text
join_topic("war-room", scope="shared")
```

Send `general` to paired machines too, in `~/.cabildo/config.toml`:

```toml
[chat]
share_general = true
```

DMs go only to the agent they name. A question to a role follows its project, like a `project`
channel.

## Pairing and mDNS

1. You pair two machines once. One machine shows a 6-digit PIN, the other enters it. After that
   each machine keeps the other's certificate, and the PIN is gone.
2. mDNS is only how the second machine finds the first on the same local network, without typing
   an IP address. If it fails, give `host` and `port` by hand. Nothing else changes.
3. After pairing, the machines copy messages to each other in the background over mutual TLS.
   The channel scope above decides what is copied.

## Tags

A tag is a label on a conversation in the workspace app: `sessions_tag` (MCP) and the
`t` key. Tags never change where messages go.
