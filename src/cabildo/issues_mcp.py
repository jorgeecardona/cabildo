"""The `issues` MCP server (stdio) — this project's follow-up register, as tools.

A second server alongside `cabildo-mcp`, and deliberately narrower: the bus server is about other
agents, this one is about this repo's work. It is wired per-folder in the same `.mcp.json` that
`cabildo add` already writes, so a session opened in a registered project gets both without anyone
configuring anything. Like the bus server it is a plain stdio subprocess — one per session, no
daemon, no port, nothing to keep alive.

It is a thin wrapper over `cabildo.issues`, which is also what `cabildo issues check` runs. Same
module both ways, so the tool an agent calls and the gate that grades it cannot disagree.

**The files stay the store.** Every tool here reads and writes markdown in the repo. If this server
is down, `git` and an editor still work and nothing is lost.

Two things it borrows from the bus when the bus is there, and shrugs off when it is not:

  * `claim_issue` takes a real `issue:<slug>` advisory lease, so two agents on the same repo cannot
    quietly start the same work — the failure a folder of markdown cannot prevent by itself.
  * the owner it writes is your bus handle, so you are not asked to type a name you already have.

Neither is required. A repo with no cabildo DB gets the register and loses only the lease.
"""

from __future__ import annotations

import os
import pathlib

from mcp.server.fastmcp import FastMCP

from . import issues

mcp = FastMCP("issues")

CLAIM_TTL = 3600.0          # an hour: long enough to hold a task, short enough that a crash frees it


# --------------------------------------------------------------------------- #
# Where we are, and who we are
# --------------------------------------------------------------------------- #
def _folder() -> pathlib.Path:
    start = pathlib.Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    found = issues.find_folder(start)
    if found is None:
        raise issues.IssueFormatError(
            f"no docs/issues/ at or above {start} — `mkdir -p docs/issues && cabildo issues init` "
            f"to start one"
        )
    return found


def _identity() -> tuple[str | None, str | None]:
    """(handle, session_id) from the bus, or (None, None) when it isn't reachable.

    Imported lazily: the register must work in a repo that has never seen the bus, and importing
    the bus server eagerly would build a second FastMCP object for nothing.
    """
    try:
        from . import store
        from .mcp_server import _me

        with store.connect() as conn:
            sid = _me(conn)
            row = store.get_session(conn, sid)
            return (row["handle"] if row else None) or store.display_name(row), sid
    except Exception:
        return None, None


def _lease(slug: str, *, take: bool) -> str:
    """Take or drop the bus lease that backs an issue claim. Best-effort by design: the markdown
    write is the real thing, and a missing bus must not stop work."""
    try:
        from . import store

        with store.connect() as conn:
            from .mcp_server import _me

            sid = _me(conn)
            row = store.get_session(conn, sid)
            if take:
                got = store.acquire_claim(
                    conn,
                    resource=f"issue:{slug}",
                    holder_session=sid,
                    holder_handle=row["handle"] if row else None,
                    project=row["project"] if row else None,
                    ttl_seconds=CLAIM_TTL,
                    note=f"working {slug}",
                )
                if not got.get("ok"):
                    return f"\n⚠ another agent holds the bus lease on this: {got.get('held_by')}"
                return ""
            store.release_claim(conn, resource=f"issue:{slug}", holder_session=sid)
            return ""
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# Rendering — dense on purpose; this is context an agent pays for every call
# --------------------------------------------------------------------------- #
def _line(issue: issues.Issue) -> str:
    bits = [issue.status.value]
    if issue.owner:
        bits.append(f"@{issue.owner}")
    for key in ("priority", "scope", "verify"):
        if key in issue.extra:
            bits.append(str(issue.extra[key]))
    return f"  {issue.slug}\n    {issue.title}\n    {issue.area} · {' · '.join(bits)}"


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
@mcp.tool()
def board(limit: int = 10) -> str:
    """**Start here.** What is actually pickup-able in this project right now, and what everything
    else is waiting on.

    `ready` is work with no open blockers that nobody has taken — you can start any of it cold.
    `waiting` says what each of the others is stuck behind, so you can go unstick one on purpose
    instead of guessing. `yours` is what you already claimed and have not closed.

    This answers "what should I be working on"; it does not decide for you."""
    folder = _folder()
    handle, _ = _identity()
    b = issues.board(folder, owner=handle)
    out: list[str] = []

    if b.ready:
        out.append(f"READY — nothing blocks these, nobody owns them ({len(b.ready)}):")
        out += [_line(i) for i in b.ready[:limit]]
        if len(b.ready) > limit:
            out.append(f"  … and {len(b.ready) - limit} more; `list_issues` for the rest")
    else:
        out.append("READY — nothing. Every open issue is owned or blocked (see below).")

    if b.mine:
        out.append(f"\nYOURS — claimed by @{handle} and not closed ({len(b.mine)}):")
        out += [_line(i) for i in b.mine]

    if b.waiting:
        out.append(f"\nWAITING ({len(b.waiting)}):")
        out += [f"  {w.issue.slug} — {w.reason}" for w in b.waiting[:limit]]
        if len(b.waiting) > limit:
            out.append(f"  … and {len(b.waiting) - limit} more")

    if b.done:
        out.append(f"\n{b.done} done (kept, with the sha that fixed them).")
    return "\n".join(out)


