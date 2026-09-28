#!/usr/bin/env python3
"""Chronological test of quote/trade features on tennis longshot buys.

The model is trained on lower-probability sides only. A training row is a
quoted opportunity to buy that side; its target is the forward mid move less
the actual entry and exit half-spreads. The time split is fixed before looking
at test outcomes. We also score recorded first-touch trades with the model and
compare their actual recorded P&L before and after the filter.

    .venv/bin/python research/tennis_walkforward.py research/tennis_features_full.csv
"""
import argparse
import bisect
import json
import sys
from collections import defaultdict
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot import config

RAW_FEATURES = [
    "book_imb", "spread", "depth", "entry_depth", "exit_depth", "vol_120",
    "flow_imb_10", "flow_imb_30", "flow_imb_120",
    "flow_vol_10", "flow_vol_30", "flow_vol_120", "flow_n_30",
    "mom_10", "mom_30", "mom_120", "mid_prob",
]


def orient(df):
    """Features for the cheaper side, whether it is YES or NO."""
    d = df.copy()
    sign = np.where(d["mid"].to_numpy() <= 0.5, 1.0, -1.0)
    d["sign"] = sign
    d["mid_prob"] = np.minimum(d["mid"], 1.0 - d["mid"])
    d["entry_depth"] = np.where(sign > 0, d["ask_depth"], d["bid_depth"])
    d["exit_depth"] = np.where(sign > 0, d["bid_depth"], d["ask_depth"])
    d["book_imb"] = d["book_imb"] * sign
    for c in ("flow_imb_10", "flow_imb_30", "flow_imb_120",
              "mom_10", "mom_30", "mom_120"):
        d[c] = d[c] * sign
    for c in RAW_FEATURES:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    return d


def thin_nonoverlap(df, seconds=180):
    """Keep observations at least one target horizon apart within a market."""
    kept = []
    for _, g in df.sort_values("ts").groupby("market", sort=False):
        last = -1e30
        for idx, ts in zip(g.index, g["ts"].to_numpy()):
            if ts - last >= seconds:
                kept.append(idx)
                last = ts
    return df.loc[kept].sort_values("ts").copy()


