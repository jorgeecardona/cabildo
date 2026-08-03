"""The per-project follow-up register — the tool layer behind a repo's `docs/issues/`.

One file per piece of work that is known, scoped, and not being done right now. Without a home,
that residue lives in a chat log nobody re-reads and "we should fix X" decays into nobody having
ever owned X.

**The files are the store.** No database, no cache, no second copy — an issue is markdown with YAML
front matter, tracked in git, readable and editable by hand. Everything derived (the README index
above all) is *generated from* the files and is never authoritative. This is the one place cabildo
deliberately does NOT use its SQLite: a duplicate store is a fault dimension, and the way to avoid
one is not to create it. If this module is broken or missing, `git` and an editor still work.

Prior art, and what was taken from it. Beads (`bd`) is the closest thing to this and the deepest
thinking about the agent case; its dependency DAG is stolen wholesale as `blocked_by`, because
"what can I pick up cold right now" is not answerable without it. Its storage choice is rejected on
purpose: Beads keeps a Dolt database and exports JSONL that is explicitly not the source of truth.
Backlog.md and ditz got file-per-issue right and are the shape here. git-bug stores issues as git
objects, which makes them unreadable without the tool — the opposite of the goal.

## Core plus per-project config

Real repos disagree about front matter, and pretending otherwise just means the tool is wrong
somewhere. So cabildo enforces a small core it can check everywhere:

    title  status  area  owner  filed  blocked_by  fixed_in

and each folder declares the rest in `docs/issues/config.toml` — its own `areas` vocabulary (which
is *necessarily* per-project: one repo's "networking" is another's "roast-live") plus whatever extra
fields it requires. `cabildo issues init` writes that config by reading the folder that already
exists, so adopting this never invalidates issues written before it.

Unknown fields are preserved verbatim on every write. A tool that silently drops the field it did
not recognise is worse than one that refuses to run.

## Status, and what closing means

    open → in-progress → done,  with blocked off to the side

`done` keeps the file and requires `fixed_in` — the sha that landed on the mainline, so the folder
carries a greppable record of what was fixed by what. (Note for the paas repo: its README says to
close by deleting the file. That is a deliberate reversal, not an oversight; that line needs
updating if paas adopts this.)

CLI:
    cabildo issues ready               # what is unblocked and unowned — start here
    cabildo issues list --status open
    cabildo issues show <slug>
    cabildo issues check               # the invariants; non-zero on problems
    cabildo issues reindex             # regenerate the README table
    cabildo issues init                # write config.toml from the folder as it is
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import enum
import pathlib
import re
import tomllib
from collections.abc import Iterable, Mapping

FOLDER_PARTS = ("docs", "issues")
CONFIG_NAME = "config.toml"
README_NAME = "README.md"

INDEX_BEGIN = "<!-- BEGIN GENERATED INDEX — `cabildo issues reindex` -->"
INDEX_END = "<!-- END GENERATED INDEX -->"


class IssueFormatError(ValueError):
    """A file in docs/issues/ does not obey the contract its config states."""


class Status(enum.StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in-progress"
    BLOCKED = "blocked"          # waiting on a decision, a credential, or another issue
    DONE = "done"                # keeps the file; requires fixed_in


#: Core front matter, in render order. Extras follow, in the order the file had them.
CORE_ORDER = ("title", "status", "area", "owner", "filed", "blocked_by", "fixed_in")


# --------------------------------------------------------------------------- #
# What a folder requires of its issues
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class Field:
    """One project-declared front-matter field beyond the core."""

    name: str
    required: bool = False
    values: tuple[str, ...] = ()      # empty means free text
    is_list: bool = False             # a YAML block list rather than a scalar
    #: a file, relative to the issue folder, this field's value must appear in — and which must
    #: link back to the issue. One direction is not enough: a register that names a gap but does
    #: not point at the task is how the same gap gets found four times and fixed zero.
    crossref: str = ""

    def problems(self, value) -> list[str]:
        if value is None or value == ():
            return [f"missing required `{self.name}`"] if self.required else []
        got = value if isinstance(value, tuple) else (value,)
        if isinstance(value, tuple) and not self.is_list:
            return [f"`{self.name}` is a list, but this folder declares it a scalar"]
        if not self.values:
            return []
        return [
            f"unknown {self.name} {v!r} — one of {', '.join(self.values)}"
            for v in got if v not in self.values
        ]


@dataclasses.dataclass(frozen=True)
class Config:
    """`docs/issues/config.toml`: what this folder's issues must say.

    `areas` has no default on purpose. It is the one field whose vocabulary cannot be guessed from
    outside the repo, and a tool that accepts any area silently accepts every typo as a new one.
    """

    areas: tuple[str, ...] = ()
    fields: Mapping[str, Field] = dataclasses.field(default_factory=dict)
    index: bool = True
    date_style: str = "DMY"          # DMY → 03-08-2026-slug.md; YMD → 2026-08-03-slug.md
    #: groups where at least one member must be present. For "name the register entry, or say why
    #: there is none" — an either/or that `required` cannot express, and where the point is that
    #: silence is not an allowed answer.
    require_one_of: tuple[tuple[str, ...], ...] = ()
    #: project fields to add as columns in the generated index. The index is for scanning, and what
    #: is worth scanning is exactly what the folder chose to track — scope and verify in one repo,
    #: priority in another. Core columns are always there.
    index_fields: tuple[str, ...] = ()

    @property
    def filename_re(self) -> re.Pattern[str]:
        head = r"(\d{2})-(\d{2})-(\d{4})" if self.date_style == "DMY" else r"(\d{4})-(\d{2})-(\d{2})"
        return re.compile(rf"^{head}-([a-z0-9]+(?:-[a-z0-9]+)*)\.md$")

    def filename(self, filed: str, slug: str) -> str:
        y, m, d = filed.split("-")
        return f"{d}-{m}-{y}-{slug}.md" if self.date_style == "DMY" else f"{y}-{m}-{d}-{slug}.md"

    def filed_from_name(self, name: str) -> str | None:
        m = self.filename_re.match(name)
        if not m:
            return None
        a, b, c = m.group(1), m.group(2), m.group(3)
        return f"{c}-{b}-{a}" if self.date_style == "DMY" else f"{a}-{b}-{c}"


DEFAULT_CONFIG = Config()


def load_config(folder: pathlib.Path) -> Config:
    path = folder / CONFIG_NAME
    if not path.exists():
        return DEFAULT_CONFIG
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    fields = {}
    for name, spec in (raw.get("fields") or {}).items():
        spec = spec or {}
        fields[name] = Field(
            name=name,
            required=bool(spec.get("required", False)),
            values=tuple(spec.get("values", ()) or ()),
            is_list=bool(spec.get("list", False)),
            crossref=str(spec.get("crossref", "") or ""),
        )
    style = str(raw.get("date_style", "DMY")).upper()
    if style not in ("DMY", "YMD"):
        raise IssueFormatError(f"{path}: date_style must be DMY or YMD, not {style!r}")
    groups = tuple(
        tuple(g) for g in (raw.get("require_one_of", ()) or ()) if g
    )
    return Config(
        areas=tuple(raw.get("areas", ()) or ()),
        fields=fields,
        index=bool(raw.get("index", True)),
        date_style=style,
        require_one_of=groups,
        index_fields=tuple(raw.get("index_fields", ()) or ()),
    )


# --------------------------------------------------------------------------- #
# An issue
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class Issue:
    path: pathlib.Path
    title: str
    status: Status
    area: str
    filed: str
    owner: str | None = None
    blocked_by: tuple[str, ...] = ()
    fixed_in: str | None = None
    #: everything this folder declares beyond the core, in file order. Preserved verbatim on write.
    extra: Mapping[str, str | tuple[str, ...]] = dataclasses.field(default_factory=dict)
    body: str = ""

    @property
    def slug(self) -> str:
        # Independent of date_style: strip a leading date of either shape, keep the rest.
        return re.sub(r"^(\d{2}-\d{2}-\d{4}|\d{4}-\d{2}-\d{2})-", "", self.path.stem)

    @property
    def open(self) -> bool:
        return self.status is not Status.DONE


@dataclasses.dataclass(frozen=True)
class Problem:
    path: pathlib.Path
    message: str

    def __str__(self) -> str:
        return f"{self.path.name}: {self.message}"


# --------------------------------------------------------------------------- #
# Front matter — a small, deliberate YAML subset
# --------------------------------------------------------------------------- #
# Flat `key: value` scalars plus single-level block lists. That covers every issue in the repos this
# was built from and nothing more, which is why this module has no third-party dependency and stays
# fast enough to run in a pre-commit hook. If a field wants deeper structure, that is a signal it
# wants to be prose in the body instead.

_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")
_ITEM_RE = re.compile(r"^\s+-\s*(.*)$")


def _unquote(raw: str) -> str | None:
    v = raw.strip()
    if v in ("null", "~", ""):
        return None
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _quote(value: str) -> str:
    return f'"{value}"' if re.search(r'[:#"]', value) else value


def _split_front(text: str, path: pathlib.Path) -> tuple[str, str]:
    if not text.startswith("---\n"):
        raise IssueFormatError(f"{path.name}: no YAML front matter — the file must start with `---`")
    parts = text.split("---\n", 2)
    if len(parts) != 3:
        raise IssueFormatError(f"{path.name}: unterminated front matter")
    return parts[1], parts[2]


def _parse_front(front: str, path: pathlib.Path) -> dict[str, str | tuple[str, ...] | None]:
    fields: dict[str, str | tuple[str, ...] | None] = {}
    key: str | None = None
    for line in front.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = _ITEM_RE.match(line)
        if item is not None:
            if key is None:
                raise IssueFormatError(f"{path.name}: list item with no key — {line!r}")
            current = fields.get(key)
            got = current if isinstance(current, tuple) else ()
            fields[key] = got + (_unquote(item.group(1)) or "",)
            continue
        m = _KEY_RE.match(line)
        if not m:
            raise IssueFormatError(f"{path.name}: front matter line is not `key: value` — {line!r}")
        key = str(m.group(1))
        inline = m.group(2).strip()
        if inline.startswith("[") and inline.endswith("]"):
            body = inline[1:-1].strip()
            fields[key] = tuple(_unquote(p) or "" for p in body.split(",")) if body else ()
        else:
            fields[key] = _unquote(inline)
    return fields


def parse(text: str, path: pathlib.Path, config: Config = DEFAULT_CONFIG) -> Issue:
    front, body = _split_front(text, path)
    fields = _parse_front(front, path)

    def required(key: str) -> str:
        value = fields.get(key)
        if value is None or isinstance(value, tuple):
            raise IssueFormatError(f"{path.name}: front matter is missing required `{key}`")
        return value

    raw_status = required("status")
    if raw_status not in tuple(Status):
        raise IssueFormatError(
            f"{path.name}: unknown status {raw_status!r} — one of {', '.join(Status)}"
        )

    # `filed` is a fact the filename already carries, so a file that omits it is not malformed.
    filed = fields.get("filed")
    if not isinstance(filed, str):
        filed = config.filed_from_name(path.name) or _dt.date.today().isoformat()

    blocked = fields.get("blocked_by")
    if blocked is None:
        blocked_by: tuple[str, ...] = ()
    elif isinstance(blocked, tuple):
        blocked_by = tuple(b for b in blocked if b)
    else:
        blocked_by = (blocked,)

    fixed_in = fields.get("fixed_in")
    owner = fields.get("owner")
    extra = {
        k: v for k, v in fields.items()
        if k not in CORE_ORDER and v is not None
    }
    return Issue(
        path=path,
        title=required("title"),
        status=Status(raw_status),
        area=required("area"),
        filed=filed,
        owner=owner if isinstance(owner, str) else None,
        blocked_by=blocked_by,
        fixed_in=fixed_in if isinstance(fixed_in, str) else None,
        extra=extra,
        body=body,
    )


def _render_value(key: str, value) -> list[str]:
    if isinstance(value, tuple):
        return [f"{key}:"] + [f"  - {_quote(v)}" for v in value]
    return [f"{key}: {_quote(value)}"]


def render(issue: Issue) -> str:
    lines = [
        "---",
        f"title: {_quote(issue.title)}",
        f"status: {issue.status}",
        f"area: {issue.area}",
        f"owner: {issue.owner or 'null'}",
        f"filed: {issue.filed}",
    ]
    if issue.blocked_by:
        lines += _render_value("blocked_by", issue.blocked_by)
    if issue.fixed_in:
        lines.append(f"fixed_in: {issue.fixed_in}")
    for key, value in issue.extra.items():
        lines += _render_value(key, value)
    lines.append("---")
    body = issue.body if issue.body.startswith("\n") else "\n" + issue.body
    return "\n".join(lines) + body


def load(path: pathlib.Path, config: Config = DEFAULT_CONFIG) -> Issue:
    return parse(path.read_text(), path, config)


def save(issue: Issue) -> None:
    issue.path.write_text(render(issue))


def load_all(
    folder: pathlib.Path, config: Config | None = None
) -> tuple[tuple[Issue, ...], tuple[Problem, ...]]:
    """Every issue in the folder, plus the files that could not be parsed. Returning both means one
    malformed file never hides the rest of the register."""
    config = load_config(folder) if config is None else config
    issues: list[Issue] = []
    problems: list[Problem] = []
    for path in sorted(folder.glob("*.md")):
        if path.name == README_NAME:
            continue
        try:
            issues.append(load(path, config))
        except IssueFormatError as exc:
            problems.append(Problem(path, str(exc).split(": ", 1)[-1]))
    return tuple(issues), tuple(problems)


# --------------------------------------------------------------------------- #
# What can I pick up right now
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class Waiting:
    issue: Issue
    on: tuple[str, ...]      # slugs of open issues blocking it
    reason: str


@dataclasses.dataclass(frozen=True)
class Board:
    """The answer to "what should I be working on" — and, just as importantly, why not the rest.

    A list of everything is not an answer; it is the thing that made the question hard. `ready` is
    work whose blockers are all closed and which nobody has taken. `waiting` says what each of the
    others is actually stuck behind, so the reader can go unstick one instead of guessing.
    """

    ready: tuple[Issue, ...]
    waiting: tuple[Waiting, ...]
    mine: tuple[Issue, ...]
    done: int


def board(folder: pathlib.Path, config: Config | None = None, *, owner: str | None = None) -> Board:
    config = load_config(folder) if config is None else config
    issues, _ = load_all(folder, config)
    by_slug = {i.slug: i for i in issues}
    open_slugs = {i.slug for i in issues if i.open}

    ready, waiting, mine = [], [], []
    for issue in issues:
        if not issue.open:
            continue
        if owner is not None and issue.owner == owner:
            mine.append(issue)
            continue
        blockers = tuple(s for s in issue.blocked_by if s in open_slugs)
        if blockers:
            titles = ", ".join(by_slug[s].title for s in blockers)
            waiting.append(Waiting(issue, blockers, f"blocked by {titles}"))
        elif issue.status is Status.BLOCKED:
            who = f"@{issue.owner}" if issue.owner else "nobody named"
            waiting.append(Waiting(issue, (), f"blocked on a decision — {who} can unblock it"))
        elif issue.owner:
            waiting.append(Waiting(issue, (), f"taken by @{issue.owner}"))
        else:
            ready.append(issue)

    order = {a: n for n, a in enumerate(config.areas)}
    ready.sort(key=lambda i: (order.get(i.area, len(order)), i.filed))
    return Board(
        ready=tuple(ready),
        waiting=tuple(waiting),
        mine=tuple(mine),
        done=sum(1 for i in issues if not i.open),
    )


# --------------------------------------------------------------------------- #
# The invariants
# --------------------------------------------------------------------------- #
_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")


def check(folder: pathlib.Path, config: Config | None = None) -> list[Problem]:
    """Every rule this folder's config states, enforced. Returns [] when the folder is honest.

    A declared invariant that nothing enforces reads as verified to anyone skimming it — which is
    precisely how an index drifts from its own folder for weeks without anybody noticing.
    """
    config = load_config(folder) if config is None else config
    issues, parse_problems = load_all(folder, config)
    problems = list(parse_problems)
    slugs = {i.slug for i in issues}
    open_slugs = {i.slug for i in issues if i.open}
    seen: dict[str, pathlib.Path] = {}

    for issue in issues:
        p = issue.path
        if not config.filename_re.match(p.name):
            shape = "DD-MM-YYYY" if config.date_style == "DMY" else "YYYY-MM-DD"
            problems.append(Problem(p, f"filename must be `{shape}-short-kebab-summary.md`"))
        first = seen.get(issue.slug)
        if first is not None:
            problems.append(Problem(p, f"duplicate slug — {first.name} covers the same thing"))
        else:
            seen[issue.slug] = p

        if config.areas and issue.area not in config.areas:
            problems.append(
                Problem(p, f"unknown area {issue.area!r} — one of {', '.join(config.areas)}")
            )
        if issue.status is Status.DONE and not issue.fixed_in:
            problems.append(
                Problem(p, "status is `done` with no `fixed_in` — name the sha that landed on the "
                           "mainline, or the folder stops being evidence of anything")
            )
        if issue.status is Status.IN_PROGRESS and not issue.owner:
            problems.append(Problem(p, "in-progress with no owner — say who is doing it"))
        if issue.status is Status.BLOCKED and not issue.owner and not issue.blocked_by:
            problems.append(
                Problem(p, "blocked with neither an owner nor a `blocked_by` — name who can "
                           "unblock it or which issue it waits on. Silence reads as an oversight")
            )
        for slug in issue.blocked_by:
            if slug not in slugs:
                problems.append(Problem(p, f"blocked_by names {slug!r}, which is not an issue here"))
        if issue.status is Status.DONE and any(s in open_slugs for s in issue.blocked_by):
            still = ", ".join(s for s in issue.blocked_by if s in open_slugs)
            problems.append(Problem(p, f"done, but still blocked_by open work: {still}"))

        for field in config.fields.values():
            for message in field.problems(issue.extra.get(field.name)):
                problems.append(Problem(p, message))
        for group in config.require_one_of:
            if not any(issue.extra.get(k) for k in group):
                problems.append(Problem(
                    p, f"none of {', '.join('`' + k + '`' for k in group)} is set — say which one "
                       f"applies, or why none does. Silence reads as an oversight and gets "
                       f"re-litigated",
                ))
        problems.extend(_broken_links(issue, folder))

    problems.extend(_cycles(issues))
    problems.extend(_crossrefs(folder, issues, config))
    problems.extend(_index_problems(folder, issues, config))
    return problems


def _crossrefs(
    folder: pathlib.Path, issues: tuple[Issue, ...], config: Config
) -> list[Problem]:
    """Enforce a field's two-way link with another document.

    Some registers are the record of *what is wrong*, and the issue folder holds one entry broken
    out into a task. Checking only that the issue names the register lets the register go silently
    stale; checking only the back-link lets an issue cite an entry that was never written.
    """
    out: list[Problem] = []
    for field in config.fields.values():
        if not field.crossref:
            continue
        target = (folder / field.crossref).resolve()
        if not target.exists():
            out.append(Problem(
                folder / CONFIG_NAME,
                f"fields.{field.name}.crossref points at {field.crossref}, which does not exist",
            ))
            continue
        text = target.read_text()
        for issue in issues:
            value = issue.extra.get(field.name)
            if not isinstance(value, str) or not value:
                continue
            if not re.search(rf"\b{re.escape(value)}\b", text):
                out.append(Problem(
                    issue.path, f"{field.name} {value} does not exist in {target.name}"
                ))
            elif issue.path.name not in text:
                out.append(Problem(
                    issue.path,
                    f"{target.name} does not link back to this file — without the back-link the "
                    f"task is invisible from the register that is supposed to track it",
                ))
    return out


def _cycles(issues: Iterable[Issue]) -> list[Problem]:
    """A blocked_by loop means nothing in it is ever ready, and the board goes quietly empty rather
    than loudly wrong. Cheap to detect, invisible otherwise."""
    graph = {i.slug: tuple(i.blocked_by) for i in issues}
    paths = {i.slug: i.path for i in issues}
    state: dict[str, int] = {}
    out: list[Problem] = []

    def walk(node: str, trail: tuple[str, ...]) -> None:
        if state.get(node) == 2:
            return
        if state.get(node) == 1:
            loop = " → ".join(trail[trail.index(node):] + (node,))
            out.append(Problem(paths[node], f"blocked_by cycle: {loop} — nothing in it can be ready"))
            return
        state[node] = 1
        for nxt in graph.get(node, ()):
            if nxt in graph:
                walk(nxt, trail + (node,))
        state[node] = 2

    for slug in graph:
        walk(slug, ())
    # One loop is reported once per entry point; collapse to distinct messages.
    return list({p.message: p for p in out}.values())


def _broken_links(issue: Issue, folder: pathlib.Path) -> list[Problem]:
    out: list[Problem] = []
    for match in _LINK_RE.finditer(issue.body):
        target = match.group(2).split("#")[0].strip()
        if not target or target.startswith(("http://", "https://", "mailto:", "[")):
            continue
        if not (folder / target).resolve().exists():
            out.append(Problem(issue.path, f"broken relative link: {target}"))
    return out


def _index_problems(
    folder: pathlib.Path, issues: tuple[Issue, ...], config: Config
) -> list[Problem]:
    readme = folder / README_NAME
    if not config.index or not readme.exists():
        return []
    text = readme.read_text()
    if INDEX_BEGIN not in text or INDEX_END not in text:
        return [Problem(readme, f"README has no generated-index region ({INDEX_BEGIN})")]
    if _replace_region(text, _index_table(issues, config)) != text:
        return [
            Problem(readme, "the generated index is stale — run `cabildo issues reindex`. A "
                            "follow-up nobody can see is what this folder exists to prevent")
        ]
    return []


# --------------------------------------------------------------------------- #
# The generated index
# --------------------------------------------------------------------------- #
def _cell(value) -> str:
    if value is None:
        return "—"
    return ", ".join(value) if isinstance(value, tuple) else str(value)


def _index_table(issues: tuple[Issue, ...], config: Config = DEFAULT_CONFIG) -> str:
    live = [i for i in issues if i.open]
    if not live:
        return "_No open follow-ups._"
    heads = ["Issue", "Area", *(f.capitalize() for f in config.index_fields),
             "Status", "Waiting on"]
    rows = ["| " + " | ".join(heads) + " |", "|" + "---|" * len(heads)]
    open_slugs = {i.slug for i in live}
    for issue in sorted(live, key=lambda i: (i.area, i.path.name)):
        owner = f" · @{issue.owner}" if issue.owner else ""
        blockers = ", ".join(s for s in issue.blocked_by if s in open_slugs) or "—"
        extras = [_cell(issue.extra.get(f)) for f in config.index_fields]
        rows.append(
            "| " + " | ".join([
                f"[{issue.title}]({issue.path.name})", issue.area, *extras,
                f"{issue.status}{owner}", blockers,
            ]) + " |"
        )
    counts = ", ".join(
        f"{sum(1 for i in live if i.status is s)} {s}"
        for s in Status if any(i.status is s for i in live)
    )
    closed = len(issues) - len(live)
    tail = f"{len(live)} open — {counts}" + (f"; {closed} done" if closed else "")
    rows += ["", f"_{tail}. Generated; edit the files, then `cabildo issues reindex`._"]
    return "\n".join(rows)


def render_index(folder: pathlib.Path, config: Config | None = None) -> str:
    config = load_config(folder) if config is None else config
    issues, _ = load_all(folder, config)
    return _index_table(issues, config)


def _replace_region(text: str, table: str) -> str:
    start = text.index(INDEX_BEGIN) + len(INDEX_BEGIN)
    end = text.index(INDEX_END)
    return text[:start] + "\n" + table + "\n" + text[end:]


def reindex(folder: pathlib.Path, config: Config | None = None) -> None:
    config = load_config(folder) if config is None else config
    readme = folder / README_NAME
    if not config.index or not readme.exists():
        return
    text = readme.read_text()
    if INDEX_BEGIN not in text or INDEX_END not in text:
        raise IssueFormatError(f"{readme.name}: no generated-index region to rewrite")
    readme.write_text(_replace_region(text, render_index(folder, config)))


def _touch_index(folder: pathlib.Path, config: Config) -> None:
    """Keep the index true after any write. Never let a failure here lose the write itself — the
    file is the store, and the index is a convenience generated from it."""
    try:
        reindex(folder, config)
    except (IssueFormatError, OSError):
        pass


# --------------------------------------------------------------------------- #
# Filing and moving work
# --------------------------------------------------------------------------- #
def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return "-".join(slug.split("-")[:12])


def resolve(folder: pathlib.Path, name: str) -> pathlib.Path:
    """A filename, or any unambiguous fragment of one. Ambiguity is an error rather than a guess:
    picking the first match is how an agent edits an issue nobody asked it to touch."""
    if name.endswith(".md") and (folder / name).exists():
        return folder / name
    matches = [p for p in folder.glob("*.md") if p.name != README_NAME and name in p.name]
    if not matches:
        raise IssueFormatError(f"no such issue: {name}")
    if len(matches) > 1:
        raise IssueFormatError(f"ambiguous: {sorted(m.name for m in matches)}")
    return matches[0]


def file_issue(
    folder: pathlib.Path,
    *,
    title: str,
    area: str,
    body: str,
    owner: str | None = None,
    status: Status = Status.OPEN,
    blocked_by: Iterable[str] = (),
    extra: Mapping[str, str | tuple[str, ...]] | None = None,
    today: str | None = None,
    config: Config | None = None,
) -> pathlib.Path:
    config = load_config(folder) if config is None else config
    if config.areas and area not in config.areas:
        raise IssueFormatError(f"unknown area {area!r} — one of {', '.join(config.areas)}")
    filed = today or _dt.date.today().isoformat()
    path = folder / config.filename(filed, slugify(title))
    if path.exists():
        raise IssueFormatError(f"{path.name} already exists — edit it, or pick a distinct title")
    issue = Issue(
        path=path,
        title=title,
        status=status,
        area=area,
        filed=filed,
        owner=owner,
        blocked_by=tuple(blocked_by),
        extra=dict(extra or {}),
        body=body,
    )
    missing = [
        m for f in config.fields.values() for m in f.problems(issue.extra.get(f.name))
    ]
    if missing:
        raise IssueFormatError("; ".join(missing))
    save(issue)
    _touch_index(folder, config)
    return path


def update(
    folder: pathlib.Path, name: str, config: Config | None = None, **changes
) -> Issue:
    """Edit one issue's front matter. `extra` fields are merged, not replaced, so setting one
    project field cannot silently drop the others."""
    config = load_config(folder) if config is None else config
    issue = load(resolve(folder, name), config)
    extra = dict(issue.extra) | dict(changes.pop("extra", None) or {})
    updated = dataclasses.replace(issue, extra=extra, **changes)
    save(updated)
    _touch_index(folder, config)
    return updated


def claim(
    folder: pathlib.Path, name: str, *, owner: str, force: bool = False,
    config: Config | None = None,
) -> Issue:
    """Put a name on a follow-up and move it to in-progress. Refuses to steal by default: an owner
    that silently changes hands is how two agents end up doing the same work."""
    config = load_config(folder) if config is None else config
    issue = load(resolve(folder, name), config)
    if issue.owner and issue.owner != owner and not force:
        raise IssueFormatError(
            f"{issue.path.name} is already owned by @{issue.owner} — pass force to take it"
        )
    status = Status.IN_PROGRESS if issue.status is Status.OPEN else issue.status
    updated = dataclasses.replace(issue, owner=owner, status=status)
    save(updated)
    _touch_index(folder, config)
    return updated


def release(folder: pathlib.Path, name: str, config: Config | None = None) -> Issue:
    """Drop the owner and put it back on the board — the honest move when you stop, so it does not
    look taken forever."""
    config = load_config(folder) if config is None else config
    issue = load(resolve(folder, name), config)
    status = Status.OPEN if issue.status is Status.IN_PROGRESS else issue.status
    updated = dataclasses.replace(issue, owner=None, status=status)
    save(updated)
    _touch_index(folder, config)
    return updated


def close(
    folder: pathlib.Path, name: str, *, fixed_in: str, config: Config | None = None
) -> Issue:
    """Mark it done. `fixed_in` is required because closing without naming what fixed it is how a
    register stops being evidence — and it must be the sha that landed on the mainline, not the one
    from a worktree that will be pruned."""
    config = load_config(folder) if config is None else config
    if not fixed_in.strip():
        raise IssueFormatError("fixed_in is required — name the commit that landed the fix")
    issue = load(resolve(folder, name), config)
    updated = dataclasses.replace(issue, status=Status.DONE, fixed_in=fixed_in.strip())
    save(updated)
    _touch_index(folder, config)
    return updated


# --------------------------------------------------------------------------- #
# Adoption: describe the folder that already exists
# --------------------------------------------------------------------------- #
STARTER_README = """\
# docs/issues — follow-ups with an owner-shaped edge

