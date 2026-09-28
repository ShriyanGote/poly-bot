#!/usr/bin/env python
"""Per-sport diagnosis of the longshot book. Run this after a week.

Reports what actually decides the strategy - how often a trade reaches the
arm, what it pays when it does, and what break-even would require - rather
than ROI, which one trade can swing by forty points.
"""
import json
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bot import config  # noqa: E402


def f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def main():
    st = json.loads(config.LONGSHOT_STATE.read_text())
    closed = st.get("closed", [])
    sports = sorted({c.get("sport") for c in closed if c.get("sport")})
    print(f"{len(closed)} closed trades\n")
    print(f"  {'sport':11}{'n':>5}{'hit arm':>9}{'avg win':>9}{'need':>7}"
          f"{'pnl':>9}{'ROI':>7}{'95% CI on ROI':>18}")
    print("  " + "-" * 76)
    for sp in sports:
        g = [c for c in closed if c.get("sport") == sp and f(c.get("entry_px"))]
        if len(g) < 5:
            continue
        r = config.rules_for(sp)
        arm = float(r.arm)
        hit = [c for c in g if f(c.get("peak_px")) >= arm * f(c["entry_px"])]
        W = [c for c in g if f(c.get("pnl")) > 0]
        L = [c for c in g if f(c.get("pnl")) <= 0]
        stk = sum(f(c["entry_px"]) * f(c["qty"]) for c in g)
        pl = sum(f(c.get("pnl")) for c in g)
        aw = sum(f(c["pnl"]) for c in W) / len(W) if W else 0.0
        need = len(L) / len(W) if W else float("inf")
        p = [f(c["pnl"]) for c in g]
        random.seed(11)
        boot = sorted(sum(random.choice(p) for _ in p) / stk * 100
                      for _ in range(2000)) if stk else [0, 0]
        ci = f"{boot[50]:+.0f}%..{boot[1950]:+.0f}%"
        flag = "" if boot[1950] > 0 else "  <- loses"
        print(f"  {sp:11}{len(g):>5}{len(hit)/len(g)*100:>8.1f}%{aw:>9.2f}"
              f"{need:>7.2f}{pl:>9.2f}{pl/stk*100:>6.0f}%{ci:>18}{flag}")
    print("\n  hit arm = share that reached the trail's arm; that is the only"
          "\n  group that ever pays. need = what an average winner must return"
          "\n  to break even at that hit rate.")
    print("  A sport is only worth trading if avg win > need, with the CI above zero.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
