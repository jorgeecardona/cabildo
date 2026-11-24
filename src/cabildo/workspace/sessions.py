"""Data layer for the Claude Code session switcher.

Pure stdlib. Models a session transcript and the sidecar metadata (label,
group, tags) we keep apart from Claude's own files, in the XDG data directory
(~/.local/share/workspace/), with the trash.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import shutil
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from cabildo import names
from cabildo.workspace import notes, rooms, xdgdirs
from cabildo.workspace.notes import Note
from cabildo.workspace.rooms import NO_ROOMS, Rooms

# --- paths -----------------------------------------------------------------


def config_dir() -> Path:
    """Root of Claude's config (honours CLAUDE_CONFIG_DIR, like the CLI does)."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def encode_cwd(cwd: str) -> str:
    """A working dir maps to a project folder: every non-alphanumeric -> '-'.

    /home/jcardona/stuff/projects -> -home-jcardona-stuff-projects
    """
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def project_dir(cwd: str) -> Path:
    return config_dir() / "projects" / encode_cwd(cwd)


def project_dirs(cwd: str) -> list[Path]:
    """The folders of one working directory: Claude names the folder after the real path
    (symlinks resolved); older transcripts may sit under the path as given.
    """
    return list(dict.fromkeys([project_dir(cwd), project_dir(os.path.realpath(cwd))]))


def folder_dirs(folder: str, limit: int = 500) -> list[str]:
    """Existing directories whose path encodes to this folder name, found by walking the
    filesystem from / (the encoding is lossy: '-' was '/', '.', '_' or '-'). No symlinks
    on the way, so each one's real path is itself.
    """
    found: list[str] = []
    todo = [("/", folder[1:])] if folder.startswith("-") else []
    while todo and limit > 0:
        base, rest = todo.pop()
        limit -= 1
        try:
            entries = list(os.scandir(base))
        except OSError:
            continue
        for e in entries:
            if not e.is_dir(follow_symlinks=False):
                continue
            name = encode_cwd(e.name)
            if rest == name:
                found.append(e.path)
            elif rest.startswith(name + "-"):
                todo.append((e.path, rest[len(name) + 1 :]))
    return sorted(found)


def launch_dir(s: Session) -> str | None:
    """A directory to start Claude in for this transcript; None when none exists any more.

    `claude --resume` from any other directory still loads the conversation, but Claude
    then works in that other directory. Claude keeps a transcript in the folder named
    after the real path of the directory it runs in, so this is a directory whose real
    path gives the transcript's folder name: the recorded cwd if it does (it need not,
    for older transcripts), else one found from the folder name.
    """
    folder = s.path.parent.name
    for d in (s.cwd, s.last_cwd):
        if d and os.path.isdir(d) and encode_cwd(os.path.realpath(d)) == folder:
            return d
    found = folder_dirs(folder)
    return found[0] if found else None


def data_dir() -> Path:
    """Our own files: $XDG_DATA_HOME/workspace (default ~/.local/share)."""
    return xdgdirs.data_home() / xdgdirs.APP


def sidecar_path() -> Path:
    """Labels, groups and tags, keyed by session id."""
    return data_dir() / "meta.json"


def trash_dir() -> Path:
    """Where deleted conversations go (recoverable: see RESTORE.txt in each folder)."""
    return data_dir() / "trash"


# --- data model ------------------------------------------------------------


@dataclass(frozen=True)
class Meta:
    """Our own metadata for one session. Stored in the sidecar, never in the JSONL."""

    label: str = ""
    group: str = ""
    tags: tuple[str, ...] = ()
    combined_from: tuple[str, ...] = ()  # session ids this one was combined from (#44)


