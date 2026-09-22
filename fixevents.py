#!/usr/bin/env python
"""Restore event labels lost when the league map was pruned mid-run.

Rows written while a market was missing from the map carry league/event "?".
league was already recovered from the slug; this does the same for event, so
rows can be grouped by fixture again. Slugs are
<prefix>-<league>-<teams>-<YYYY-MM-DD>[-<market detail>], so the event is
everything after the prefix up to and including the date.

    python fixevents.py --dry-run
    python fixevents.py
"""

import argparse
import csv
import gzip
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bot import config, tapes  # noqa: E402
from bot.storage import BOOK_HEADER  # noqa: E402

DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def event_from_slug(slug):
    m = DATE.search(slug or "")
    if not m:
        return None
    head = slug[:m.end()]
    parts = head.split("-", 1)
    return parts[1] if len(parts) > 1 else None


def _require_snapshot():
    """Refuse to rewrite tapes unless a snapshot was taken recently AND the
    recorder is stopped.

    Two failures on 2026-09-21 motivated this: rebuilding without a backup lost
    a market's data, and renaming files the recorder still had open sent its
    subsequent writes into the archive.
    """
    import os
    import time
    from pathlib import Path

    root = Path(__file__).resolve().parent
    lock = root / "data" / "recorder.pid"
    if lock.exists():
        try:
            pid = int(lock.read_text().strip())
            os.kill(pid, 0)
            raise SystemExit(
                f"refusing to modify tapes: recorder is running (pid {pid}).\n"
                "stop it first:  pkill -f 'supervise.sh' && pkill -f 'Python run.py'"
            )
        except (ValueError, ProcessLookupError, PermissionError):
            pass

    snaps = sorted(root.glob("backups/snapshot-*"))
    fresh = [s for s in snaps if time.time() - s.stat().st_mtime < 3600]
    if not fresh:
        raise SystemExit(
            "refusing to modify tapes: no snapshot in the last hour.\n"
            "run ./snapshot.sh first."
        )
    return fresh[-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sport")
    args = ap.parse_args()
    if not getattr(args, 'dry_run', False):
        print(f'snapshot in place: {_require_snapshot().name}')

    sports = [args.sport] if args.sport else tapes.sports_present("tape")
    for sport in sports:
        files = tapes.tape_files("tape", sport)
        if not files:
            continue
        rows, fixed, already = [], Counter(), 0
        for f in files:
            for r in tapes._rows(f):
                if r.get("event") in ("?", "", None):
                    ev = event_from_slug(r.get("market"))
                    if ev:
                        r["event"] = ev
                        fixed[sport] += 1
                    else:
                        continue
                else:
                    already += 1
                rows.append(r)
        if not fixed[sport]:
            continue
        print(f"{sport}: {fixed[sport]:,} rows relabelled "
              f"({already:,} already had an event)")
        if args.dry_run:
            ev = Counter(r["event"] for r in rows if r.get("event"))
            for k, v in ev.most_common(5):
                print(f"     {k:44}{v:>8,}")
            continue

        # rewrite the sport's tapes as one clean file, archiving the originals
        day = files[-1].stem.replace(".csv", "").split("-", 2)[2]
        dest = config.DATA / f"tape-{sport}-{day}-rebuilt.csv.gz"
        with gzip.open(dest, "wt", newline="", compresslevel=6) as fh:
            w = csv.writer(fh)
            w.writerow(BOOK_HEADER)
            for r in rows:
                w.writerow([r.get(k, "") for k in BOOK_HEADER])
        for f in files:
            f.rename(f.with_suffix(".gz.preevents"))
        dest.rename(config.DATA / f"tape-{sport}-{day}.csv.gz")
        print(f"     rebuilt -> tape-{sport}-{day}.csv.gz "
              f"({len(rows):,} rows); originals archived .preevents")


if __name__ == "__main__":
    main()
