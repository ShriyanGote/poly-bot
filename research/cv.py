#!/usr/bin/env python
"""Leave-one-game-out cross-validation.

With only 14 games a single train/test split is a coin flip - the earlier run
gave +7 ticks on one fold and -3 on another from the same data. LOGO trains on
13 games and tests on the held-out one, every game taking a turn, so we see the
spread across folds rather than one lucky draw.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.metrics import roc_auc_score

HERE = Path(__file__).resolve().parent
# Tick size differs by sport: football quotes in half cents, the rest in cents.
TICKS = {"football": 0.005}
DEFAULT_TICK = 0.01

FEATS = ["book_imb", "depth", "spread", "vol_120",
         "flow_imb_10", "flow_imb_30", "flow_imb_120",
         "flow_vol_10", "flow_vol_30", "flow_vol_120",
         "flow_n_30", "mom_10", "mom_30", "mom_120", "mid"]


def pick(pred, frac):
    """Indices of the most bullish/bearish frac, handling heavy ties."""
    n = len(pred)
    k = max(5, min(int(n * frac) or 5, n // 2))
    order = np.argsort(pred)
    return order[-k:], order[:k]   # (most bullish, most bearish)


def fold_pnl(y, pred, spread, frac=0.10):
    """Net P&L charging each trade the spread actually quoted at entry.

    A flat cost badly understates reality outside football: tennis medians 5c
    and basketball 6c against the 1c I was charging, which manufactured an
    edge out of nothing.
    """
    hi_i, lo_i = pick(pred, frac)
    longs = y[hi_i] - spread[hi_i]
    shorts = -y[lo_i] - spread[lo_i]
    both = np.concatenate([longs, shorts])
    return both.mean(), len(both), (both > 0).mean()


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", default="football")
    ap.add_argument("--fast", action="store_true", help="fewer trees, quicker")
    ap.add_argument("--independent", action="store_true",
                    help="thin samples so forward windows never overlap")
    args = ap.parse_args()

    global TICK, SPREAD_COST
    TICK = TICKS.get(args.sport, DEFAULT_TICK)
    SPREAD_COST = TICK

    p = HERE / f"{args.sport}_features.csv"
    if not p.exists():
        p = HERE / f"{args.sport}_features.parquet"
    df = pd.read_csv(p) if p.suffix == ".csv" else pd.read_parquet(p)
    df = df.dropna(subset=FEATS)

    if args.independent:
        # Adjacent rows share most of their forward window (median 8.8s apart
        # vs a 60s horizon), so they are not independent observations and any
        # t-statistic computed over them is inflated by ~sqrt(overlap).
        keep = []
        for _m, g in df.groupby("market", sort=False):
            g = g.sort_values("ts")
            last = -1e18
            for idx, ts in zip(g.index, g["ts"].values):
                if ts - last >= 180:
                    keep.append(idx)
                    last = ts
        before = len(df)
        df = df.loc[keep]
        print(f"independent sampling: {before:,} -> {len(df):,} rows "
              f"(>=180s apart per market)")

    print(f"sport {args.sport}  tick {TICK}  spread cost {SPREAD_COST}\n")
    n_trees = 60 if args.fast else 120

    for horizon in (60, 180):
        tgt = f"fwd_{horizon}"
        d = df.dropna(subset=[tgt]).copy()
        games = sorted(d["game"].unique())
        if len(games) > 25:
            counts = d.groupby("game").size().sort_values(ascending=False)
            games = sorted(counts.head(25).index)
            d = d[d["game"].isin(games)]
        print(f"=== horizon {horizon}s | {len(games)} games, {len(d):,} rows ===")
        print(f"{'held-out game':32}{'n':>7}{'AUC':>7}{'flow net':>10}{'GB net':>10}")
        print("-" * 66)

        flow_nets, gb_nets, aucs = [], [], []
        for g in games:
            tr = d[d["game"] != g]
            te = d[d["game"] == g]
            min_te = 25 if args.independent else 200
            if len(te) < min_te or len(tr) < 300:
                continue
            Xtr, ytr = tr[FEATS].values, tr[tgt].values
            Xte, yte = te[FEATS].values, te[tgt].values

            gb = GradientBoostingRegressor(n_estimators=n_trees, max_depth=3,
                                           learning_rate=0.05, subsample=0.8,
                                           random_state=0).fit(Xtr, ytr)
            pred = gb.predict(Xte)
            moved = yte != 0
            auc = roc_auc_score((yte[moved] > 0).astype(int), pred[moved]) \
                if moved.sum() > 50 else np.nan

            sp = te["spread"].values
            fnet, _, _ = fold_pnl(yte, te["flow_imb_30"].values, sp)
            gnet, n, _ = fold_pnl(yte, pred, sp)
            flow_nets.append(fnet)
            gb_nets.append(gnet)
            aucs.append(auc)
            print(f"{g[:31]:32}{n:>7}{auc:>7.3f}{fnet/TICK:>9.2f}t{gnet/TICK:>9.2f}t")

        def summ(name, arr):
            a = np.array([x for x in arr if not np.isnan(x)])
            pos = (a > 0).sum()
            # simple t-stat on the fold means
            t = a.mean() / (a.std(ddof=1) / np.sqrt(len(a))) if len(a) > 1 and a.std() > 0 else 0
            print(f"  {name:22} mean {a.mean()/TICK:+.2f}t   median {np.median(a)/TICK:+.2f}t   "
                  f"{pos}/{len(a)} folds positive   t={t:+.2f}")

        print()
        summ("flow_imb_30 alone", flow_nets)
        summ("gradient boosting", gb_nets)
        a = np.array([x for x in aucs if not np.isnan(x)])
        print(f"  {'AUC':22} mean {a.mean():.3f}   {(a>0.5).sum()}/{len(a)} folds above 0.5")
        print()


if __name__ == "__main__":
    main()
