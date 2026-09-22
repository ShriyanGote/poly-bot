#!/usr/bin/env python
"""Offline analysis of collected tape.

    python analyze.py              # all tape files
    python analyze.py --day 2026-09-20
"""

import argparse
import csv
import gzip
from collections import Counter, defaultdict
from decimal import Decimal

from bot import config

TICK = Decimal("0.01")


def read_tape(day=None):
    pat = f"{config.TAPE_PREFIX}-{day}.csv.gz" if day else f"{config.TAPE_PREFIX}-*.csv.gz"
    rows = []
    for f in sorted(config.DATA.glob(pat)):
        try:
            with gzip.open(f, "rt") as fh:
                # Append row-by-row: a live file raises at EOF and we must keep
                # everything read up to that point.
                for row in csv.DictReader(fh):
                    rows.append(row)
        except (EOFError, OSError, gzip.BadGzipFile):
            pass  # live file, take what we got
    return rows


def series_by_side(rows):
    """{(market, side): [(idx, entry_px, exit_px, league)]} in time order."""
    out = defaultdict(list)
    for r in rows:
        try:
            bid, ask = Decimal(r["bid"]), Decimal(r["ask"])
        except Exception:
            continue
        if bid <= 0 or ask <= 0:
            continue
        # long: pay ask, exit at bid.  short: pay 1-bid, exit at 1-ask.
        out[(r["market"], "long")].append((ask, bid, r["league"]))
        out[(r["market"], "short")].append((Decimal("1") - bid, Decimal("1") - ask, r["league"]))
    return out


def round_trip_test(series, exit_ticks=2):
    """From each price level: did the exit price reach entry+N ticks before dropping a tick?

    This is the strategy's actual question, measured on real tape.
    """
    buckets = defaultdict(lambda: {"win": 0, "loss": 0, "open": 0})
    for (market, side), pts in series.items():
        n = len(pts)
        i = 0
        while i < n:
            entry = pts[i][0]
            if entry <= 0 or entry >= Decimal("0.60"):
                i += 1
                continue
            target = entry + exit_ticks * TICK
            stop = entry - TICK
            outcome = "open"
            for j in range(i + 1, min(n, i + 3000)):
                ex = pts[j][1]
                if ex >= target:
                    outcome = "win"
                    break
                if ex <= stop:
                    outcome = "loss"
                    break
            b = f"{entry:.2f}"
            buckets[b][outcome] += 1
            i += 25  # sample, don't count every tick of the same episode
    return buckets


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day")
    ap.add_argument("--exit-ticks", type=int, default=2)
    args = ap.parse_args()

    rows = read_tape(args.day)
    if not rows:
        print("no tape data yet")
        return

    print(f"rows: {len(rows):,}")
    lg = Counter(r["league"] for r in rows)
    print("leagues: " + ", ".join(f"{k}={v:,}" for k, v in lg.most_common(10)))
    mkts = {r["market"] for r in rows}
    print(f"markets: {len(mkts)}")

    series = series_by_side(rows)
    buckets = round_trip_test(series, args.exit_ticks)

    print(f"\nROUND TRIP: reach entry+{args.exit_ticks} ticks before losing 1 tick?")
    print(f"{'entry':>7}{'win':>7}{'loss':>7}{'open':>7}{'P(win)':>9}{'needed':>9}{'edge':>8}")
    print("-" * 54)
    for b in sorted(buckets, key=lambda x: Decimal(x)):
        v = buckets[b]
        res = v["win"] + v["loss"]
        if res < 20:
            continue
        p = Decimal(v["win"]) / res
        entry = Decimal(b)
        # breakeven: win pays exit_ticks, loss costs 1 tick
        need = Decimal(1) / (Decimal(args.exit_ticks) + 1)
        edge = p - need
        flag = "  <<<" if edge > 0 else ""
        print(f"{b:>7}{v['win']:>7}{v['loss']:>7}{v['open']:>7}{p:>9.3f}{need:>9.3f}{edge:>+8.3f}{flag}")

    print("\nnote: 'needed' is the breakeven hit rate given a 2-tick win vs 1-tick loss.")
    print("positive edge columns are where the strategy would have made money.")


if __name__ == "__main__":
    main()
