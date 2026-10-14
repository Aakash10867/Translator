#!/usr/bin/env python3
"""
pick_book - resolve a loosely-typed book name to an actual file.

GitHub's workflow_dispatch cannot build a dropdown from repository contents,
so the input is free text and typos are inevitable. This accepts anything
recognisable - with or without the extension, the directory, or the exact
case - and when it cannot match, prints what is actually available instead of
failing silently.

    python pick_book.py --dir output --suffix .epub --want "Crime and Punishment.hi"
    python pick_book.py --dir output --skip-existing .hinglish.epub
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path


def normalise(name: str) -> str:
    name = Path(name).name.lower()
    for tail in (".hinglish.epub", ".hi.epub", ".epub", ".mobi", ".azw3",
                 ".azw", ".prc", ".hi", ".hinglish"):
        if name.endswith(tail):
            name = name[: -len(tail)]
    return re.sub(r"[^a-z0-9]+", "", name)


def candidates(directory: Path, suffixes: list[str],
               skip_existing: str | None) -> list[Path]:
    out = []
    for f in sorted(directory.glob("*")):
        if not f.is_file() or f.suffix.lower() not in suffixes:
            continue
        if "hinglish" in f.name.lower():
            continue
        if skip_existing:
            done = f.with_suffix("")
            if Path(str(done) + skip_existing).exists():
                continue
        out.append(f)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, required=True)
    ap.add_argument("--suffix", action="append", default=None,
                    help="allowed extensions, repeatable (default .epub)")
    ap.add_argument("--want", default="", help="what the user typed")
    ap.add_argument("--skip-existing", default=None,
                    help="ignore files that already have this companion, "
                         "e.g. .hinglish.epub")
    args = ap.parse_args()

    suffixes = [s.lower() for s in (args.suffix or [".epub"])]
    if not args.dir.exists():
        print(f"no such directory: {args.dir}", file=sys.stderr)
        sys.exit(3)

    pool = candidates(args.dir, suffixes, args.skip_existing)
    if not pool:
        print(f"nothing left to do in {args.dir}/", file=sys.stderr)
        sys.exit(3)

    want = args.want.strip()
    if not want:
        print(pool[0])
        return

    # exact filename first, then normalised, then substring, then closest
    for f in pool:
        if f.name == want:
            print(f)
            return
    target = normalise(want)
    for f in pool:
        if normalise(f.name) == target:
            print(f)
            return
    partial = [f for f in pool if target and target in normalise(f.name)]
    if len(partial) == 1:
        print(partial[0])
        return
    close = difflib.get_close_matches(target, [normalise(f.name) for f in pool],
                                      n=1, cutoff=0.6)
    if close:
        for f in pool:
            if normalise(f.name) == close[0]:
                print(f)
                return

    print(f'could not match "{want}". Available:', file=sys.stderr)
    for f in pool:
        print(f"  {f.name}", file=sys.stderr)
    sys.exit(3)


if __name__ == "__main__":
    main()
