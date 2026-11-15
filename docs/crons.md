# Crons

`/loop` and `CronCreate` jobs are stored in cabildo, so every agent and you can see them, and
they do not expire after 7 days. The cabildo-push mod does this. You do not change how you use
`/loop`.

## See the jobs

List this session's jobs:

```bash
cabildo cron list
```

List every session's jobs:

```bash
cabildo cron list --all
```

Delete one job by its id:

```bash
cabildo cron rm cab-3
```

## How it works

The cabildo-push mod hooks four Claude Code tools. These are the tools `/loop` calls.

| Tool | What the mod does |
| --- | --- |
| `CronCreate` | Stores the job in the `crons` table. The id the model sees is `cab-<n>`. |
| `ScheduleWakeup` | Stores a one-shot job, clamped to 60-3600 s like Claude Code. |
| `CronDelete` | Deletes a `cab-<n>` job. Any other id goes to Claude Code. |
| `CronList` | Returns Claude Code's jobs and this session's cabildo jobs together. |

A loop in the mod asks `cabildo cron due` every 15 s at most, and fires each due job into its
session:

- A prompt that starts with `/name` runs as that slash command. Other prompts are submitted as
  the person's own words.
- A wakeup fires as `/loop <prompt>`, so the `/loop` skill runs again and schedules the next one.
- A recurring job that missed several matches (laptop asleep) fires once. Its next fire is the
  first match after now.
- While push Do-Not-Disturb is on (`/cabildo-dnd`), no job fires. Held jobs fire once when it
  is off.
- After `/clear`, the session's jobs move to the new session id.

## When Claude Code schedules the job instead

The mod falls back to Claude Code's own scheduler, so `/loop` works as before, when:

- the `cabildo` CLI fails, for example on a bad cron expression or a missing install;
- the mod has no cabildo session yet;
- the prompt is an autonomous-loop sentinel (`<<autonomous-loop>>`,
  `<<autonomous-loop-dynamic>>`). Claude Code replaces it with its own instructions at fire
  time, and cabildo cannot.

## Limits

- A job belongs to one session. When that session ends, its jobs stay in the table and do not
  fire. Delete them with `cabildo cron rm`.
- A job fires up to 15 s late.
