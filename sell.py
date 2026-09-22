#!/usr/bin/env python
"""Sell an open longshot position by hand.

    ./sell.py hamzec              # match on any part of the slug
    ./sell.py hamzec --side short # only that side
    ./sell.py --min-mult 3        # everything at or above 3x
    ./sell.py --all               # close the book

The sale happens on the position's next book update, at the bid that exists
then - the same price a market order would get. Nothing is closed at a made-up
price, so a market with no book stays open until it settles.

The request is left as a file for the running recorder to pick up, because it
rewrites longshot_state.json in full and would discard a direct edit.
"""

import argparse
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bot import config  # noqa: E402

STALE_QUOTE_SECS = 60


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("match", nargs="*", help="substring(s) of the market slug")
    ap.add_argument("--side", choices=("long", "short"))
    ap.add_argument("--min-mult", type=float, metavar="N",
                    help="close every position at or above this multiple")
    ap.add_argument("--all", action="store_true", help="close everything open")
    ap.add_argument("-n", "--dry-run", action="store_true")
    a = ap.parse_args()

    if not (a.match or a.all or a.min_mult):
        ap.error("name a market, or use --all / --min-mult")

    if not config.LONGSHOT_STATE.exists():
        print("no state file")
        return 1
    pos = json.loads(config.LONGSHOT_STATE.read_text()).get("positions", {})
    if not pos:
        print("nothing open")
        return 0

    chosen = []
    for key, p in pos.items():
        e, pk = Decimal(p["entry_px"]), Decimal(p.get("peak_px", p["entry_px"]))
        mult = float(pk / e) if e else 0.0
        if a.side and p["side"] != a.side:
            continue
        if a.all:
            pass
        elif a.min_mult is not None:
            if mult < a.min_mult:
                continue
        elif not any(m in key for m in a.match):
            continue
        chosen.append((key, p, mult))

    if not chosen:
        print("no open position matches that")
        return 1

    now = time.time()
    print(f"{'side':6}{'entry':>6}{'peak':>6}{'mult':>6}{'last quote':>12}  market")
    dead = []
    for key, p, mult in chosen:
        quiet = now - float(p.get("last_seen", 0) or 0)
        # A sale needs a live bid. If the book has gone quiet the match is
        # probably over, and the order will sit unfilled until settlement.
        stale = quiet > STALE_QUOTE_SECS
        if stale:
            dead.append(key)
        print(f"{p['side']:6}{float(p['entry_px']):>6.2f}"
              f"{float(p.get('peak_px', p['entry_px'])):>6.2f}{mult:>5.1f}x"
              f"{(f'{quiet/60:.0f} min ago' if stale else 'live'):>12}  {p['slug']}")
    if dead:
        print(f"\n  WARNING: {len(dead)} of these have not quoted in over "
              f"{STALE_QUOTE_SECS}s.")
        print("  A sale fills on the next book update. If the match has ended")
        print("  there will not be one, and the position settles at its real")
        print("  outcome instead - the queued order simply never fires.")

    if a.dry_run:
        print(f"\n(dry run) {len(chosen)} position(s) would be sold")
        return 0

    config.REQUESTS.mkdir(exist_ok=True)
    f = config.REQUESTS / f"close-{time.time():.3f}.json"
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps({"match": [k for k, _, _ in chosen]}))
    tmp.replace(f)                       # atomic, so a partial file is never read
    print(f"\nqueued {len(chosen)} position(s); they sell on the next book "
          f"update for each market")
    return 0


if __name__ == "__main__":
    sys.exit(main())
