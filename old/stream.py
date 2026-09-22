"""WebSocket price recorder for in-play markets.

Push-based: sub-second updates with no polling and no rate limits. REST is
used only to discover which matches are in play (once a minute).

    python stream.py            # record in-play books to tape.csv
    python stream.py --band     # also alert when a side enters 1-5c
"""

import argparse
import asyncio
import csv
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv
from polymarket_us import PolymarketUS

load_dotenv()

HERE = Path(__file__).parent
TAPE = HERE / "tape.csv"

SERIES = {
    16: "ATP", 17: "WTA", 55: "ITFM", 56: "ITFW", 79: "ITFME", 80: "ITFWO",
    156: "ATPCQ", 275: "WTADB", 266: "ATPDB", 330: "UTR",
    11: "EPL", 12: "UCL", 10: "MLS", 113: "BRA", 117: "CSL", 123: "RPL",
    18: "BUN", 20: "LAL", 19: "SEA", 219: "LIG2", 218: "LIG1",
}
SKIP_PERIODS = ("NS", "", None, "FT", "AET", "PEN", "ENDED", "FINAL", "POSTP", "CANC", "SUSP")
BAND_LO, BAND_HI = Decimal("0.01"), Decimal("0.05")
DISCOVER_EVERY = 60  # seconds between REST discovery sweeps


def log(m):
    print(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {m}", flush=True)


def d(v):
    return Decimal(str(v or "0"))


def discover(client):
    """REST sweep for in-play market slugs. Kept slow to respect the rate limit."""
    found = {}
    for sid, label in SERIES.items():
        try:
            res = client.search.query({"seriesIds": [sid], "status": "active", "limit": 20})
        except Exception:
            continue
        for e in res.get("events", []):
            if e.get("closed") or e.get("period") in SKIP_PERIODS:
                continue
            for m in e.get("markets", []):
                if m.get("status") == "MARKET_STATUS_OPEN" and m.get("slug"):
                    found[m["slug"]] = (label, e.get("slug", ""), e.get("period"))
    return found


class Recorder:
    def __init__(self, alert_band):
        self.alert_band = alert_band
        self.meta = {}
        self.msgs = 0
        self.in_band = set()
        new = not TAPE.exists()
        self.fh = TAPE.open("a", newline="")
        self.w = csv.writer(self.fh)
        if new:
            self.w.writerow(["ts", "league", "event", "market", "period",
                             "bid", "ask", "short_px", "bid_depth", "ask_depth"])

    def on_message(self, payload, *_):
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                return
        md = (payload or {}).get("marketData")
        if not md:
            return
        slug = md.get("marketSlug")
        if not slug:
            return
        bids, offers = md.get("bids") or [], md.get("offers") or []
        if not bids or not offers:
            return
        bid, ask = d(bids[0]["px"]["value"]), d(offers[0]["px"]["value"])
        bq, aq = bids[0]["qty"], offers[0]["qty"]
        league, event, period = self.meta.get(slug, ("?", "?", "?"))
        self.msgs += 1
        self.w.writerow([datetime.now(timezone.utc).isoformat(), league, event, slug,
                         period, bid, ask, Decimal("1") - bid, bq, aq])

        if self.alert_band:
            short_px = Decimal("1") - bid
            for side, px, depth in (("long", ask, aq), ("short", short_px, bq)):
                key = f"{slug}|{side}"
                if BAND_LO <= px <= BAND_HI:
                    if key not in self.in_band:
                        self.in_band.add(key)
                        log(f"  IN BAND  {league} {side} @ {px}  depth {depth}  {slug[:40]}")
                else:
                    self.in_band.discard(key)

    def close(self):
        self.fh.flush()
        self.fh.close()


async def run(alert_band):
    client = PolymarketUS(
        key_id=os.environ["POLYMARKET_KEY_ID"],
        secret_key=os.environ["POLYMARKET_SECRET_KEY"],
    )
    rec = Recorder(alert_band)
    ws = client.ws.markets()
    ws.on("message", rec.on_message)
    ws.on("error", lambda *a: log(f"WS ERROR {a}"))
    await ws.connect()
    log("websocket connected")

    subscribed, req = set(), 0
    try:
        while True:
            found = discover(client)
            rec.meta.update(found)
            new = [s for s in found if s not in subscribed]
            if new:
                req += 1
                await ws.subscribe_market_data(f"r{req}", new)
                subscribed |= set(new)
                log(f"subscribed to {len(new)} new market(s); {len(subscribed)} total")
            if not subscribed:
                log("nothing in play right now; re-checking in 60s")
            else:
                log(f"{len(subscribed)} markets live | {rec.msgs} updates recorded")
            await asyncio.sleep(DISCOVER_EVERY)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        rec.close()
        log(f"stopped after {rec.msgs} updates")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--band", action="store_true", help="alert when a side enters 1-5c")
    args = ap.parse_args()
    try:
        asyncio.run(run(args.band))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
