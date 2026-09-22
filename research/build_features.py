#!/usr/bin/env python
"""Build a football feature matrix for modelling.

One row per sampled moment per market side, with only information available at
that instant, plus the forward return we are trying to predict. Everything is
tagged with its game so train/test can split on games rather than rows -
samples inside one game are heavily autocorrelated and a random split leaks.
"""

import bisect
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot import tapes  # noqa: E402

SAMPLE_EVERY = 10          # book updates between samples
FLOW_WINDOWS = (10, 30, 120)
HORIZONS = (30, 60, 180)   # seconds ahead


def load(SPORT):
    books = defaultdict(list)
    meta = {}
    for r in tapes.read("tape", SPORT):
        try:
            ts = datetime.fromisoformat(r["ts"]).timestamp()
            bid, ask = float(r["bid"]), float(r["ask"])
            bt, at = float(r["bid_total"] or 0), float(r["ask_total"] or 0)
        except Exception:
            continue
        if bid <= 0 or ask <= 0 or ask <= bid:
            continue
        books[r["market"]].append((ts, bid, ask, bt, at, r.get("period", "")))
        meta[r["market"]] = r.get("event", "?")

    flows = defaultdict(list)
    for r in tapes.read("trades", SPORT):
        try:
            ts = datetime.fromisoformat(r["ts"]).timestamp()
            q = float(r["qty"] or 0)
            px = float((r["price"] or 0))
        except Exception:
            continue
        side = r.get("taker_side", "")
        if not side or q <= 0:
            continue
        flows[r["market"]].append((ts, q if "BUY" in side else -q, px))

    for v in books.values():
        v.sort()
    for v in flows.values():
        v.sort()
    return books, flows, meta


def build(SPORT):
    books, flows, meta = load(SPORT)
    rows = []
    for market, bars in books.items():
        if len(bars) < 200:
            continue
        game = meta.get(market, "?")
        bts = [b[0] for b in bars]
        f = flows.get(market, [])
        fts = [x[0] for x in f]

        for i in range(100, len(bars) - 1, SAMPLE_EVERY):
            ts, bid, ask, bt, at, period = bars[i]
            mid = (bid + ask) / 2
            spread = ask - bid
            if mid <= 0.02 or mid >= 0.98:
                continue

            rec = {"market": market, "game": game, "ts": ts, "mid": mid,
                   "spread": spread, "period": period,
                   "book_imb": (bt - at) / (bt + at) if (bt + at) else 0.0,
                   "depth": bt + at}

            # trade-flow features over several lookbacks
            for w in FLOW_WINDOWS:
                lo = bisect.bisect_left(fts, ts - w)
                hi = bisect.bisect_right(fts, ts)
                seg = f[lo:hi]
                vol = sum(abs(x[1]) for x in seg)
                net = sum(x[1] for x in seg)
                rec[f"flow_imb_{w}"] = (net / vol) if vol else 0.0
                rec[f"flow_vol_{w}"] = vol
                rec[f"flow_n_{w}"] = len(seg)

            # price momentum over the same lookbacks
            for w in FLOW_WINDOWS:
                j = bisect.bisect_left(bts, ts - w)
                prev = (bars[j][1] + bars[j][2]) / 2 if j < len(bars) else mid
                rec[f"mom_{w}"] = mid - prev

            # realised volatility over 120s
            j = bisect.bisect_left(bts, ts - 120)
            seg = [(b[1] + b[2]) / 2 for b in bars[j:i + 1]]
            rec["vol_120"] = float(np.std(seg)) if len(seg) > 2 else 0.0

            # forward returns - the targets
            ok = True
            for h in HORIZONS:
                k = bisect.bisect_left(bts, ts + h)
                if k >= len(bars):
                    ok = False
                    break
                fwd = (bars[k][1] + bars[k][2]) / 2
                rec[f"fwd_{h}"] = fwd - mid
            if not ok:
                continue
            rows.append(rec)

    df = pd.DataFrame(rows)
    out = Path(__file__).resolve().parent / f"{SPORT}_features.parquet"
    try:
        df.to_parquet(out)
    except Exception:
        out = out.with_suffix(".csv")
        df.to_csv(out, index=False)
    print(f"rows: {len(df):,}")
    print(f"markets: {df['market'].nunique()}   games: {df['game'].nunique()}")
    print(f"saved -> {out.name}")
    return df


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", default="football")
    build(ap.parse_args().sport)
