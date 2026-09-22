#!/usr/bin/env python
"""Are we selling too early? Sweep the profit target against a fixed stop.

For every simulated entry, walk the tape forward and see which comes first:
entry + T ticks (win) or entry - STOP ticks (loss). Repeat for T = 1..10 and
compare expected value per trade. The peak is the target worth using.
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
STOP = config.STOP_TICKS
MAX_LOOK = 6000
STEP = 20


def load():
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
                    out[(r["market"], "long")].append(bid)
                    out[(r["market"], "short")].append(Decimal("1") - ask)
        except (EOFError, OSError, gzip.BadGzipFile):
            pass
    return out


def main():
    series = load()
    results = {}
    mfe_hist = defaultdict(int)   # how far winners actually ran

    for target in range(1, 11):
        wins = losses = unresolved = 0
        for (_m, _s), pts in series.items():
            n = len(pts)
            for i in range(0, n, STEP):
                entry = pts[i]
                if not (config.BAND_LO <= entry <= config.BAND_HI):
                    continue
                up = entry + target * TICK
                dn = entry - STOP * TICK
                out = None
                for j in range(i + 1, min(n, i + MAX_LOOK)):
                    px = pts[j]
                    if px >= up:
                        out = "w"
                        break
                    if px <= dn or px <= 0:
                        out = "l"
                        break
                if out == "w":
                    wins += 1
                elif out == "l":
                    losses += 1
                else:
                    unresolved += 1
        res = wins + losses
        if not res:
            continue
        p = Decimal(wins) / res
        ev = p * target - (1 - p) * STOP
        results[target] = (wins, losses, unresolved, p, ev)

    # how far did winners run past +2?
    for (_m, _s), pts in series.items():
        n = len(pts)
        for i in range(0, n, STEP):
            entry = pts[i]
            if not (config.BAND_LO <= entry <= config.BAND_HI):
                continue
            dn = entry - STOP * TICK
            peak = Decimal("0")
            for j in range(i + 1, min(n, i + MAX_LOOK)):
                px = pts[j]
                gain = (px - entry) / TICK
                if gain > peak:
                    peak = gain
                if px <= dn or px <= 0:
                    break
            if peak >= 2:
                mfe_hist[min(int(peak), 15)] += 1

    print(f"TARGET SWEEP   stop fixed at -{STOP} ticks\n")
    print(f"{'target':>7}{'wins':>8}{'losses':>8}{'unres':>8}{'P(win)':>9}"
          f"{'EV ticks':>10}{'':>3}")
    print("-" * 56)
    best = max(results, key=lambda t: results[t][4]) if results else None
    for t, (w, l, u, p, ev) in results.items():
        flag = "  <-- best" if t == best else ""
        print(f"{t:>7}{w:>8,}{l:>8,}{u:>8,}{p:>9.2f}{ev:>10.2f}{flag}")

    print(f"\ncurrent setting: target +{config.EXIT_TICKS} ticks")
    if best:
        cur_ev = results.get(config.EXIT_TICKS, (0, 0, 0, 0, Decimal("0")))[4]
        print(f"best by EV:      target +{best} ticks "
              f"({results[best][4]:.2f} vs {cur_ev:.2f} ticks/trade)")

    print("\nHOW FAR WINNERS ACTUALLY RAN (peak gain before stop)")
    tot = sum(mfe_hist.values())
    if tot:
        cum = 0
        for k in sorted(mfe_hist):
            cum += mfe_hist[k]
            label = f"{k}+" if k == 15 else str(k)
            print(f"  reached +{label:>3} ticks: {mfe_hist[k]:>6,}  "
                  f"({100*(tot-cum+mfe_hist[k])/tot:>5.1f}% got at least this far)")


if __name__ == "__main__":
    main()
