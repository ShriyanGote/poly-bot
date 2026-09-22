#!/usr/bin/env python
"""Compare tennis entry rules on the recorded tape.

Three rules, same exit (the live trailing stop), same markets:

    first-touch   buy the first tick inside the 1-5% band
    bounce        dip to <=0.04, then buy when it comes back to >=0.05
    bounce+hold   as above, but it must STAY at >=0.05 for N seconds  (LIVE)

This is a replay rather than something bolted into the recorder: the engine
already has enough moving parts, and the tape holds everything needed.

    ./entryrules.py                 # everything recorded
    ./entryrules.py --since 03:00   # only entries after that UTC time today
    ./entryrules.py --sport tennis --hold 30
"""

import argparse
import math
import sys
from collections import defaultdict, deque
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bot import config, tapes  # noqa: E402


def load(sport):
    paths = defaultdict(list)
    for r in tapes.read("tape", sport):
        m = r.get("market") or ""
        if config.LS_MONEYLINE_ONLY and not m.startswith("aec-"):
            continue
        try:
            ts = datetime.fromisoformat(r["ts"]).timestamp()
            b, a = Decimal(r["bid"]), Decimal(r["ask"])
        except (ValueError, KeyError, TypeError):
            continue
        if b <= 0 or a <= 0 or a <= b:
            continue
        paths[m].append((ts, b, a))
    for v in paths.values():
        v.sort()
    return paths


def replay(paths, rule, lo, hi, dip, back, hold, arm, draw, since, min_ticks=0):
    """One entry per market-side, exited by the live trailing stop."""
    out = []
    for mk, v in paths.items():
        for side in ("long", "short"):
            entry = None
            pnl = None
            armed = False
            up = None
            recent = deque()
            for ts, b, a in v:
                recent.append(ts)
                while recent and ts - recent[0] > config.MIN_TICKS_WINDOW:
                    recent.popleft()
                price = a if side == "long" else Decimal(1) - b
                exitp = b if side == "long" else Decimal(1) - a
                if entry is None:
                    if exitp <= 0:
                        continue
                    if rule == "first-touch":
                        if lo <= price <= hi:
                            entry, at = price, ts
                    else:
                        if lo <= price <= dip:
                            armed, up = True, None
                        elif armed and price >= back:
                            if up is None:
                                up = ts
                            ready = rule == "bounce" or not hold or ts - up >= hold
                            if ready and price <= hi:
                                entry, at = price, ts
                        else:
                            up = None
                    # A dead book cannot produce a run, so the live rule will
                    # not buy into one. Applied after the entry test so it
                    # only rejects trades that would otherwise be taken.
                    if entry is not None and min_ticks and len(recent) < min_ticks:
                        entry = None
                        continue
                    if entry is None:
                        continue
                    if since and at < since:      # entered before the window
                        entry = None
                        continue
                    qty = int(config.LS_STAKE / entry)
                    peak = exitp
                    continue
                if exitp > peak:
                    peak = exitp
                if peak >= entry * arm and exitp <= peak * (Decimal(1) - draw):
                    pnl = (exitp - entry) * qty
                    break
            if entry is None:
                continue
            if pnl is None:
                pnl = (Decimal(0) - entry) * qty     # never ran; scored at zero
            out.append((pnl, entry * qty, peak, entry, at, mk, side))
    return out


def report(label, t, base_n):
    if not t:
        print(f"  {label:32}{0:>5}{'-':>9}{'-':>7}{'-':>6}{'-':>8}")
        return
    p = [float(x[0]) for x in t]
    n = len(p)
    mean = sum(p) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in p) / (n - 1)) if n > 1 else 0
    se = sd / math.sqrt(n) if n > 1 else 0
    staked = float(sum(x[1] for x in t))
    wins = sum(1 for x in p if x > 0)
    vol = f"{n / base_n * 100:.0f}%" if base_n else "-"
    print(f"  {label:32}{n:>5}{sum(p) / staked * 100:>8.0f}%"
          f"{(mean / se if se else 0):>7.2f}{wins / n * 100:>5.0f}%{vol:>8}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", default="tennis")
    ap.add_argument("--hold", type=float, help="override the hold seconds")
    ap.add_argument("--since", help="UTC HH:MM today; only entries after this")
    ap.add_argument("--trades", action="store_true", help="list every trade")
    a = ap.parse_args()

    r = config.rules_for(a.sport)
    hold = a.hold if a.hold is not None else r.hold_secs
    dip = r.dip_to or Decimal("0.04")
    back = r.buy_back or Decimal("0.05")

    since = None
    if a.since:
        h, m = a.since.split(":")
        now = datetime.now(timezone.utc)
        since = now.replace(hour=int(h), minute=int(m), second=0,
                            microsecond=0).timestamp()

    paths = load(a.sport)
    print(f"{a.sport}: {len(paths)} moneyline markets"
          + (f", entries after {a.since} UTC" if since else "") + "\n")

    mt = r.min_ticks
    runs = [("first-touch (original)", "first-touch", 0),
            (f"bounce {float(dip):.2f}->{float(back):.2f}", "bounce", 0),
            ("first-touch + activity", "first-touch", mt or 100),
            (f"{config.rule_label(a.sport)}  (LIVE)", "hold" if hold else "bounce", mt)]
    base = replay(paths, "first-touch", r.band_lo, r.band_hi, dip, back,
                  hold, r.arm, r.drawdown, since)
    print(f"  {'rule':32}{'n':>5}{'ROI':>9}{'t':>7}{'win%':>6}{'volume':>8}")
    print("  " + "-" * 67)
    results = {}
    for label, kind, mticks in runs:
        t = (base if kind == "first-touch" and not mticks else
             replay(paths, kind, r.band_lo, r.band_hi, dip, back, hold,
                    r.arm, r.drawdown, since, mticks))
        results[label] = t
        report(label, t, len(base))

    if a.trades:
        for label, kind, _mt in runs:
            t = results[label]
            if not t:
                continue
            print(f"\n{label}: {len(t)} trades")
            for pnl, _, peak, e, at, mk, side in sorted(t, key=lambda x: x[4]):
                when = datetime.fromtimestamp(at, timezone.utc).strftime("%H:%M:%S")
                print(f"  {when}  {side:5} entry {float(e):.2f} peak {float(peak):.2f} "
                      f"{float(peak / e):>4.1f}x  pnl {float(pnl):+6.2f}  {mk[:40]}")


if __name__ == "__main__":
    main()
