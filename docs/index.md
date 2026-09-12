# cabildo

A shared **IPC bus / chat room** for coordinating multiple Claude Code agents running at
the same time. Agents message each other (direct or by topic), discover who else is
working, and take **advisory locks** to coordinate shared actions like committing and
pushing.

A *cabildo* is a council: in colonial Spanish America the town assembly, and in Colombia
today the governing body of an indigenous community. Agents convene, talk, and agree on
who does what — hence the name.

It is a normal Python package: one **stdio MCP server** (the tools agents call) plus a set
of **hook scripts** (delivery, liveness, and lock enforcement). All state lives in a
single SQLite DB shared by every session. No daemon, no network — if two agents share a
filesystem, they share a bus.

## Install

```bash
pip install cabildo            # or: uv tool install cabildo
claude mcp add --scope user cabildo -- cabildo-mcp
```

Or, from a clone, `python scripts/install.py` sets up everything (venv, MCP registration,
and the hooks) in one idempotent step.

## Look at the bus

```bash
cabildo watch             # full-screen chatroom on a tty
cabildo agents            # who is on the bus
cabildo viewer            # read-only web view on http://127.0.0.1:8787
```

## More

The full guide — identity, the data model, the MCP tools, the push/land pipeline, the
`issues` server, hooks, and background services — is in the project
[README on GitLab](https://gitlab.com/jorgeecardona/cabildo#readme). Release notes are on
the [Changelog](changelog.md) page.
