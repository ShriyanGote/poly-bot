#!/usr/bin/env python
"""Where should the stop go? Measured from the book tape.

For every simulated entry we track the maximum adverse excursion (MAE) and
whether the trade ever reached target (+EXIT_TICKS). That gives:

    P(eventually wins | already down D ticks)

A stop at D only makes sense if that conditional probability has fallen below
the break-even needed to keep holding.
"""

import csv
import gzip
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bot import config  # noqa: E402

TICK = Decimal("0.01")
TARGET_TICKS = config.EXIT_TICKS
MAX_LOOK = 4000          # samples forward (~a few minutes of book updates)
STEP = 20                # thin out overlapping entries


def load_series():
    """(market, side) -> [(entry_px, exit_px)] in time order."""
    out = defaultdict(list)
    for f in sorted(config.DATA.glob("tape-*.csv.gz")):
        try:
            with gzip.open(f, "rt") as fh:
                for r in csv.DictReader(fh):
                    try:
                        bid, ask = Decimal(r["bid"]), Decimal(r["ask"])
                    except Exception:
                        continue
                    if bid <= 0 or ask <= 0:
                        continue
                    # long: enter at bid (maker), exit into bid
                    out[(r["market"], "long")].append((bid, bid))
                    # short: enter at 1-ask, exit into 1-ask
                    out[(r["market"], "short")].append((Decimal("1") - ask, Decimal("1") - ask))
        except (EOFError, OSError, gzip.BadGzipFile):
            pass
    return out


def run():
    series = load_series()
    # mae_bucket -> [wins, total]
    reached = defaultdict(lambda: [0, 0])
    by_entry = defaultdict(lambda: [0, 0])
    n_entries = 0

    for (market, side), pts in series.items():
        n = len(pts)
        for i in range(0, n, STEP):
            entry = pts[i][0]
            if not (config.BAND_LO <= entry <= config.BAND_HI):
                continue
            target = entry + TARGET_TICKS * TICK
            n_entries += 1
            worst = Decimal("0")          # ticks down from entry
            won = False
            seen = set()
            for j in range(i + 1, min(n, i + MAX_LOOK)):
                px = pts[j][1]
                dd = (entry - px) / TICK
                if dd > worst:
                    worst = dd
                # record every drawdown level this trade passed through
                for d in range(1, 11):
                    if worst >= d and d not in seen:
                        seen.add(d)
                if px >= target:
                    won = True
                    break
                if px <= 0:
                    break
            for d in seen:
                reached[d][1] += 1
                if won:
                    reached[d][0] += 1
            b = f"{entry:.2f}"
            by_entry[b][1] += 1
            if won:
                by_entry[b][0] += 1

    print(f"simulated entries: {n_entries:,}  (target +{TARGET_TICKS} ticks)\n")

    print("P(reaches target | already down D ticks)")
    print(f"{'down':>6}{'trades':>9}{'wins':>8}{'P(win)':>9}{'need':>8}{'verdict':>12}")
    print("-" * 54)
    for d in sorted(reached):
        w, t = reached[d]
        if t < 30:
            continue
        p = w / t
        # holding on risks d+? more; breakeven to keep holding with +2 upside
        need = d / (d + TARGET_TICKS)
        verdict = "hold" if p > need else "CUT"
        print(f"{d:>6}{t:>9,}{w:>8,}{p:>9.2f}{need:>8.2f}{verdict:>12}")

    print("\nbaseline win rate by entry price")
    print(f"{'entry':>7}{'trades':>9}{'wins':>8}{'P(win)':>9}")
    print("-" * 34)
    for b in sorted(by_entry, key=float):
        w, t = by_entry[b]
        if t < 30:
            continue
        print(f"{b:>7}{t:>9,}{w:>8,}{w/t:>9.2f}")


if __name__ == "__main__":
    run()
