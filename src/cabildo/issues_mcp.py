"""The issues/agenda tools — this project's follow-up register and the work agenda, as tools.

An internal module, not a separate server: `cabildo.mcp_server` adopts these tools into the single
`cabildo` server (see `_merge_issue_tools`), so a session sees one server with the bus tools and
these together. Kept a separate module on purpose — it is about this repo's (and the agenda's) work,
not other agents — and it must run with no bus DB present, so the bus is imported lazily and every
borrow from it degrades to a no-op.

It is a thin wrapper over `cabildo.issues` and `cabildo.agenda`, which is also what `cabildo issues
check` runs. Same module both ways, so the tool an agent calls and the gate that grades it cannot
disagree.

**The files stay the store.** Every tool here reads and writes markdown in the repo. If this server
is down, `git` and an editor still work and nothing is lost.

Two things it borrows from the bus when the bus is there, and shrugs off when it is not:

  * `claim_issue` takes a real `issue:<slug>` advisory lease, so two agents on the same repo cannot
    quietly start the same work — the failure a folder of markdown cannot prevent by itself.
  * the owner it writes is your bus handle, so you are not asked to type a name you already have.

Neither is required. A repo with no cabildo DB gets the register and loses only the lease.
"""

from __future__ import annotations

import json
import os
import pathlib

from mcp.server.fastmcp import FastMCP

from . import agenda, issues

mcp = FastMCP("issues")

