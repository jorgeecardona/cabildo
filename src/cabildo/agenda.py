"""A personal or per-project work agenda, built on the issues register.

An agenda is nothing but an issues folder with a preset config: tasks ranked by a
`tier` (NOW..CANDIDATES) instead of a priority, a `today` flag, a single exclusive
`highlight`, an optional tracker key, and `done` that needs no commit sha because
an agenda task is not a code change. Everything else — the markdown-file store, the
board, blocked_by, the bus lease behind a claim — is the issues subsystem unchanged.

Two places an agenda can live, chosen by `scope`:

  * ``project`` — ``docs/agenda/`` in the repo, found by walking up from the cwd,
    for work that belongs to one codebase.
  * ``global`` — one home-wide agenda spanning every project, at
    ``$CABILDO_AGENDA_DIR`` or ``<cabildo home>/agenda``.

The preset shipped here is generic on purpose: the tracker field `jira` matches an
ordinary uppercase project key, and a user narrows it (or renames it) in the
folder's own ``config.toml``. Nothing project- or company-specific lives in this
file.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import os
import pathlib
import re

from . import issues, store

#: The generic agenda config written into a fresh agenda folder. A user edits it in
#: place: narrow `jira.pattern` to their tracker, add `areas`, and so on.
PRESET_TOML = """\
# cabildo agenda — a work agenda on the issues store. Edit freely; this is yours.
closed_needs_fixed_in = false   # an agenda task is done when it is done, no sha
rank_by = "tier"                # the board is NOW-first
index = false
singleton_fields = ["highlight"]  # at most one task is the focus

[fields.tier]
values = ["NOW", "NEXT", "LATER", "CLEANUP", "CANDIDATES"]

[fields.today]
values = ["true"]

[fields.highlight]
values = ["true"]

