#!/usr/bin/env python
"""Reattribute rows misfiled under sport '?' to their real sport.

A recorder bug dropped markets from the league map while they were still
subscribed, so their rows were written with sport/league unknown. The market
slug still identifies the league (<prefix>-<league>-<teams>-<date>...), so the
rows can be recovered rather than discarded.

    python repair.py --dry-run
    python repair.py
"""

import argparse
import csv
import gzip
import sys
import zlib
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bot import config  # noqa: E402
from bot.storage import BOOK_HEADER  # noqa: E402
from bot import tapes  # noqa: E402


def league_from_slug(slug):
    parts = (slug or "").split("-")
    return parts[1].upper() if len(parts) > 1 else "?"


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
    args = ap.parse_args()
    if not getattr(args, 'dry_run', False):
        print(f'snapshot in place: {_require_snapshot().name}')

    srcs = sorted(config.DATA.glob("tape-?-*.csv.gz")) + \
        sorted(config.DATA.glob("tape-other-*.csv.gz"))
    for src in srcs:
        day = src.stem.replace(".csv", "").split("-", 2)[2]
        out, counts, bad = {}, Counter(), 0
        # Use the recovering reader: the plain gzip reader stops at the first
        # damaged member and was finding 13,407 of 49,407 rows.
        for row in tapes._rows(src):
            if row.get("league") not in ("?", "", None) and \
                    row.get("sport") not in ("?", "other", "", None):
                continue                  # already correctly attributed
            lg = league_from_slug(row.get("market"))
            sport = config.sport_of(lg)
            row["league"], row["sport"] = lg, sport
            counts[sport] += 1
            out.setdefault(sport, []).append(row)

        total = sum(counts.values())
        print(f"{src.name}: {total:,} rows recovered"
              + ("  (file truncated, kept what parsed)" if bad else ""))
        for sp, n in counts.most_common():
            print(f"   -> {sp:12} {n:>8,}")

        if args.dry_run:
            continue

        for sport, rows in out.items():
            dest = config.DATA / f"tape-{sport}-{day}.csv.gz"
            new = not dest.exists()
            with gzip.open(dest, "at", newline="") as fh:
                w = csv.writer(fh)
                if new:
                    w.writerow(BOOK_HEADER)
                for r in rows:
                    w.writerow([r.get(k, "") for k in BOOK_HEADER])
        src.rename(src.with_suffix(".gz.repaired"))
        print(f"   merged and archived {src.name} -> {src.name}.repaired")


if __name__ == "__main__":
    main()