One file per follow-up, self-contained, with YAML front matter. This folder is for work that is
**known, scoped, and not being done right now** — the residue a session leaves behind when it lands
its main change. Without a home, that residue lives in a chat log nobody re-reads, and "we should
fix X" decays into nobody having ever owned X.

**The files are the store.** No database, no second copy — an issue is markdown in git, readable and
editable by hand. Everything derived (the index below above all) is *generated from* the files and
is never authoritative. A duplicate store is a fault dimension; the way to avoid one is not to
create it.

## The rules that make one worth writing

1. **Answerable cold.** Assume the reader has none of the author's context and no access to the
   session that found it. Concrete instances, file:line, the actual commands.
2. **Say what is MEASURED and what is INFERRED.** Separately, and in those words. An issue that
   states an inference as a finding sends the next person to fix the wrong thing, and they will
   believe you because you wrote it down.
3. **Define done.** Not "improve X" — the specific observation that will be true afterwards, and
   how to make it. `verify:` says which kind of evidence closes it, because they are different
   claims: `test` (a red-then-green test is enough), `probe` (only sensing the real thing counts),
   `inspect` (the artifact itself is the evidence).
4. **Name what you deliberately did NOT do,** with the reason. Silence reads as an oversight and
   gets re-litigated.
5. **Close by marking it done,** with `fixed_in:` naming the sha that landed the fix. A register
   that cannot say what fixed something has stopped being evidence.