@dataclass(frozen=True)
class Filter:
    """Which conversations the list shows: group is one of `groups` (when any), at least
    one tag is in `tags` (when any), AND every word of `titles` is in the title. Names
    compare without case.
    """

    groups: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    titles: tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        return bool(self.groups or self.tags or self.titles)

    def matches(self, meta: Meta, title: str = "") -> bool:
        """``title`` is the text the title words are looked up in (see ``Row.titles``)."""
        if self.groups and meta.group.casefold() not in {g.casefold() for g in self.groups}:
            return False
        if self.titles:
            haystack = title.casefold()
            if not all(t.casefold() in haystack for t in self.titles):
                return False
        if self.tags:
            want = {t.casefold() for t in self.tags}
            return any(t.casefold() in want for t in meta.tags)
        return True

    def text(self) -> str:
        """As typed in the `f` minibuffer: `g:work t:urgent q:trip` (quoted when a name has
        spaces).
        """
        words = (
            [f"g:{g}" for g in self.groups]
            + [f"t:{t}" for t in self.tags]
            + [f"q:{q}" for q in self.titles]
        )
        return " ".join(shlex.quote(w) for w in words)

    @classmethod
    def parse(cls, text: str) -> Filter:
        """The inverse of `text`. Raises ValueError on a word without `g:`, `t:` or `q:`."""
        found: dict[str, list[str]] = {"g": [], "t": [], "q": []}
        for word in shlex.split(text):
            kind, _, name = word.partition(":")
            kind, name = kind.lower(), name.strip()
            if kind not in found or not name:
                raise ValueError(f"not g:<group>, t:<tag> or q:<title text>: {word}")
            found[kind].append(name)
        return cls(groups=tuple(found["g"]), tags=tuple(found["t"]), titles=tuple(found["q"]))


NO_FILTER = Filter()  # every conversation


@dataclass(frozen=True)
class Tokens:
    """Token counts of a conversation, from the ``usage`` of its assistant replies."""

    context: int = 0  # prompt size of the last reply: what the context window holds now
    input: int = 0  # every reply's input, cache writes and cache reads, summed
    output: int = 0  # every reply's output, summed
    model: str = ""  # the model of the last reply: what ``context`` is measured against
    cached: int = 0  # the part of ``input`` read from the prompt cache, summed
    effort: str = ""  # the reasoning effort of the last reply; "" when the line had none

    def __add__(self, other: Tokens) -> Tokens:
        """Totals added; the context and model are ``other``'s when it has a context (the
        newer session or reply).
        """
        newer = other if other.context else self
        return Tokens(
            context=newer.context,
            input=self.input + other.input,
            output=self.output + other.output,
            model=newer.model,
            cached=self.cached + other.cached,
            effort=newer.effort,
        )

    def __sub__(self, other: Tokens) -> Tokens:
        """Totals without ``other``'s; the context and model stay."""
        return replace(
            self,
            input=self.input - other.input,
            output=self.output - other.output,
            cached=self.cached - other.cached,
        )


NO_TOKENS = Tokens()

_MODEL_VERSION = re.compile(r"claude-([a-z]+)-(\d+)(?:-(\d)(?!\d))?")
# the first version with a 1M window, per family (platform.claude.com, context windows)
_MILLION_FROM = {
    "fable": (0, 0),
    "mythos": (0, 0),
    "opus": (4, 6),
    "sonnet": (4, 6),
    "haiku": (5, 5),
}


def context_window(model: str) -> int | None:
    """The context window of a model id from a transcript; None when it is not a Claude id
    (``<synthetic>``).

    1M for Fable, Mythos, Opus and Sonnet from 4.6, Haiku from 5.5; 200k for the others.
    A ``[1m]`` suffix (Claude Code's 1M choice) is 1M for any model.
    """
    if model.endswith("[1m]"):
        return 1_000_000
    if not model.startswith("claude-"):
        return None
    m = _MODEL_VERSION.match(model)
    if m is None:
        return 200_000  # an older naming (``claude-3-5-sonnet-…``)
    family, major, minor = m.group(1), int(m.group(2)), int(m.group(3) or 0)
    start = _MILLION_FROM.get(family)
    return 1_000_000 if start is not None and (major, minor) >= start else 200_000


def model_name(model: str) -> str:
    """``opus 5.5`` for ``claude-opus-5-5``; the id as is when it is not a known naming."""
    m = _MODEL_VERSION.match(model)
    if m is None:
        return model
    name = f"{m.group(1)} {m.group(2)}" + (f".{m.group(3)}" if m.group(3) else "")
    return name + (" [1m]" if model.endswith("[1m]") else "")


