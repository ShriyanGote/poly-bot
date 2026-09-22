#!/usr/bin/env python
"""Would a stop-loss have helped the longshot book?

The trailing stop only protects a position that fades through a price. Most
losers do not fade - the match ends and the bid disappears - so the trail
never fires and the whole stake goes. A stop placed close to entry would fire
while a bid still exists, but it also sits right where the spread already
puts us on day one, so it can cut winners off before they run.

This replays every closed tennis position against its real book tape and
settles it at the real outcome (0 or 1), not at the last price we happened to
see, which is what lssim.py does and why "hold" looks profitable there.

    ./lsstop.py [sport]
"""

import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bot import config, tapes  # noqa: E402

CACHE = config.DATA / "settlement_cache.json"


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


def settlements(slugs):
    """Real outcome per market, cached: the whole point is to stop marking
    losers out at whatever price they were last quoted at."""
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    missing = [s for s in slugs if s not in cache]
    if missing:
        from dotenv import load_dotenv
        load_dotenv(".env")
        from polymarket_us import PolymarketUS
        c = PolymarketUS(key_id=os.environ["POLYMARKET_KEY_ID"],
                         secret_key=os.environ["POLYMARKET_SECRET_KEY"])
        for i, slug in enumerate(missing):
            val = None
            try:
                v = c.markets.settlement(slug).get("settlement")
                val = str(v) if v is not None else None
            except Exception:
                pass
            if val is None:
                try:
                    md = c.markets.bbo(slug).get("marketData") or {}
                    if md.get("state") == "MARKET_STATE_EXPIRED":
                        px = (md.get("settlementPx") or {}).get("value")
                        val = str(px) if px is not None else None
                except Exception:
                    pass
            cache[slug] = val
            time.sleep(1.2)                       # well under the rate limit
            if i % 10 == 9:
                CACHE.write_text(json.dumps(cache))
        CACHE.write_text(json.dumps(cache))
    return cache


def run(entry, qty, path, payoff, stop_frac, arm, draw):
    """P&L for one position under trail(arm, draw) plus an optional stop.

    Returns (pnl, outcome) where outcome says which rule ended it.
    """
    peak = entry
    floor = entry * (Decimal("1") - stop_frac) if stop_frac else None
    for px in path:
        if px > peak:
            peak = px
        if floor is not None and px <= floor:
            return (px - entry) * qty, "stopped"
        if peak >= entry * arm and px <= peak * (Decimal("1") - draw):
            return (px - entry) * qty, "trail"
    return (payoff - entry) * qty, "settled"


def main():
    sport = sys.argv[1] if len(sys.argv) > 1 else "tennis"
    arm, draw = config.LS_TRAIL_ARM, config.LS_TRAIL_DRAWDOWN
    s = json.loads(config.LONGSHOT_STATE.read_text())
    pos = [c for c in s.get("closed", []) if c["sport"] == sport]
    paths = load_paths(sport)

    # A position we closed at settlement already carries its real payoff in
    # exit_px, side adjustment included. Only the ones the trail or a take
    # profit sold early need the outcome looking up.
    need = sorted({p["slug"] for p in pos
                   if paths.get(p["slug"]) and p["reason"] != "settled"})
    known = settlements(need)

    cases = []
    for p in pos:
        v = paths.get(p["slug"])
        if not v:
            continue
        if p["reason"] == "settled":
            payoff = Decimal(p["exit_px"])
        else:
            val = known.get(p["slug"])
            if val is None:
                continue                          # undecided: cannot score it
            payoff = (Decimal(val) if p["side"] == "long"
                      else Decimal("1") - Decimal(val))
        seq = [(b if p["side"] == "long" else sh)
               for ts, b, sh in v if ts >= p["opened"]]
        if len(seq) < 5:
            continue
        cases.append((Decimal(p["entry_px"]), p["qty"], seq, payoff, p))

    if not cases:
        print("no positions with both a price path and a settled outcome")
        return

    staked = sum(e * q for e, q, _, _, _ in cases)
    won = sum(1 for _, _, _, pay, _ in cases if pay > 0)
    print(f"{sport}: {len(cases)} positions, ${staked:.2f} staked, "
          f"{won} settled in our favour ({won/len(cases)*100:.0f}%)\n")

    # How far underwater does the spread put us the instant we enter?
    print("mark-to-bid at entry (we buy the ask, we can only sell the bid):")
    gaps = sorted((seq[0] / e) for e, _, seq, _, _ in cases)
    for label, q in (("worst", 0.0), ("25th", .25), ("median", .5),
                     ("75th", .75), ("best", .999)):
        g = gaps[min(int(q * len(gaps)), len(gaps) - 1)]
        print(f"  {label:7} {float(g)*100:5.0f}% of entry"
              f"   ({float(g - 1)*100:+.0f}%)")
    print()

    print(f"{'stop':>8}{'pnl':>9}{'ROI':>7}{'stopped':>9}{'of those, would':>18}"
          f"{'trail':>7}{'settle':>8}")
    print(f"{'':>8}{'':>9}{'':>7}{'':>9}{'have won':>18}{'':>7}{'':>8}")
    print("-" * 70)
    for stop in [None, Decimal("0.20"), Decimal("0.30"), Decimal("0.40"),
                 Decimal("0.50"), Decimal("0.60"), Decimal("0.70")]:
        tot = Decimal("0")
        n = defaultdict(int)
        killed = 0
        for e, q, seq, pay, p in cases:
            pnl, why = run(e, q, seq, pay, stop, arm, draw)
            tot += pnl
            n[why] += 1
            if why == "stopped" and pay > e:
                killed += 1
        lbl = "none" if stop is None else f"-{int(stop*100)}%"
        roi = tot / staked * 100 if staked else 0
        print(f"{lbl:>8}{float(tot):>9.2f}{float(roi):>6.0f}%"
              f"{n['stopped']:>9}{killed:>18}{n['trail']:>7}{n['settled']:>8}")

    print(f"\n  arm {arm}x, drawdown {float(draw)*100:.0f}%  "
          f"(the live settings; the stop is added on top)")
    print("  'would have won' = positions the stop sold that settled in our favour")


if __name__ == "__main__":
    main()
