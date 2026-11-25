# The session host

Turn on the session host when you want a Claude session to keep running after you close the
workspace. The host is a small process that owns each session's terminal. The workspace, the
console, and later an editor only view it.

## Turn it on

Add this to `~/.config/workspace/config.toml`:

```toml
[sessions]
owner = "host"
```

The default is `owner = "app"`: the workspace owns each Claude, and quitting it ends them.

With `owner = "host"`:

- A new conversation and a resumed one start in the host.
- Quitting the workspace (`q`) leaves them running. Start the workspace again and open the row:
  it shows the running session.
- Closing a conversation (`x`) ends its session.
- Reloading the workspace (`R`) gives the sessions it owns to the host. Nothing restarts.

The host starts by itself the first time it is needed. If it cannot start, the workspace tells
you and runs Claude itself, as before.

## Use it from the console

```bash
cabildo host new --cwd ~/project -- claude
```

Starts a session and prints its id.

```bash
cabildo host list
cabildo attach <id>
```

`attach` shows the session in this terminal. Close the terminal to leave it; the session keeps
running. Two terminals can attach to one session: both see the output and both can type. The
window size follows the one that resized last.

```bash
cabildo host kill <id>
```

Hangs the session up.

## Use it from an agent

An agent starts and drives other Claudes with these MCP tools:

- `sibling_start(prompt, cwd, permission_mode)`: starts a Claude in the host and returns its id.
  The prompt is its first message. It is told who started it and to answer with `send`.
- `sibling_send(id, text)`: types the text into it and presses Enter.
- `siblings()`: lists the hosted sessions, with `mine` for the ones you started.
- `sibling_kill(id)`: hangs one up.
- `host_stop(force)`: stops the host. Refused while sessions run, unless `force`.

Without `permission_mode`, a sibling asks for approval of each tool in its terminal, and only
someone running `cabildo attach <id>` can answer. At most 4 siblings run at once. A sibling can
start one more level of siblings, not a third.

`about` shows the build of the host and of the daemon next to the installed one, and says what
to restart.

## What it does not do

- Output printed before you attach is not kept. Claude redraws its screen when you attach, so
  you see the current screen, not the scrollback.
- If the host process stops, every session it owns gets a hangup and ends.
- A new conversation with no message yet has no Claude session id, so the workspace cannot
  find it again by row. `cabildo host list` still shows it.
- A session started in a plain terminal is not hosted. Only sessions started by the workspace
  (with the option on) or by `cabildo host new` are.
- Each session starts with the environment of the process that asked for it (`SSH_AUTH_SOCK`
  and the rest), not the environment of the host.

The socket is `~/.cabildo/host.sock` (mode 600). Set `CABILDO_HOST_SOCKET` to move it. The host
writes its log to `~/.cabildo/host.log`.
