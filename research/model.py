#!/usr/bin/env python
"""Can we predict short-horizon football price moves?

Splits by GAME, never by row: samples inside one game are heavily
autocorrelated, so a random split leaks the answer and inflates every metric.
Reports out-of-sample results only, and converts them into money at the end -
a classifier that beats 50% but not the spread is worthless.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.metrics import roc_auc_score

HERE = Path(__file__).resolve().parent
TICK = 0.005          # football quotes in half cents
SPREAD_COST = 0.005   # one tick round trip, crossing once

FEATS = ["book_imb", "depth", "spread", "vol_120",
         "flow_imb_10", "flow_imb_30", "flow_imb_120",
         "flow_vol_10", "flow_vol_30", "flow_vol_120",
         "flow_n_30", "mom_10", "mom_30", "mom_120", "mid"]


def load():
    p = HERE / "football_features.csv"
    if not p.exists():
        p = HERE / "football_features.parquet"
        return pd.read_parquet(p)
    return pd.read_csv(p)


def split_by_game(df, holdout=0.35, seed=0):
    games = sorted(df["game"].unique())
    rng = np.random.default_rng(seed)
    rng.shuffle(games)
    n_test = max(1, int(len(games) * holdout))
    test_games = set(games[:n_test])
    tr = df[~df["game"].isin(test_games)]
    te = df[df["game"].isin(test_games)]
    return tr, te, sorted(test_games)


def evaluate(name, y_true, y_pred, mids):
    """Direction accuracy, AUC, and money after costs."""
    up = (y_true > 0).astype(int)
    moved = y_true != 0
    auc = roc_auc_score(up[moved], y_pred[moved]) if moved.sum() > 50 else float("nan")

    out = [f"  {name}"]
    out.append(f"    AUC (direction)          {auc:.4f}")

    # trade only the most confident signals
    for q in (0.10, 0.05, 0.02):
        hi = np.quantile(y_pred, 1 - q)
        lo = np.quantile(y_pred, q)
        longs = y_true[y_pred >= hi]
        shorts = -y_true[y_pred <= lo]
        both = np.concatenate([longs, shorts])
        if len(both) < 50:
            continue
        gross = both.mean()
        net = gross - SPREAD_COST
        wr = (both > 0).mean()
        out.append(f"    top/bottom {q:>4.0%}  n={len(both):>6}  "
                   f"win {wr:>5.1%}  gross {gross:+.5f}  net {net:+.5f}  "
                   f"({net/TICK:+.2f} ticks)")
    return "\n".join(out)


def main():
    df = load().dropna(subset=FEATS + ["fwd_60"])
    print(f"rows {len(df):,}  markets {df['market'].nunique()}  games {df['game'].nunique()}\n")

    for horizon in (30, 60, 180):
        target = f"fwd_{horizon}"
        if target not in df.columns:
            continue
        d = df.dropna(subset=[target])
        tr, te, test_games = split_by_game(d)
        print(f"=== horizon {horizon}s ===")
        print(f"train {len(tr):,} rows / {tr['game'].nunique()} games   "
              f"test {len(te):,} rows / {te['game'].nunique()} games")

        Xtr, ytr = tr[FEATS].values, tr[target].values
        Xte, yte = te[FEATS].values, te[target].values
        mids = te["mid"].values

        # baseline: flow imbalance alone, no model
        print(evaluate("flow_imb_30 alone (no model)", yte, te["flow_imb_30"].values, mids))

        mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
        ridge = Ridge(alpha=1.0).fit((Xtr - mu) / sd, ytr)
        print(evaluate("ridge", yte, ridge.predict((Xte - mu) / sd), mids))

        gb = GradientBoostingRegressor(n_estimators=150, max_depth=3,
                                       learning_rate=0.05, subsample=0.8,
                                       random_state=0).fit(Xtr, ytr)
        pred = gb.predict(Xte)
        print(evaluate("gradient boosting", yte, pred, mids))

        imp = sorted(zip(FEATS, gb.feature_importances_), key=lambda x: -x[1])
        print("    top features: " + ", ".join(f"{k}={v:.2f}" for k, v in imp[:6]))
        print()


if __name__ == "__main__":
    main()
