---
title: "Fleet-aware attention engine: schedule-driven wind-down, bedtime signal, and a day-timeline viewer"
status: open
area: scheduling
owner: null
filed: 2026-10-04
scope: L
verify: probe
---

## Why (the user need, not a nicety)

Agentic coding runs on variable-ratio reinforcement — each prompt might be the "jackpot" answer, at high frequency — the same loop as slot machines/gambling. The operator-facing symptom: you set a stop time ("I'll stop at 21:00"), blow past it, run 8+ projects in parallel, and cannot let go at night because you don't trust that nothing needs you. The ask is NOT more automation of the work; it is for cabildo to help the human keep a line they set for themselves: wind down at a chosen time, let go when it's safe, and review without reading walls of text. cabildo is uniquely placed because it is the only component that sees ALL sessions at once and already nudges at the hook level (the trunk guard).

This issue is one feature — a **fleet-aware attention engine** — with several faces. It is filed as one on purpose (see "deliberately NOT doing").

## MEASURED

- Prior art confirms the mechanism and that the user is not alone: Axios "'They operate like slot machines': AI agents are scrambling power users' brains" (https://www.axios.com/2026/04/04/ai-agents-burnout-addiction-claude-code-openclaw); "The Slot-Machine Was the Point" (https://dev.to/arthurpro/the-slot-machine-was-the-point-4fm1); "Your coding agent is a slot machine" (https://dev.to/desty2k/your-coding-agent-is-a-slot-machine-1a2h); "The Slot Machine in Your IDE" (https://www.mrlatte.net/en/stories/2026/03/18/ai-coding-is-gambling/). The ~80% versus ~5% payout comparison in those posts is a blog claim, not a measurement.
- On 2026-10-04, the `standup` tool with `hours=12` listed 8 names active in those 12 hours, and one of them was `@daemon`. This does not measure how many ran at the same time.
- cabildo already has the primitives this needs: hook-level nudging on tool use (the trunk guard, `_deny_commit_on_trunk` in `src/cabildo/guard.py`) proves an advisory message can be injected at a session's next tool call; `record_decision`/`read_decisions` store STRUCTURED decisions (not prose); `standup`/`history` hold activity over time; the bus delivers across all projects on the machine.
- Constraint confirmed by the user and by the model: an **idle** session cannot be interrupted. A nudge can only land on a session's **next tool call**. An actively-coding session fires tools constantly, so this is fine for the active case and is exactly why the signal must be schedule-anchored, not clock-triggered.

## Research (what the evidence actually supports — 2026-10-04)

These findings come from a subagent's report. The full briefing was in a temporary folder and no longer exists, and the sources were not re-read when this section was written.

- **The gambling analogy is grounded but overstated.** Gambling disorder is the *only* formally recognized behavioral addiction in DSM-5; ICD-11 Gaming Disorder (2019) is contested; "AI coding addiction" has **no diagnostic status and no clinical literature**. The dopamine / reward-prediction-error basis of variable-ratio reinforcement is MEASURED (Schultz); its application to agentic coding is INFERRED, not demonstrated.
- **One direct empirical study exists** — Santos et al., arXiv 2608.05561 (2026), n=119 engineers — and found *functional dependence* common but *clinical impaired control* uncommon. So the realistic target is habit/overuse, not a clinical disorder.
- **What is specific to agentic coding:** zero-friction prompting removes the fatigue signal that used to end sessions; watching agents work is a passive-active hybrid with no felt completion; ~80% success still produces reward-prediction-error because each outcome is uncertain. Practitioner-observed (Rousseau, Karpathy, Ronacher), corroborated by a CHI 2025 design-patterns paper and the Santos survey.
- **Risk factors:** ADHD is a MEASURED risk factor for *technology addiction generally* (PMC11798432, n=346, cross-sectional, mediated by impulsivity); its application to AI tools is INFERRED. No at-risk-population study exists for AI coding.
- **Intervention evidence — this cuts against an advisory-only default.** Ranked by evidence: (1) **commitment devices / Ulysses contracts** — pre-commit to a hard stop while cold, the best-supported class (Bryan/Karlan/Nelson RCT review; Lembke); (2) **friction at the point of extending** — a "keep working past your stop?" step that cannot be dismissed in one click; (3) **implementation intentions** — a pre-session "I stop at X" declaration; (4) **passive advisory nudges** — the WEAKEST, and not well-supported for habitual use on their own. Zero clinical trials exist for any of this in the AI-coding context.

**Design consequence:** advisory nudges carry the *information* layer (they let you keep a line you already want to keep), but the evidence says they will not hold against genuine compulsion. The commitment-device / friction / hard-stop-when-enrolled path is therefore promoted to a first-class option below, not a vague "later."

Sources: [Santos et al. 2026](https://arxiv.org/abs/2608.05561) · [Rousseau, "One More Prompt"](https://blog.quent.in/blog/2026/03/09/one-more-prompt-the-dopamine-trap-of-agentic-coding/) · [ADHD × tech addiction, PMC11798432](https://pmc.ncbi.nlm.nih.gov/articles/PMC11798432/) · [Bryan/Karlan/Nelson, Commitment Devices](http://houdekpetr.cz/!data/public_html/papers/Bryan%20et%20al%202010.pdf) · [Axios, "operate like slot machines"](https://www.axios.com/2026/04/04/ai-agents-burnout-addiction-claude-code-openclaw)

## The feature

One engine: **schedule inputs → propagated over the bus to all sessions → rendered as three faces → all reading one compressed per-session feed.**

**Schedule inputs (what defines "the line"):**
- An explicit stop time (e.g. 21:00), per-project or global.
- Read-only import of the user's existing calendar (they run Google Calendar) for personal blocks — dinner, lunch, one-offs. cabildo READS it; it does not become a calendar. This handles "dinner out at 19:00 today" without teaching cabildo a rule.
- Ad-hoc, on-the-go declaration: a single command / bus message like `cabildo dinner 1h` writes a temporary block for today. Because every session is on the bus, that one declaration is **fleet-wide** — it lands in all 8 projects, not one. This is the thing only the middle component can do.

**Propagation:** the active block/stop is published on the bus; each session's hook sees it at its next tool call and winds down. (Idle sessions simply aren't working, so they need no interruption.)

**Three faces of the same state:**
1. **In-session wind-down nudge** — injected into context on tool activity inside the wrap-up window. Gentle first ("40 min to your 21:00 stop; good point to pick a finishing task rather than start a big one?"), escalating as the line approaches and passes ("it's 22:00, you said 21:00, 8 projects open — keep going or wrap?"). Anti-nag guardrails: silent outside the window; at most one message per escalation step; schedule-anchored so it NEVER fires at 3pm (the failure mode of a per-session clock reminder); never blocks.
2. **Overnight / bedtime attention signal** — inverts the compulsive pull-to-check into a trustworthy push. Silent while nothing needs the human; ONE notification the moment a session flips into "needs you" (blocked on input, errored, unread question in inbox). Lets the user leave the laptop and trust they'll hear only if it's real. Honest scope below.
3. **Viewer** — two tiers. (a) An ambient GNOME top-panel glance: a count + stop countdown ("3 need you · stop in 1h58"), kept thin. (b) A full overview page behind it, as a NORMAL window (extend `cabildo watch`, or a local web view), showing per-session live activity, the three buckets (needs-you / working / done-safe), and a **day timeline** (start, blocks worked, breaks, dinner, stop line) so decisions are made against the day's shape, plus the **reckoning**.

**Priority management / the reckoning:** cabildo holds all projects in one place, so it can answer "given N minutes to the stop and these M tasks in flight, you cannot land them all — which do you drop?" Non-code projects (e.g. an unfinished family-trip booking) belong on the same board as projects with deadlines, so they compete for attention EXPLICITLY instead of gnawing as invisible open loops — which is itself a trigger to context-switch and cram.

**Compressed output (the "20 pages" problem):** the long summaries exist because agents write narrative prose and nothing compresses it. Fix at the source: on wind-down a session must emit a **headline + open-decisions** (one line of what it did + any y/n items that need the human), via the existing `record_decision`/`read_decisions` channel. The viewer and the bedtime readout default to this terse layer — 8 one-liners, each expandable to full prose on demand. You review by scanning 8 lines and tapping the 2 that need you.

**Shared dependency (build first):** a lightweight per-session activity feed — a tool-use hook that records each session's current activity and, on wind-down, its headline + open-decisions. Every face reads this; `history` feeds the timeline. Without this feed the viewer and signal are only as precise as presence alone, which is the "finicky/disconnected" state today.

## Deliberately NOT doing (so it isn't re-litigated)

- **Advisory is the default, commitment-mode is first-class (not "later").** The engine stays advisory by default because the user explicitly wants to keep control, and the information layer alone helps you keep a line you already want to keep. BUT the evidence (see Research) is clear that advisory nudges are the weakest intervention and will not hold against genuine compulsion — so a **commitment mode** is a first-class, shipped option, not a someday: enroll while cold (a Ulysses contract) in a hard stop at time T, and from then on crossing T takes real friction — a confirmation that cannot be one-click dismissed, or, at the strongest setting, the guard actually denies (it can already deny). Enrollment is the user's choice; the default is never a surprise block. What is deliberately NOT done: no block is imposed on a user who did not enroll.
- **Not a general life organizer.** No groceries, errands, to-do lists. cabildo reads the schedule and defends it against the coding pull; it does not try to run the user's life. The moment it overreaches it loses the precision that earns trust.
- **cabildo does NOT claim "all your work is correct / all good."** It can truthfully report only its OWN data: what is *waiting on you* or *erroring* (unread inbox questions, blocked claims, post-error idle). It must not assert correctness — one wrong "all good" and the user stops trusting it, reintroducing the finickiness that keeps them checking. The signal is attention-needed, not all-clear.
- **Heavy UI is NOT inside the GNOME Shell extension.** Shell extensions break on every GNOME version bump. Keep the panel indicator thin (count + countdown + click-to-open); the real timeline/overview lives in a normal window so a GNOME bump can only break the glance, never the data or the view.

## INFERRED (not yet measured)

- That schedule-anchored wind-down nudges actually reduce overruns, and that a trustworthy "needs-you only" signal actually lets the user disengage at night. Plausible from the mechanism and the prior art, but UNTESTED — this is a behavioral claim about one user, so `verify: probe`.
- That emitting headline + open-decisions at the source yields reviews the user can act on in ~1 minute post-dinner.

## Done (probe)

Observed true on the real fleet:
1. User sets a stop time AND an ad-hoc block (`dinner 1h`); every ACTIVE session surfaces a wind-down nudge within one tool call of entering the window; the nudge fires at most once per escalation step and never outside the window (no 3pm nags).
2. The ad-hoc block is fleet-wide from one declaration — all active projects reflect it, not just the one it was typed in.
3. Overnight: zero notifications while nothing needs the user; exactly one push when a session first flips to needs-you, naming which session and why.
4. The viewer shows per-session one-line headline + open-decisions (8 lines, not 20 pages), with full prose only on drill-down; the overview shows a day timeline with the stop line and the reckoning (M tasks vs minutes-left, and what to drop to land the rest).
5. The GNOME panel glance shows the needs-you count + stop countdown and opens the overview; disabling/breaking the extension leaves the data and the `cabildo watch` view intact.
6. With commitment mode enrolled for a hard stop at T, crossing T is NOT dismissable in one click (friction confirmation, or guard-level denial at the strongest setting); with no enrollment, no block is ever imposed.

## State on 2026-11-05

- Built: `cabildo stop <time>` and the fleet wind-down nudge (ff1e027, `src/cabildo/notify.py`).
- Not found by a search of `src/`: commitment mode and the reckoning.
- Not checked: the other faces (bedtime signal, calendar import, GNOME panel glance, day
  timeline, headline feed). Search terms matched files for these, but no file was read.

## Open design note

Filed as ONE feature because the faces share one engine (schedule + fleet state) and one data feed (per-session headline). If build sequencing wants it split, the natural seam is: (1) per-session activity+headline feed, (2) schedule+wind-down nudge, (3) bedtime signal, (4) viewer/timeline/GNOME glance — in that order, each usable before the next.