@dataclass(frozen=True)
class Session:
    """One transcript on disk, read-only."""

    session_id: str
    path: Path
    cwd: str  # launch dir (first `user` line)
    last_cwd: str  # latest cwd recorded; follows `cd` during the session
    git_branch: str
    first_prompt: str
    ai_title: str
    custom_title: str
    continued_in: str  # `continued-in` line: ← in Claude moved the conversation to this id
    has_messages: bool  # False for a continuation's stub (title lines only)
    mtime: float
    size: int
    tokens: Tokens = NO_TOKENS


@dataclass(frozen=True)
class Row:
    """One conversation — a `continued-in` chain, origin first — joined with our metadata.

    Pressing ← inside Claude continues the conversation under a new session id
    (a background session). The chain keeps those ids together as one row.
    """

    chain: tuple[Session, ...]
    meta: Meta
    meta_key: str  # sidecar key: the chain id that already has metadata, else the origin
    rooms: Rooms = NO_ROOMS  # project and topics on the bus; set by ``attach_rooms``
    notes: tuple[Note, ...] = ()  # open notes typed in the panel; set by ``attach_notes``

    @property
    def head(self) -> Session:
        """The newest session of the conversation: the one to open."""
        return self.chain[-1]

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(s.session_id for s in self.chain)

    def _newest(self, attr: str) -> str:
        return next((getattr(s, attr) for s in reversed(self.chain) if getattr(s, attr)), "")

    @property
    def title(self) -> str:
        """Best human-readable name Claude itself knows."""
        return (
            self._newest("custom_title")
            or self._newest("ai_title")
            or self.chain[0].first_prompt
            or self.head.session_id[:8]
        )

    @property
    def display_title(self) -> str:
        return self.meta.label or self.title

    @property
    def titles(self) -> str:
        """Every name the conversation answers to: your label, Claude's title, the robot name
        of each of its sessions, and the ``@handle`` once ``attach_rooms`` has read it.
        """
        robots = " ".join(names.robot_name(i) for i in self.ids)
        handle = f"@{self.rooms.handle}" if self.rooms.handle else ""
        return f"{self.meta.label}\n{self.title}\n{robots}\n{handle}"

    @property
    def cwd(self) -> str:
        """Where the conversation is working now."""
        return self._newest("last_cwd") or self._newest("cwd")

    @property
    def git_branch(self) -> str:
        return self._newest("git_branch")

    @property
    def tokens(self) -> Tokens:
        """Totals over the whole chain; the context of its newest session that has one."""
        total = NO_TOKENS
        for s in self.chain:
            total += s.tokens
        return total

    @property
    def mtime(self) -> float:
        return max(s.mtime for s in self.chain)

    @property
    def resumable(self) -> Session:
        """Newest session with real messages: what `claude --resume` can load."""
        return next((s for s in reversed(self.chain) if s.has_messages), self.chain[0])

    @property
    def age(self) -> str:
        secs = max(0.0, time.time() - self.mtime)
        for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
            if secs >= size:
                return f"{int(secs // size)}{unit}"
        return f"{int(secs)}s"


# --- transcript parsing ----------------------------------------------------


