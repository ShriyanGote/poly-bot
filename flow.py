#!/usr/bin/env python
"""Does trade-flow imbalance predict short-horizon price moves?

Uses the aggressor side recorded on every trade (taker BUY vs SELL), which the
mean-reversion work never touched. For each sampled moment we measure net
aggressor volume over a lookback window and check where the mid sat later.

    python flow.py
    python flow.py --sport football --window 30 --horizon 60
"""

import argparse
import bisect
import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bot import config, tapes  # noqa: E402

BUCKETS = [(-1.01, -0.5, "strong sell"), (-0.5, -0.15, "sell"),
           (-0.15, 0.15, "neutral"), (0.15, 0.5, "buy"), (0.5, 1.01, "strong buy")]


def bucket(x):
    for lo, hi, name in BUCKETS:
        if lo <= x < hi:
            return name
    return "neutral"


def load(sport):
    flows = defaultdict(list)
    for r in tapes.read("trades", sport):
        try:
            ts = datetime.fromisoformat(r["ts"]).timestamp()
            q = Decimal(r["qty"] or 0)
        except Exception:
            continue
        side = r.get("taker_side", "")
        if not side or q <= 0:
            continue
        flows[r["market"]].append((ts, q if "BUY" in side else -q))

    bars = defaultdict(list)
    for r in tapes.read("tape", sport):
        try:
            ts = datetime.fromisoformat(r["ts"]).timestamp()
            bid, ask = Decimal(r["bid"]), Decimal(r["ask"])
        except Exception:
            continue
        if bid <= 0 or ask <= 0:
            continue
        bars[r["market"]].append((ts, (bid + ask) / 2, ask - bid))
    for v in flows.values():
        v.sort()
    for v in bars.values():
        v.sort()
    return flows, bars


def run(sport, window, horizon, step=25):
    flows, bars = load(sport)
    res = defaultdict(lambda: {"n": 0, "up": 0, "move": Decimal("0"), "spread": Decimal("0")})
    for m, series in bars.items():
        f = flows.get(m)
        if not f or len(series) < 50:
            continue
        fts = [x[0] for x in f]
        bts = [x[0] for x in series]
        for i in range(0, len(series), step):
            t0, p0, sp = series[i]
            lo = bisect.bisect_left(fts, t0 - window)
            hi = bisect.bisect_right(fts, t0)
            if hi - lo < 3:
                continue
            net = sum(x[1] for x in f[lo:hi])
            vol = sum(abs(x[1]) for x in f[lo:hi])
            if vol <= 0:
                continue
            j = bisect.bisect_left(bts, t0 + horizon)
            if j >= len(series):
                continue
            p1 = series[j][1]
            k = bucket(float(net / vol))
            r = res[k]
            r["n"] += 1
            r["move"] += (p1 - p0)
            r["spread"] += sp
            if p1 > p0:
                r["up"] += 1
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport")
    ap.add_argument("--window", type=int, default=30)
    ap.add_argument("--horizon", type=int, default=60)
    args = ap.parse_args()

    sports = [args.sport] if args.sport else \
        ["football", "soccer", "basketball", "tennis", "esports"]

    print(f"flow window {args.window}s -> price {args.horizon}s later\n")
    print(f"{'sport':12}{'flow':14}{'samples':>9}{'P(up)':>8}{'avg move':>11}{'edge vs mid':>13}")
    print("-" * 67)
    for sp in sports:
        res = run(sp, args.window, args.horizon)
        if not res:
            continue
        mids = [r for k, r in res.items() if k == "neutral"]
        base = (mids[0]["move"] / mids[0]["n"]) if mids and mids[0]["n"] else Decimal("0")
        for _lo, _hi, k in BUCKETS:
            r = res.get(k)
            if not r or r["n"] < 50:
                continue
            avg = r["move"] / r["n"]
            print(f"{sp:12}{k:14}{r['n']:>9,}{r['up']/r['n']:>8.1%}"
                  f"{float(avg):>11.4f}{float(avg-base):>13.4f}")
        print()
    print("edge vs mid = average move minus the neutral-flow bucket's move")
    print("(a real signal shows monotonic P(up) and a positive edge on buy flow)")


if __name__ == "__main__":
    main()
