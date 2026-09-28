#!/usr/bin/env python3
"""Leakage-aware search for entry filters on recorded tennis longshot trades.

Use quote features timestamped no later than each recorded entry. Train on
trades before --split-date and evaluate once on later trades. Outcomes are the
actual recorded exit P&L for the fixed strategy, not hypothetical peak exits.
"""
import argparse
import bisect
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import sys

import numpy as np
import pandas as pd
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression
from sklearn.tree import DecisionTreeClassifier, export_text
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.model_selection import StratifiedGroupKFold, cross_val_predict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot import config

FEATURES = [
    "book_imb", "spread", "depth", "entry_depth", "exit_depth", "vol_120",
    "flow_imb_10", "flow_imb_30", "flow_imb_120", "flow_vol_10",
    "flow_vol_30", "flow_vol_120", "flow_n_30", "mom_10", "mom_30",
    "mom_120", "mid_prob",
]


def oriented(df):
    d = df.copy()
    d["sign"] = np.where(d.mid.to_numpy() <= .5, 1., -1.)
    d["mid_prob"] = np.minimum(d.mid, 1-d.mid)
    d["entry_depth"] = np.where(d.sign > 0, d.ask_depth, d.bid_depth)
    d["exit_depth"] = np.where(d.sign > 0, d.bid_depth, d.ask_depth)
    d["book_imb"] *= d.sign
    for c in ("flow_imb_10", "flow_imb_30", "flow_imb_120", "mom_10", "mom_30", "mom_120"):
        d[c] *= d.sign
    for c in FEATURES:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("features", type=Path)
    ap.add_argument("--split-date", default="2026-09-23")
    ap.add_argument("--max-feature-age", type=float, default=30)
    args = ap.parse_args()

    d = oriented(pd.read_csv(args.features).replace([np.inf, -np.inf], np.nan))
    d = d.dropna(subset=FEATURES + ["game"])
    groups = {m: g.sort_values("ts") for m, g in d.groupby("market", sort=False)}
    times = {m: g.ts.to_numpy() for m, g in groups.items()}
    state = json.loads(config.LONGSHOT_STATE.read_text())
    rule = config.rule_label("tennis")
    all_trades = [p for p in state.get("closed", [])
                  if p.get("sport") == "tennis" and p.get("kind") == "moneyline"
                  and .01 <= float(p.get("entry_px") or 0) <= .05]
    # The legacy unlabeled records used the original low-price first-touch
    # rule. Keep those plus the current rule as training examples; evaluate
    # only the current rule in the later holdout.
    trades = [p for p in all_trades
              if p.get("entry_rule") in (None, rule)]
    rows = []
    for p in trades:
        g = groups.get(p["slug"])
        if g is None:
            continue
        opened = float(p["opened"])
        j = bisect.bisect_right(times[p["slug"]], opened) - 1
        if j < 0:
            continue
        row = g.iloc[j]
        age = opened - float(row.ts)
        wanted_sign = 1 if p["side"] == "long" else -1
        if age < 0 or age > args.max_feature_age or int(row.sign) != wanted_sign:
            continue
        rec = {c: float(row[c]) for c in FEATURES}
        day = datetime.fromtimestamp(opened, timezone.utc).strftime("%Y-%m-%d")
        rows.append({**rec, "slug": p["slug"], "game": str(row.game), "day": day,
                     "opened": opened, "feature_age": age,
                     "pnl": float(p["pnl"]),
                     "stake": float(p["entry_px"]) * int(p["qty"]),
                     "win": int(float(p["pnl"]) > 0),
                     "entry_rule": p.get("entry_rule"),
                     "peak2": int(float(p.get("peak_px") or 0) /
                                  max(float(p["entry_px"]), 1e-9) >= 2),
                     "peak_mult": float(p.get("mult") or 0),
                     "entry_px": float(p["entry_px"]),
                     "reason": p.get("reason", "")})
    z = pd.DataFrame(rows)
    if z.empty:
        raise SystemExit("No entry rows joined")
    split = pd.Timestamp(args.split_date, tz="UTC")
    train = z[z.day < args.split_date].copy()
    test = z[(z.day >= args.split_date) & (z.entry_rule == rule)].copy()
    train_games = set(train.game)
    seen = test.game.isin(train_games) & ~test.game.isin(["", "?"])
    n_seen = int(seen.sum())
    test = test[~seen].copy()

    def summary(label, x):
        if not len(x):
            print(f"  {label}: n=0")
            return
        pnl, stake = x.pnl.sum(), x.stake.sum()
        print(f"  {label:24} n={len(x):3} wins={int(x.win.sum()):3} "
              f"win%={x.win.mean()*100:5.1f} pnl=${pnl:+7.2f} "
              f"stake=${stake:7.2f} ROI={pnl/stake*100:+6.1f}%")

    print(f"CURRENT RULE: {rule}")
    print(f"joined {len(z)}/{len(trades)} eligible historical first-touch trades; "
          f"later test is current rule only; max quote age {args.max_feature_age:.0f}s; "
          f"dropped {n_seen} test trades from games seen in training")
    print(f"mean feature age train/test: {train.feature_age.mean():.2f}s / {test.feature_age.mean():.2f}s")
    print(f"train {train.day.min()}..{train.day.max()} n={len(train)}; "
          f"test {test.day.min()}..{test.day.max()} n={len(test)}")
    print(f"  historical training trades that later peaked >=2x: {int(train.peak2.sum())}/{len(train)} "
          f"({train.peak2.mean()*100:.1f}%)")
    summary("train baseline", train)
    summary("test baseline", test)
    for day, g in test.groupby("day", sort=True):
        summary("test " + day, g)
    if len(train) < 50 or len(test) < 30 or train.peak2.nunique() < 2:
        raise SystemExit("Too few joined trades for this split")

    Xtr, Xte = train[FEATURES].to_numpy(), test[FEATURES].to_numpy()
    ytr = train.peak2.to_numpy()
    cv_groups = train.game.where(~train.game.isin(["", "?"]), train.slug).to_numpy()
    cv = StratifiedGroupKFold(n_splits=4, shuffle=True, random_state=9)
    models = {
        "logistic": make_pipeline(StandardScaler(), LogisticRegression(C=.1, max_iter=2000)),
        "shallow tree": DecisionTreeClassifier(max_depth=2, min_samples_leaf=20,
                                                class_weight="balanced", random_state=9),
        "boosted trees": HistGradientBoostingClassifier(max_iter=80, max_leaf_nodes=5,
                                                       l2_regularization=2, random_state=9),
    }

    def show_selected(label, sel):
        summary(label, sel)
        if not len(sel):
            return
        print(f"    peak >=2x: {int(sel.peak2.sum())}/{len(sel)} ({sel.peak2.mean()*100:.1f}%)")
        for day, daily in sel.groupby("day", sort=True):
            summary("    " + day, daily)
        by_market = defaultdict(lambda: [0., 0.])
        for _, r in sel.iterrows():
            by_market[r.slug][0] += float(r.pnl)
            by_market[r.slug][1] += float(r.stake)
        keys = list(by_market)
        rng = np.random.default_rng(20260924 + len(sel))
        boot = []
        for _ in range(5000):
            draw = rng.choice(keys, size=len(keys), replace=True)
            pn = sum(by_market[k][0] for k in draw)
            st = sum(by_market[k][1] for k in draw)
            boot.append(100*pn/st if st else 0.)
        lo, hi = np.quantile(boot, [.025, .975])
        print(f"    match-cluster bootstrap 95% ROI interval: [{lo:+.1f}%, {hi:+.1f}%]")

    for name, model in models.items():
        # Define score cutoffs using out-of-fold training predictions so the
        # calibration sample is not scored by a model fitted on itself.
        oof_score = cross_val_predict(model, Xtr, ytr, groups=cv_groups,
                                      cv=cv, method="predict_proba", n_jobs=1)[:, 1]
        model.fit(Xtr, ytr)
        te_score = model.predict_proba(Xte)[:, 1]
        print(f"\n{name}")
        print("  cutoffs from grouped out-of-fold training scores:")
        for q in (.5, .75, .9):
            cutoff = float(np.quantile(oof_score, q))
            mask = te_score >= cutoff
            sel = test.loc[mask]
            show_selected(f"test score >= train q{q:.2f} ({cutoff:.3f})", sel)
        if name == "shallow tree":
            print(export_text(model, feature_names=FEATURES, decimals=3))

    # Hand-checkable one-feature gates selected on training only. Require at
    # least 25% coverage of train; select by ROI, then test exactly once.
    candidates = []
    for feat in FEATURES:
        vals = train[feat].to_numpy()
        for q in (.25, .4, .5, .6, .75):
            cut = float(np.quantile(vals, q))
            for op, mask in (("ge", vals >= cut), ("le", vals <= cut)):
                if mask.mean() < .25 or mask.sum() < 30:
                    continue
                sub = train.loc[mask]
                precision = sub.peak2.mean()
                candidates.append((precision, int(mask.sum()), feat, op, cut))
    candidates.sort(reverse=True)
    if candidates:
        _, _, feat, op, cut = candidates[0]
        print("\nBEST SINGLE-FEATURE RULE selected on training only")
        print(f"  {feat} {op} {cut:.6g}")
        trsel = train[train[feat] >= cut if op == "ge" else train[feat] <= cut]
        tsel = test[test[feat] >= cut if op == "ge" else test[feat] <= cut]
        print(f"  train peak >=2x: {int(trsel.peak2.sum())}/{len(trsel)} ({trsel.peak2.mean()*100:.1f}%)")
        summary("test selected", tsel)
        if len(tsel):
            print(f"  test peak >=2x: {int(tsel.peak2.sum())}/{len(tsel)} ({tsel.peak2.mean()*100:.1f}%)")


if __name__ == "__main__":
    main()
