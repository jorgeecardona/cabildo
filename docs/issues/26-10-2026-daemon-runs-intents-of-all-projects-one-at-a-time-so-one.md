---
title: Daemon runs intents of all projects one at a time, so one project's push waits on another's
status: open
area: daemon
owner: null
filed: 2026-10-26
priority: high
scope: S
verify: test
---

MEASURED: `store.take_next_intent` (src/cabildo/store.py) took the oldest pending intent across all projects, and the daemon loop (src/cabildo/daemon.py, `main`) ran it inline before taking the next. A slow push in project A (gate tests) blocked a push in project B, which cannot conflict with it.

Fix: lanes. A push runs one at a time per `project`; different projects run in parallel. spawn/ask keep one shared lane because spawn checks a single `max_agents` cap. `take_next_intent` picks the oldest pending intent whose lane has nothing `running` (one UPDATE..RETURNING, still atomic). The daemon runs each taken intent on its own thread with its own connection, up to a worker limit.

Done when: a test (tests/test_intent_lanes.py) queues a push for project A and one for project B, and both are `running` at the same time; two pushes for the same project never are.