def pnl_summary(label, d, values, stake):
    n = len(d)
    if not n:
        print(f"  {label:27} n=0")
        return
    values = np.asarray(values, dtype=float)
    stake = np.asarray(stake, dtype=float)
    total = values.sum()
    dollars = stake.sum()
    print(f"  {label:27} n={n:5} mean={values.mean():+.5f}/share "
          f"ROI={100*total/dollars:+7.1f}% win={100*(values>0).mean():5.1f}% "
          f"P&L/share=${total:.3f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("features", type=Path)
    ap.add_argument("--split-date", default="2026-09-23")
    args = ap.parse_args()

    df = pd.read_csv(args.features)
    df = df.replace([np.inf, -np.inf], np.nan)
    df = orient(df)
    df = df.dropna(subset=RAW_FEATURES + ["fwd_60", "fwd_spread_60", "game"])
    # The lower-priced outcome token is the one being considered for purchase.
    df = df[(df["mid_prob"] >= 0.01) & (df["mid_prob"] <= 0.10)].copy()
    # Exact net mark-to-book return for buying at the ask and selling at the
    # future bid, oriented to the cheaper YES/NO side.
    df["target_net_60"] = (df["sign"] * df["fwd_60"] -
                           (df["spread"] + df["fwd_spread_60"]) / 2)
    df["day"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.strftime("%Y-%m-%d")
    df = thin_nonoverlap(df, 180)

    split = pd.Timestamp(args.split_date, tz="UTC").timestamp()
    train = df[df["ts"] < split].copy()
    test = df[df["ts"] >= split].copy()
    # Remove events already observed in training; those are not fresh test
    # matches. When event is unknown, use market slug as the group.
    train["group_id"] = np.where(train["game"].astype(str).str.strip().isin(["", "?"]),
                                 train["market"], train["game"])
    test["group_id"] = np.where(test["game"].astype(str).str.strip().isin(["", "?"]),
                                test["market"], test["game"])
    seen = set(train["group_id"])
    dropped = test["group_id"].isin(seen).sum()
    test = test[~test["group_id"].isin(seen)].copy()

    print(f"low-probability tennis observations, spaced >=180s per market")
    print(f"  all {len(df):,} | train {len(train):,} | test {len(test):,}; "
          f"removed {dropped:,} test rows from previously seen matches")
    print(f"  train dates {train.day.min()} to {train.day.max()} | "
          f"test dates {test.day.min()} to {test.day.max()}")
    if len(train) < 100 or len(test) < 50:
        raise SystemExit("Too few independent observations for this split")

    model = GradientBoostingRegressor(n_estimators=100, max_depth=2,
                                      learning_rate=0.04, subsample=0.8,
                                      random_state=7)
    model.fit(train[RAW_FEATURES].to_numpy(), train["target_net_60"].to_numpy())
    train["pred"] = model.predict(train[RAW_FEATURES].to_numpy())
    test["pred"] = model.predict(test[RAW_FEATURES].to_numpy())

    print("\nMODEL NET RETURN (buy lower-probability token at ask, sell at 60s bid)")
    for label, d in (("train all", train), ("test all", test)):
        stake = d["mid_prob"].to_numpy() + d["spread"].to_numpy()/2
        pnl_summary(label, d, d["target_net_60"], stake)
    for q in (0.50, 0.75, 0.90, 0.95):
        threshold = float(train["pred"].quantile(q))
        sel = test[test["pred"] >= threshold]
        stake = sel["mid_prob"].to_numpy() + sel["spread"].to_numpy()/2
        pnl_summary(f"test pred >= train q{q:.2f}", sel,
                    sel["target_net_60"], stake)
        print(f"    threshold {threshold:+.5f}")
    positive = test[test["pred"] > 0]
    pnl_summary("test predicted net > 0", positive,
                positive["target_net_60"],
                positive["mid_prob"].to_numpy()+positive["spread"].to_numpy()/2)

    print("\nTEST RESULTS BY DAY (model-positive entries)")
    for day, g in test[test["pred"] > 0].groupby("day", sort=True):
        pnl_summary(day, g, g["target_net_60"],
                    g["mid_prob"].to_numpy()+g["spread"].to_numpy()/2)

    # Apply the same model to later recorded first-touch trades. The nearest
    # feature row must precede entry, to avoid peeking beyond the buy time.
    groups = {m: g.sort_values("ts") for m, g in df.groupby("market", sort=False)}
    times = {m: g["ts"].to_numpy() for m, g in groups.items()}
    state = json.loads(config.LONGSHOT_STATE.read_text())
    current_label = config.rule_label("tennis")
    actual = [p for p in state.get("closed", [])
              if p.get("sport") == "tennis" and p.get("kind") == "moneyline"
              and p.get("entry_rule") == current_label and float(p["opened"]) >= split]
    records = []
    for p in actual:
        m = p["slug"]
        g = groups.get(m)
        if g is None:
            continue
        j = bisect.bisect_right(times[m], float(p["opened"])) - 1
        if j < 0 or float(p["opened"]) - times[m][j] > 600:
            continue
        row = g.iloc[j]
        chosen_sign = 1 if p["side"] == "long" else -1
        if int(row["sign"]) != chosen_sign:
            continue
        features = row[RAW_FEATURES].to_numpy(dtype=float).reshape(1, -1)
        score = float(model.predict(features)[0])
        entry = float(p["entry_px"])
        records.append((p, score, entry * int(p["qty"])))
    print(f"\nCURRENT FIRST-TOUCH TRADES AFTER {args.split_date}: "
          f"joined {len(records)}/{len(actual)} closed trades using only prior features")
    if records:
        base_pnl = sum(float(p["pnl"]) for p, _, _ in records)
        base_stake = sum(s for _, _, s in records)
        print(f"  joined baseline: n={len(records)} P&L=${base_pnl:.2f} "
              f"stake=${base_stake:.2f} ROI={100*base_pnl/base_stake:.1f}%")
        train_candidates = train[(train["mid_prob"] + train["spread"]/2 >= .01) &
                                 (train["mid_prob"] + train["spread"]/2 <= .05)]
        # Fixed thresholds are set from training scores, never test outcomes.
        train_candidates = train_candidates.copy()
        train_candidates["candidate_score"] = model.predict(
            train_candidates[RAW_FEATURES].to_numpy())
        for q in (.50, .75, .90):
            threshold = float(train_candidates["candidate_score"].quantile(q))
            selected = [(p, s, stake) for p, s, stake in records if s >= threshold]
            if not selected:
                print(f"  score >= train candidate q{q:.2f} ({threshold:+.5f}): n=0")
                continue
            pnl = sum(float(p["pnl"]) for p, _, _ in selected)
            stake = sum(x[2] for x in selected)
            print(f"  score >= train candidate q{q:.2f} ({threshold:+.5f}): n={len(selected):3} "
                  f"P&L=${pnl:7.2f} stake=${stake:.2f} ROI={100*pnl/stake:+6.1f}% "
                  f"wins={sum(float(p['pnl'])>0 for p,_,_ in selected)}")
            for day in sorted({datetime.fromtimestamp(float(p["opened"]), timezone.utc).strftime("%Y-%m-%d")
                               for p, _, _ in selected}):
                daily = [(p, s, st) for p, s, st in selected
                         if datetime.fromtimestamp(float(p["opened"]), timezone.utc).strftime("%Y-%m-%d") == day]
                dp = sum(float(p["pnl"]) for p, _, _ in daily)
                ds = sum(st for _, _, st in daily)
                print(f"    {day}: n={len(daily):3} P&L=${dp:7.2f} "
                      f"stake=${ds:.2f} ROI={100*dp/ds:+6.1f}%")
            if q == .75:
                # Cluster bootstrap by match slug; repeat signals in the same
                # match are correlated, so do not treat them as independent.
                by_market = defaultdict(lambda: [0.0, 0.0])
                for p, _, st in selected:
                    by_market[p["slug"]][0] += float(p["pnl"])
                    by_market[p["slug"]][1] += st
                keys = list(by_market)
                rng = np.random.default_rng(20260923)
                vals = []
                for _ in range(4000):
                    draw = rng.choice(keys, size=len(keys), replace=True)
                    pn = sum(by_market[k][0] for k in draw)
                    st = sum(by_market[k][1] for k in draw)
                    vals.append(100 * pn / st if st else 0.0)
                lo, hi = np.quantile(vals, [.025, .975])
                print(f"    match-cluster bootstrap 95% ROI interval: [{lo:+.1f}%, {hi:+.1f}%]")


if __name__ == "__main__":
    main()
