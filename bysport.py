#!/usr/bin/env python
"""Per-sport breakdown. Different sports have different price structure, so a
single blended number hides which ones the strategy can actually work in.

    .venv/bin/python bysport.py
    .venv/bin/python bysport.py --min-moves 500
"""

import argparse
import csv
import gzip
import sys
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bot import config, tapes  # noqa: E402
from bot.signals import PriceHistory, evaluate  # noqa: E402

TICK = Decimal("0.01")
MAX_LOOK = 6000


def unpack(s):
    out = []
    for part in (s or "").split("|"):
        if ":" in part:
            px, q = part.split(":", 1)
            out.append({"px": {"value": px}, "qty": q})
    return out


def load(sport=None):
    rows = list(tapes.read("tape", sport))
    rows.sort(key=lambda r: r.get("ts", ""))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-moves", type=int, default=200)
    ap.add_argument("--sport", help="restrict to one sport")
    args = ap.parse_args()

    rows = load(args.sport)
    if not rows:
        print("no tape yet")
        return

    sport_of, league_of = {}, {}
    per = defaultdict(list)
    rws = defaultdict(list)
    for r in rows:
        try:
            bid, ask = Decimal(r["bid"]), Decimal(r["ask"])
        except Exception:
            continue
        if bid <= 0 or ask <= 0:
            continue
        m = r["market"]
        sport_of[m] = r.get("sport", "?")
        league_of[m] = r.get("league", "?")
        per[(m, "long")].append(bid)
        rws[(m, "long")].append(r)
        per[(m, "short")].append(Decimal("1") - ask)
        rws[(m, "short")].append(r)

    # ---- structure: move sizes, reversals, gaps ---------------------------
    jumps = defaultdict(Counter)
    flips = defaultdict(lambda: [0, 0])
    games = defaultdict(set)
    depth = defaultdict(list)
    for r in rows:
        sp = r.get("sport", "?")
        if r.get("event"):
            games[sp].add(r["event"])
        try:
            depth[sp].append(Decimal(r.get("bid_total") or "0"))
        except Exception:
            pass

    for (m, side), v in per.items():
        if side != "long":
            continue
        sp = sport_of[m]
        seq, prev = [], None
        for p in v:
            if p != prev:
                seq.append(p)
                prev = p
        for i in range(len(seq) - 1):
            jumps[sp][min(int(abs(seq[i + 1] - seq[i]) / TICK), 20)] += 1
        last = 0
        for i in range(1, len(seq)):
            d = 1 if seq[i] > seq[i - 1] else -1
            if last and d != last:
                flips[sp][0] += 1
            flips[sp][1] += 1
            last = d

    print("PRICE STRUCTURE BY SPORT")
    print(f"{'sport':12}{'games':>7}{'moves':>9}{'1tick':>8}{'3+':>7}{'5+gap':>8}"
          f"{'rev%':>7}{'med depth':>11}")
    print("-" * 69)
    order = sorted(jumps, key=lambda s: -sum(jumps[s].values()))
    for sp in order:
        tot = sum(jumps[sp].values())
        if tot < args.min_moves:
            continue
        c = jumps[sp]
        one = c[1] / tot
        three = sum(v for k, v in c.items() if k >= 3) / tot
        five = sum(v for k, v in c.items() if k >= 5) / tot
        fl = flips[sp]
        rev = fl[0] / fl[1] if fl[1] else 0
        d = sorted(depth[sp])
        med = d[len(d) // 2] if d else 0
        print(f"{sp:12}{len(games[sp]):>7}{tot:>9,}{one:>7.0%}{three:>7.0%}"
              f"{five:>8.0%}{rev:>7.0%}{med:>11,.0f}")

    # ---- gated EV per sport ----------------------------------------------
    hist = PriceHistory()
    res = defaultdict(lambda: [0, 0])          # sport -> [wins, losses]
    tgt, stp = config.EXIT_TICKS, config.STOP_TICKS
    for key in per:
        m, side = key
        pts, rr = per[key], rws[key]
        sp = sport_of[m]
        for i, r in enumerate(rr):
            try:
                ts = datetime.fromisoformat(r["ts"]).timestamp()
            except Exception:
                continue
            px = pts[i]
            hk = f"{m}|{side}"
            hist.add(hk, ts, px)
            if not (config.BAND_LO <= px <= config.BAND_HI):
                continue
            q = int(config.STAKE / px) if px > 0 else 0
            if q <= 0:
                continue
            lv = unpack(r["bid_levels"] if side == "long" else r["ask_levels"])
            ok, _, _ = evaluate(px, px + tgt * TICK, Decimal(q), lv, hist, hk, ts,
                                (config.BAND_LO, config.BAND_HI))
            if not ok:
                continue
            up, dn = px + tgt * TICK, px - stp * TICK
            for j in range(i + 1, min(len(pts), i + MAX_LOOK)):
                p2 = pts[j]
                if p2 >= up:
                    res[sp][0] += 1
                    break
                if p2 <= dn or p2 <= 0:
                    res[sp][1] += 1
                    break

    print(f"\nGATED STRATEGY BY SPORT   target +{tgt} / stop -{stp}")
    be = Decimal(stp) / (stp + tgt)
    print(f"break-even win rate: {be:.0%}\n")
    print(f"{'sport':12}{'trades':>9}{'wins':>8}{'P(win)':>9}{'EV ticks':>11}{'':>4}")
    print("-" * 53)
    for sp in sorted(res, key=lambda s: -(res[s][0] + res[s][1])):
        w, l = res[sp]
        n = w + l
        if n < 100:
            continue
        p = Decimal(w) / n
        ev = p * tgt - (1 - p) * stp
        flag = "  <<<" if ev > 0 else ""
        print(f"{sp:12}{n:>9,}{w:>8,}{p:>9.3f}{ev:>11.3f}{flag}")
    print("\n(gaps larger than the stop are the main loss source, so a high "
          "'5+gap' column is a warning sign for that sport)")


if __name__ == "__main__":
    main()
