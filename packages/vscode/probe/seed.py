# ruff: noqa: INP001 (a script the probe runs, not a module of a package)
"""Serve a throwaway broker for the probe: ``seed.py <socket> <agent pid> <folder>``.

It adds one agent, which runs in the process ``<agent pid>``, and one message from it to the
person. ``CABILDO_HOME`` must name a folder under the folder of ``<socket>``, so the probe never
writes to the real bus.
"""

import os
import sys
from pathlib import Path

from cabildo import broker, store
from cabildo.cli import ensure_human

socket_path, pid, folder = sys.argv[1], int(sys.argv[2]), sys.argv[3]
if not store.home().resolve().is_relative_to(Path(socket_path).resolve().parent):
    sys.exit(f"CABILDO_HOME={os.environ.get('CABILDO_HOME')} is not the probe's folder")

conn = store.connect(check_same_thread=False)
ensure_human(conn)
store.register_session(conn, "probe-agent", cwd=folder, claude_pid=pid)
store.set_handle(conn, "probe-agent", "probe-agent")
store.post_message(
    conn,
    from_session="probe-agent",
    from_handle="probe-agent",
    to_session=store.HUMAN_SESSION,
    body="hello from the probe\nsecond line",
)
conn.commit()
broker.serve(socket_path, conn=conn)
