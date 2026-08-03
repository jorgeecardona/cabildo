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
def board(status: str = "", area: str = "", owner: str = "", limit: int = 10) -> str:
    """**Start here** — what is pickup-able in this project now, and what everything else waits on.

    Ready = no open blockers, nobody has taken it; start any of it cold. Waiting says what each of
    the rest is stuck behind, so you can unstick one on purpose. Yours = what you claimed.

    Any filter (status open/in-progress/blocked/done, area, owner — `none` for unowned) switches it
    to a flat filtered list instead. Done issues keep their file, so they only show when asked for.
    """
    folder = _folder()
    handle, _ = _identity()

    if status or area or owner:
        found, broken = issues.load_all(folder)
        rows = [
            i for i in found
            if (status == "done" or i.open)
            and (not status or i.status == status)
            and (not area or i.area == area)
            and (not owner or (i.owner or "none") == owner)
        ]
        out = "\n".join(_line(i) for i in rows) or "(nothing matches)"
        if broken:
            out += "\n\nUNPARSEABLE: " + ", ".join(p.path.name for p in broken)
        return out

    b = issues.board(folder, owner=handle)
    out_lines: list[str] = []
    if b.ready:
        out_lines.append(f"READY — nothing blocks these, nobody owns them ({len(b.ready)}):")
        out_lines += [_line(i) for i in b.ready[:limit]]
        if len(b.ready) > limit:
            out_lines.append(f"  … and {len(b.ready) - limit} more; filter by area for the rest")
    else:
        out_lines.append("READY — nothing. Every open issue is owned or blocked (see below).")
    if b.mine:
        out_lines.append(f"\nYOURS — claimed by @{handle} and not closed ({len(b.mine)}):")
        out_lines += [_line(i) for i in b.mine]
    if b.waiting:
        out_lines.append(f"\nWAITING ({len(b.waiting)}):")
        out_lines += [f"  {w.issue.slug} — {w.reason}" for w in b.waiting[:limit]]
        if len(b.waiting) > limit:
            out_lines.append(f"  … and {len(b.waiting) - limit} more")
    if b.done:
        out_lines.append(f"\n{b.done} done (kept, with the sha that fixed them).")
    return "\n".join(out_lines)


@mcp.tool()
def show_issue(name: str) -> str:
    """One follow-up in full. `name` is a filename or any unambiguous part of its slug."""
    return issues.resolve(_folder(), name).read_text()


@mcp.tool()
def file_issue(
    title: str,
    area: str,
    body: str,
    blocked_by: list[str] | None = None,
    fields: dict[str, str] | None = None,
) -> str:
    """File a follow-up: work that is KNOWN, SCOPED and NOT BEING DONE — not a vague idea, not
    something you are about to do anyway.

    Body must be answerable cold: concrete instances, file:line, what is MEASURED vs INFERRED in
    those words, and done stated as an observation. The folder's README has the full rules.

    `blocked_by` = slugs that must close first. `fields` = whatever config.toml requires (scope,
    verify, priority …); `check_issues` names what you missed."""
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
    """Change an issue's front matter. Only what you pass is touched; `fields` merges, so setting
    one project field cannot drop the others. Set `blocked_by` when you find a real ordering — it
    is what moves work off `ready` for everyone, not just you."""
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
def claim_issue(name: str, release: bool = False, force: bool = False) -> str:
    """Take a follow-up — writes you as owner, moves it to in-progress, and takes an `issue:<slug>`
    bus lease so another agent here cannot start the same work.

    `release=True` hands it back: the honest move when you stop, so it stops looking taken. Refuses
    to steal a named owner unless you pass force."""
    folder = _folder()
    if release:
        updated = issues.release(folder, name)
        _lease(updated.slug, take=False)
        return f"released {updated.path.name} — back on the board"
    handle, _ = _identity()
    if not handle:
        return ("cannot tell who you are — the cabildo bus is not reachable, so there is no handle "
                "to write. Use `update_issue(name, fields={'owner': ...})`.")
    updated = issues.claim(folder, name, owner=handle, force=force)
    return f"claimed {updated.path.name} as @{handle}" + _lease(updated.slug, take=True)


@mcp.tool()
def close_issue(name: str, fixed_in: str) -> str:
    """Mark a follow-up done. The file stays, carrying `fixed_in`, so the folder records what was
    fixed by what. Use the sha on the MAINLINE — the land path re-commits, and a worktree sha
    resolves only until that worktree is pruned."""
    folder = _folder()
    updated = issues.close(folder, name, fixed_in=fixed_in)
    _lease(updated.slug, take=False)
    left = len(issues.board(folder).ready)
    return f"closed {updated.path.name} (fixed_in {updated.fixed_in}) — {left} ready now"


@mcp.tool()
def check_issues() -> str:
    """Grade the folder against its own config.toml — front matter, filenames, `done` without a
    sha, blocked_by cycles, broken links, a stale index, and whatever else that config demands.
    Run it after editing issues by hand. Same module as `cabildo issues check` and the repo's gate,
    so a green here is a green there."""
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
