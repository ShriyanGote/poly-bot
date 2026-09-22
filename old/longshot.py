"""Live in-play longshot scalper for Polymarket US.

Watches IN-PLAY matches only. A single market slug covers both sides, so a
cheap longshot is either a low ask (buy long) or a high bid (buy short).

    python longshot.py --scan       # one-shot look at what's in play
    python longshot.py --record     # log price paths to CSV, no trading
    python longshot.py              # paper trade
    python longshot.py --live --max-spend 10
"""

import argparse
import csv
import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv
from polymarket_us import PolymarketUS

load_dotenv()

HERE = Path(__file__).parent
STATE_FILE = HERE / "longshot_state.json"
TAPE_FILE = HERE / "tape.csv"

# seriesId -> label. Tennis and soccer, per client.sports.list().
SERIES = {
    16: "ATP", 17: "WTA", 55: "ITFM", 56: "ITFW", 79: "ITFME", 80: "ITFWO",
    156: "ATPCQ", 275: "WTADB", 266: "ATPDB", 330: "UTR",
    11: "EPL", 12: "UCL", 10: "MLS", 113: "BRA", 117: "CSL", 123: "RPL",
    18: "BUN", 20: "LAL", 19: "SEA", 219: "LIG2", 218: "LIG1",
}

ENTRY_MIN = Decimal("0.01")
ENTRY_MAX = Decimal("0.05")
EXIT_TICKS = 2
STAKE = Decimal("3.00")
MAX_POSITIONS = 3
POLL_SECONDS = 15
TICK = Decimal("0.01")
# Periods that mean "no live trading": not started, or already finished.
SKIP_PERIODS = ("NS", "", None, "FT", "AET", "PEN", "ENDED", "FINAL", "POSTP", "CANC", "SUSP")


def log(msg):
    print(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {msg}", flush=True)


def d(v):
    return Decimal(str(v or "0"))


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"positions": {}, "closed": []}


def save_state(s):
    STATE_FILE.write_text(json.dumps(s, indent=2))


def get_book(client, slug):
    """Book levels. The API nests these under marketData; the SDK types don't say so."""
    try:
        raw = client.markets.book(slug)
    except Exception:
        return [], []
    md = raw.get("marketData") or raw
    return (md.get("bids") or []), (md.get("offers") or [])


def in_play(client):
    """Every currently in-play event, with its markets."""
    out = []
    for sid, label in SERIES.items():
        try:
            res = client.search.query({"seriesIds": [sid], "status": "active", "limit": 20})
        except Exception:
            continue
        for e in res.get("events", []):
            if e.get("closed") or e.get("period") in SKIP_PERIODS:
                continue
            out.append((label, e))
    return out


def candidates(client):
    """In-play markets where one side sits in the entry band.

    side 'long'  -> ask is cheap, buy it directly
    side 'short' -> bid is rich, buy the other side via BUY_SHORT at 1-bid
    """
    rows = []
    for label, e in in_play(client):
        for m in e.get("markets", []):
            if m.get("status") != "MARKET_STATUS_OPEN":
                continue
            slug = m.get("slug", "")
            bid, ask = d((m.get("bestBidQuote") or {}).get("value")), d((m.get("bestAskQuote") or {}).get("value"))
            # Need a real two-sided quote; an empty book reads as 0 and fakes a signal.
            if bid <= 0 or ask <= 0 or ask <= bid:
                continue
            if ENTRY_MIN <= ask <= ENTRY_MAX:
                rows.append({"side": "long", "px": ask, "exit_bid": bid, "slug": slug,
                             "league": label, "period": e.get("period"), "event": e.get("slug", "")})
            short_px = Decimal("1") - bid
            if bid > 0 and ENTRY_MIN <= short_px <= ENTRY_MAX:
                rows.append({"side": "short", "px": short_px, "exit_bid": Decimal("1") - ask,
                             "slug": slug, "league": label, "period": e.get("period"),
                             "event": e.get("slug", "")})
    rows.sort(key=lambda r: r["px"])
    return rows


def record(client):
    """Log every in-play price to CSV so the dip-and-recover thesis can be checked."""
    new = not TAPE_FILE.exists()
    with TAPE_FILE.open("a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["ts", "league", "event", "market", "period", "bid", "ask",
                        "short_px", "bid_depth", "ask_depth"])
        events = in_play(client)
        for label, e in events:
            for m in e.get("markets", []):
                if m.get("status") != "MARKET_STATUS_OPEN":
                    continue
                slug = m.get("slug", "")
                bid = d((m.get("bestBidQuote") or {}).get("value"))
                ask = d((m.get("bestAskQuote") or {}).get("value"))
                bids, offers = get_book(client, slug)
                bq = bids[0]["qty"] if bids else "0"
                aq = offers[0]["qty"] if offers else "0"
                w.writerow([datetime.now(timezone.utc).isoformat(), label, e.get("slug", ""),
                            slug, e.get("period"), bid, ask, Decimal("1") - bid, bq, aq])
        return len(events)


def show_scan(client):
    events = in_play(client)
    print(f"\n{len(events)} matches in play")
    for label, e in events:
        print(f"\n  {label}  {e.get('title','')[:46]}  [{e.get('period')}]")
        for m in e.get("markets", []):
            slug = m.get("slug", "")
            bid = d((m.get("bestBidQuote") or {}).get("value"))
            ask = d((m.get("bestAskQuote") or {}).get("value"))
            bids, offers = get_book(client, slug)
            bq = bids[0]["qty"] if bids else "0"
            aq = offers[0]["qty"] if offers else "0"
            print(f"    {slug[:44]:46} bid {bid} x{bq:>10}  ask {ask} x{aq:>10}  spread {ask-bid}")
            print(f"    {'':46} short side would cost {Decimal('1')-bid}")
    cands = candidates(client)
    print(f"\n{len(cands)} in the {ENTRY_MIN}-{ENTRY_MAX} entry band")
    for r in cands[:15]:
        print(f"    {r['league']:6} {r['side']:5} @ {r['px']}  exit_bid {r['exit_bid']}  {r['slug'][:40]}")
    print()