@mcp.tool()
def list_issues(status: str = "", area: str = "", owner: str = "", include_done: bool = False) -> str:
    """The register, filtered. `status` is open/in-progress/blocked/done, `owner` is a handle or
    `none` for unowned. Done issues are excluded unless you ask for them — they keep their file, so
    the folder only grows.

    For deciding what to do next, prefer `board`: this is the flat list, which is the thing that
    made the question hard in the first place."""
    folder = _folder()
    found, broken = issues.load_all(folder)
    rows = [
        i for i in found
        if (include_done or status == "done" or i.open)
        and (not status or i.status == status)
        and (not area or i.area == area)
        and (not owner or (i.owner or "none") == owner)
    ]
    out = "\n".join(_line(i) for i in rows) or "(no matching follow-ups)"
    if broken:
        out += "\n\nUNPARSEABLE: " + ", ".join(p.path.name for p in broken)
    return out


@mcp.tool()
def show_issue(name: str) -> str:
    """One follow-up in full — front matter and body. `name` is a filename or any unambiguous part
    of its slug."""
    return issues.resolve(_folder(), name).read_text()


@mcp.tool()
def file_issue(
    title: str,
    area: str,
    body: str,
    blocked_by: list[str] | None = None,
    fields: dict[str, str] | None = None,
) -> str:
    """File a follow-up: work that is KNOWN, SCOPED and NOT BEING DONE — the residue a session
    leaves behind when it lands its main change, not a vague idea and not something you are about
    to do anyway.

    Write the body so it is **answerable cold**: assume the reader has none of your context and no
    access to this session. Concrete instances, file:line, the actual commands. Say what is MEASURED
    and what is INFERRED, in those words — an issue that states an inference as a finding sends the
    next person to fix the wrong thing, and they will believe you because you wrote it down. Define
    done as the specific observation that will be true afterwards.

    `blocked_by` takes the slugs of issues that must close first; that is what keeps `board` honest.
    `fields` carries whatever else this folder's config.toml requires (scope, priority, verify …) —
    `check_issues` will tell you if you missed one."""
    folder = _folder()
    path = issues.file_issue(
        folder,
        title=title,
        area=area,
        body=body,
        blocked_by=tuple(blocked_by or ()),
        extra=dict(fields or {}),
    )
    return f"filed {path.name}"


@mcp.tool()
def update_issue(
    name: str,
    title: str = "",
    status: str = "",
    area: str = "",
    blocked_by: list[str] | None = None,
    fields: dict[str, str] | None = None,
) -> str:
    """Change an issue's front matter. Only what you pass is touched; `fields` merges rather than
    replaces, so setting one project field cannot silently drop the others.

    Use `blocked_by` when you discover a real ordering — it is what moves work off `ready` and onto
    `waiting` for everyone, including whoever reads this next week."""
    folder = _folder()
    changes: dict = {}
    if title:
        changes["title"] = title
    if status:
        if status not in tuple(issues.Status):
            return f"unknown status {status!r} — one of {', '.join(issues.Status)}"
        changes["status"] = issues.Status(status)
    if area:
        changes["area"] = area
    if blocked_by is not None:
        changes["blocked_by"] = tuple(blocked_by)
    if fields:
        changes["extra"] = dict(fields)
    if not changes:
        return "nothing to change"
    updated = issues.update(folder, name, **changes)
    return f"updated {updated.path.name}\n{_line(updated)}"


@mcp.tool()
def claim_issue(name: str, force: bool = False) -> str:
    """Take a follow-up: writes you as its owner, moves it to in-progress, and takes a bus lease on
    `issue:<slug>` so another agent on this repo cannot start the same work.

    Refuses to steal from a named owner unless you pass force — an owner that changes hands quietly
    is how two windows end up doing the same thing."""
    folder = _folder()
    handle, _ = _identity()
    if not handle:
        return ("cannot tell who you are — the cabildo bus is not reachable from here, so there is "
                "no handle to write. Set one with `update_issue(name, fields={'owner': ...})`.")
    updated = issues.claim(folder, name, owner=handle, force=force)
    return f"claimed {updated.path.name} as @{handle}" + _lease(updated.slug, take=True)


@mcp.tool()
def release_issue(name: str) -> str:
    """Drop a follow-up you claimed: clears the owner, returns it to open, releases the bus lease.
    The honest move when you stop, so it goes back on the board instead of looking taken forever."""
    updated = issues.release(_folder(), name)
    _lease(updated.slug, take=False)
    return f"released {updated.path.name} — back on the board"


@mcp.tool()
def close_issue(name: str, fixed_in: str) -> str:
    """Mark a follow-up done. The file stays, carrying `fixed_in` — the sha that landed the fix, so
    the folder is a record of what was actually fixed by what.

    `fixed_in` must be the sha **on the mainline**, not the one from your worktree: the land path
    re-commits, and a worktree sha resolves only until that worktree is pruned."""
    folder = _folder()
    updated = issues.close(folder, name, fixed_in=fixed_in)
    _lease(updated.slug, take=False)
    left = len(issues.board(folder).ready)
    return f"closed {updated.path.name} (fixed_in {updated.fixed_in}) — {left} ready now"


@mcp.tool()
def check_issues() -> str:
    """Grade the folder against its own config.toml: front matter, filename convention, unknown
    areas, `done` without a sha, `blocked` with nothing named, blocked_by pointing at nothing,
    blocked_by cycles, broken links, and a README index that has drifted from the files.

    Run it after editing issues by hand. Same code path as `cabildo issues check`, so a green here
    is a green there."""
    folder = _folder()
    problems = issues.check(folder)
    if not problems:
        found, _ = issues.load_all(folder)
        return f"{folder}: clean ({len(found)} follow-ups)"
    return "\n".join(str(p) for p in problems) + f"\n\n{len(problems)} problem(s)"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