CLAIM_TTL = 3600.0  # an hour: long enough to hold a task, short enough that a crash frees it
SHOW_LIMIT = 12_000  # chars. p90 of the issues in these repos is 7.4k, so this only bites runaways


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
    write is the real thing, and a missing bus must not stop work.
    """
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
            i
            for i in found
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
    # Budget the footer, path included, against the cap. `path` is absolute and its length is
    # unbounded (a macOS pytest temp dir alone runs to /private/var/folders/…), so slicing the body
    # to a fixed SHOW_LIMIT and then appending the footer overran the cap. Subtract the rendered
    # footer instead, so body + footer stays within SHOW_LIMIT no matter how long the path is.
    footer = (
        f"\n\n… truncated at {SHOW_LIMIT} of {len(text)} chars. Read {path} directly for the rest."
    )
    return text[: max(0, SHOW_LIMIT - len(footer))] + footer


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
    string. `check_issues` names what you missed.
    """
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
    blocked_by: list[str]
    | None = None,  # stays optional: None leaves blockers alone, [] clears them
    fields: dict[str, str | list[str]] = {},
) -> str:
    """Change an issue's front matter. Only what you pass is touched; `fields` merges, so setting
    one project field cannot drop the others. Set `blocked_by` when you find a real ordering — it
    is what moves work off `ready` for everyone, not just you.
    """
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
    to steal a named owner unless you pass force.
    """
    folder = _folder()
    if release:
        updated = issues.release(folder, name)
        _lease(updated.slug, take=False)
        return f"released {updated.path.name} — back on the board"
    handle, _ = _identity()
    if not handle:
        return (
            "cannot tell who you are — the cabildo bus is not reachable, so there is no handle "
            "to write. Use `update_issue(name, fields={'owner': ...})`."
        )
    updated = issues.claim(folder, name, owner=handle, force=force)
    return f"claimed {updated.path.name} as @{handle}" + _lease(updated.slug, take=True)


@mcp.tool(structured_output=False)
def close_issue(name: str, fixed_in: str) -> str:
    """Mark a follow-up done. The file stays, carrying `fixed_in`, so the folder records what was
    fixed by what. Use the sha on the MAINLINE — the push path re-commits, and a worktree sha
    resolves only until that worktree is pruned.
    """
    folder = _folder()
    updated = issues.close(folder, name, fixed_in=fixed_in)
    _lease(updated.slug, take=False)
    ready = issues.newly_ready(folder, updated.slug)
    left = len(issues.board(folder).ready)
    msg = f"closed {updated.path.name} (fixed_in {updated.fixed_in}) — {left} ready now"
    if ready:
        msg += "; unblocked: " + ", ".join(i.slug for i in ready)
    return msg


@mcp.tool(structured_output=False)
def check_issues() -> str:
    """Grade the folder against its own config.toml — front matter, filenames, `done` without a
    sha, blocked_by cycles, broken links, a stale index, and whatever else that config demands.
    Run it after editing issues by hand. Same module as `cabildo issues check` and the repo's gate,
    so a green here is a green there.
    """
    folder = _folder()
    problems = issues.check(folder)
    if not problems:
        found, _ = issues.load_all(folder)
        return f"{folder}: clean ({len(found)} follow-ups)"
    return "\n".join(str(p) for p in problems) + f"\n\n{len(problems)} problem(s)"


# --------------------------------------------------------------------------- #
# Agenda — the same store with the agenda preset, ranked by tier
#
# An agenda is an issues register (see cabildo.agenda) living either in this repo
# (`scope="project"` -> docs/agenda/) or home-wide (`scope="global"`). The tools
# below are the personal-workflow verbs; everything under them is the issues store.
# --------------------------------------------------------------------------- #
def _agenda_folder(scope: str) -> pathlib.Path:
    return agenda.resolve_dir(scope, create=True)


def _agenda_line(issue: issues.Issue) -> str:
    bits: list[str] = []
    if issue.extra.get("tier"):
        bits.append(str(issue.extra["tier"]))
    if issue.extra.get("today"):
        bits.append("@today")
    if issue.extra.get("highlight"):
        bits.append("★")
    if issue.extra.get("jira"):
        bits.append(str(issue.extra["jira"]))
    if issue.owner:
        bits.append(f"@{issue.owner}")
    bits.append(issue.status.value)
    return f"  {issue.slug}\n    {issue.title}\n    {' · '.join(bits)}"


@mcp.tool(structured_output=False)
def agenda_board(scope: str = "project", limit: int = 15) -> str:
    """**Your agenda** — tasks ranked NOW→CANDIDATES, with what is ready, yours, and waiting.

    `scope="project"` reads this repo's docs/agenda/; `scope="global"` reads the one home-wide
    agenda. Ready = nothing blocks it and nobody owns it; Waiting says what each is stuck behind.
    """
    try:
        folder = _agenda_folder(scope)
    except ValueError as exc:
        return str(exc)
    handle, _ = _identity()
    b = agenda.board(folder, owner=handle)
    lines: list[str] = []
    if b.ready:
        lines.append(f"READY ({len(b.ready)}):")
        lines += [_agenda_line(i) for i in b.ready[:limit]]
        if len(b.ready) > limit:
            lines.append(f"  … and {len(b.ready) - limit} more")
    else:
        lines.append("READY — nothing unblocked and unowned.")
    if b.mine:
        lines.append(f"\nYOURS ({len(b.mine)}):")
        lines += [_agenda_line(i) for i in b.mine]
    if b.waiting:
        lines.append(f"\nWAITING ({len(b.waiting)}):")
        lines += [f"  {w.issue.slug} — {w.reason}" for w in b.waiting[:limit]]
    if b.done:
        lines.append(f"\n{b.done} done.")
    return "\n".join(lines)


@mcp.tool(structured_output=False)
def agenda_add(
    title: str,
    tier: str,
    body: str = "",
    area: str = "general",
    today: bool = False,
    blocked_by: tuple[str, ...] = (),
    fields: dict[str, str | list[str]] = {},
    scope: str = "project",
) -> str:
    """Add a task. `tier` is one of NOW, NEXT, LATER, CLEANUP, CANDIDATES; `body` is its
    description; `blocked_by` names task slugs that must close first; `today=True` flags
    it for today. `fields` is any extra data your agenda's config declares (a tracker
    key, a due date, tags …) — cabildo carries it, it does not presume any of it.
    """
    try:
        folder = _agenda_folder(scope)
        path = agenda.add(
            folder,
            title=title,
            tier=tier,
            area=area,
            body=body,
            today=today,
            blocked_by=blocked_by,
            fields=_fields(fields),
        )
    except (ValueError, issues.IssueFormatError) as exc:
        return f"not added: {exc}"
    return f"added {path.name}"


@mcp.tool(structured_output=False)
def agenda_today(name: str, on: bool = True, scope: str = "project") -> str:
    """Flag a task for today (`on=False` clears it). `name` is any unambiguous part of its slug."""
    try:
        folder = _agenda_folder(scope)
        updated = agenda.set_flag(folder, name, "today", on)
    except (ValueError, issues.IssueFormatError) as exc:
        return f"no change: {exc}"
    return f"{'today' if on else 'cleared'}: {updated.path.name}"


@mcp.tool(structured_output=False)
def agenda_highlight(name: str, scope: str = "project") -> str:
    """Make one task the single highlight (the focus), clearing any other. Exclusive by design."""
    try:
        folder = _agenda_folder(scope)
        updated = agenda.set_highlight(folder, name)
    except (ValueError, issues.IssueFormatError) as exc:
        return f"no change: {exc}"
    return f"highlight: {updated.path.name}"


@mcp.tool(structured_output=False)
def agenda_done(name: str, scope: str = "project") -> str:
    """Mark a task done. No sha needed — an agenda task is not a commit. Names any task
    that was waiting on it and is now ready.
    """
    try:
        folder = _agenda_folder(scope)
        updated = agenda.mark_done(folder, name)
    except (ValueError, issues.IssueFormatError) as exc:
        return f"no change: {exc}"
    ready = issues.newly_ready(folder, updated.slug, agenda.config(folder))
    msg = f"done: {updated.path.name}"
    if ready:
        msg += " — now ready: " + ", ".join(i.slug for i in ready)
    return msg


@mcp.tool(structured_output=False)
def agenda_show(name: str, scope: str = "project") -> str:
    """One task in full — its front matter and description body."""
    try:
        folder = _agenda_folder(scope)
        path = issues.resolve(folder, name)
    except (ValueError, issues.IssueFormatError) as exc:
        return str(exc)
    text = path.read_text()
    if len(text) <= SHOW_LIMIT:
        return text
    footer = f"\n\n… truncated at {SHOW_LIMIT} of {len(text)} chars. Read {path} for the rest."
    return text[: max(0, SHOW_LIMIT - len(footer))] + footer


@mcp.tool(structured_output=False)
def agenda_claim(
    name: str, release: bool = False, force: bool = False, scope: str = "project"
) -> str:
    """Take a task: writes you as owner, moves it to in-progress, and takes an `issue:<slug>` bus
    lease so another of your agents cannot start the same one. `release=True` hands it back.
    """
    try:
        folder = _agenda_folder(scope)
    except ValueError as exc:
        return str(exc)
    if release:
        updated = issues.release(folder, name)
        _lease(updated.slug, take=False)
        return f"released {updated.path.name}"
    handle, _ = _identity()
    if not handle:
        return "cannot tell who you are — the bus is not reachable. Set owner via agenda tools once it is."
    try:
        updated = issues.claim(folder, name, owner=handle, force=force)
    except issues.IssueFormatError as exc:
        return f"not claimed: {exc}"
    return f"claimed {updated.path.name} as @{handle}" + _lease(updated.slug, take=True)


@mcp.tool(structured_output=False)
def agenda_view(scope: str = "project") -> str:
    """The full agenda as structured JSON for a skill to render: Today, the tier
    Roadmap, and session signals (live_wip, drift, fragmentation, gaps).

    Each task carries its matched sessions — the ones holding its `issue:<slug>` bus
    lease, so which session is on which task is ground truth (claim = flow), not a
    keyword guess — plus each session's recent token/cost usage. Any tracker key or
    other extra data a task carries is under its `extra`; cabildo does not interpret
    it, a caller that wants live tracker status composes that itself.
    """
    try:
        folder = _agenda_folder(scope)
    except ValueError as exc:
        return str(exc)
    try:
        from . import store

        with store.connect() as conn:
            data = agenda.view(conn, folder)
    except Exception:
        data = agenda.view(None, folder)  # no bus: structure without session matching
    return json.dumps(data, indent=2)


@mcp.tool(structured_output=False)
def agenda_handoff(name: str, next_step: str, scope: str = "project") -> str:
    """Leave a handoff note on a task: what state it's in and the next step, so the
    next agent (or you) resumes cold. Appends to the task's body under a dated
    Handoff heading. Do this before you stop on a task you did not finish.
    """
    from . import handoff

    try:
        folder = _agenda_folder(scope)
    except ValueError as exc:
        return str(exc)
    handle, _ = _identity()
    try:
        updated = handoff.append(folder, name, next_step, who=handle or "someone")
    except issues.IssueFormatError as exc:
        return f"no handoff: {exc}"
    return f"handoff noted on {updated.path.name}"


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
except Exception:  # private attribute: if a FastMCP bump moves it we just ship fatter schemas
    pass

# No `main()`: these tools are not a standalone server. `cabildo.mcp_server`
# adopts them into the single `cabildo` server; this module only defines them.