def enter(client, cand, state, live):
    key = f"{cand['slug']}|{cand['side']}"
    if key in state["positions"]:
        return False
    px = cand["px"]
    qty = int(STAKE / px)
    intent = "ORDER_INTENT_BUY_LONG" if cand["side"] == "long" else "ORDER_INTENT_BUY_SHORT"
    req = {"marketSlug": cand["slug"], "intent": intent, "type": "ORDER_TYPE_LIMIT",
           "price": {"value": str(px), "currency": "USD"}, "quantity": qty,
           "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL"}
    try:
        client.orders.preview({"request": req})
    except Exception as e:
        log(f"  preview rejected {cand['slug'][:34]}: {type(e).__name__} {str(e)[:70]}")
        return False

    oid = None
    if live:
        try:
            resp = client.orders.create(req)
            oid = (resp.get("order") or {}).get("id") or resp.get("id")
        except Exception as e:
            log(f"  ORDER FAILED {cand['slug'][:34]}: {type(e).__name__} {str(e)[:90]}")
            return False

    state["positions"][key] = {
        "slug": cand["slug"], "side": cand["side"], "entry_px": str(px), "qty": qty,
        "target_px": str(px + EXIT_TICKS * TICK), "league": cand["league"],
        "opened": datetime.now(timezone.utc).isoformat(), "order_id": oid, "live": live,
    }
    log(f"  {'LIVE BUY ' if live else 'paper buy'} {cand['side']:5} {qty} @ {px} = ${px*qty:.2f} "
        f"-> target {px + EXIT_TICKS*TICK}  {cand['slug'][:38]}")
    return True


def check_exits(client, state, live):
    for key, pos in list(state["positions"].items()):
        slug = pos["slug"]
        try:
            mk = client.markets.retrieve_by_slug(slug)
            mk = mk.get("market", mk)
        except Exception:
            continue
        bid = d((mk.get("bestBidQuote") or {}).get("value"))
        ask = d((mk.get("bestAskQuote") or {}).get("value"))
        # What we could sell into right now.
        cur = bid if pos["side"] == "long" else Decimal("1") - ask
        target, entry, qty = Decimal(pos["target_px"]), Decimal(pos["entry_px"]), pos["qty"]

        if cur >= target:
            pnl = (cur - entry) * qty
            if live:
                intent = "ORDER_INTENT_SELL_LONG" if pos["side"] == "long" else "ORDER_INTENT_SELL_SHORT"
                try:
                    client.orders.create({"marketSlug": slug, "intent": intent,
                                          "type": "ORDER_TYPE_LIMIT",
                                          "price": {"value": str(cur), "currency": "USD"},
                                          "quantity": qty,
                                          "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"})
                except Exception as e:
                    log(f"  EXIT FAILED {slug[:34]}: {type(e).__name__}")
                    continue
            log(f"  {'LIVE SELL' if live else 'paper sell'} @ {cur}  pnl {pnl:+.2f}  {slug[:34]}")
            state["closed"].append({**pos, "exit_px": str(cur), "pnl": str(pnl), "reason": "target"})
            del state["positions"][key]

        elif mk.get("status") != "MARKET_STATUS_OPEN" or mk.get("closed"):
            pnl = -entry * qty
            log(f"  market closed, position expired  pnl {pnl:+.2f}  {slug[:34]}")
            state["closed"].append({**pos, "exit_px": "0", "pnl": str(pnl), "reason": "resolved"})
            del state["positions"][key]


def summary(state):
    closed = state["closed"]
    if not closed:
        return
    total = sum(Decimal(c["pnl"]) for c in closed)
    wins = sum(1 for c in closed if Decimal(c["pnl"]) > 0)
    log(f"closed {len(closed)} | wins {wins} | net {total:+.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--record", action="store_true", help="log price paths, never trade")
    ap.add_argument("--max-spend", type=float, default=10.0)
    args = ap.parse_args()

    client = PolymarketUS(
        key_id=os.environ.get("POLYMARKET_KEY_ID"),
        secret_key=os.environ.get("POLYMARKET_SECRET_KEY"),
    )

    if args.scan:
        show_scan(client)
        return

    if args.record:
        log(f"recording to {TAPE_FILE.name}, ctrl-c to stop")
        try:
            while True:
                n = record(client)
                log(f"sampled {n} in-play matches")
                time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            log("stopped")
        return

    state = load_state()
    spent = Decimal("0")
    log(f"{'LIVE' if args.live else 'PAPER'} | stake ${STAKE} | band {ENTRY_MIN}-{ENTRY_MAX} | "
        f"exit +{EXIT_TICKS} ticks | in-play only")
    try:
        while True:
            check_exits(client, state, args.live)
            if len(state["positions"]) < MAX_POSITIONS:
                for cand in candidates(client):
                    if len(state["positions"]) >= MAX_POSITIONS:
                        break
                    if args.live and spent + STAKE > Decimal(str(args.max_spend)):
                        log("spend cap reached")
                        break
                    if enter(client, cand, state, args.live):
                        spent += STAKE
            save_state(state)
            summary(state)
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        save_state(state)
        summary(state)
        log("stopped")


if __name__ == "__main__":
    main()