## The format

```yaml
---
title: One line — the finding, not the fix
status: open          # open | in-progress (name an owner) | blocked | done (needs fixed_in)
scope: M              # XS | S | M | L | ?
owner: null           # a handle, or null when unowned
filed: {today}     # when it was filed; it never changes
area: {area}
verify: probe         # test | probe | inspect — see rule 3
blocked_by: null      # slug(s) of issues that must close first
fixed_in: null        # the sha on the mainline; required once status is done
---
```

**`blocked_by` is what makes the board honest.** `status: blocked` names a *person* who can unblock
something; `blocked_by` names the *work* it waits on. Only the second can be computed, which is why
"what can I pick up cold right now" is answerable at all.

**Naming:** `DD-MM-YYYY-short-kebab-summary.md` — the date it was filed, then a slug.

## Tooling — the folder checks itself

Managed by `cabildo issues`. The CLI, the `issues` MCP server, and any pre-commit gate are one
module, so the tool an agent calls and the gate that grades it cannot disagree.
`docs/issues/config.toml` holds this folder's specifics.

```bash
cabildo issues                # what's ready, what's yours, what everything else waits on
cabildo issues check          # the invariants; non-zero on problems
cabildo issues reindex        # regenerate the index below
cabildo issues show <slug>
```

## Open now