def _text_of(content) -> str:
    """A user message's content is a string or a list of blocks; pull text out."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        ]
        return " ".join(p for p in parts if p)
    return ""


# raw-line patterns; "\s*" so they hold for any JSON spacing (Claude writes it compact)
_SESSION_ID = re.compile(rb'"sessionId"\s*:\s*"([^"\\]+)"')
_TYPE = re.compile(rb'"type"\s*:\s*"([a-z-]+)"')
_PARSED_TYPES = {b"ai-title", b"custom-title", b"continued-in"}
# an assistant line starts with its "message", whose "role" comes within the first 300 bytes
_ASSISTANT = re.compile(rb'"role"\s*:\s*"assistant"')
_SIDECHAIN = re.compile(rb'"isSidechain"\s*:\s*true')
_MESSAGE_ID = re.compile(rb'"id"\s*:\s*"(msg_[^"\\]+)"')
_MODEL = re.compile(rb'"model"\s*:\s*"([^"\\]+)"')
_EFFORT = re.compile(rb'"effort"\s*:\s*"([a-z]+)"')
# unescaped, so a "usage" quoted inside a tool input or result does not match
_USAGE = re.compile(rb'"usage"\s*:\s*\{')
_USAGE_FIELDS = {
    key: re.compile(rb'"' + key + rb'"\s*:\s*(\d+)')
    for key in (
        b"input_tokens",
        b"cache_creation_input_tokens",
        b"cache_read_input_tokens",
        b"output_tokens",
    )
}


def _reply_tokens(line: bytes) -> tuple[str, Tokens] | None:
    """The message id and token counts of a main-chain assistant line; None for any other.

    Claude writes one line per content block of a reply, each with the reply's ``usage``,
    so the caller counts a message id once. The top-level counts come first in ``usage``,
    before the nested ``iterations`` that repeat them.
    """
    if not _ASSISTANT.search(line, 0, 300) or _SIDECHAIN.search(line, 0, 300):
        return None
    usage = _USAGE.search(line)
    msg_id = _MESSAGE_ID.search(line, 0, 300)
    if usage is None or msg_id is None:
        return None
    counts = {
        key: int(m.group(1)) if (m := field.search(line, usage.end(), usage.end() + 600)) else 0
        for key, field in _USAGE_FIELDS.items()
    }
    prompt = (
        counts[b"input_tokens"]
        + counts[b"cache_creation_input_tokens"]
        + counts[b"cache_read_input_tokens"]
    )
    if not prompt:
        return None  # a synthetic reply (an error, an interrupt): no request was made
    model = m.group(1).decode() if (m := _MODEL.search(line, 0, 300)) else ""
    # the line's own metadata comes after the message, at the end of the line
    effort = m.group(1).decode() if (m := _EFFORT.search(line, max(0, len(line) - 800))) else ""
    return msg_id.group(1).decode(), Tokens(
        prompt, prompt, counts[b"output_tokens"], model, counts[b"cache_read_input_tokens"], effort
    )


def _json_str(raw: str) -> str:
    """Decode the inside of a JSON string literal (escapes included)."""
    try:
        return json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        return ""


def _last_cwd(path: Path, chunk: int = 65536) -> str:
    """The newest `"cwd":"..."` in the file, read backwards from the end."""
    with path.open("rb") as fh:
        end = fh.seek(0, os.SEEK_END)
        tail = b""
        while end > 0:
            start = max(0, end - chunk)
            fh.seek(start)
            tail = fh.read(end - start) + tail
            matches = list(_CWD_BYTES.finditer(tail))
            # a match touching the window's start may be cut: widen once more unless at 0
            usable = [m for m in matches if m.start() > 0 or start == 0]
            if usable:
                return _json_str(usable[-1].group(1).decode("utf-8", "replace"))
            end = start
    return ""


_CWD_BYTES = re.compile(rb'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"')


@dataclass(frozen=True)
class Read:
    """One transcript as far as it was read. Claude only appends to a transcript, so the
    next read starts at `end` and adds the new lines to `session`.
    """

    stamp: tuple[int, int]  # (mtime_ns, size) of the file when it was read
    end: int  # bytes read: up to the end of the last complete line
    session: Session
    reply_id: str = ""  # the last reply counted in ``session.tokens``
    reply: Tokens = NO_TOKENS  # that reply's own counts, replaced when it is written again


def parse_transcript(path: Path) -> Session | None:
    read = read_transcript(path)
    return read.session if read else None


def read_transcript(path: Path, prev: Read | None = None) -> Read | None:
    """Read a transcript; with `prev`, only the lines appended since (all of it if it shrank)."""
    try:
        st = path.stat()
    except OSError:
        return None
    if prev is not None and st.st_size < prev.end:
        prev = None  # rewritten: read it again from the start
    old = prev.session if prev else None
    session_id = old.session_id if old else ""
    cwd = old.cwd if old else ""
    git_branch = old.git_branch if old else ""
    first_prompt = old.first_prompt if old else ""
    ai_title = old.ai_title if old else ""
    custom_title = old.custom_title if old else ""
    continued_in = old.continued_in if old else ""
    has_messages = old.has_messages if old else False
    tokens = old.tokens if old else NO_TOKENS
    reply_id = prev.reply_id if prev else ""
    reply = prev.reply if prev else NO_TOKENS
    end = prev.end if prev else 0

    try:
        with path.open("rb") as fh:
            fh.seek(end)
            for line in fh:
                # a last line without its newline is still being written: it is read
                # now and again next time (every field below takes it twice the same way)
                if line.endswith(b"\n"):
                    end += len(line)
                # Transcripts reach hundreds of MB: look at the start of the raw line and
                # parse JSON only for the few lines that carry what the list shows. In user
                # and title lines Claude writes the top-level "type" before any nested
                # object, within the first 200 bytes (checked on every local transcript:
                # 2.1.302). An assistant line mostly starts with its nested "message", so
                # its first "type" is "message": a conversation counts as having messages
                # by its user lines (and the few assistant lines written type-first).
                if not session_id and (m := _SESSION_ID.search(line)):
                    session_id = m.group(1).decode()
                m = _TYPE.search(line, 0, 200)
                kind = m.group(1) if m else b""
                if kind in (b"user", b"assistant"):
                    has_messages = True
                if kind != b"user" and (counted := _reply_tokens(line)):
                    msg_id, counts = counted
                    if msg_id == reply_id:
                        tokens -= reply  # the same reply again: count it once
                    tokens += counts
                    reply_id, reply = msg_id, counts
                wanted = (
                    kind == b"user" and not (cwd and git_branch and first_prompt)
                ) or kind in _PARSED_TYPES
                if not wanted:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = d.get("type")
                if t == "ai-title" and d.get("aiTitle"):
                    ai_title = d["aiTitle"]
                elif t == "custom-title" and d.get("customTitle"):
                    custom_title = d["customTitle"]
                elif t == "continued-in" and d.get("continuedInSessionId"):
                    continued_in = d["continuedInSessionId"]
                elif t == "user":
                    if not cwd and d.get("cwd"):
                        cwd = d["cwd"]
                    if not git_branch and d.get("gitBranch"):
                        git_branch = d["gitBranch"]
                    if not first_prompt:
                        msg = d.get("message") or {}
                        txt = _text_of(msg.get("content")) if isinstance(msg, dict) else ""
                        # skip tool-result / slash-command noise for the preview
                        if txt and not txt.startswith(("<", "/")):
                            first_prompt = txt.strip()
        last_cwd = _last_cwd(path)
    except OSError:
        return None

    session = Session(
        session_id=session_id or path.stem,
        path=path,
        cwd=cwd,
        last_cwd=last_cwd,
        git_branch=git_branch,
        first_prompt=first_prompt,
        ai_title=ai_title,
        custom_title=custom_title,
        continued_in=continued_in,
        has_messages=has_messages,
        mtime=st.st_mtime,
        size=st.st_size,
        tokens=tokens,
    )
    return Read(
        stamp=(st.st_mtime_ns, st.st_size),
        end=end,
        session=session,
        reply_id=reply_id,
        reply=reply,
    )


_reads: dict[Path, Read] = {}
_scan_lock = threading.Lock()  # a second scan waits, then finds the first one's reads


class Cancelled(Exception):
    """The scan was cancelled (the app is closing)."""


def _never() -> bool:
    return False


def under(path: str, cwd: str) -> bool:
    """``path`` is ``cwd`` or a directory inside it."""
    base = cwd.rstrip("/")
    return path == base or path.startswith(base + "/")


def scan(
    cwd: str | None, cancelled: Callable[[], bool] = _never, subfolders: bool = False
) -> list[Session]:
    """All transcripts of one working directory (None: of every directory), newest first.
    With ``subfolders``, also those of every directory inside it.

    A transcript whose mtime and size did not change is not read again; one that grew
    is read from where the last read stopped. `cancelled` is checked before each file
    (raises Cancelled), so a first read of every directory does not hold up a quit.
    """
    with _scan_lock:
        found = _scan(cwd, cancelled, subfolders)
    if cwd is None or not subfolders:
        return found
    # the folder name is lossy ("-" was "/", "." or "-"): keep only what is really inside
    bases = {cwd, os.path.realpath(cwd)}
    return [s for s in found if any(under(s.cwd, b) for b in bases)]


def _scan(cwd: str | None, cancelled: Callable[[], bool], subfolders: bool) -> list[Session]:
    root = config_dir() / "projects"
    if cwd is not None and subfolders:
        names = {d.name for d in project_dirs(cwd)}
        folders = [
            p
            for p in (root.iterdir() if root.is_dir() else [])
            if p.is_dir() and any(p.name == n or p.name.startswith(n + "-") for n in names)
        ]
    elif cwd is not None:
        folders = project_dirs(cwd)
    else:
        folders = [p for p in root.iterdir() if p.is_dir()] if root.is_dir() else []
    sessions: list[Session] = []
    for folder in folders:
        for p in folder.glob("*.jsonl"):
            if cancelled():
                raise Cancelled
            try:
                st = p.stat()
            except OSError:
                continue
            prev = _reads.get(p)
            if prev is None or prev.stamp != (st.st_mtime_ns, st.st_size):
                read = read_transcript(p, prev)
                if read is None:
                    continue
                _reads[p] = prev = read
            sessions.append(prev.session)
    sessions.sort(key=lambda s: s.mtime, reverse=True)
    return sessions


# --- sidecar (our label/group/tags) ---------------------------------------


def load_meta() -> dict[str, Meta]:
    """The sidecar; entries of the wrong shape are skipped (it is a file people can edit)."""
    return read_meta(sidecar_path())


def read_meta(path: Path) -> dict[str, Meta]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return meta_from_raw(raw)


def meta_from_raw(raw: object) -> dict[str, Meta]:
    """The metadata in parsed sidecar JSON; entries of the wrong shape are skipped."""
    out: dict[str, Meta] = {}
    for sid, m in raw.items() if isinstance(raw, dict) else ():
        if not isinstance(m, dict):
            continue
        label, group, tags = m.get("label", ""), m.get("group", ""), m.get("tags", [])
        src = m.get("combined_from", [])
        out[sid] = Meta(
            label=label if isinstance(label, str) else "",
            group=group if isinstance(group, str) else "",
            tags=tuple(t for t in tags if isinstance(t, str)) if isinstance(tags, list) else (),
            combined_from=(
                tuple(x for x in src if isinstance(x, str)) if isinstance(src, list) else ()
            ),
        )
    return out


def save_meta(meta: dict[str, Meta]) -> None:
    """Write the sidecar atomically: a crash leaves the old file or the new one, never half."""
    path = sidecar_path()
    xdgdirs.make_dirs(path.parent, xdgdirs.data_home())
    payload = {
        sid: {"label": m.label, "group": m.group, "tags": list(m.tags)}
        | ({"combined_from": list(m.combined_from)} if m.combined_from else {})
        for sid, m in meta.items()
        if m.label or m.group or m.tags or m.combined_from
    }
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


@contextmanager
def meta_locked() -> Generator[None]:
    """Hold the sidecar for a read-modify-write: excludes other threads and other app
    instances (flock on a lock file next to it).
    """
    lock = sidecar_path().with_name(sidecar_path().name + ".lock")
    xdgdirs.make_dirs(lock.parent, xdgdirs.data_home())
    with lock.open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield  # closing the file releases the lock


def update_meta(key: str, change: Callable[[Meta], Meta]) -> None:
    """Change one session's metadata."""
    with meta_locked():
        meta = load_meta()
        meta[key] = change(meta.get(key, Meta()))
        save_meta(meta)


