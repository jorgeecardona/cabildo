---
title: "test_sync_records_what_each_pull_did is flaky: sync reports "not paired" right after join"
status: done
area: tests
owner: brass-vision-4216c
filed: 2026-11-01
fixed_in: 62d122f (tests/test_pair.py::test_answer_pair_saves_the_peer_before_it_says_ok)
scope: S
verify: test
---

MEASURED: tests/test_pair.py:382 `assert st.error == "" and st.at > 0` failed with `st.error == "not paired"` in CI on 3a43401 (pipeline https://gitlab.com/cardona-oss/cabildo/-/pipelines/3015103031, job 17646577718), and once locally in a full `pytest tests` run on 2026-11-01; it passed when re-run alone. stderr shows "lan: paired with <node> at 127.0.0.1" before the failing sync, so pairing completed on one side.

INFERRED: a race between `pair.join` writing the peer record and `svc.sync_once()` reading it (the service may hold a cached peer list, or the other machine's side has not stored the pairing yet).

Done: the test passes 50 runs in a row in a full-suite run (`pytest tests -p no:randomly --count` or a loop), and CI on main is green for it.