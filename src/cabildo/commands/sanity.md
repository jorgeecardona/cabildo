---
description: Daily sanity read — today's focus and the sessions serving it, WIP and drift, from the cabildo agenda. The Today view of /agenda.
---

Give me a sanity read: what I'm actually on today, whether I'm over capacity, and
what's drifting. This is the **Today** view of the one agenda (the cabildo agenda,
read over MCP). `/agenda` shows Today + Suggested + Roadmap; `/sanity` is the same
data, centered on Today and session state.

Handle the argument ($ARGUMENTS): any text is a focus filter; empty is the full Today
read. To capture or commit work use the `/agenda` sub-commands (`add`, `commit`,
`highlight`, `done`).

## The read

Call `agenda_view(scope="global")` once and use its `today` block and `signals`. Each
today task carries its `sessions` (claim = flow: live / idle / cold) and any custom
data in `extra`. Lead with insight, not a list. Then:

1. **Concurrency up front**: `signals.live_wip` live sessions. If over ~3, say so
   plainly: starting is cheap, finishing counts.
2. **Highlight + focus**: the `highlight` task first, then the committed items and the
   NOW tier, each with the sessions serving it (live / idle / cold).
3. **Gaps**: `signals.gaps` — a today task with no session on it.
4. **Fragmentation**: `signals.fragmentation` — one session spread across several tasks;
   name what to drop.
5. **Drift**: `signals.drift` — live sessions on no today task. Pulling you off?
6. **One move**: the single highest-leverage action to protect the highlight.

## Close on calm

Validate first (closed loops, what's committed), end with one next move plus "the rest
is captured in the agenda, you can stop thinking about it." Keep it tight. No em-dashes.
