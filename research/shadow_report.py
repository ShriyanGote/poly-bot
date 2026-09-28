#!/usr/bin/env python3
"""Report the frozen paper-only tennis filter experiment."""
import argparse
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "longshot_signals.sqlite3"
VOL_CUTOFF = 0.0129214176  # frozen from the Sep 21-22 training split


def summarize(rows):
    settled = [r for r in rows if r["simulated_exit"] is not None]
    if not settled:
        return f"n={len(rows):4d} settled=0"
    returns = [r["simulated_exit"] / r["entry_px"] - 1 for r in settled]
    pnl = sum(returns)
    wins = sum(x > 0 for x in returns)
    return (f"n={len(rows):4d} settled={len(settled):4d}  "
            f"ROI={pnl / len(settled):+7.1%}  pnl=${pnl:+8.2f}/$"
            f"{len(settled):.0f}  win={wins / len(settled):5.1%}  "
            f"peak2x={sum(r['peak_exit'] >= 2*r['entry_px'] for r in settled) / len(settled):5.1%}  "
            f"peak<=1.2x={sum(r['peak_exit'] <= 1.2*r['entry_px'] for r in settled) / len(settled):5.1%}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DB)
    args = ap.parse_args()
    if not args.db.exists():
        print(f"No shadow ledger yet: {args.db}")
        return
    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute("SELECT * FROM shadow_tennis ORDER BY ts")]
    db.close()
    if not rows:
        print("No shadow entries recorded yet.")
        return

    print("TENNIS SHADOW TEST — paper only; $1 stake; fixed 5x arm / 20% trail")
    print(f"Entry snapshots: {len(rows)} | vol cutoff: {VOL_CUTOFF:.4f} (frozen) | "
          "ROI excludes fees, queue position, and impact")
    variants = [
        ("Baseline: every eligible 1-5c first touch", lambda r: True),
        ("Volatility >= frozen cutoff", lambda r: r["vol120"] is not None and r["vol120"] >= VOL_CUTOFF),
        ("Vol + exclude UTR and ITF-MENS (exploratory)", lambda r: r["vol120"] is not None and r["vol120"] >= VOL_CUTOFF and r["league"] not in {"UTR", "ITF-MENS"}),
        ("Exclude doubles only (exploratory)", lambda r: not r["is_doubles"]),
    ]
    for name, pred in variants:
        chosen = [r for r in rows if pred(r)]
        print(f"\n{name}\n  {summarize(chosen)}")

    print("\nBaseline descriptive slices (not filters selected for trading):")
    def slices(label, key):
        groups = defaultdict(list)
        for r in rows:
            groups[key(r)].append(r)
        for group, vals in sorted(groups.items(), key=lambda p: str(p[0])):
            print(f"  {label}={group or '(blank)':18} {summarize(vals)}")
    slices("league", lambda r: r["league"])
    slices("side", lambda r: r["side"])
    slices("period", lambda r: r["period"])
    slices("doubles", lambda r: "yes" if r["is_doubles"] else "no")
    slices("UTC hour", lambda r: datetime.fromtimestamp(r["ts"], timezone.utc).hour)
    slices("score", lambda r: r["score"])
    slices("activity", lambda r: "0-2" if r["activity"] <= 2 else "3-5" if r["activity"] <= 5 else "6+")
    slices("vol120", lambda r: "missing" if r["vol120"] is None else "<0.010" if r["vol120"] < .010 else "0.010-0.013" if r["vol120"] < VOL_CUTOFF else ">=0.01292")
    slices("spread/entry", lambda r: "unknown" if r["spread"] is None else "<=20%" if r["spread"] / r["entry_px"] <= .20 else "20-50%" if r["spread"] / r["entry_px"] <= .50 else ">50%")
    slices("entry depth", lambda r: "0" if not r["entry_depth"] else "<10" if r["entry_depth"] < 10 else "10-49" if r["entry_depth"] < 50 else "50+")

    days = defaultdict(list)
    for r in rows:
        day = datetime.fromtimestamp(r["ts"], timezone.utc).date().isoformat()
        days[day].append(r)
    print("\nDaily baseline (use full days; recent/open matches remain unsettled):")
    for day, vals in sorted(days.items()):
        print(f"  {day}  {summarize(vals)}")
    print("\nDaily volatility-filter variant:")
    vol_days = defaultdict(list)
    for r in rows:
        if r["vol120"] is not None and r["vol120"] >= VOL_CUTOFF:
            day = datetime.fromtimestamp(r["ts"], timezone.utc).date().isoformat()
            vol_days[day].append(r)
    for day, vals in sorted(vol_days.items()):
        print(f"  {day}  {summarize(vals)}")
    unsettled = sum(r["simulated_exit"] is None for r in rows)
    print(f"\nUnsettled shadow positions: {unsettled}")


if __name__ == "__main__":
    main()
