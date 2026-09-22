#!/usr/bin/env python
"""Fold archived tape variants (.preevents/.repaired) back into the live tapes.

A rebuild renamed files the recorder still had open, so writes continued into
the archive. This merges everything back, de-duplicating on
(ts, market) so re-running is safe.
"""

import csv
import gzip
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bot import config, tapes  # noqa: E402
from bot.storage import BOOK_HEADER  # noqa: E402


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
    print(f'snapshot in place: {_require_snapshot().name}')
    archives = sorted(config.DATA.glob("tape-*.preevents")) + \
        sorted(config.DATA.glob("tape-*.repaired"))
    if not archives:
        print("nothing to merge")
        return

    by_target = {}
    for a in archives:
        stem = a.name.split(".csv.gz")[0]          # tape-tennis-2026-09-21
        by_target.setdefault(stem, []).append(a)

    for stem, files in sorted(by_target.items()):
        live = config.DATA / f"{stem}.csv.gz"
        seen = set()
        rows = []
        for src in ([live] if live.exists() else []) + files:
            n = 0
            for r in tapes._rows(src):
                key = (r.get("ts"), r.get("market"))
                if key in seen:
                    continue
                seen.add(key)
                rows.append(r)
                n += 1
            print(f"  {src.name:52} +{n:>9,}")
        if not rows:
            continue
        rows.sort(key=lambda r: r.get("ts") or "")
        tmp = config.DATA / f"{stem}.merging.csv.gz"
        with gzip.open(tmp, "wt", newline="", compresslevel=6) as fh:
            w = csv.writer(fh)
            w.writerow(BOOK_HEADER)
            for r in rows:
                w.writerow([r.get(k, "") for k in BOOK_HEADER])
        tmp.replace(live)
        for a in files:
            a.rename(a.with_suffix(a.suffix + ".merged"))
        print(f"{stem}: {len(rows):,} unique rows -> {live.name}\n")


if __name__ == "__main__":
    main()
