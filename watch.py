#!/usr/bin/env python
"""Live terminal feed: new games, buys, sells, and problems. Nothing else.

    .venv/bin/python watch.py            # follow live
    .venv/bin/python watch.py --replay   # show today's history first, then follow
    .venv/bin/python watch.py --no-color
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bot import config  # noqa: E402

C = {"game": "\033[36m", "buy": "\033[34m", "win": "\033[32m", "loss": "\033[31m",
     "issue": "\033[33m", "dim": "\033[90m", "bold": "\033[1m", "off": "\033[0m"}

# Only these reach the screen.
ISSUE = re.compile(r"FEED STALE|RATE LIMITED|WS ERROR|paper error|connection lost|"
                   r"SUBSCRIPTION CAP|max subscriptions|WS CLOSED")


def paint(s, key, on=True):
    return f"{C[key]}{s}{C['off']}" if on else s


def logfile():
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    p = config.LOGS / f"run-{day}.log"
    if p.exists():
        return p
    found = sorted(config.LOGS.glob("run-*.log"))
    return found[-1] if found else None


def classify(line):
    if "GAME  |" in line:
        return "game"
    if "BUY   |" in line:
        return "buy"
    if "SELL  |" in line:
        return "sell"
    if ISSUE.search(line):
        return "issue"
    return None


def render(line, color):
    kind = classify(line)
    if not kind:
        return None
    ts = line[:14].strip()
    body = line[15:] if len(line) > 15 else line

    if kind == "game":
        return f"{paint(ts,'dim',color)} {paint(body,'game',color)}"
    if kind == "buy":
        return f"{paint(ts,'dim',color)} {paint(body,'buy',color)}"
    if kind == "sell":
        m = re.search(r"pnl\s+([+-][\d.]+)", body)
        good = m and float(m.group(1)) > 0
        return f"{paint(ts,'dim',color)} {paint(body,'win' if good else 'loss',color)}"
    return f"{paint(ts,'dim',color)} {paint('ISSUE | ' + body.strip(),'issue',color)}"


def pnl_line(color):
    if not config.STATE_FILE.exists():
        return None
    try:
        s = json.loads(config.STATE_FILE.read_text())
    except Exception:
        return None
    closed = s.get("closed", [])      # every trade counts, gapped or not
    real = sum(Decimal(c["pnl"]) for c in closed) if closed else Decimal("0")
    wins = sum(1 for c in closed if Decimal(c["pnl"]) > 0)
    unreal, unmarked = Decimal("0"), 0
    for p in s.get("positions", {}).values():
        if p.get("mark") is None:
            unmarked += 1
            continue
        unreal += (Decimal(p["mark"]) - Decimal(p["entry_px"])) * p["qty"]
    total = real + unreal
    key = "win" if total > 0 else "loss"
    body = (f"RESULT  closed {len(closed)} ({wins}W/{len(closed)-wins}L)  "
            f"realised {real:+.2f}  unrealised {unreal:+.2f}  "
            f"TOTAL {total:+.2f}")
    if unmarked:
        body += f"  ({unmarked} unmarked)"
    return paint(body, key, color)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", action="store_true", help="print today's history first")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()
    color = not args.no_color and sys.stdout.isatty()

    path = logfile()
    if not path:
        print("no log file yet - is the recorder running?")
        return 1

    print(paint(f"watching {path.name}  (games / buys / sells / issues only)", "bold", color))
    print(paint("-" * 78, "dim", color))

    fh = path.open("r", errors="replace")
    if args.replay:
        for line in fh:
            out = render(line.rstrip("\n"), color)
            if out:
                print(out)
    else:
        fh.seek(0, 2)

    p = pnl_line(color)
    if p:
        print(paint("-" * 78, "dim", color))
        print(p)
        print(paint("-" * 78, "dim", color))

    last_pnl = time.time()
    try:
        while True:
            line = fh.readline()
            if line:
                out = render(line.rstrip("\n"), color)
                if out:
                    print(out)
                    sys.stdout.flush()
                continue

            # rotated to a new day?
            cur = logfile()
            if cur and cur != path:
                fh.close()
                path, fh = cur, cur.open("r", errors="replace")
                print(paint(f"-- rolled to {path.name} --", "dim", color))
                continue

            if time.time() - last_pnl > 60:
                p = pnl_line(color)
                if p:
                    print(p)
                    sys.stdout.flush()
                last_pnl = time.time()
            time.sleep(0.4)
    except KeyboardInterrupt:
        p = pnl_line(color)
        if p:
            print(paint("-" * 78, "dim", color))
            print(p)
        return 0


if __name__ == "__main__":
    sys.exit(main())