def drop_meta(session_ids: tuple[str, ...]) -> None:
    with meta_locked():
        meta = load_meta()
        if any(sid in meta for sid in session_ids):
            save_meta({sid: m for sid, m in meta.items() if sid not in session_ids})


def chains(sessions: list[Session]) -> list[tuple[Session, ...]]:
    """Group transcripts into conversations by following `continued-in` links."""
    by_id = {s.session_id: s for s in sessions}
    targets = {s.continued_in for s in sessions if s.continued_in in by_id}
    origins = [s for s in sessions if s.session_id not in targets]
    placed: set[str] = set()
    out: list[tuple[Session, ...]] = []
    # origins first; then whatever is left (only a broken, cyclic chain) so nothing disappears
    for start in origins + [s for s in sessions if s.session_id in targets]:
        if start.session_id in placed:
            continue
        chain, cur = [start], start
        placed.add(start.session_id)
        while cur.continued_in in by_id and cur.continued_in not in placed:
            cur = by_id[cur.continued_in]
            chain.append(cur)
            placed.add(cur.session_id)
        out.append(tuple(chain))
    return out


def build_rows(
    cwd: str | None,
    cancelled: Callable[[], bool] = _never,
    only: Filter = NO_FILTER,
    subfolders: bool = False,
) -> list[Row]:
    """The conversations of one working directory (None: of every directory; with
    ``subfolders``, of it and every directory inside it) that `only` matches.
    """
    meta = load_meta()
    found: list[Row] = []
    for chain in chains(scan(cwd, cancelled, subfolders)):
        key = next((s.session_id for s in chain if s.session_id in meta), chain[0].session_id)
        found.append(Row(chain=chain, meta=meta.get(key, Meta()), meta_key=key))
    if only.titles:
        found = attach_rooms(found)  # a title word may be an @handle, which only the bus knows
    rows = [r for r in found if only.matches(r.meta, r.titles)]
    # group together, ungrouped last, then newest-first inside each group
    rows.sort(key=lambda r: (r.meta.group == "", r.meta.group, -r.mtime))
    return rows