{begin}
_No open follow-ups._
{end}
"""


def starter_config(areas: Iterable[str] = ()) -> str:
    """A config for a folder with nothing in it yet.

    `areas` is asked for rather than defaulted. It is the one vocabulary that cannot be guessed from
    outside a repo, and a folder that accepts any area silently accepts every typo as a new one.
    """
    names = tuple(areas)
    listed = (
        "areas = [" + ", ".join(f'"{a}"' for a in names) + "]" if names
        else '# areas = ["backend", "frontend", "ci"]   # REQUIRED — until set, any area is accepted'
    )
    return f"""\
# docs/issues/config.toml — what this folder's issues must say.
#
# Read by `cabildo issues`: the CLI, the `issues` MCP server, and any pre-commit gate are one
# module, so the tool an agent calls and the gate that grades it cannot disagree.

date_style = "DMY"          # 03-08-2026-short-kebab-summary.md
index = true                # the generated table in README.md
index_fields = ["scope", "verify"]

# The one vocabulary that cannot be guessed from outside this repo.
{listed}

[fields.scope]
required = true
values = ["XS", "S", "M", "L", "?"]

# How "done" gets checked. The distinction is load-bearing: a green test is not the same claim as a
# sensed fact, and an issue that blurs them sends the next person to the wrong evidence.
[fields.verify]
required = true
values = ["test", "probe", "inspect"]
"""


def scaffold(
    folder: pathlib.Path, areas: Iterable[str] = (), today: str | None = None
) -> list[pathlib.Path]:
    """Start a register in an empty folder: a config and a README with an index region.

    Writes only what is missing. A folder that already has a README keeps it — the marker can be
    pasted in by hand, and silently rewriting somebody's documentation to install a tool is not a
    trade anyone agreed to.
    """
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    config = folder / CONFIG_NAME
    if not config.exists():
        config.write_text(starter_config(areas))
        written.append(config)
    readme = folder / README_NAME
    if not readme.exists():
        names = tuple(areas)
        readme.write_text(STARTER_README.format(
            today=today or _dt.date.today().isoformat(),
            area=names[0] if names else "<one of the areas above>",
            begin=INDEX_BEGIN,
            end=INDEX_END,
        ))
        written.append(readme)
    return written


def infer_config(folder: pathlib.Path) -> str:
    """The config.toml that makes this folder valid as it stands, as TOML text.

    Adoption has to be non-destructive or nobody adopts. Writing a config from first principles
    would declare 78 existing issues malformed on day one; reading it from what is actually there
    means `check` passes immediately, and tightening a field to `required` afterwards is a decision
    taken on purpose rather than a migration forced by a tool.
    """
    issues, _ = load_all(folder, DEFAULT_CONFIG)
    areas = sorted({i.area for i in issues})
    seen: dict[str, dict] = {}
    for issue in issues:
        for key, value in issue.extra.items():
            spec = seen.setdefault(key, {"count": 0, "list": False, "values": set()})
            spec["count"] += 1
            if isinstance(value, tuple):
                spec["list"] = True
                spec["values"].update(value)
            else:
                spec["values"].add(value)

    style = "DMY"
    for issue in issues:
        if re.match(r"^\d{4}-\d{2}-\d{2}-", issue.path.name):
            style = "YMD"
            break

    # Only claim the index if the README actually has a region to write into. A folder that keeps
    # its own summary another way is not broken, and `check` demanding a marker nobody put there is
    # the tool inventing work on first contact.
    readme = folder / README_NAME
    has_region = readme.exists() and INDEX_BEGIN in readme.read_text()

    lines = [
        "# docs/issues/config.toml — what this folder's issues must say.",
        "#",
        "# Written by `cabildo issues init` from the files that were already here, so `check`",
        "# passes today. Tighten it deliberately: flip a field to required, or pin its values.",
        "",
        f'date_style = "{style}"',
        f"index = {'true' if has_region else 'false'}"
        + ("" if has_region else "   # no generated-index region in README.md; add one, then flip this"),
        "",
        "# The one vocabulary that cannot be guessed from outside this repo.",
        "areas = [" + ", ".join(f'"{a}"' for a in areas) + "]",
    ]
    for key, spec in sorted(seen.items(), key=lambda kv: -kv[1]["count"]):
        values = sorted(v for v in spec["values"] if v)
        # A field with few distinct values is an enum worth pinning; free text is not.
        enum_like = 0 < len(values) <= 8 and not spec["list"]
        lines += ["", f"[fields.{key}]"]
        lines.append(f"required = {'true' if spec['count'] == len(issues) else 'false'}"
                     f"   # on {spec['count']}/{len(issues)} issues today")
        if spec["list"]:
            lines.append("list = true")
        if enum_like:
            lines.append("values = [" + ", ".join(f'"{v}"' for v in values) + "]")
    return "\n".join(lines) + "\n"


def find_folder(start: pathlib.Path) -> pathlib.Path | None:
    """This project's `docs/issues/`, searching upward from `start` — so the server works from any
    subdirectory of the repo, which is where sessions actually run."""
    here = start.resolve()
    for base in (here, *here.parents):
        candidate = base.joinpath(*FOLDER_PARTS)
        if candidate.is_dir():
            return candidate
        if (base / ".git").exists():
            break
    return None
