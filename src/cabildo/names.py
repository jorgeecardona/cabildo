"""Deterministic robot codenames for sessions.

A session's real id is Claude Code's full UUID (e.g.
``8f801073-440e-4c02-956f-511a8704a7bc``); everywhere it used to surface as the
bland first-8-hex ``8f801073``. This module turns that same id into a *cute
robot codename* — ``cute-sentinel-8f801``, ``rusty-bender-a3f0c`` — that reads
like a name but is still an id: it's a pure function of the session_id, so the
same session always gets the same codename, and the five-hex tail is lifted
straight from the raw id so the name stays tied to it (and stays unique).

Because it's deterministic there is no allocation, no DB column, and no
uniqueness bookkeeping: given an id you can always recompute the name, and given
a name you can match it back by recomputing over the active sessions
(:func:`cabildo.store.session_for_robot_name`). A ``@handle`` set via
``set_handle`` still wins over the generated name — that precedence lives in
:func:`cabildo.store.display_name`.
"""

from __future__ import annotations

# hashlib is imported inside robot_name, not here. store imports this module, and
# every hook fire imports store: ~38k fires so far, each paying ~2.4ms to load
# _hashlib for a name most of them never render. The digest itself stays sha256 —
# these names are stable identities on the bus (`session_for_robot_name` resolves
# them), so the algorithm is not something to trade for milliseconds.

# The codename is <adjective>-<noun>-<hhhhh>. Adjectives give it flavour; nouns
# are famous fictional robots / androids / AIs. Both are lowercase and free of
# hyphens so a name always splits into exactly three parts.
ADJECTIVES = [
    "cute",
    "rusty",
    "shiny",
    "clanky",
    "quiet",
    "brass",
    "chrome",
    "plucky",
    "dinky",
    "snappy",
    "zippy",
    "sparky",
    "glossy",
    "wonky",
    "jolly",
    "mighty",
    "tiny",
    "grumpy",
    "cosmic",
    "neon",
    "turbo",
    "atomic",
    "solar",
    "quantum",
    "retro",
    "bronze",
    "cobalt",
    "velvet",
    "silent",
    "electric",
    "lucky",
    "nimble",
    "stout",
    "dapper",
    "feral",
    "gentle",
    "bold",
    "sleek",
    "hyper",
    "wired",
    "molten",
    "frosty",
    "gilded",
    "spry",
    "brave",
    "sly",
    "keen",
    "vivid",
]

NOUNS = [
    "bender",
    "hal",
    "optimus",
    "megatron",
    "glados",
    "tars",
    "marvin",
    "ultron",
    "skynet",
    "asimo",
    "robby",
    "gort",
    "baymax",
    "clank",
    "claptrap",
    "gerty",
    "sonny",
    "astro",
    "voltron",
    "jarvis",
    "cortana",
    "sentinel",
    "roomba",
    "viki",
    "data",
    "vision",
    "chappie",
    "walle",
    "robocop",
    "terminator",
    "bishop",
    "ash",
    "case",
    "dalek",
    "cylon",
    "replicant",
    "android",
    "golem",
    "automaton",
    "modok",
    "atlas",
    "spot",
    "oracle",
    "proton",
    "crusher",
    "dynamo",
    "sprocket",
    "widget",
]


def robot_name(session_id: str | None) -> str:
    """Deterministic ``<adjective>-<noun>-<hhhhh>`` codename for a session UUID.

    e.g. ``cute-sentinel-8f801``. Empty / unknown ids collapse to ``"?"``
    (matching the old short-id path).
    """
    if not session_id:
        return "?"
    import hashlib

    digest = hashlib.sha256(session_id.encode()).digest()
    adj = ADJECTIVES[digest[0] % len(ADJECTIVES)]
    noun = NOUNS[digest[1] % len(NOUNS)]
    # Tail from the id's own leading hex keeps a visible tie to the short id
    # everyone used to read (8f801073 -> ...-8f801) and, at five digits, makes a
    # name collision within a working fleet effectively impossible. Fall back to
    # hashed bytes for ids that carry no hex up front (e.g. "human:jorge").
    hexchars = [c for c in session_id.lower() if c in "0123456789abcdef"]
    tail = "".join(hexchars[:5]) if len(hexchars) >= 5 else digest[2:5].hex()
    return f"{adj}-{noun}-{tail}"
