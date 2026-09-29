#!/usr/bin/env python
"""Simulate market making against the recorded book and trade tapes.

We have been paying the spread. This asks what happens on the other side of
it: post a bid and an ask, let other people trade with us, and see whether the
spread earned covers what we lose holding inventory when the price runs away.

The two things that decide it are modelled explicitly rather than assumed:

  queue position  When the spread is one tick there is nowhere to post inside
                  it, so we join the back of the queue and only trade after
                  the size ahead of us is consumed. That size is the median
                  $9 in tennis but tens of thousands of shares in the deep
                  markets, and ignoring it is how a market-making backtest
                  turns imaginary.

  adverse selection  Fills are not random. We buy exactly when someone wants
                  to sell, which is disproportionately when the price is about
                  to fall. This falls out of the simulation on its own, because
                  inventory is marked at what the market actually did next.

Fills come from the trade tape: a seller crossing at or below our bid trades
with us once the queue ahead is gone. Nothing is inferred from quote movement.

    ./mmsim.py --sport tennis
    ./mmsim.py --sport tennis --edge 2 --max-inventory 200
"""

import argparse
import collections
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bot import config, tapes  # noqa: E402

TICK = 0.01


def ts(v):
    try:
        return datetime.fromisoformat(v).timestamp()
    except (TypeError, ValueError):
        return None


def load(sport, lo, hi):
    """Book snapshots and trades per market, both time-ordered."""
    books = collections.defaultdict(list)
    for r in tapes.read("tape", sport):
        mk = r.get("market") or ""
        if not mk.startswith("aec-"):
            continue
        t = ts(r.get("ts"))
        if t is None:
            continue
        try:
            b, a = float(r["bid"]), float(r["ask"])
            bd = float(r.get("bid_depth") or 0)
            ad = float(r.get("ask_depth") or 0)
        except (TypeError, ValueError, KeyError):
            continue
        if b <= 0 or a <= 0 or a <= b:
            continue
        mid = (a + b) / 2
        if not (lo <= mid <= hi):
            continue
        books[mk].append((t, b, a, bd, ad))

    trades = collections.defaultdict(list)
    for r in tapes.read("trades", sport):
        mk = r.get("market") or ""
        if mk not in books:
            continue
        t = ts(r.get("ts"))
        if t is None:
            continue
        try:
            px, q = float(r["price"]), float(r["qty"])
        except (TypeError, ValueError, KeyError):
            continue
        if px <= 0 or q <= 0:
            continue
        trades[mk].append((t, px, q, r.get("taker_side") or ""))
    for v in books.values():
        v.sort()
    for v in trades.values():
        v.sort()
    return books, trades


