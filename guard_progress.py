#!/usr/bin/env python3
"""
guard_progress - refuse to commit a checkpoint that has lost work.

A GitHub Actions run is pinned to the SHA it had when it was QUEUED. If an
earlier run pushed while this one waited on the concurrency lock, this run
starts from a stale tree, rebuilds a nearly empty checkpoint, and then
faithfully commits it over thousands of translated blocks.

So before committing, compare every checkpoint against the copy on the branch
and keep whichever has more translated blocks. Losing an hour of work is
recoverable; silently overwriting a day of it is not.

    python guard_progress.py --ref origin/main
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def block_count(text: str) -> int:
    try:
        data = json.loads(text)
        return len(data.get("blocks") or {})
    except Exception:
        return -1


def from_git(ref: str, path: str) -> str | None:
    r = subprocess.run(["git", "show", f"{ref}:{path}"],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default="origin/main")
    ap.add_argument("--dir", type=Path, default=Path("booktrans_work"))
    args = ap.parse_args()

    if not args.dir.exists():
        return

    restored = 0
    for cp in sorted(args.dir.glob("*/checkpoint.json")):
        rel = cp.as_posix()
        mine = block_count(cp.read_text(encoding="utf-8"))
        theirs_raw = from_git(args.ref, rel)
        if theirs_raw is None:
            print(f"  {rel}: new ({mine} blocks)")
            continue
        theirs = block_count(theirs_raw)
        if theirs > mine:
            cp.write_text(theirs_raw, encoding="utf-8")
            print(f"  {rel}: KEEPING branch copy - it has {theirs} blocks, "
                  f"this run has {mine}")
            restored += 1
        else:
            print(f"  {rel}: {mine} blocks (branch has {theirs})")

    if restored:
        print(f"\n  {restored} checkpoint(s) protected from being overwritten.")
        print("  This run started from a stale tree; rerun to continue.")


if __name__ == "__main__":
    main()
