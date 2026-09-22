#!/usr/bin/env python
"""Coverage check: is every discovered game actually producing data?

The Bandecchi-Dodin failure was invisible because nothing compared what
discovery found against what the tapes contain. This does, so a subscribed
market that streams nothing shows up immediately rather than days later.

    python coverage.py            # today
    python coverage.py --hours 3
"""

import argparse
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bot import config, tapes  # noqa: E402

GAME_LINE = re.compile(r"^(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d) GAME  \| (\S+)\s+(\S+)")


def discovered(hours):
    """Games the recorder logged as live, within the window."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    out = {}
    for log in sorted(config.LOGS.glob("run-*.log")):
        try:
            text = log.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            m = GAME_LINE.match(line)
            if not m:
                continue
            mo, d, H, M, S, league, event = m.groups()
            try:
                ts = datetime(now.year, int(mo), int(d), int(H), int(M), int(S),
                              tzinfo=timezone.utc)
            except ValueError:
                continue
            if ts < cutoff:
                continue
            out.setdefault(event, [league, ts, ts])
            out[event][2] = max(out[event][2], ts)
    return out


def recorded(hours):
    """Games that actually have book rows in the window."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = Counter()
    last = {}
    for sport in tapes.sports_present("tape"):
        for r in tapes.read("tape", sport):
            ts = r.get("ts", "")
            if ts < cutoff:
                continue
            ev = r.get("event")
            if not ev or ev == "?":
                continue
            rows[ev] += 1
            last[ev] = max(last.get(ev, ""), ts)
    return rows, last


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24)
    args = ap.parse_args()

    disc = discovered(args.hours)
    rows, last = recorded(args.hours)

    missing = {e: v for e, v in disc.items() if rows.get(e, 0) == 0}
    thin = {e: v for e, v in disc.items() if 0 < rows.get(e, 0) < 50}

    print(f"window: last {args.hours:g}h")
    print(f"  games discovered : {len(disc)}")
    print(f"  games with data  : {len(disc) - len(missing)}")
    print(f"  NO DATA          : {len(missing)}")
    print(f"  thin (<50 rows)  : {len(thin)}")

    if missing:
        print("\nDISCOVERED BUT NEVER RECORDED  <-- these are missed games")
        by_league = defaultdict(list)
        for e, (lg, first, lastseen) in missing.items():
            by_league[lg].append((e, first, lastseen))
        for lg, items in sorted(by_league.items(), key=lambda x: -len(x[1])):
            print(f"  {lg} ({len(items)})")
            for e, first, lastseen in sorted(items, key=lambda x: x[1])[:6]:
                mins = (lastseen - first).total_seconds() / 60
                print(f"     {e[:44]:46} seen {first:%H:%M} "
                      f"for {mins:.0f}m")

    if thin:
        print(f"\nTHIN COVERAGE (<50 rows) - {len(thin)} games")
        for e, (lg, first, _l) in sorted(thin.items())[:8]:
            print(f"     {e[:44]:46} {rows[e]:>5} rows")

    rate = 100 * (len(disc) - len(missing)) / len(disc) if disc else 100
    print(f"\ncoverage: {rate:.0f}%")
    return 0 if rate >= 95 else 1


if __name__ == "__main__":
    sys.exit(main())
