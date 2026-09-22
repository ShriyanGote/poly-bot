#!/usr/bin/env python
"""True hindsight ceiling for longshot positions.

The peak stored on a position stops at whatever exit we took, so it understates
what was available. This reconstructs the real maximum from the tape after each
entry, which separates "the entries are bad" from "the exit rule is bad".
"""

import json
import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bot import config, tapes  # noqa: E402


def main():
    sport = sys.argv[1] if len(sys.argv) > 1 else "tennis"
    s = json.loads(config.LONGSHOT_STATE.read_text())
    pos = [c for c in s.get("closed", []) if c["sport"] == sport]
    if not pos:
        print(f"no closed {sport} positions")
        return
    want = {p["slug"] for p in pos}

    series = defaultdict(list)
    for r in tapes.read("tape", sport):
        if r["market"] not in want:
            continue
        try:
            ts = datetime.fromisoformat(r["ts"]).timestamp()
            bid, ask = Decimal(r["bid"]), Decimal(r["ask"])
        except Exception:
            continue
        if bid <= 0 or ask <= 0:
            continue
        series[r["market"]].append((ts, bid, ask))
    for v in series.values():
        v.sort()

    rows, no_tape = [], 0
    for p in pos:
        v = series.get(p["slug"])
        if not v:
            no_tape += 1
            continue
        t0 = p["opened"]
        entry = Decimal(p["entry_px"])
        qty = p["qty"]
        best = None
        for ts, bid, ask in v:
            if ts < t0:
                continue
            px = bid if p["side"] == "long" else (Decimal("1") - ask)
            if best is None or px > best:
                best = px
        if best is None:
            no_tape += 1
            continue
        rows.append({
            "slug": p["slug"], "game": p["slug"], "entry": entry, "qty": qty,
            "peak": best, "mult": float(best / entry) if entry else 0,
            "actual": Decimal(p["pnl"]),
            "peak_pnl": (best - entry) * qty,
        })

    staked = sum(r["entry"] * r["qty"] for r in rows)
    actual = sum(r["actual"] for r in rows)
    peak = sum(r["peak_pnl"] for r in rows)
    print(f"{sport}: {len(rows)} positions with tape coverage "
          f"({no_tape} without)\n")
    print(f"  staked            ${staked:.2f}")
    print(f"  actual pnl        ${actual:+.2f}   ({100*actual/staked:+.0f}%)")
    print(f"  if sold at peak   ${peak:+.2f}   ({100*peak/staked:+.0f}%)")
    print(f"  left on the table ${peak-actual:+.2f}\n")

    buckets = [(1, 2), (2, 3), (3, 5), (5, 10), (10, 1e9)]
    print("  PEAK MULTIPLE DISTRIBUTION")
    for lo, hi in buckets:
        sel = [r for r in rows if lo <= r["mult"] < hi]
        if not sel:
            continue
        lbl = f"{lo}-{hi}x" if hi < 1e9 else f"{lo}x+"
        print(f"    {lbl:>8}  {len(sel):>4} positions  "
              f"({100*len(sel)/len(rows):>4.0f}%)  "
              f"peak pnl ${sum(r['peak_pnl'] for r in sel):>8.2f}")
    never = [r for r in rows if r["mult"] <= 1.0]
    print(f"    {'<=1x':>8}  {len(never):>4} positions  "
          f"({100*len(never)/len(rows):>4.0f}%)  never traded above entry")

    print("\n  TOP 10 BY PEAK VALUE")
    print(f"    {'mult':>6}{'entry':>7}{'peak':>7}{'actual':>9}{'at peak':>9}  market")
    for r in sorted(rows, key=lambda x: -x["peak_pnl"])[:10]:
        print(f"    {r['mult']:>5.1f}x{float(r['entry']):>7.2f}{float(r['peak']):>7.2f}"
              f"{float(r['actual']):>9.2f}{float(r['peak_pnl']):>9.2f}  {r['slug'][:38]}")


if __name__ == "__main__":
    main()