[fields.jira]
# Your tracker's key shape. Narrow this to e.g. "^ABC-\\\\d+$" for one project.
pattern = "^[A-Z][A-Z0-9]+-\\\\d+$"
"""

TIERS = ("NOW", "NEXT", "LATER", "CLEANUP", "CANDIDATES")
FLAG = "true"  # the stored value of a boolean-ish field; absence means false
DEFAULT_AREA = "general"
_DIRNAME = "agenda"
_GLOBAL_ENV = "CABILDO_AGENDA_DIR"


# --------------------------------------------------------------------------- #
# Where an agenda lives
# --------------------------------------------------------------------------- #
def global_dir() -> pathlib.Path:
    """The one home-wide agenda: ``$CABILDO_AGENDA_DIR`` or ``<cabildo home>/agenda``."""
    override = os.environ.get(_GLOBAL_ENV, "").strip()
    if override:
        return pathlib.Path(override).expanduser()
    return store.home() / _DIRNAME


def project_dir(cwd: str | pathlib.Path | None = None) -> pathlib.Path | None:
    """This repo's ``docs/agenda/``, searched upward from `cwd` and stopping at the
    git root, so a repo never adopts an ancestor's agenda. None when there is none.
    """
    here = pathlib.Path(cwd or os.getcwd()).resolve()
    for base in (here, *here.parents):
        cand = base / "docs" / _DIRNAME
        if cand.is_dir():
            return cand
        if (base / ".git").exists():
            break
    return None


def _project_target(cwd: str | pathlib.Path | None) -> pathlib.Path:
    """Where a project agenda should be created: ``<git root>/docs/agenda`` when in a
    repo, else ``<cwd>/docs/agenda``.
    """
    here = pathlib.Path(cwd or os.getcwd()).resolve()
    for base in (here, *here.parents):
        if (base / ".git").exists():
            return base / "docs" / _DIRNAME
    return here / "docs" / _DIRNAME


def resolve_dir(
    scope: str = "project", cwd: str | pathlib.Path | None = None, *, create: bool = False
) -> pathlib.Path:
    """The agenda folder for `scope`. With ``create=True`` it is made (with the preset
    config) when absent; otherwise a missing folder raises.
    """
    if scope == "global":
        folder = global_dir()
    elif scope == "project":
        folder = project_dir(cwd) or _project_target(cwd)
    else:
        raise ValueError(f"scope must be 'project' or 'global', not {scope!r}")
    if create:
        return ensure(folder)
    if not folder.is_dir():
        raise issues.IssueFormatError(
            f"no agenda at {folder} — create one with scope create, or `cabildo agenda init`"
        )
    return folder


def ensure(folder: pathlib.Path) -> pathlib.Path:
    """Create the folder and drop the preset config if it has none. Idempotent."""
    folder.mkdir(parents=True, exist_ok=True)
    cfg = folder / issues.CONFIG_NAME
    if not cfg.exists():
        cfg.write_text(PRESET_TOML)
    return folder


def config(folder: pathlib.Path) -> issues.Config:
    return issues.load_config(folder)


# --------------------------------------------------------------------------- #
# The workflow, on top of the issues store
# --------------------------------------------------------------------------- #
def add(
    folder: pathlib.Path,
    *,
    title: str,
    tier: str,
    area: str = DEFAULT_AREA,
    body: str = "",
    jira: str = "",
    today: bool = False,
    blocked_by: tuple[str, ...] = (),
) -> pathlib.Path:
    """Create a task. `tier` places it on the board; `body` is its description."""
    if tier not in TIERS:
        raise issues.IssueFormatError(f"unknown tier {tier!r} — one of {', '.join(TIERS)}")
    extra: dict[str, str | tuple[str, ...]] = {"tier": tier}
    if jira:
        extra["jira"] = jira
    if today:
        extra["today"] = FLAG
    return issues.file_issue(
        folder,
        title=title,
        area=area,
        body=body,
        blocked_by=blocked_by,
        extra=extra,
        config=config(folder),
    )


def _set_field(issue: issues.Issue, field: str, on: bool, cfg: issues.Config) -> issues.Issue:
    """Rewrite one issue with `field` set or removed. Removing keeps the file clean;
    `issues.update` merges and so cannot drop a key.
    """
    extra = dict(issue.extra)
    if on:
        extra[field] = FLAG
    else:
        extra.pop(field, None)
    updated = dataclasses.replace(issue, extra=extra)
    issues.save(updated, cfg)
    return updated


def set_flag(folder: pathlib.Path, name: str, field: str, on: bool) -> issues.Issue:
    """Set or clear a boolean-ish field (``today``)."""
    cfg = config(folder)
    issue = issues.load(issues.resolve(folder, name), cfg)
    return _set_field(issue, field, on, cfg)


def set_highlight(folder: pathlib.Path, name: str) -> issues.Issue:
    """Make one task the single highlight, clearing any other. Exclusivity is the
    point: `check` rejects two, so this enforces it at write time.
    """
    cfg = config(folder)
    target = issues.resolve(folder, name).resolve()
    picked: issues.Issue | None = None
    for path in sorted(folder.glob("*.md")):
        if path.name == issues.README_NAME:
            continue
        issue = issues.load(path, cfg)
        want = path.resolve() == target
        if want == bool(issue.extra.get("highlight")):
            picked = issue if want else picked
            continue
        updated = _set_field(issue, "highlight", want, cfg)
        picked = updated if want else picked
    if picked is None:
        raise issues.IssueFormatError(f"no agenda task matches {name!r}")
    return picked


def mark_done(folder: pathlib.Path, name: str) -> issues.Issue:
    """Close a task. No sha: the preset sets ``closed_needs_fixed_in = false``."""
    return issues.update(folder, name, status=issues.Status.DONE, config=config(folder))


def board(folder: pathlib.Path, *, owner: str | None = None) -> issues.Board:
    return issues.board(folder, config(folder), owner=owner)


def check(folder: pathlib.Path) -> list[issues.Problem]:
    return issues.check(folder, config(folder))


# --------------------------------------------------------------------------- #
# The view — the agenda plus which live sessions are on which task (claim = flow)
# --------------------------------------------------------------------------- #
# Replaces flows-mcp's keyword flow-matching with ground truth: a task's sessions
# are exactly the ones holding its `issue:<slug>` bus lease. Warmth comes from how
# recently the session was seen, not from guessing.
WARM_WINDOW = 120.0  # seen this recently => warm/running


def _session_state(age: float) -> str:
    if age < WARM_WINDOW:
        return "warm"
    if age < store.ACTIVE_WINDOW:
        return "idle"
    return "cold"


def matched_sessions(conn, slug: str) -> list[dict]:
    """The sessions holding this task's `issue:<slug>` lease, each with how long ago
    it was seen and whether it is warm/idle/cold. [] when the bus is absent.
    """
    if conn is None:
        return []
    now = store._now()
    out: list[dict] = []
    for row in conn.execute(
        "SELECT * FROM claims WHERE resource=? AND expires_at>? ORDER BY acquired_at",
        (f"issue:{slug}", now),
    ):
        sess = store.get_session(conn, row["holder_session"])
        if sess is None:
            continue
        age = now - sess["last_seen_at"]
        state = _session_state(age)
        out.append(
            {
                "session": store.display_name(sess, row["holder_session"]),
                "session_id": row["holder_session"],
                "last_active_s": round(age, 1),
                "running": state == "warm",
                "state": state,
            }
        )
    return out


def _task_claims(conn) -> dict[str, list[str]]:
    """{session_id: [slug, ...]} of the task leases each session currently holds. A
    task lease is exclusive (one holder), so the interesting split is the other way:
    one session holding several tasks at once.
    """
    now = store._now()
    out: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT holder_session, resource FROM claims WHERE resource LIKE 'issue:%' AND expires_at>? "
        "ORDER BY acquired_at",
        (now,),
    ):
        out.setdefault(row["holder_session"], []).append(row["resource"][len("issue:") :])
    return out


def _item(issue: issues.Issue, sessions: list[dict]) -> dict:
    return {
        "slug": issue.slug,
        "title": issue.title,
        "tier": str(issue.extra.get("tier") or ""),
        "jira": str(issue.extra.get("jira") or ""),
        "highlight": bool(issue.extra.get("highlight")),
        "today": bool(issue.extra.get("today")),
        "status": issue.status.value,
        "blocked_by": list(issue.blocked_by),
        "sessions": sessions,
    }


def view(conn, folder: pathlib.Path) -> dict:
    """The structured agenda for a skill to render: Today, the tier Roadmap, and the
    session-derived signals (live_wip, drift, fragmentation, gaps, to_track). Tasks
    carry their matched sessions from the bus; with no bus, the structure still comes
    back with empty session lists. Jira *status* is not fetched here — the returned
    `jira` is the key string only; the skill composes live status via ops-mcp.
    """
    cfg = config(folder)
    all_issues, _ = issues.load_all(folder, cfg)
    open_issues = [i for i in all_issues if i.open]
    by_slug_sessions = {i.slug: matched_sessions(conn, i.slug) for i in open_issues}
    items = {i.slug: _item(i, by_slug_sessions[i.slug]) for i in open_issues}

    today = [items[i.slug] for i in open_issues if i.extra.get("today")]
    today_slugs = {i.slug for i in open_issues if i.extra.get("today")}
    roadmap = {
        tier: [items[i.slug] for i in open_issues if str(i.extra.get("tier")) == tier]
        for tier in TIERS
    }

    live_wip = sum(1 for s in by_slug_sessions.values() if any(x["running"] for x in s))
    gaps = [slug for slug in today_slugs if not by_slug_sessions.get(slug)]
    to_track = [i.slug for i in open_issues if not i.extra.get("jira")]

    fragmentation: list[dict] = []
    drift: list[dict] = []
    if conn is not None:
        claims_by_session = _task_claims(conn)
        fragmentation = [
            {"session": store.display_name(store.get_session(conn, sid), sid), "slugs": slugs}
            for sid, slugs in claims_by_session.items()
            if len(slugs) >= 2
        ]
        now = store._now()
        for sess in store.active_sessions(conn):
            if now - sess["last_seen_at"] >= WARM_WINDOW:
                continue  # only warm sessions count as drift
            held = claims_by_session.get(sess["session_id"], [])
            if not any(slug in today_slugs for slug in held):
                drift.append({"session": store.display_name(sess), "on": held or None})

    return {
        "today": today,
        "roadmap": roadmap,
        "signals": {
            "live_wip": live_wip,
            "drift": drift,
            "fragmentation": fragmentation,
            "gaps": gaps,
            "to_track": to_track,
        },
        "counts": {
            "open": len(open_issues),
            "today": len(today),
            "done": sum(1 for i in all_issues if not i.open),
        },
    }


# --------------------------------------------------------------------------- #
# Importing a flat roadmap.md (a checklist under `## TIER` headings) into files
# --------------------------------------------------------------------------- #
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")
_ITEM_RE = re.compile(r"^- \[([ xX~])\]\s*(.*)$")
#: A tracker key such as ABC-123. Generic on purpose; captured into the `jira`
#: field so the source text stays readable.
_TICKET_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
_MARK_STATUS = {
    " ": issues.Status.OPEN,
    "x": issues.Status.DONE,
    "X": issues.Status.DONE,
    "~": issues.Status.IN_PROGRESS,
}


@dataclasses.dataclass
class RoadmapTask:
    title: str
    tier: str | None
    status: issues.Status
    today: bool
    highlight: bool
    jira: str
    body: str


def parse_roadmap(text: str) -> tuple[list[RoadmapTask], list[str]]:
    """Parse a flat agenda file into tasks. Top-level ``- [ ]`` / ``- [x]`` / ``- [~]``
    items become tasks (open / done / in-progress); the enclosing ``## TIER`` heading
    is the tier (a ``## DONE`` heading carries no tier, only the done status); indented
    lines are the body; ``@today`` / ``@highlight`` are flags; the first tracker key on
    the line is the jira field. Returns (tasks, warnings). The highlight is made
    exclusive here, so importing a file with several keeps only the first.
    """
    tasks: list[RoadmapTask] = []
    warnings: list[str] = []
    tier: str | None = None
    current: RoadmapTask | None = None
    body: list[str] = []

    def flush() -> None:
        nonlocal current
        if current is not None:
            current.body = "\n".join(body).strip()
            tasks.append(current)
        current = None
        body.clear()

    for line in text.splitlines():
        section = _SECTION_RE.match(line)
        if section:
            flush()
            name = section.group(1).strip().upper()
            tier = name if name in TIERS else None
            continue
        item = _ITEM_RE.match(line)
        if item:
            flush()
            rest = item.group(2).strip()
            today = "@today" in rest
            highlight = "@highlight" in rest
            rest = rest.replace("@today", "").replace("@highlight", "")
            rest = re.sub(r"\s{2,}", " ", rest).strip()
            ticket = _TICKET_RE.search(rest)
            current = RoadmapTask(
                title=rest,
                tier=tier,
                status=_MARK_STATUS[item.group(1)],
                today=today,
                highlight=highlight,
                jira=ticket.group(0) if ticket else "",
                body="",
            )
            continue
        if current is not None and line.strip():
            body.append(line.strip())
    flush()

    lit = [t for t in tasks if t.highlight]
    for extra in lit[1:]:
        extra.highlight = False
        warnings.append(f"dropped a second @highlight on: {extra.title[:60]}")
    return tasks, warnings


def _unique_slug(folder: pathlib.Path, slug: str, taken: set[str]) -> str:
    """A slug not already used in this import or on disk, so two similar titles do
    not collide on one filename.
    """
    base = slug or "task"
    candidate = base
    n = 2
    while candidate in taken or any(folder.glob(f"*-{candidate}.md")):
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def import_tasks(
    folder: pathlib.Path, tasks: list[RoadmapTask], *, owner: str = "me"
) -> dict[str, int]:
    """Write parsed tasks into the agenda folder as issue files. In-progress tasks get
    `owner` (the register requires one). Returns counts per status.
    """
    ensure(folder)
    cfg = config(folder)
    taken: set[str] = set()
    counts = {"open": 0, "in-progress": 0, "done": 0}
    filed = _dt.date.today().isoformat()
    for task in tasks:
        extra: dict[str, str | tuple[str, ...]] = {}
        if task.tier:
            extra["tier"] = task.tier
        if task.today:
            extra["today"] = FLAG
        if task.highlight:
            extra["highlight"] = FLAG
        if task.jira:
            extra["jira"] = task.jira
        slug = _unique_slug(folder, issues.slugify(task.title), taken)
        taken.add(slug)
        issue = issues.Issue(
            path=folder / cfg.filename(filed, slug),
            title=task.title,
            status=task.status,
            area=DEFAULT_AREA,
            filed=filed,
            owner=owner if task.status is issues.Status.IN_PROGRESS else None,
            extra=extra,
            body="\n" + task.body if task.body else "\n",
        )
        issues.save(issue, cfg)
        counts[task.status.value] += 1
    return counts
