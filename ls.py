#!/usr/bin/env python
"""Longshot trade viewer.

    ./ls.py              trades under the current trailing-stop rule
    ./ls.py --all        include trades from the older exit rules
    ./ls.py --open       just what is open right now
    ./ls.py --sport tennis
    ./ls.py --watch      refresh every 30s
"""

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bot import config  # noqa: E402

G, R, Y, D, B, X = ("\033[32m", "\033[31m", "\033[33m",
                    "\033[90m", "\033[1m", "\033[0m")


def paint(s, c, on=True):
    return f"{c}{s}{X}" if on else s


TITLES = config.DATA / "event_titles.json"
REFETCH_SECS = 300


def event_slug(market_slug):
    """Market slugs are the event slug with a market-type prefix."""
    for pre in ("aec-", "astatc-", "asc-", "tsc-", "atc-"):
        if market_slug.startswith(pre):
            return market_slug[len(pre):]
    return market_slug


def titles(wanted):
    """{event_slug: "A vs. B"}, cached on disk.

    The engine never stored titles, and the abbreviations in a slug are not
    reversible (puilav-smirac is Puiac, Alexia Lavinia vs Smith, Rachael), so
    they have to come from the API. One bulk fetch fills in everything on
    screen; after that this is a file read.
    """
    cache = {}
    if TITLES.exists():
        try:
            cache = json.loads(TITLES.read_text())
        except ValueError:
            cache = {}
    missing = {e for e in wanted if e not in cache}
    if not missing:
        return cache
    # The recorder is polling the same API every 30s, so a viewer must not
    # retry a full fetch on every invocation just because some old event is
    # no longer served. One attempt per REFETCH_SECS, whatever the outcome.
    if time.time() - float(cache.get("__last_fetch", 0) or 0) < REFETCH_SECS:
        return cache
    cache["__last_fetch"] = time.time()
    try:
        import os
        from datetime import timedelta
        from dotenv import load_dotenv
        from polymarket_us import PolymarketUS
        load_dotenv(str(Path(__file__).resolve().parent / ".env"))
        c = PolymarketUS(key_id=os.environ["POLYMARKET_KEY_ID"],
                         secret_key=os.environ["POLYMARKET_SECRET_KEY"])
        now = datetime.now(timezone.utc)
        for closed in (False, True):
            for page in range(1, 9):
                q = {"limit": 100, "closed": closed,
                     "startDateMin": (now - timedelta(hours=30)).isoformat(),
                     "startDateMax": (now + timedelta(hours=12)).isoformat(),
                     "orderBy": ["startDate"], "orderDirection": "asc"}
                if page > 1:
                    q["offset"] = (page - 1) * 100
                try:
                    ev = c.events.list(q).get("events", [])
                except Exception:
                    time.sleep(5)          # most likely a rate limit
                    break
                for e in ev:
                    if e.get("slug") and e.get("title"):
                        cache[e["slug"]] = e["title"]
                if len(ev) < 100:
                    break
                time.sleep(2.5)
            if not {e for e in wanted if e not in cache}:
                break
        # The window fetch misses events whose startDate falls outside it, but
        # a slug lookup still finds them. Fill a bounded number per run so the
        # gaps close over a few invocations without a burst of requests.
        still = [e for e in sorted(missing) if e not in cache]
        for e in still[:25]:
            try:
                got = c.events.list({"limit": 3, "slug": e}).get("events", [])
            except Exception:
                break
            for ev in got:
                if ev.get("slug") and ev.get("title"):
                    cache[ev["slug"]] = ev["title"]
            cache.setdefault(e, "")
            time.sleep(1.8)
        for e in missing:
            cache.setdefault(e, "")   # a title the API no longer serves
    except Exception:
        pass                          # a viewer must still work offline
    if cache:
        # Save whatever was collected even if the fetch died partway, so a
        # rate limit mid-run does not throw away every title already in hand.
        try:
            TITLES.write_text(json.dumps(cache))
        except OSError:
            pass
    return cache


def load():
    if not config.LONGSHOT_STATE.exists():
        return [], []
    s = json.loads(config.LONGSHOT_STATE.read_text())
    return s.get("closed", []), list(s.get("positions", {}).values())


def money(v, on):
    v = float(v)
    return paint(f"{v:+7.2f}", G if v > 0 else (R if v < 0 else D), on)


