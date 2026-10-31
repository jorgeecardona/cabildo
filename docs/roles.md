# Roles

Use roles to split work between sessions, for example product, design and backend. Several
sessions can hold the same role.

## Define roles

Write each role in `.cabildo/config.toml`. The text is the role description. Cabildo shows it to
the session verbatim and adds no rule of its own, so write what the role does, what it does not
do, and which role to ask instead.

```toml
[roles]
product = "Owns needs, priorities and acceptance. Writes no code. For anything technical, ask the backend role."
backend = "Owns src/ and tests. Asks the product role when a requirement is unclear."
```

## Take a role

1. Call `set_role("product")` inside the session. The result returns the description.
2. Or start the session with `CABILDO_ROLE=product` in its environment. The SessionStart banner
   then shows the description.

## Ask a role a question

1. Ask with `send_to_role("backend", "Can the export run offline?", question=True)`. It returns the
   question guid.
2. Every session holding `backend` sees the question in `inbox`, with a `question` field.
3. A holder answers with `send("Yes, since v0.9", reply_to="<guid>")`. The answer is a direct
   message to the asker.
4. The first answer removes the question from the other holders' inboxes.
5. Call `questions` to list what you asked and what waits for your role.

Without `question=True`, `send_to_role` sends one direct message to each holder.

## Rules

- Only a session that holds the role can answer. `send` refuses other `reply_to` calls.
- With no holder, the question waits. A session that takes the role later sees it.
- A question reaches sessions of the same project name only, including on a paired machine. `[lan] exclude` withholds it like a project channel.
- An answer goes to the asker. `send` refuses `reply_to` with another `to` or a `topic`.
- Role names compare with spaces trimmed and ASCII letters lowercased.
- The question stays addressed to the role, not to the session that held it.
- Open or answered is derived from the messages. Two answers are both kept; the earliest counts.
- A role is a label. Cabildo does not block any tool for a role.
- Both machines must run this version to sync questions over LAN or WAN.
