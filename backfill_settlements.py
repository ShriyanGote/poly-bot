#!/usr/bin/env python
"""Record outcomes for markets that finished while nothing was watching.

The settlement queue lived only in memory until now, so every restart threw
away whatever was pending, and a market leaves discovery exactly once - it is
never requeued. This walks the tapes instead, which remember every market we
ever recorded, and asks the exchange about the ones we have no outcome for.

    ./backfill_settlements.py --dry-run
    ./backfill_settlements.py --limit 400
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bot import config, tapes  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", help="only this sport's tapes")
    ap.add_argument("--limit", type=int, default=500, help="max lookups")
    ap.add_argument("--pause", type=float, default=0.35,
                    help="seconds between lookups (endpoint rate-limits)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    have = {}
    if config.SETTLEMENTS.exists():
        try:
            have = json.loads(config.SETTLEMENTS.read_text())
        except ValueError:
            have = {}

    seen, last = set(), {}
    for r in tapes.read("tape", a.sport):
        m = r.get("market") or ""
        if m.startswith("aec-"):
            seen.add(m)
            ts = r.get("ts") or ""
            if ts > last.get(m, ""):
                last[m] = ts
    missing = sorted(seen - set(have), key=lambda m: last.get(m, ""))
    print(f"{len(seen)} moneyline markets in the tapes, "
          f"{len(have)} already settled, {len(missing)} without an outcome")
    if a.dry_run or not missing:
        for m in missing[:10]:
            print(f"  would ask: {m}  (last seen {last.get(m,'?')[5:19]})")
        return 0

    from polymarket_us import PolymarketUS
    client = PolymarketUS(key_id=os.environ["POLYMARKET_KEY_ID"],
                          secret_key=os.environ["POLYMARKET_SECRET_KEY"])
    got = unpublished = failed = 0
    last_err = None
    for i, slug in enumerate(missing[:a.limit], 1):
        # The endpoint rate-limits well below what a tight loop sends, and a
        # throttled call is indistinguishable from "no settlement" unless you
        # retry it - 205 of 210 lookups failed that way on the first attempt.
        v = None
        for attempt in range(4):
            try:
                v = client.markets.settlement(slug).get("settlement")
                break
            except Exception as e:
                last_err = f"{type(e).__name__}: {str(e)[:120]}"
                if attempt == 3:
                    v = "__error__"
                time.sleep(a.pause * (2 ** attempt) * 4)
        if v == "__error__":
            failed += 1
        elif v is None:
            unpublished += 1
        else:
            have[slug] = str(v)
            got += 1
        if i % 25 == 0:
            config.SETTLEMENTS.write_text(json.dumps(have, indent=0))
            print(f"  {i}/{min(len(missing), a.limit)}  recorded {got}, "
                  f"{unpublished} unpublished, {failed} failed")
        time.sleep(a.pause)
    if failed and last_err:
        print(f"  last error: {last_err}")
    config.SETTLEMENTS.write_text(json.dumps(have, indent=0))
    print(f"recorded {got} new outcomes  ({unpublished} not published yet, "
          f"{failed} lookup errors)")
    print(f"settlements.json now holds {len(have)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
