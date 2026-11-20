#!/usr/bin/env python3
"""Remove local recording audio for meetings that are already archived.

Hex keeps uncompressed wav (about 220 MB per hour) next to each meeting's
transcript. Once an archive reaches status 'done' in the ledger, that audio is
only needed if you want to re-upload at full quality or re-transcribe from
scratch. This script lists those recordings and, with --apply, deletes the wav
files while keeping transcripts and manifests.

    python scripts/cleanup_audio.py            # dry run (default)
    python scripts/cleanup_audio.py --apply    # delete

Meetings that are not in the ledger as 'done' are never touched.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

AUDIO_SUFFIXES = {".wav"}


def default_meetings_dir() -> Path:
    return Path(os.environ.get("APPDATA") or Path.home()) / "hex" / "meetings"


def default_db() -> Path:
    return (Path(os.environ.get("APPDATA") or Path.home())
            / "hex-flowus-voice-note" / "archive_ledger.sqlite3")


def archived_meeting_ids(db: Path) -> set[str]:
    """Meeting ids whose archive finished. Read-only, safe while the bridge runs."""
    if not db.is_file():
        return set()
    uri = "file:" + db.as_posix() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        rows = conn.execute(
            "select distinct meeting_id from archives where status = 'done'"
        ).fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows if r[0]}


def human(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="actually delete (default is a dry run)")
    ap.add_argument("--meetings-dir",
                    help="Hex meetings directory (default: %%APPDATA%%/hex/meetings)")
    ap.add_argument("--db", help="archive ledger path")
    ap.add_argument("--keep-system", action="store_true",
                    help="keep system.wav (desktop audio, not the microphone track)")
    args = ap.parse_args()

    meetings_dir = Path(args.meetings_dir) if args.meetings_dir else default_meetings_dir()
    db = Path(args.db) if args.db else default_db()
    if not meetings_dir.is_dir():
        print(f"meetings dir not found: {meetings_dir}", file=sys.stderr)
        return 2

    done = archived_meeting_ids(db)
    print(f"ledger      : {db}")
    print(f"meetings dir: {meetings_dir}")
    print(f"archived    : {len(done)} meeting(s)\n")

    total = 0
    touched = 0
    skipped = 0
    for entry in sorted(meetings_dir.iterdir()):
        if not entry.is_dir():
            continue
        files = [p for p in entry.iterdir()
                 if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES]
        if args.keep_system:
            files = [p for p in files if p.name != "system.wav"]
        if not files:
            continue
        size = sum(p.stat().st_size for p in files)
        if entry.name not in done:
            skipped += 1
            print(f"SKIP  {entry.name}  {human(size):>10}  (not archived)")
            continue
        total += size
        touched += 1
        print(f"{'DEL  ' if args.apply else 'WOULD'} {entry.name}  {human(size):>10}")
        if args.apply:
            for p in files:
                p.unlink()
                print(f"        removed {p.name}")

    print()
    if not touched:
        print("nothing to do")
        return 0
    verb = "freed" if args.apply else "would free"
    print(f"{verb} {human(total)} across {touched} meeting(s); "
          f"{skipped} not archived (kept)")
    if not args.apply:
        print("re-run with --apply to delete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
