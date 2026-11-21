# Names

Use this to keep the names you gave sessions when the machine is lost, and to put them back on
a new one.

A robot name such as `tiny-dynamo-616b8` is computed from the session id. It needs no backup, and
it comes back with the session. These names are not computed, so they are lost with the machine:

- The `@handle` and role of a session (in the bus database, `~/.cabildo/chat.db`).
- The title, group and tags of a conversation (in `~/.local/share/workspace/meta.json`).

## Keep them

The daemon writes them to one file, about once a minute, and only when something changed. The
default is `~/.cabildo/names.json`. That folder is on the same disk as the database, so point the
file at a folder you back up or sync:

```toml
# ~/.cabildo/config.toml
[names]
file = "~/Sync/cabildo-names.json"
```

The daemon reads the config each time, so a change applies within a minute.

## Check and write by hand

```bash
cabildo names            # counts what would be saved
cabildo names export     # write the file now
```

## Put them back

On the new machine, install cabildo, copy the file, and run:

```bash
cabildo names import --file ~/Sync/cabildo-names.json
```

Import fills names that are empty and never overwrites a name you already set. A session the
database does not know is added as ended, so its handle and role are there when you resume it.
The command prints how many it added, filled and left as they were.

## What it does not do

- It does not save the conversations. A name is useful only if the transcript in
  `~/.claude/projects` is saved too.
- A resume with `--fork-session` gets a new session id, so it gets a new robot name. A plain
  resume keeps the id and the name.
