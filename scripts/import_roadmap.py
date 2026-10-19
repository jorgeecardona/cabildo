#!/usr/bin/env python3
"""Import a flat roadmap/checklist markdown file into a cabildo agenda register.

One-shot cutover tool: it reads a file of ``## TIER`` headings with ``- [ ]`` /
``- [x]`` / ``- [~]`` items (open / done / in-progress), turns each into its own
markdown task file with the description in the body, and leaves the source file
untouched so you can verify before deleting it.

    python scripts/import_roadmap.py                     # ~/.claude/sanity/roadmap.md -> global agenda
    python scripts/import_roadmap.py --source path.md --scope project
    python scripts/import_roadmap.py --out /some/dir --dry-run

The parser and writer live in ``cabildo.agenda`` (and are unit-tested there); this
script is only the command-line front. Nothing here is specific to any project or
tracker: a tracker key like ABC-123 is captured generically into the ``jira`` field.
"""

from __future__ import annotations

import argparse
import getpass
import pathlib
import sys

from cabildo import agenda


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="import_roadmap", description=__doc__)
    p.add_argument(
        "--source",
        default=str(pathlib.Path("~/.claude/sanity/roadmap.md").expanduser()),
        help="the flat markdown file to import (default: ~/.claude/sanity/roadmap.md)",
    )
    p.add_argument(
        "--scope",
        choices=("project", "global"),
        default="global",
        help="which agenda to write into (default: global)",
    )
    p.add_argument("--out", default="", help="write into this folder instead of the scope's agenda")
    p.add_argument(
        "--owner",
        default=getpass.getuser(),
        help="owner written on in-progress tasks (the register requires one)",
    )
    p.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    args = p.parse_args(argv)

    source = pathlib.Path(args.source).expanduser()
    if not source.is_file():
        print(f"no such file: {source}", file=sys.stderr)
        return 1
    tasks, warnings = agenda.parse_roadmap(source.read_text())

    by_status: dict[str, int] = {}
    for t in tasks:
        by_status[t.status.value] = by_status.get(t.status.value, 0) + 1
    print(f"parsed {len(tasks)} task(s) from {source}: " + ", ".join(f"{v} {k}" for k, v in by_status.items()))
    for w in warnings:
        print(f"  warning: {w}")

    if args.dry_run:
        print("dry run: nothing written")
        return 0

    folder = pathlib.Path(args.out).expanduser() if args.out else agenda.resolve_dir(args.scope, create=True)
    counts = agenda.import_tasks(folder, tasks, owner=args.owner)
    problems = agenda.check(folder)
    print(f"wrote {sum(counts.values())} file(s) to {folder}: " + ", ".join(f"{v} {k}" for k, v in counts.items()))
    print(f"check: {'clean' if not problems else f'{len(problems)} problem(s)'}")
    for problem in problems:
        print(f"  {problem}")
    print(f"source left in place: {source}")
    return 0 if not problems else 2


if __name__ == "__main__":
    raise SystemExit(main())