def attach_notes(rows: list[Row]) -> list[Row]:
    """The rows with the open notes of every session of each conversation, oldest first."""
    found = notes.load(i for r in rows for i in r.ids)
    return [
        replace(
            r, notes=tuple(sorted((n for i in r.ids for n in found.get(i, ())), key=lambda n: n.id))
        )
        for r in rows
    ]


def attach_rooms(rows: list[Row]) -> list[Row]:
    """The rows with the bus's project and topics for each conversation.

    A conversation takes the rooms of its newest session the bus knows.
    """
    found = rooms.load(i for r in rows for i in r.ids)
    return [
        replace(r, rooms=next((found[i] for i in reversed(r.ids) if i in found), NO_ROOMS))
        for r in rows
    ]


def trash(row: Row) -> Path:
    """Move the conversation's transcripts to the trash and drop its metadata.

    After this, neither the list nor Claude's `/resume` shows it. Returns the
    trash folder; its RESTORE.txt says where each file came from.
    """
    dest = trash_dir() / f"{time.strftime('%Y%m%d-%H%M%S')}-{row.chain[0].session_id[:8]}"
    xdgdirs.make_dirs(dest, xdgdirs.data_home())
    moved = []
    for s in row.chain:
        if s.path.exists():
            target = dest / s.path.name
            shutil.move(s.path, target)
            moved.append(f"{target} -> {s.path}")
    (dest / "RESTORE.txt").write_text(
        f"{row.display_title}\nMove each file back to restore the conversation:\n"
        + "\n".join(moved)
        + "\n",
        encoding="utf-8",
    )
    drop_meta(row.ids)
    return dest


if __name__ == "__main__":
    # quick sanity check: print the current dir's conversations
    cwd = os.getcwd()
    for r in build_rows(cwd):
        g = r.meta.group or "-"
        tags = ",".join(r.meta.tags) or "-"
        print(f"[{g:12}] {r.age:>4}  {len(r.chain)}  {tags:18}  {r.display_title[:50]}  {r.cwd}")