def run_market(book, trade, size, max_inv, settle, skew=False,
               min_spread=1, inside=1, latency=0.0, cap=None, res=None):
    """One market. Returns (realised, inventory, last_mid, fills, adverse)."""
    cash = 0.0
    inv = 0.0
    fills = 0
    # Edge captured at the instant of each fill: how far inside the mid we
    # bought or sold. Everything in the total that this does not explain is
    # what the inventory then did to us, which is the number that decides it.
    edge = 0.0
    # Split the result by side. A maker should earn on both; if it all comes
    # from the buys, this is a directional bet on wide markets wearing a
    # market-maker's clothes, and it should be judged as one.
    bought = sold = 0.0
    buy_px = sell_px = 0.0
    # Queue ahead of us on each side, in shares. Reset whenever we repost.
    q_bid = q_ask = 0.0
    my_bid = my_ask = None
    last_mid = None
    marks = []                      # (price at fill, mid 30s later) for adverse selection
    track = []                      # (minute bucket, inventory, mid) for capital
    resting = []                    # (minute bucket, buying power our quotes reserve)

    bi = 0
    for t, px, q, taker in trade:
        while bi < len(book) and book[bi][0] <= t - latency:
            _, b, a, bd, ad = book[bi]
            last_mid = (a + b) / 2
            # Requote. A spread of min_spread ticks is the least that pays for
            # being run over, so below it we stand aside entirely rather than
            # join a one-tick queue for half a cent.
            ticks = round((a - b) / TICK)
            if ticks < min_spread:
                nb = na = None
            else:
                # Post `inside` WHOLE ticks off the touch. (ticks-1)//2 keeps
                # the two sides from crossing, and the floor division keeps the
                # quote on the 1c grid: a half-tick price does not exist, and
                # quoting one let the simulator earn edge no real order could.
                step = TICK * min(inside, (ticks - 1) // 2)
                nb, na = b + step, a - step
            if skew and max_inv and nb is not None:
                # Lean against the position. Holding to settlement is what
                # sank the symmetric version: we were handed the loser all
                # the way down and then marked at zero. Long inventory means
                # stop bidding and sell cheaper; short means the reverse.
                lean = inv / max_inv                 # -1 .. +1
                if lean > 0.25:
                    nb = None if lean > 0.75 else nb - TICK
                    na = max(a - TICK, b + TICK) if (a - b) > TICK * 1.5 else b
                elif lean < -0.25:
                    na = None if lean < -0.75 else na + TICK
                    nb = min(b + TICK, a - TICK) if (a - b) > TICK * 1.5 else a
            # nb/na are None when the skew pulls that side entirely.
            if nb != my_bid:
                my_bid = nb
                q_bid = 0.0 if nb is None or nb > b else bd
            if na != my_ask:
                my_ask = na
                q_ask = 0.0 if na is None or na < a else ad
            track.append((int(book[bi][0] // 60), inv, last_mid))
            rest = 0.0
            if nb is not None:
                rest += min(size, max(0.0, max_inv - inv)) * nb
            if na is not None:
                rest += min(size, max(0.0, max_inv + inv)) * 1.20
            resting.append((int(book[bi][0] // 60), rest))
            bi += 1
        if last_mid is None:
            continue


        # A seller crossing down to our bid trades with the queue first.
        if my_bid is not None and taker.endswith("SELL") and px <= my_bid + 1e-9:
            if q_bid > 0:
                eaten = min(q_bid, q)
                q_bid -= eaten
                q -= eaten
            if q > 0 and inv < max_inv:
                take = min(q, size, max_inv - inv)
                if take > 0:
                    cash -= take * my_bid
                    inv += take
                    fills += 1
                    edge += take * (last_mid - my_bid)
                    bought += take
                    buy_px += take * my_bid
                    marks.append((t, my_bid, +1))
        elif my_ask is not None and taker.endswith("BUY") and px >= my_ask - 1e-9:
            if q_ask > 0:
                eaten = min(q_ask, q)
                q_ask -= eaten
                q -= eaten
            if q > 0 and inv > -max_inv:
                take = min(q, size, max_inv + inv)
                if take > 0:
                    cash += take * my_ask
                    inv -= take
                    fills += 1
                    edge += take * (my_ask - last_mid)
                    sold += take
                    sell_px += take * my_ask
                    marks.append((t, my_ask, -1))

    # Close out: at settlement if known, else at the last mid.
    close = settle if settle is not None else last_mid
    if close is None:
        return 0.0, 0.0, None, 0, 0.0, 0.0, (0.0,) * 6
    cash += inv * close

    # Adverse selection: where did the mid go after each fill?
    # Both lists are time-ordered, so walk them together. Rescanning the book
    # for every fill is O(fills x book), which is minutes per market once a
    # busy match has 9,000 fills against 50,000 quotes.
    adverse = 0.0
    j = 0
    for ft, fpx, sign in marks:
        while j < len(book) and book[j][0] < ft + 30:
            j += 1
        if j >= len(book):
            break
        _t, b, a, _bd, _ad = book[j]
        adverse += sign * ((a + b) / 2 - fpx)
    # Per-side P&L against the settlement: every share bought is worth
    # (close - what we paid), every share sold (what we got - close).
    m = min(bought, sold)
    ab = buy_px / bought if bought else 0.0
    asl = sell_px / sold if sold else 0.0
    matched = m * (asl - ab)
    if bought >= sold:
        residual = (bought - sold) * (close - ab)
    else:
        residual = (sold - bought) * (asl - close)
    assert abs(matched + residual - cash) < 1e-6 * max(1.0, abs(cash)), (
        matched, residual, cash)
    sides = (matched, residual, bought, sold, m, 0.0)
    if cap is not None:
        # One sample per minute per market: the last position we held in it.
        per_min = {}
        for b, i, mid in track:
            per_min[b] = (i, mid)
        for b, (i, mid) in per_min.items():
            cap[b] = cap.get(b, 0.0) + (i * mid if i > 0 else -i * (1.20))
        per_min_r = {}
        for b, r in resting:
            per_min_r[b] = r
        for b, r in per_min_r.items():
            res[b] = res.get(b, 0.0) + r
    return cash, inv, close, fills, adverse, edge, sides


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", default="tennis")
    ap.add_argument("--size", type=float, default=20, help="shares per fill")
    ap.add_argument("--max-inventory", type=float, default=200)
    ap.add_argument("--lo", type=float, default=0.02, help="min mid to quote")
    ap.add_argument("--hi", type=float, default=0.98)
    ap.add_argument("--skew", action="store_true",
                    help="lean quotes against inventory")
    ap.add_argument("--min-spread", type=int, default=1,
                    help="ticks of spread below which we do not quote")
    ap.add_argument("--inside", type=int, default=1,
                    help="ticks off the touch to post")
    ap.add_argument("--latency", type=float, default=0.0,
                    help="seconds our quote lags the book we are reacting to")
    ap.add_argument("--settled-only", action="store_true",
                    help="skip markets with no settlement, so leftover "
                         "inventory is never marked at the last mid")
    ap.add_argument("--dump", help="append per-market results to this CSV, "
                                   "so several sports can be pooled and "
                                   "block-bootstrapped together")
    ap.add_argument("--sweep", action="store_true",
                    help="grid over --min-spread/--inside, loading tapes once")
    a = ap.parse_args()

    dumped = []
    capital = {}
    reserve = {}
    sett = json.loads(config.SETTLEMENTS.read_text())
    books, trades = load(a.sport, a.lo, a.hi)
    print(f"{len(books)} markets with a two-sided book in {a.sport}, "
          f"{sum(len(v) for v in trades.values()):,} trades\n")

    def one(min_spread, inside):
        """Run every market at one setting. Returns the aggregates."""
        tot = fills = adverse = edge_tot = inv_open = 0.0
        done = nsettled = 0
        per = []
        byday = collections.defaultdict(float)
        sd = [0.0] * 6
        for mk, book in books.items():
            tr = trades.get(mk)
            if not tr:
                continue
            sv = sett.get(mk)
            if a.settled_only and sv is None:
                continue
            pnl, inv, close, f, adv, edge, sides = run_market(
                book, tr, a.size, a.max_inventory,
                float(sv) if sv is not None else None, skew=a.skew,
                min_spread=min_spread, inside=inside,
                latency=a.latency, cap=capital, res=reserve)
            if close is None or not f:
                continue
            done += 1
            tot += pnl
            fills += f
            adverse += adv
            edge_tot += edge
            inv_open += abs(inv)
            nsettled += sv is not None
            for i in range(6):
                sd[i] += sides[i]
            day = datetime.fromtimestamp(book[0][0]).date().isoformat()
            byday[day] += pnl
            if a.dump:
                dumped.append((a.sport, day, mk, pnl, f))
            per.append((pnl, f, mk))
        return (tot, fills, adverse, edge_tot, inv_open, done, per,
                nsettled, byday, sd)

    def flush():
        if not a.dump or not dumped:
            return
        import csv as _csv
        new = not Path(a.dump).exists()
        with open(a.dump, "a", newline="") as fh:
            w = _csv.writer(fh)
            if new:
                w.writerow(["sport", "day", "market", "pnl", "fills"])
            w.writerows(dumped)
        print(f"  wrote {len(dumped)} rows to {a.dump}")

    if a.sweep:
        # Bootstrap over markets, because one match can carry the whole total
        # and a grid this size will otherwise hand back whichever cell got
        # lucky. Nothing here is worth acting on unless the interval clears 0.
        import random
        print(f"  {'spread>=':<10}{'inside':>7}{'mkts':>6}{'settled':>8}"
              f"{'fills':>9}{'net':>9}{'per fill':>10}"
              f"{'  95% CI on net':<22}{'matched':>10}{'residual':>10}{'per matched sh':>15}")
        for ms in (1, 2, 3, 4, 6):
            for ins in (1, 2):
                if ins > 1 and ms < 3:
                    continue          # no room to post 2 ticks inside
                (tot, f, adv, edge, invo, done, per, nset, byday,
                 sd) = one(ms, ins)
                if not f:
                    print(f"  {ms:<10}{ins:>7}{0:>10}     no fills")
                    continue
                pnls = [p for p, _, _ in per]
                boot = []
                rng = random.Random(0)
                for _ in range(400):
                    boot.append(sum(rng.choice(pnls) for _ in pnls))
                boot.sort()
                lo, hi = boot[int(0.025 * 400)], boot[int(0.975 * 400)]
                print(f"  {ms:<10}{ins:>7}{done:>6}{nset:>8}"
                      f"{f:>9,.0f}{tot:>9,.0f}{tot/f:>10.4f}"
                      f"  [{lo:>+7,.0f},{hi:>+7,.0f}]"
                      f"{sd[0]:>+10,.0f}{sd[1]:>+10,.0f}"
                      f"{sd[0]/sd[4]*100:>+14.3f}c")
        return 0

    (tot, fills, adverse, edge_tot, inv_open, done, per,
     nset, byday, sd) = one(a.min_spread, a.inside)
    flush()
    if capital:
        vals = sorted(capital.values())
        print(f"\n  capital tied up across all markets at once, by minute")
        print(f"    median  ${vals[len(vals)//2]:>9,.0f}")
        print(f"    p90     ${vals[int(0.90*len(vals))]:>9,.0f}")
        print(f"    p99     ${vals[int(0.99*len(vals))]:>9,.0f}")
        print(f"    peak    ${vals[-1]:>9,.0f}")
        print(f"    (longs at the price paid, shorts at $1.20/share)")
    if reserve:
        rv = sorted(reserve.values())
        print(f"\n  buying power reserved by RESTING QUOTES, by minute")
        print(f"    median  ${rv[len(rv)//2]:>9,.0f}")
        print(f"    p90     ${rv[int(0.90*len(rv))]:>9,.0f}")
        print(f"    p99     ${rv[int(0.99*len(rv))]:>9,.0f}")
        print(f"    peak    ${rv[-1]:>9,.0f}")
    if not done:
        print("  no market produced a fill")
        return 0
    print(f"  {done} markets traded, {fills:,.0f} fills\n")
    print(f"  net P&L            {tot:>10,.2f}")
    print(f"  per fill           {tot/fills:>10.4f}")
    print(f"  edge captured      {edge_tot:>10,.2f}   "
          f"(distance inside the mid we quoted, summed over fills)")
    print(f"  inventory P&L      {tot-edge_tot:>10,.2f}   "
          f"(what the position then did, fill mid -> settlement)")
    print(f"  adverse selection  {adverse:>10,.2f}   "
          f"(mid move against us 30s after each fill)")
    print(f"  avg |inventory| at close {inv_open/done:>6.0f} shares")
    per.sort(reverse=True)
    print(f"\n  best markets:")
    for pn, f, mk in per[:4]:
        print(f"    {pn:+9.2f} on {f:>4.0f} fills  {mk[:44]}")
    print(f"  worst markets:")
    for pn, f, mk in per[-4:]:
        print(f"    {pn:+9.2f} on {f:>4.0f} fills  {mk[:44]}")
    wins = sum(1 for pn, _, _ in per if pn > 0)
    print(f"\n  {wins}/{len(per)} markets profitable ({wins/len(per)*100:.0f}%)")
    drop = sorted(pn for pn, _, _ in per)
    print(f"  net without the best market:  {sum(drop[:-1]):+,.2f}")
    print(f"  net without the worst market: {sum(drop[1:]):+,.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
