#!/usr/bin/env python
"""Replay every longshot entry against its real price path under several exit
rules, so achievable rules can be compared to the (unattainable) peak.

Rules are applied to the same entries and the same tape, so the only thing
that differs is when you sell.
"""

import json
import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bot import config, tapes  # noqa: E402


def load_paths(sport):
    """{market: [(ts, exitable_long, exitable_short)]} sorted by time."""
    out = defaultdict(list)
    for r in tapes.read("tape", sport):
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


def simulate(path, entry, qty, rule):
    """Walk the path; return realised P&L for one rule.

    `path` is the sequence of prices we could have SOLD at, in time order.
    """
    peak = entry
    half_done = False
    realised = Decimal("0")
    held = qty

    for px in path:
        if px > peak:
            peak = px

        kind = rule[0]
        if kind == "hold":
            continue

        if kind == "tp":
            if px >= entry * rule[1]:
                return realised + (px - entry) * held
        elif kind == "trail":
            # arm only once in profit, then sell on a drawdown from the peak
            if peak >= entry * rule[1] and px <= peak * (Decimal("1") - rule[2]):
                return realised + (px - entry) * held
        elif kind == "half":
            # bank half at the first multiple, trail the rest
            if not half_done and px >= entry * rule[1]:
                sell = held // 2
                realised += (px - entry) * sell
                held -= sell
                half_done = True
            elif half_done and peak >= entry * rule[2] and \
                    px <= peak * (Decimal("1") - rule[3]):
                return realised + (px - entry) * held

    final = path[-1] if path else entry
    return realised + (final - entry) * held


def main():
    sport = sys.argv[1] if len(sys.argv) > 1 else "tennis"
    s = json.loads(config.LONGSHOT_STATE.read_text())
    pos = [c for c in s.get("closed", []) if c["sport"] == sport]
    paths = load_paths(sport)

    entries = []
    for p in pos:
        v = paths.get(p["slug"])
        if not v:
            continue
        t0 = p["opened"]
        side = p["side"]
        seq = [(b if side == "long" else sh) for ts, b, sh in v if ts >= t0]
        if len(seq) < 5:
            continue
        entries.append((Decimal(p["entry_px"]), p["qty"], seq, p))

    if not entries:
        print("no entries with path coverage")
        return

    staked = sum(e * q for e, q, _, _ in entries)
    print(f"{sport}: {len(entries)} positions, ${staked:.2f} staked\n")

    rules = [
        ("hold to end", ("hold",)),
        ("take profit 2x", ("tp", Decimal(2))),
        ("take profit 3x", ("tp", Decimal(3))),
        ("take profit 5x", ("tp", Decimal(5))),
        ("take profit 10x", ("tp", Decimal(10))),
        ("trail 25% after 2x", ("trail", Decimal(2), Decimal("0.25"))),
        ("trail 40% after 2x", ("trail", Decimal(2), Decimal("0.40"))),
        ("trail 25% after 3x", ("trail", Decimal(3), Decimal("0.25"))),
        ("trail 40% after 3x", ("trail", Decimal(3), Decimal("0.40"))),
        ("trail 50% after 5x", ("trail", Decimal(5), Decimal("0.50"))),
        ("half@3x, trail 40%", ("half", Decimal(3), Decimal(3), Decimal("0.40"))),
        ("half@5x, trail 40%", ("half", Decimal(5), Decimal(5), Decimal("0.40"))),
    ]

    print(f"{'rule':>22}{'pnl':>10}{'ROI':>8}{'winners':>9}")
    print("-" * 51)
    best = None
    for name, rule in rules:
        tot = Decimal("0")
        wins = 0
        for entry, qty, seq, _ in entries:
            p = simulate(seq, entry, qty, rule)
            tot += p
            if p > 0:
                wins += 1
        roi = tot / staked * 100
        if best is None or tot > best[1]:
            best = (name, tot, roi)
        print(f"{name:>22}{float(tot):>10.2f}{float(roi):>7.0f}%{wins:>9}")

    peak_total = sum(max(seq) * q - e * q for e, q, seq, _ in entries)
    print(f"\n{'sell at exact peak':>22}{float(peak_total):>10.2f}"
          f"{float(peak_total/staked*100):>7.0f}%   (unattainable)")
    print(f"\nbest achievable: {best[0]} at {float(best[2]):+.0f}%")


if __name__ == "__main__":
    main()
