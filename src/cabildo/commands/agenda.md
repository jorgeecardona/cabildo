---
description: One smart agenda — Today, Suggested, and the Roadmap in a single read from the cabildo agenda over MCP
---

Show me my whole agenda in one read: what I'm on Today, what you Suggest, and the
standing Roadmap. The store is the cabildo agenda (one markdown file per task, read
over the cabildo MCP server). "Today" is a flag on a task (`today`, one exclusive
`highlight`), so committing to today flags a task, it never drifts into a second list.

Use the cabildo MCP tools by their names: `agenda_view`, `agenda_add`, `agenda_today`,
`agenda_highlight`, `agenda_done`, `agenda_board`, `agenda_show`. Always pass
`scope="global"` for the home-wide agenda (a per-repo agenda is `scope="project"`).

Handle the argument ($ARGUMENTS) first, then always run the read. `<keyword>` is any
unambiguous fragment of a task's slug.

- **`add <text>`** (optionally `... in <tier>`) — `agenda_add` (title + tier, default NEXT).
- **`commit <keyword>`** (alias `promote`) — flag it for today via `agenda_today` (on=true).
  Keep today small; push back gently past ~3.
- **`highlight <keyword>`** — `agenda_highlight` (the one thing; exclusive).
- **`uncommit <keyword>`** — `agenda_today` (on=false).
- **`done <keyword>`** — `agenda_done`.
- **`suggest`** — make the Suggested section the whole output.
- **any other text** — focus filter: restrict to matching tasks/tiers.
- **empty** — the full three-section read.

## The read (Today, then Suggested, then Roadmap)

Call `agenda_view(scope="global")` once; it returns `today`, `roadmap` (tasks by tier),
`signals`, and `counts`. Each task carries `tier`, `highlight`, `extra` (any custom
fields, e.g. a tracker key), and `sessions` — the sessions holding its lease (claim =
flow): running (live) / idle / cold, with `last_active_s` and each session's
`cost_weighted`. Lead with one line of insight, then:

**1. Today** (from `today`): the highlight first, then the committed items and the NOW
tier, each with the sessions serving it (live / idle / cold). Then, in priority order,
from `signals`:
- **Gaps** (`signals.gaps`): a today task with no session on it.
- **WIP** (`signals.live_wip`): if over ~3 live, say so; starting is cheap.
- **Fragmentation** (`signals.fragmentation`): one session spread across several tasks;
  name what to drop.
- **Drift** (`signals.drift`): live sessions on no today task.

**2. Suggested**: from `roadmap` and the signals, propose what to pull into NOW/NEXT or
commit to today. Skip rewords of tasks already in `roadmap`. Per item give a plain
what-it-is and a concrete call: commit, add to a tier (`agenda_add`), or leave.

**3. Roadmap** (from `roadmap`): NEXT in full, LATER / CLEANUP / CANDIDATES compressed to
a line each, DONE as `counts.done`.

On `suggest`, expand section 2 and collapse 1 and 3.

## Close on calm

Validate first (what's closed, what's committed), end with one concrete next move plus
"the rest is captured in the agenda, you can stop holding it." Keep it tight. No
em-dashes. Claim first, then the evidence.
