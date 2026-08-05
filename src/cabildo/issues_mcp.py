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
SHOW_LIMIT = 12_000         # chars. p90 of the issues in these repos is 7.4k, so this only bites runaways


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


def _fields(given: dict | None) -> dict[str, str | tuple[str, ...]]:
    """MCP hands us JSON, so a list arrives as a list; the store keeps tuples.

    Converting here rather than asking callers to is the whole point: the previous signature could
    only carry strings, so an agent facing a list-valued field had no correct move and tried four
    separators before giving up and losing three of its four tags.
    """
    out: dict[str, str | tuple[str, ...]] = {}
    for k, v in (given or {}).items():
        out[k] = tuple(str(x) for x in v) if isinstance(v, (list, tuple)) else v
    return out


# --------------------------------------------------------------------------- #
# Tools
#
# Signatures below prefer `tuple[str, ...] = ()` and `dict = {}` over `| None = None`. Pydantic
# renders an optional as an `anyOf` against `null`, which is a third of that parameter's schema
# spent saying "or omit it" — something a default already says. The empty defaults are never
# mutated, only read. Where `None` is load-bearing it stays, and says so.
#
# `structured_output=False` on every tool because every one of them returns prose. Left to
# auto-detect, a `-> str` tool gets an output schema, and FastMCP then answers each call with the
# text in `content` AND the same text again in `structuredContent` — the whole reply, twice, for a
# `{"result": ...}` wrapper no caller here unpacks. Measured on `board`: 4,646 wire chars down to
# 2,166. If a tool ever returns something genuinely structured, drop the flag on that one.
# --------------------------------------------------------------------------- #
@mcp.tool(structured_output=False)
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


@mcp.tool(structured_output=False)
def show_issue(name: str) -> str:
    """One follow-up in full. `name` is a filename or any unambiguous part of its slug."""
    path = issues.resolve(_folder(), name)
    text = path.read_text()
    if len(text) <= SHOW_LIMIT:
        return text
    return (
        f"{text[:SHOW_LIMIT]}\n\n… truncated at {SHOW_LIMIT} of {len(text)} chars. "
        f"Read {path} directly for the rest."
    )


@mcp.tool(structured_output=False)
def file_issue(
    title: str,
    area: str,
    body: str,
    blocked_by: tuple[str, ...] = (),
    fields: dict[str, str | list[str]] = {},
) -> str:
    """File a follow-up: work that is KNOWN, SCOPED and NOT BEING DONE — not a vague idea, not
    something you are about to do anyway.

    Body must be answerable cold: concrete instances, file:line, what is MEASURED vs INFERRED in
    those words, and done stated as an observation. The folder's README has the full rules.

    `blocked_by` = slugs that must close first. `fields` = whatever config.toml requires (scope,
    verify, priority …); a list-valued field like `tags` takes a real list, never a delimited
    string. `check_issues` names what you missed."""
    folder = _folder()
    path = issues.file_issue(
        folder,
        title=title,
        area=area,
        body=body,
        blocked_by=tuple(blocked_by),
        extra=_fields(fields),
    )
    return f"filed {path.name}"


@mcp.tool(structured_output=False)
def update_issue(
    name: str,
    title: str = "",
    status: str = "",
    area: str = "",
    blocked_by: list[str] | None = None,   # stays optional: None leaves blockers alone, [] clears them
    fields: dict[str, str | list[str]] = {},
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
        changes["extra"] = _fields(fields)
    if not changes:
        return "nothing to change"
    updated = issues.update(folder, name, **changes)
    return f"updated {updated.path.name}\n{_line(updated)}"


@mcp.tool(structured_output=False)
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


@mcp.tool(structured_output=False)
def sleep_issue(name: str, days: int = issues.SLEEP_DAYS, reason: str = "", wake: bool = False) -> str:
    """Park a follow-up for `days` (default a week) — it leaves `ready` and comes back on its own.

    Use it when work is real but genuinely not now: waiting on someone else's deploy, a credential
    that rotates Monday, a decision you have asked for. Not for work you have decided against —
    that is a `done`/`blocked` call, or a body edit saying why.

    What it stores is a wake DATE, not a `sleeping` status, and that matters to you as the caller:
    nothing has to remember to wake it, so nothing can forget. It reappears in `board` the day the
    date passes, with no second call. Until then it sits in `waiting` wearing its wake date and your
    reason — asleep is not gone, and the board still says why it is not ready.

    `reason` is required: an unexplained snooze is indistinguishable from avoidance, and the next
    reader will believe whatever the file says. `wake=True` brings it back now."""
    folder = _folder()
    if wake:
        updated = issues.wake(folder, name)
        return f"woke {updated.path.name} — back on the board"
    if not reason.strip():
        return ("sleeping needs a reason — say what it is waiting ON, so the next reader can tell a "
                "deferral from a dodge. Pass reason=...")
    updated = issues.sleep(folder, name, days=days, reason=reason)
    left = len(issues.board(folder).ready)
    return (f"slept {updated.path.name} until {updated.sleep_until} ({days}d) — {left} ready now. "
            "It returns on its own; no follow-up call needed.")


@mcp.tool(structured_output=False)
def close_issue(name: str, fixed_in: str) -> str:
    """Mark a follow-up done. The file stays, carrying `fixed_in`, so the folder records what was
    fixed by what. Use the sha on the MAINLINE — the land path re-commits, and a worktree sha
    resolves only until that worktree is pruned."""
    folder = _folder()
    updated = issues.close(folder, name, fixed_in=fixed_in)
    _lease(updated.slug, take=False)
    left = len(issues.board(folder).ready)
    return f"closed {updated.path.name} (fixed_in {updated.fixed_in}) — {left} ready now"


@mcp.tool(structured_output=False)
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


# --------------------------------------------------------------------------- #
# Schema slimming — the tool list is the part every session pays for, always
# --------------------------------------------------------------------------- #
def _slim(schema: dict) -> dict:
    """Strip Pydantic's auto-generated `title` keys out of a generated JSON Schema, in place.

    Every property gets one — `blocked_by` earns `{"title": "Blocked By"}` — and it only restates the
    key it hangs off, so it informs nobody and costs on every call. This list ships in the system
    prompt of every request in every registered repo and is re-read from cache each time, which makes
    it the one part of this server worth counting bytes on.

    Only the auto-generated weight goes. Descriptions are what keep these tools used correctly and
    they stay whole.

    Careful: `title` is a schema keyword *and* a parameter of `file_issue`. Under `properties` the
    keys are parameter names, never keywords, so recursion there walks values only — popping blindly
    would delete that parameter from the schema.
    """
    schema.pop("title", None)
    for key, value in schema.items():
        if key == "properties":
            for sub in value.values():
                _slim(sub)
        elif key in ("items", "additionalProperties") and isinstance(value, dict):
            _slim(value)
        elif key in ("anyOf", "oneOf", "allOf"):
            for sub in value:
                _slim(sub)
    return schema


try:
    for _t in mcp._tool_manager._tools.values():
        _slim(_t.parameters)
except Exception:       # private attribute: if a FastMCP bump moves it we just ship fatter schemas
    pass


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
