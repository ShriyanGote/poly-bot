#!/usr/bin/env python
"""Compare tennis entry rules on the recorded tape.

Four rules, the live exit, same markets:

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
import json
import math
import sys
from collections import defaultdict, deque
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bot import config, tapes  # noqa: E402


def settlements():
    """Real payoffs, so a trade that never triggered is not just assumed lost.

    Scoring an un-exited trade at zero deletes exactly the tail the strategy
    exists to catch: one market that settled at 1.00 was being booked as a
    total loss. Where the exchange has told us what a market paid, use it.
    """
    try:
        raw = json.loads(config.SETTLEMENTS.read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for slug, v in raw.items():
        try:
            out[slug] = Decimal(str(v))
        except (ArithmeticError, ValueError):
            continue
    return out


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


def replay(paths, rule, lo, hi, dip, back, hold, arm, draw, since, min_ticks=0,
           take=None, runner=None, settled=None):
    """One entry per market-side, exited exactly the way the live rule exits.

    The exit has to track config, or the comparison silently reports one
    rule's entry under another rule's exit.
    """
    out = []
    settled = settled or {}
    unresolved = 0
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
                # Take the fixed multiple, unless the run already cleared
                # `runner` - then the trail chases the tail instead.
                if take is not None:
                    past = runner is not None and peak >= entry * runner
                    if not past and exitp >= entry * take:
                        pnl = (exitp - entry) * qty
                        break
                if peak >= entry * arm and exitp <= peak * (Decimal(1) - draw):
                    pnl = (exitp - entry) * qty
                    break
            if entry is None:
                continue
            resolved = True
            if pnl is None:
                px = settled.get(mk)
                if px is not None:
                    final = px if side == "long" else Decimal(1) - px
                    pnl = (final - entry) * qty
                else:
                    # No settlement and it never exited. Assuming zero deletes
                    # the winners; marking to the last tick rescues the losers,
                    # and the tape usually stops well before a match ends. Both
                    # are guesses, so carry both and let the caller see the
                    # range rather than pick one and call it a result.
                    resolved = False
                    unresolved += 1
                    mark = v[-1][1] if side == "long" else Decimal(1) - v[-1][2]
                    if mark < 0:
                        mark = Decimal(0)
                    pnl = (Decimal(0) - entry) * qty       # low
                    pnl_hi = (mark - entry) * qty          # high
                    out.append((pnl, entry * qty, peak, entry, at, mk, side,
                                resolved, pnl_hi))
                    continue
            out.append((pnl, entry * qty, peak, entry, at, mk, side,
                        resolved, pnl))
    return out, unresolved


def report(label, t, base_n, unresolved):
    """Report a bound, not a point estimate.

    Reporting only trades with a known outcome looks rigorous and is the
    worst option of the three: a trade resolves by hitting take-profit or the
    trail, and both of those are wins, so the "known" subset is very nearly
    the winners. Losers do not resolve, they just run out of tape. So score
    every trade twice - unresolved ones at zero, then at their last price -
    and report the interval those two produce. The truth is inside it.
    """
    if not t:
        print(f"  {label:46}{0:>4}{'-':>7}{'-':>8}{'-':>8}{'-':>8}{'-':>7}")
        return
    n = len(t)
    staked = float(sum(x[1] for x in t))
    lo = float(sum(x[0] for x in t)) / staked * 100
    hi = float(sum(x[8] for x in t)) / staked * 100
    # t-stat on the low scoring: every trade counted, one consistent rule.
    p = [float(x[0]) for x in t]
    mean = sum(p) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in p) / (n - 1)) if n > 1 else 0
    se = sd / math.sqrt(n) if n > 1 else 0
    wins = sum(1 for x in t if float(x[8]) > 0) / n * 100
    vol = f"{n / base_n * 100:.0f}%" if base_n else "-"
    unk = f"{unresolved / n * 100:.0f}%" if n else "-"
    print(f"  {label:46}{n:>4}{unk:>7}{lo:>7.0f}%{hi:>7.0f}%"
          f"{(mean / se if se else 0):>8.2f}{wins:>7.0f}%{vol:>7}")


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
    # Which entry the live rule actually uses, read from config rather than
    # assumed. This was hardcoded to the bounce family, so when tennis moved
    # back to first-touch the LIVE row kept replaying bounce under the
    # first-touch label - it printed the bounce row's numbers exactly.
    if r.dip_to is None:
        live_kind = "first-touch"
    elif hold:
        live_kind = "hold"
    else:
        live_kind = "bounce"
    runs = [("first-touch (original)", "first-touch", 0),
            (f"bounce {float(dip):.2f}->{float(back):.2f}", "bounce", 0),
            ("first-touch + activity", "first-touch", mt or 100),
            (f"{config.rule_label(a.sport)}  (LIVE)", live_kind, mt)]
    # If the live rule is identical to a row already listed, say so on that
    # row instead of printing the same trades twice.
    dupe = next((i for i, (_, k, m) in enumerate(runs[:3])
                 if k == live_kind and m == mt), None)
    if dupe is not None:
        label, k, m = runs[dupe]
        runs[dupe] = (f"{label}  (LIVE)", k, m)
        runs = runs[:3]
    # Every row uses the LIVE exit, so the table isolates the entry rule
    # instead of comparing one rule's entry against another rule's exit.
    sett = settlements()

    def run(kind, mticks):
        return replay(paths, kind, r.band_lo, r.band_hi, dip, back, hold,
                      r.arm, r.drawdown, since, mticks,
                      take=r.take, runner=r.runner, settled=sett)

    base, base_unres = run("first-touch", 0)
    exit_desc = (f"take {float(r.take):.0f}x, trail {float(r.drawdown):.0%} "
                 f"above {float(r.runner):.0f}x" if r.take is not None
                 else f"trail {float(r.drawdown):.0%} from {float(r.arm):.1f}x")
    print(f"  exit applied to every row: {exit_desc}")
    print(f"  {'rule':46}{'n':>4}{'unkn':>7}{'ROI lo':>8}{'ROI hi':>8}"
          f"{'t':>8}{'win%':>7}{'vol':>7}")
    print("  " + "-" * 95)
    results = {}
    unres = {}
    for label, kind, mticks in runs:
        if kind == "first-touch" and not mticks:
            t, u = base, base_unres
        else:
            t, u = run(kind, mticks)
        results[label] = t
        unres[label] = u
        report(label, t, len(base), u)
    tot = sum(len(v) for v in results.values())
    bad = sum(unres.values())
    if tot:
        print(f"\n  'unkn' is the share with no recorded settlement that never "
              f"exited ({bad} of {tot} overall).")
        print("  ROI lo scores those at zero, ROI hi at their last seen price. "
              "The real number is between.")
        print("  t is on the lo scoring. win% is on the hi scoring, so it is "
              "an upper bound too.")

    if a.trades:
        for label, kind, _mt in runs:
            t = results[label]
            if not t:
                continue
            print(f"\n{label}: {len(t)} trades")
            for pnl, _, peak, e, at, mk, side, _res, _hi in sorted(
                    t, key=lambda x: x[4]):
                when = datetime.fromtimestamp(at, timezone.utc).strftime("%H:%M:%S")
                print(f"  {when}  {side:5} entry {float(e):.2f} peak {float(peak):.2f} "
                      f"{float(peak / e):>4.1f}x  pnl {float(pnl):+6.2f}  {mk[:40]}")


if __name__ == "__main__":
    main()
