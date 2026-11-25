# Use cabildo well

Read this once at the start of a session. It lists what to do with cabildo and what to avoid,
in the order you meet each case. In a session, call the `help` tool for this page: `help("start")`.

## Know where you are

1. Call `whoami` to see your name, project and channel.
2. Call `list_agents` to see who else works in this project.
3. Call `inbox(mark_read=True)` for messages. Without `mark_read`, call `read` on the ids you
   handled: messages are not marked as read for you.

## Talk to other agents

- Send a direct message with `send(body, to=...)`. Without `to`, the message goes to the project
  channel. With the cabildo-push plugin, `cabildo_ask` waits a few seconds for the reply.
- Send one when you are about to edit an area another agent claimed, when you change an API,
  schema or config another project uses, when you are blocked, and when you finish work someone
  waits for.
- Do not reply only to say "ok" or "thanks". Every message wakes the other agent and costs a turn.
- Post in the project channel for news every agent of the project should see. Use `send_to_role`
  to reach a role.

## Share a checkout

Several agents often work in one folder. These rules keep their work apart.

- Do not run `git push`. Commit, then queue a push:

  ```text
  request(action="push", params={"cwd": "<your tree>", "target": "main"})
  ```

  The daemon rebases, runs the gate, pushes and checks the result, one agent at a time. It
  sends you a message when it is done.
- If the project sets `[push].mode = "manual"`, the push request is refused. Take
  `claim("push:<repo>")`, push by hand, then `release` it.
- Stage explicit paths: `git add <file>`. Never run `git add -A` or `git add .` in a shared
  checkout, because it commits the files of other agents.
- If another agent has written in the folder, the guard denies `add -A`, `add .`, `commit -a`,
  `stash`, `reset`, `restore`, `clean`, `switch`, `rebase`, `merge` and `pull`. A path-scoped
  `add` and `commit` stay allowed. An agent that only reads never blocks you.
- Work in your own worktree when you can: `git worktree add ../wt-<name> --detach origin/main`.
- After you queue a push, do not edit or commit in the tree it was queued from until it is done.
  With no uncommitted tracked change, the rebase and the gate run in that tree. Otherwise they
  run on a copy of your HEAD, and afterwards you run `git pull --rebase --autostash`.

## Claim what you edit

- `claim(resource)` takes an advisory lock on a name, for example `file:src/app.py` or
  `push:<project>`. It expires after `ttl_seconds` (600 by default). `claims` lists the locks and
  `release(resource)` gives yours back.
- A claim does not stop an edit. It tells the others to ask you first.

## Keep what you learn

- `note_add` and `notes` keep short notes for a session. `note_done` and `note_park` close them.
- `record_decision` and `read_decisions` keep decisions that others need to find.
- `record_minutes` and `read_minutes` keep the minutes of a working session.

## CI

- `ci_status` shows the pipeline of the current commit. It works for GitLab and GitHub.
- `ci_retry` runs a failed job again.
- `ci_run` (a new pipeline) and `ci_play` (a manual job, such as a deploy) need your human's
  approval in a popup.

## Copy to the clipboard

`clipboard_copy(text="...")` puts text on the user's clipboard, on Linux (Wayland or X11) and
macOS. Ask an agent "copy that command" and it calls the tool. `cabildo clip TEXT` does the same
from a shell, and reads stdin when it has no text.

- It uses `xclip` or `xsel` when there is an X display, `wl-copy` under Wayland only, and `pbcopy`
  on macOS. Install one of them if the tool says none is found.
- The text goes to the clipboard of the machine the cabildo server runs on. Any program of the
  user can read it, so agents must not copy secrets unless you ask.
- Limit: 1 MB.

## Find old sessions

`sessions_list(query="words")` searches the title, the label, the first prompt, the robot name
(`tiny-dynamo-616b8`), the `@handle` and the session id. `contains="words"` searches all text.
The `sessions_*` tools and `cabildo sessions` also take a robot name or an `@handle` where they
take an id. The workspace `q:` filter matches the same names. `sessions_read` prints one conversation.
`sessions_rename` gives a session a name you can find later, and `sessions_tag` adds tags.

## Take me to a session

Ask an agent "take me to tiny-dynamo-616b8" (or an `@handle`, an id, or part of a title). The
agent calls `sessions_focus(name="...")`. The workspace that is running opens that session, starts
it again if it had ended, moves the list cursor to it and gives it the keys.

- It needs a running `cabildo workspace` that lists the session. A filter or a smaller scope hides
  it: the tool then answers "no session ... in this workspace's list".
- A name that matches several titles changes nothing and lists them. An id, robot name or
  `@handle` always wins over a part of a title.
- With several workspaces open, each one looks in its own list and the first that has the session
  shows it.

## More topics

Call `help()` for the list. Each topic is a page of this guide.

- `help("tools")` lists every cabildo tool with its first line.
- `help(query="push")` searches all pages.
- `version` shows the version this MCP server and the daemon run. If one is older than the
  install (`cabildo doctor` tells you), restart the session or run `/mcp`.
