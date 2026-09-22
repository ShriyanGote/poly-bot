#!/usr/bin/env python
"""Re-simulate the CURRENT strategy's trades under different exit parameters.

Only positions opened after LS_TRAIL_SINCE, replayed against their real price
paths, so every variant is scored on identical entries.
"""

import json
import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bot import config, tapes  # noqa: E402


def paths(sports):
    out = defaultdict(list)
    for sp in sports:
        for r in tapes.read("tape", sp):
            try:
                ts = datetime.fromisoformat(r["ts"]).timestamp()
                bid, ask = Decimal(r["bid"]), Decimal(r["ask"])
            except Exception:
                continue
            if bid <= 0 or ask <= 0:
                continue
            out[r["market"]].append((ts, bid, Decimal("1") - ask))
    for v in out.values():
        v.sort()
    return out


def run(seq, entry, qty, arm, dd):
    """Trailing exit: arm at `arm` x entry, sell on `dd` drawdown from peak."""
    peak = entry
    for px in seq:
        if px > peak:
            peak = px
        if peak >= entry * arm and px <= peak * (Decimal(1) - dd):
            return (px - entry) * qty
    return ((seq[-1] if seq else entry) - entry) * qty


def main():
    s = json.loads(config.LONGSHOT_STATE.read_text())
    pos = [c for c in s.get("closed", []) + list(s.get("positions", {}).values())
           if c.get("opened", 0) >= config.LS_TRAIL_SINCE]
    sports = sorted({p["sport"] for p in pos})
    P = paths(sports)

    entries = []
    for p in pos:
        v = P.get(p["slug"])
        if not v:
            continue
        seq = [(b if p["side"] == "long" else sh)
               for ts, b, sh in v if ts >= p["opened"]]
        if len(seq) < 5:
            continue
        entries.append((Decimal(p["entry_px"]), p["qty"], seq, p["sport"]))

    if not entries:
        print("no entries with path coverage")
        return

    def score(sel, arm, dd):
        st = sum(e * q for e, q, _, _ in sel)
        pl = sum(run(sq, e, q, arm, dd) for e, q, sq, _ in sel)
        return pl, st

    print(f"{len(entries)} positions with price paths "
          f"(${sum(e*q for e,q,_,_ in entries):.2f} staked)\n")

    print("DRAWDOWN SENSITIVITY  (ROI %, rows = arm, cols = drawdown)")
    dds = [Decimal(x) for x in ("0.15", "0.25", "0.35", "0.50", "0.65")]
    print("      " + "".join(f"{int(d*100):>8}%" for d in dds))
    print("-" * 52)
    for arm in [Decimal(x) for x in ("1.5", "2", "3", "4", "6")]:
        row = []
        for dd in dds:
            pl, st = score(entries, arm, dd)
            row.append(f"{float(pl/st*100) if st else 0:>8.0f}")
        print(f"{float(arm):>5.1f}x" + "".join(row))

    print("\nBY SPORT  (arm 4x / 50% drawdown, current setting)")
    print(f"{'sport':12}{'n':>4}{'staked':>9}{'pnl':>9}{'ROI':>8}")
    for sp in sports:
        sel = [e for e in entries if e[3] == sp]
        if not sel:
            continue
        pl, st = score(sel, config.LS_TRAIL_ARM, config.LS_TRAIL_DRAWDOWN)
        print(f"{sp:12}{len(sel):>4}{float(st):>9.2f}{float(pl):>9.2f}"
              f"{float(pl/st*100) if st else 0:>7.0f}%")

    print("\nBEST PARAMS PER SPORT")
    for sp in sports:
        sel = [e for e in entries if e[3] == sp]
        if len(sel) < 4:
            continue
        best = None
        for arm in [Decimal(x) for x in ("1.5", "2", "3", "4", "6")]:
            for dd in dds:
                pl, st = score(sel, arm, dd)
                roi = (pl / st * 100) if st else Decimal(0)
                if best is None or roi > best[0]:
                    best = (roi, arm, dd, pl)
        print(f"  {sp:12} arm {float(best[1]):>4.1f}x  dd {float(best[2])*100:>3.0f}%  "
              f"-> {float(best[0]):>+5.0f}%  (${float(best[3]):+.2f})")


if __name__ == "__main__":
    main()
