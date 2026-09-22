#!/usr/bin/env python
"""Longshot report: how each exit rule is performing, and on what.

Every rule is scored on the SAME positions, so differences are the rule and
nothing else.
"""

import json
import sys
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bot import config  # noqa: E402

RULES = ["hold"] + [f"tp{m}x" for m in config.LS_TP_LADDER]


def variants_for(c):
    """Use the stored variants when present; otherwise reconstruct them from
    the recorded peak, so positions closed before variant tracking existed
    still count."""
    v = c.get("variants")
    if v:
        return v
    entry = Decimal(c["entry_px"])
    qty = c["qty"]
    peak = Decimal(c.get("peak_px", c["entry_px"]))
    final = Decimal(c["exit_px"])
    out = {"hold": str((final - entry) * qty)}
    for m in config.LS_TP_LADDER:
        target = entry * m
        exitp = target if peak >= target else final
        out[f"tp{m}x"] = str((exitp - entry) * qty)
    return out


def main():
    if not config.LONGSHOT_STATE.exists():
        print("no longshot state yet")
        return
    s = json.loads(config.LONGSHOT_STATE.read_text())
    closed, open_ = s.get("closed", []), s.get("positions", {})
    print(f"open {len(open_)}   closed {len(closed)}")
    if not closed:
        return

    staked = sum(Decimal(c["entry_px"]) * c["qty"] for c in closed)
    print(f"staked ${staked:.2f}\n")

    print("EXIT RULE COMPARISON (same positions, different rules)")
    print(f"{'rule':>8}{'pnl':>10}{'ROI':>9}{'winners':>10}")
    print("-" * 38)
    for r in RULES:
        tot = Decimal("0")
        wins = 0
        for c in closed:
            v = variants_for(c).get(r)
            p = Decimal(v) if v is not None else Decimal(c["pnl"])
            tot += p
            if p > 0:
                wins += 1
        roi = (tot / staked * 100) if staked else Decimal("0")
        print(f"{r:>8}{float(tot):>10.2f}{float(roi):>8.0f}%{wins:>10}")

    print("\nBY SPORT (actual rule in force)")
    by = defaultdict(lambda: [0, 0, Decimal("0"), Decimal("0")])
    for c in closed:
        b = by[c["sport"]]
        p = Decimal(c["pnl"])
        b[0] += 1
        if p > 0:
            b[1] += 1
        b[2] += p
        b[3] += Decimal(c["entry_px"]) * c["qty"]
    print(f"{'sport':12}{'n':>5}{'W':>4}{'staked':>9}{'pnl':>9}{'ROI':>8}")
    for sp, (n, w, p, st) in sorted(by.items(), key=lambda x: x[1][2]):
        roi = (p / st * 100) if st else 0
        print(f"{sp:12}{n:>5}{w:>4}{float(st):>9.2f}{float(p):>9.2f}{float(roi):>7.0f}%")

    print("\nBY MARKET KIND")
    kd = defaultdict(lambda: [0, 0, Decimal("0")])
    for c in closed:
        k = kd[c["kind"]]
        k[0] += 1
        if Decimal(c["pnl"]) > 0:
            k[1] += 1
        k[2] += Decimal(c["pnl"])
    for k, (n, w, p) in kd.items():
        print(f"  {k:12}{n:>5} closed{w:>4}W  pnl {float(p):>8.2f}")

    peaks = sorted((float(Decimal(c.get("peak_px", c["entry_px"])) / Decimal(c["entry_px"]))
                    for c in closed), reverse=True)
    if peaks:
        print("\nPEAK MULTIPLE REACHED (closed positions)")
        for m in config.LS_TP_LADDER:
            n = sum(1 for x in peaks if x >= m)
            print(f"  reached {m:>2}x+ : {n:>4} / {len(peaks)}  ({100*n/len(peaks):.0f}%)")


if __name__ == "__main__":
    main()
