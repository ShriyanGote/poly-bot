#!/usr/bin/env python
"""Entrypoint for the in-play recorder / paper trader.

    python run.py                 # record + paper trade
    python run.py --no-paper      # record only
    python run.py --status        # summarise collected data and exit

For a multi-day run on a Mac, prevent sleep:
    caffeinate -is .venv/bin/python run.py
"""

import argparse
import asyncio
import os
import signal
import sys

from dotenv import load_dotenv

load_dotenv()

from bot import config  # noqa: E402
from bot.recorder import Recorder  # noqa: E402


def status():
    import gzip
    import csv
    import zlib
    from collections import Counter

    def count(prefix):
        """Per-sport totals; tapes are one file per sport per day."""
        tot, rows_by = 0, Counter()
        by_sport = Counter()
        sizes = Counter()
        for f in sorted(config.DATA.glob(f"{prefix}-*.csv.gz")):
            n = 0
            # Tally each ROW's sport: legacy pre-split files hold every sport
            # in one file, so attributing the whole file to one is wrong.
            here = Counter()
            try:
                with gzip.open(f, "rt") as fh:
                    for row in csv.DictReader(fh):
                        n += 1
                        rows_by[row.get("league", "?")] += 1
                        here[row.get("sport") or "?"] += 1
            except (EOFError, OSError, gzip.BadGzipFile, zlib.error, csv.Error):
                # live file mid-write, or one damaged by an earlier
                # concurrent-writer incident; keep whatever parsed
                pass
            tot += n
            for sp, c in here.items():
                by_sport[sp] += c
                # apportion file size by row share
                sizes[sp] += int(f.stat().st_size * (c / n)) if n else 0
        for sp, n in by_sport.most_common():
            print(f"  {sp:14} {n:>11,} rows  {sizes[sp]/1e6:>7.1f} MB")
        return tot, rows_by

    print("BOOKS");   b, lg = count(config.TAPE_PREFIX)
    print("TRADES");  t, _ = count("trades")
    print("EXCURSIONS"); e, _ = count("excursions")
    print(f"\ntotals: {b:,} book rows | {t:,} trades | {e:,} excursion events")
    if lg:
        print("top leagues: " + ", ".join(f"{k}={v:,}" for k, v in lg.most_common(10)))

    sc = config.DATA / "scorecard.csv"
    if sc.exists():
        rows = list(csv.DictReader(sc.open()))
        rows = [r for r in rows if int(r["dips"]) >= 2]
        rows.sort(key=lambda r: -float(r["rec2_rate"]))
        print(f"\nSCORECARD - markets that dip and recover (top 12 of {len(rows)})")
        print(f"  {'league':9}{'side':6}{'thr':>6}{'dips':>6}{'rec1':>6}{'rec2':>6}{'rec3':>6}{'rec2%':>7}  market")
        for r in rows[:12]:
            print(f"  {r['league']:9}{r['side']:6}{r['threshold']:>6}{r['dips']:>6}"
                  f"{r['rec1']:>6}{r['rec2']:>6}{r['rec3']:>6}"
                  f"{float(r['rec2_rate'])*100:>6.0f}%  {r['market'][:36]}")

    if config.STATE_FILE.exists():
        import json
        s = json.loads(config.STATE_FILE.read_text())
        closed = s.get("closed", [])
        from decimal import Decimal
        net = sum(Decimal(c["pnl"]) for c in closed) if closed else Decimal("0")
        wins = sum(1 for c in closed if Decimal(c["pnl"]) > 0)
        print(f"\npaper: {len(s.get('positions',{}))} open, {len(s.get('resting',{}))} resting, "
              f"{len(closed)} closed, {wins} wins, net {net:+.2f}")


def acquire_lock():
    """Refuse to start a second recorder - two writers corrupt the same tape."""
    lock = config.DATA / "recorder.pid"
    if lock.exists():
        try:
            old = int(lock.read_text().strip())
            os.kill(old, 0)          # raises if not running
            print(f"ERROR: recorder already running as pid {old}", file=sys.stderr)
            print(f"stop it first (kill {old}) or remove {lock}", file=sys.stderr)
            return None
        except (ValueError, ProcessLookupError, PermissionError):
            pass                     # stale lock, take it
    lock.write_text(str(os.getpid()))
    return lock


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-paper", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    if args.status:
        status()
        return

    lock = acquire_lock()
    if lock is None:
        return 1

    rec = Recorder(paper_enabled=not args.no_paper)

    async def go():
        task = asyncio.create_task(rec.run())
        loop = asyncio.get_running_loop()
        for s in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(s, task.cancel)
        try:
            await task
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(go())
    finally:
        rec.shutdown()
        try:
            lock.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
