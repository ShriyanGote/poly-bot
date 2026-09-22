#!/usr/bin/env python
"""Can the duds be filtered at entry, and how fast do movers move?

~80% of longshot entries never trade above the price paid. If those look
different at entry, a filter beats any exit-rule tuning. And if movers declare
themselves quickly, a time-based bail-out caps the cost of the rest.
"""

import json
import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bot import config, tapes  # noqa: E402


def load():
    s = json.loads(config.LONGSHOT_STATE.read_text())
    closed = s.get("closed", [])
    want = {c["slug"] for c in closed}

    rows = defaultdict(list)
    for sp in ("tennis", "esports", "soccer", "other", "basketball", "football"):
        for r in tapes.read("tape", sp):
            if r["market"] not in want:
                continue
            try:
                ts = datetime.fromisoformat(r["ts"]).timestamp()
                bid, ask = Decimal(r["bid"]), Decimal(r["ask"])
            except Exception:
                continue
            if bid <= 0 or ask <= 0:
                continue
            rows[r["market"]].append({
                "ts": ts, "bid": bid, "ask": ask,
                "spread": ask - bid,
                "bt": Decimal(r.get("bid_total") or 0),
                "at": Decimal(r.get("ask_total") or 0),
                "period": r.get("period", ""),
            })
    for v in rows.values():
        v.sort(key=lambda x: x["ts"])
    return closed, rows


def main():
    closed, rows = load()
    recs = []
    for p in closed:
        series = rows.get(p["slug"])
        if not series:
            continue
        t0 = p["opened"]
        after = [r for r in series if r["ts"] >= t0]
        before = [r for r in series if r["ts"] < t0][-40:]
        if len(after) < 5:
            continue
        entry = Decimal(p["entry_px"])
        side = p["side"]

        def px(r):
            return r["bid"] if side == "long" else (Decimal("1") - r["ask"])

        path = [(r["ts"] - t0, px(r)) for r in after]
        peak = max(x[1] for x in path)
        mult = float(peak / entry) if entry else 0

        # time to first reach 1.5x / 2x / 4x
        def first_at(m):
            tgt = entry * Decimal(str(m))
            for dt, v in path:
                if v >= tgt:
                    return dt
            return None

        at_entry = after[0]
        prior = [px(r) for r in before] or [entry]
        recs.append({
            "sport": p["sport"], "kind": p["kind"], "mult": mult,
            "entry": float(entry),
            "spread": float(at_entry["spread"]),
            "depth": float(at_entry["bt"] + at_entry["at"]),
            "imb": float((at_entry["bt"] - at_entry["at"]) /
                         (at_entry["bt"] + at_entry["at"]))
            if (at_entry["bt"] + at_entry["at"]) else 0.0,
            "prior_range": float((max(prior) - min(prior)) / entry) if entry else 0,
            "t15": first_at(1.5), "t2": first_at(2), "t4": first_at(4),
            "period": at_entry["period"],
        })

    if not recs:
        print("no positions with price paths")
        return

    movers = [r for r in recs if r["mult"] >= 2]
    duds = [r for r in recs if r["mult"] < 1.01]
    mid = [r for r in recs if 1.01 <= r["mult"] < 2]
    print(f"{len(recs)} settled positions with paths")
    print(f"  movers (>=2x peak) : {len(movers):>3}  ({100*len(movers)/len(recs):.0f}%)")
    print(f"  small (1-2x)       : {len(mid):>3}")
    print(f"  duds (never >1x)   : {len(duds):>3}  ({100*len(duds)/len(recs):.0f}%)")

    print("\nHOW FAST DO MOVERS MOVE?  (seconds after entry)")
    for lbl, key in (("reach 1.5x", "t15"), ("reach 2x", "t2"), ("reach 4x", "t4")):
        vals = [r[key] for r in recs if r[key] is not None]
        if not vals:
            continue
        v = np.array(vals)
        print(f"  {lbl:10} n={len(v):>3}   p25 {np.quantile(v,.25):>6.0f}s   "
              f"median {np.median(v):>6.0f}s   p75 {np.quantile(v,.75):>6.0f}s   "
              f"max {v.max():>6.0f}s")

    print("\nENTRY FEATURES: movers vs duds")
    print(f"  {'feature':14}{'movers':>12}{'duds':>12}{'ratio':>9}")
    for f in ("entry", "spread", "depth", "imb", "prior_range"):
        m = np.median([r[f] for r in movers]) if movers else 0
        d = np.median([r[f] for r in duds]) if duds else 0
        ratio = (m / d) if d else float("inf")
        print(f"  {f:14}{m:>12.4f}{d:>12.4f}{ratio:>9.2f}")

    print("\nBY MARKET KIND")
    for kind in sorted({r["kind"] for r in recs}):
        sel = [r for r in recs if r["kind"] == kind]
        mv = [r for r in sel if r["mult"] >= 2]
        print(f"  {kind:12}{len(sel):>4} positions   "
              f"{len(mv):>3} movers ({100*len(mv)/len(sel):>3.0f}%)")

    print("\nBY SPORT")
    for sp in sorted({r["sport"] for r in recs}):
        sel = [r for r in recs if r["sport"] == sp]
        mv = [r for r in sel if r["mult"] >= 2]
        print(f"  {sp:12}{len(sel):>4} positions   "
              f"{len(mv):>3} movers ({100*len(mv)/len(sel):>3.0f}%)")


if __name__ == "__main__":
    main()