def render(args, color):
    closed, open_ = load()
    if args.sport:
        closed = [c for c in closed if c["sport"] == args.sport]
        open_ = [p for p in open_ if p["sport"] == args.sport]

    trail_only = not args.all
    if trail_only:
        # Only the current strategy: opened after the trailing rule went live.
        # Older positions ran under fixed take-profit or hold-forever rules.
        shown = [c for c in closed
                 if c.get("opened", 0) >= config.LS_TRAIL_SINCE]
        # Open positions are NOT gated on the cutoff. Whatever rule they were
        # entered under, every live position is being managed by the current
        # trailing stop right now, so hiding one because it opened a few
        # minutes early just makes it look like the bot lost track of it.
    else:
        shown = closed

    name = titles({event_slug(c["slug"]) for c in (shown if not args.open else [])}
                  | {event_slug(p["slug"]) for p in open_})
    name.pop("__last_fetch", None)

    out = []
    hdr = "CURRENT STRATEGY (trailing stop)" if trail_only else "ALL CLOSED TRADES (incl. old rules)"
    out.append(paint(f"\n{hdr}   (arm {config.LS_TRAIL_ARM}x, "
                     f"exit on {float(config.LS_TRAIL_DRAWDOWN)*100:.0f}% drawdown)", B, color))

    if not args.open:
        if not shown:
            out.append(paint("  none yet", D, color))
        else:
            out.append(f"  {'when':9}{'sport':10}{'league':12}{'side':6}"
                       f"{'entry':>6}{'peak':>6}{'exit':>6}{'mult':>6}{'pnl':>9}"
                       f"  {'match':38}market")
            out.append(paint("  " + "-" * 130, D, color))
            for c in sorted(shown, key=lambda x: x.get("closed", 0)):
                e = Decimal(c["entry_px"])
                pk = Decimal(c.get("peak_px", c["entry_px"]))
                x = Decimal(c["exit_px"])
                when = datetime.fromtimestamp(c.get("closed", 0), timezone.utc).strftime("%H:%M:%S")
                out.append(f"  {when:9}{c['sport']:10}{c.get('league','?')[:11]:12}"
                           f"{c['side']:6}{float(e):>6.2f}{float(pk):>6.2f}{float(x):>6.2f}"
                           f"{float(pk/e) if e else 0:>5.1f}x{money(c['pnl'], color)}  "
                           f"{name.get(event_slug(c['slug']), '') or '?':<38.36}"
                           f"{c['slug'][:34]}")

            staked = sum(Decimal(c["entry_px"]) * c["qty"] for c in shown)
            pnl = sum(Decimal(c["pnl"]) for c in shown)
            wins = sum(1 for c in shown if Decimal(c["pnl"]) > 0)
            roi = (pnl / staked * 100) if staked else Decimal("0")
            out.append(paint("  " + "-" * 92, D, color))
            out.append(f"  {len(shown)} trades   {wins}W/{len(shown)-wins}L   "
                       f"staked ${float(staked):.2f}   "
                       f"pnl {money(pnl, color)}   "
                       f"ROI {paint(f'{float(roi):+.0f}%', G if roi > 0 else R, color)}")

            by = defaultdict(lambda: [0, Decimal("0"), Decimal("0")])
            for c in shown:
                b = by[c["sport"]]
                b[0] += 1
                b[1] += Decimal(c["pnl"])
                b[2] += Decimal(c["entry_px"]) * c["qty"]
            if len(by) > 1:
                out.append("")
                for sp, (n, p, st) in sorted(by.items(), key=lambda x: -x[1][1]):
                    r = (p / st * 100) if st else 0
                    out.append(f"    {sp:12}{n:>4} trades  {money(p, color)}"
                               f"  {float(r):>+5.0f}%")

    pre = sum(1 for p in open_ if p.get("opened", 0) < config.LS_TRAIL_SINCE)
    out.append(paint(f"\nOPEN ({len(open_)})"
                     + (f"  - {pre} marked * opened under an older rule"
                        if pre and not args.all else ""), B, color))
    if not open_:
        out.append(paint("  none", D, color))
    else:
        out.append(f"  {'sport':10}{'league':12}{'side':6}{'entry':>6}{'peak':>6}"
                   f"{'mult':>6}{'armed':>7}  {'match':38}market")
        out.append(paint("  " + "-" * 118, D, color))
        for p in sorted(open_, key=lambda x: -(Decimal(x.get("peak_px", x["entry_px"]))
                                               / Decimal(x["entry_px"]))):
            e = Decimal(p["entry_px"])
            pk = Decimal(p.get("peak_px", e))
            mult = pk / e if e else Decimal(0)
            armed = "yes" if mult >= config.LS_TRAIL_ARM else ""
            floor = f"{float(pk * (1 - config.LS_TRAIL_DRAWDOWN)):.2f}" if armed else "-"
            out.append(f"  {p['sport']:10}{p.get('league','?')[:11]:12}{p['side']:6}"
                       f"{float(e):>6.2f}{float(pk):>6.2f}{float(mult):>5.1f}x"
                       f"{paint(f'{floor:>7}', Y, color) if armed else f'{floor:>7}'}"
                       f"  {name.get(event_slug(p['slug']), '') or '?':<38.36}"
                       f"{p['slug'][:34]}"
                       + ("" if p.get("opened", 0) >= config.LS_TRAIL_SINCE
                          else paint(" *", D, color)))
        out.append(paint("  armed = trail active; the number is the price that "
                         "triggers a sale", D, color))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="include older exit rules")
    ap.add_argument("--open", action="store_true", help="only open positions")
    ap.add_argument("--sport")
    ap.add_argument("--every", type=float, default=5.0,
                    help="seconds between refreshes with --watch (default 5)")
    ap.add_argument("--watch", action="store_true", help="refresh every 30s")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()
    color = not args.no_color and sys.stdout.isatty()

    if not args.watch:
        print(render(args, color))
        return
    try:
        while True:
            print("\033[2J\033[H", end="")
            print(paint(f"longshot  {datetime.now(timezone.utc):%H:%M:%S} UTC", D, color))
            print(render(args, color))
            time.sleep(args.every)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
