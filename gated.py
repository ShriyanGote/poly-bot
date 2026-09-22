#!/usr/bin/env python
"""Does the gate set actually add edge, or is it decoration?

Replays the book tape in time order, feeds every update through the real
PriceHistory + evaluate() gates from bot/signals.py, and simulates an entry
whenever they pass. Compares the result against unfiltered entries drawn from
the identical tape and walked forward the identical way, so the only variable
is the gating.
"""

import csv
import gzip
import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bot import config, tapes  # noqa: E402
from bot.signals import PriceHistory, evaluate  # noqa: E402

TICK = Decimal("0.01")
MAX_LOOK = 6000
BASELINE_STEP = 25


def unpack(s):
    """'0.12:400|0.11:900' -> book levels shaped like the live API."""
    out = []
    for part in (s or "").split("|"):
        if not part or ":" not in part:
            continue
        px, qty = part.split(":", 1)
        try:
            out.append({"px": {"value": px}, "qty": qty})
        except Exception:
            continue
    return out


def load_rows(sport=None):
    rows = list(tapes.read("tape", sport))
    rows.sort(key=lambda r: r.get("ts", ""))
    return rows


def walk(pts, i, entry, target_t, stop_t):
    """Forward walk from index i. Returns 'w', 'l' or None."""
    up = entry + target_t * TICK
    dn = entry - stop_t * TICK
    for j in range(i + 1, min(len(pts), i + MAX_LOOK)):
        px = pts[j]
        if px >= up:
            return "w"
        if px <= dn or px <= 0:
            return "l"
    return None


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", help="restrict to one sport's tapes")
    args = ap.parse_args()
    rows = load_rows(args.sport)
    if args.sport:
        print(f"sport: {args.sport}")
    if not rows:
        print("no tape")
        return
    print(f"tape rows: {len(rows):,}\n")

    # exit-price series per (market, side), plus the index of each row
    series = defaultdict(list)
    row_idx = defaultdict(list)
    for r in rows:
        try:
            bid, ask = Decimal(r["bid"]), Decimal(r["ask"])
        except Exception:
            continue
        if bid <= 0 or ask <= 0:
            continue
        series[(r["market"], "long")].append(bid)
        row_idx[(r["market"], "long")].append(r)
        series[(r["market"], "short")].append(Decimal("1") - ask)
        row_idx[(r["market"], "short")].append(r)

    target_t, stop_t = config.EXIT_TICKS, config.STOP_TICKS

    # ---- gated pass -------------------------------------------------------
    hist = PriceHistory()
    gated = []
    counters = defaultdict(int)
    for key in series:
        market, side = key
        pts, rws = series[key], row_idx[key]
        for i, r in enumerate(rws):
            try:
                ts = datetime.fromisoformat(r["ts"]).timestamp()
            except Exception:
                continue
            px = pts[i]
            hkey = f"{market}|{side}"
            hist.add(hkey, ts, px)

            if not (config.BAND_LO <= px <= config.BAND_HI):
                continue
            qty = int(config.STAKE / px) if px > 0 else 0
            if qty <= 0:
                continue
            exit_levels = unpack(r["bid_levels"] if side == "long" else r["ask_levels"])
            ok, reasons, m = evaluate(px, px + target_t * TICK, Decimal(qty),
                                      exit_levels, hist, hkey, ts,
                                      (config.BAND_LO, config.BAND_HI))
            if not ok:
                for rs in reasons:
                    counters[rs.split("(")[0].strip()[:34]] += 1
                continue
            out = walk(pts, i, px, target_t, stop_t)
            if out:
                gated.append((out, px))

    # ---- unfiltered baseline on the same tape ------------------------------
    base = []
    for key in series:
        pts = series[key]
        for i in range(0, len(pts), BASELINE_STEP):
            px = pts[i]
            if not (config.BAND_LO <= px <= config.BAND_HI):
                continue
            out = walk(pts, i, px, target_t, stop_t)
            if out:
                base.append((out, px))

    def stats(trades, label):
        if not trades:
            print(f"{label}: no trades")
            return None
        w = sum(1 for o, _ in trades if o == "w")
        n = len(trades)
        p = Decimal(w) / n
        ev = p * target_t - (1 - p) * stop_t
        # dollar EV uses the real per-trade sizing
        dollars = Decimal("0")
        for o, px in trades:
            q = int(config.STAKE / px)
            dollars += (target_t * TICK * q) if o == "w" else (-stop_t * TICK * q)
        print(f"{label}")
        print(f"   trades {n:,}   wins {w:,}   P(win) {p:.3f}")
        print(f"   EV {ev:+.3f} ticks/trade   ${dollars/n:+.3f}/trade   total ${dollars:+.2f}")
        return ev

    print(f"target +{target_t} / stop -{stop_t}   band {config.BAND_LO}-{config.BAND_HI}\n")
    be = Decimal(stop_t) / (stop_t + target_t)
    print(f"break-even win rate needed: {be:.3f}\n")
    ev_g = stats(gated, "GATED (real signals.py filters)")
    print()
    ev_b = stats(base, "UNFILTERED baseline")
    if ev_g is not None and ev_b is not None:
        print(f"\ngates add {ev_g - ev_b:+.3f} ticks/trade vs baseline")
        print("VERDICT:", "gates help but still negative" if ev_g < 0 < ev_g - ev_b
              else ("gates are PROFITABLE" if ev_g > 0 else "gates do not rescue it"))

    print("\ntop gate rejections")
    for k, v in sorted(counters.items(), key=lambda x: -x[1])[:6]:
        print(f"   {v:>8,}  {k}")


if __name__ == "__main__":
    main()
