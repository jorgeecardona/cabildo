"""Claims backed by the cabildo bus."""

from __future__ import annotations

import os
from pathlib import Path

from cabildo import store
from cabildo.context import git_project

CLIENT = "local"
CLAIM_TTL_SECONDS = 600.0


class BusClaims:
    """Registers one bus session and takes ``file:<path>`` claims through it.

    A path is refused when any other session holds a claim that covers it, including
    glob claims such as ``file:src/*``. The claims this object took are released by
    ``release_all``.
    """

    def __init__(self, cwd: Path, *, session_id: str) -> None:
        self.session_id = session_id
        root, name = git_project(str(cwd))
        if not cwd.resolve().is_relative_to(root):
            # git_project can name a project the folder is not inside (a pin or an env hint);
            # claims are repo-relative, so fall back to the folder itself.
            root, name = str(cwd.resolve()), cwd.resolve().name
        self.root, self.project_name = root, name
        self._taken: list[str] = []
        conn = store.connect()
        try:
            store.register_session(
                conn,
                session_id,
                cwd=str(cwd),
                project=self.root,
                project_name=self.project_name,
                client=CLIENT,
                owner_pid=os.getpid(),
            )
        finally:
            conn.close()

    def acquire(self, path: Path) -> str | None:
        """Take a claim on ``path``. Returns None on success, else the holder's name."""
        rel = path.relative_to(self.root).as_posix()
        conn = store.connect()
        try:
            holders = store.file_claim_holders(
                conn, self.root, [rel], exclude_session=self.session_id
            )
            if holders:
                return str(holders[0]["holder"])
            result = store.acquire_claim(
                conn,
                resource=f"file:{rel}",
                holder_session=self.session_id,
                holder_handle=None,
                project=self.root,
                ttl_seconds=CLAIM_TTL_SECONDS,
                note=f"{CLIENT} agent",
            )
        finally:
            conn.close()
        if result["ok"]:
            self._taken.append(f"file:{rel}")
            return None
        return str(result["held_by"])

    def release_all(self) -> None:
        """Release every claim this object took."""
        conn = store.connect()
        try:
            for resource in self._taken:
                store.release_claim(conn, resource=resource, holder_session=self.session_id)
        finally:
            conn.close()
        self._taken.clear()
