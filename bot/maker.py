"""Paper market maker: quote both sides, but only where the spread pays.

Every engine before this one paid the spread to get in. This one charges it.
We post a bid and an ask at the same time and let other people trade with us;
we take no view on who wins. The income is the gap between our two prices, and
the cost is the inventory we get stuck with when only one side fills and the
price keeps going.

Measured over ~1 week of tape across six sports, that trade is only worth doing
in wide markets:

    spread >= 1 tick    +0.126c per share captured  - loses badly
    spread >= 4 ticks   +1.7c                       - about break-even
    spread >= 6 ticks   +3.4c to +7.2c              - profitable

so below MIN_SPREAD we stand aside and do nothing, which is most of the time.
Pooled across six sports at a realistic 0.2s latency the simulation put this at
+$3,319/week, CI [+$1,717, +$5,082]. That number assumes something this engine
exists to test: that nobody else shows up.

WHAT THIS ENGINE IS ACTUALLY FOR

The backtest cannot answer the question that decides the strategy, because our
tape only records a world in which we were not quoting. A 6-tick spread exists
*because* nobody wants to provide liquidity there, and if we step in, one
competitor posting a tick better takes the edge from +$2,027 to +$227 on
tennis. So this engine's primary output is not its P&L - it is `undercut` and
`alone_secs`: how long a quote of ours would have stood as the best price
before somebody else matched or beat it. We are not in the real book, so every
price we see in it belongs to someone else; if the real bid climbs to where we
would have been, that is a competitor arriving.

Nothing here sends an order. Fills come from the trade feed: a taker selling at
or below our bid trades with us, because posting inside the touch puts us alone
at the front of the queue.
"""

import collections
import json
from datetime import datetime, timezone
from decimal import Decimal

from . import config

ZERO = Decimal("0")


class Maker:
    def __init__(self, log, on_fill=None):
        self.log = log
        # Called with each fill so the recorder can tape it. Kept as a callback
        # rather than a Store reference so the engine stays testable offline.
        self.on_fill = on_fill
        # Last N fills, for the status page. Bounded: a busy day is thousands.
        self.recent = collections.deque(maxlen=40)
        self.books = {}          # slug -> per-market state
        self.closed = []         # settled markets, for the running total
        self._load()

    # --- persistence ---------------------------------------------------------
    def _load(self):
        p = config.MAKER_STATE
        if not p.exists():
            return
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            return
        self.books = d.get("books") or {}
        self.closed = d.get("closed") or []

    def save(self):
        try:
            config.MAKER_STATE.write_text(json.dumps(
                {"books": self.books, "closed": self.closed}, indent=1))
        except OSError as e:
            self.log(f"maker save failed: {type(e).__name__}")

    def _state(self, slug, league):
        st = self.books.get(slug)
        if st is None:
            st = self.books[slug] = {
                "league": league,
                "inv": "0", "cash": "0",
                "bought": "0", "sold": "0",
                "bid": None, "ask": None,       # our resting quotes
                "fills": 0, "quotes": 0,
                # The competition metrics this engine exists to collect.
                "undercut": 0,                  # someone matched or beat us
                "alone_secs": 0.0,              # time our quote stood best
                "last_seen": None,              # for accumulating alone_secs
                "last_mid": None,
                "last_bid": None, "last_ask": None,
            }
        return st

    # --- book ---------------------------------------------------------------
    def on_book(self, slug, league, bid, ask, tick, now):
        """Requote on a book update, and notice if anyone has joined us."""
        if config.MAKER_PREFIXES and not slug.startswith(config.MAKER_PREFIXES):
            return
        if config.MAKER_SPORTS and config.sport_of(league) not in config.MAKER_SPORTS:
            return
        st = self._state(slug, league)
        st["last_mid"] = str((bid + ask) / 2)
        st["last_bid"], st["last_ask"] = str(bid), str(ask)
        tick = tick or config.DEFAULT_TICK
        ticks = int((ask - bid) / tick)

        # Credit the time our previous quote spent as the best price, and count
        # an undercut when the real book has reached it. `bid` here is someone
        # else's order, because we never actually place ours.
        prev_bid = Decimal(st["bid"]) if st["bid"] else None
        prev_ask = Decimal(st["ask"]) if st["ask"] else None
        if prev_bid is not None and st["last_seen"] is not None:
            dt = max(0.0, now - st["last_seen"])
            # STRICTLY better only. A book whose bid merely RISES TO our price
            # is the market drifting up to us, which is ordinary making; a bid
            # strictly through our price means somebody chose to stand inside
            # it. Counting the first as competition put the "undercut rate" at
            # 66%, which measured price movement, not rivals.
            beat = bid > prev_bid or (prev_ask is not None and ask < prev_ask)
            if beat:
                st["undercut"] += 1
            else:
                st["alone_secs"] = round(st["alone_secs"] + dt, 1)
        st["last_seen"] = now

        if ticks < config.MAKER_MIN_SPREAD:
            # Not worth standing here. Pull both sides.
            st["bid"] = st["ask"] = None
            return

        # Post INSIDE ticks off the touch, in whole ticks, never crossing.
        step = tick * min(config.MAKER_INSIDE, (ticks - 1) // 2)
        nb, na = bid + step, ask - step
        inv = Decimal(st["inv"])
        # Stop adding to a side we are already loaded on.
        if inv >= config.MAKER_MAX_INV:
            nb = None
        if inv <= -config.MAKER_MAX_INV:
            na = None
        if str(nb) != str(st["bid"]) or str(na) != str(st["ask"]):
            st["quotes"] += 1
        st["bid"] = str(nb) if nb is not None else None
        st["ask"] = str(na) if na is not None else None

    def _book_fill(self, slug, st, side, px, qty):
        """Record a fill: state, the rolling feed, and the tape."""
        st["fills"] += 1
        if side == "buy":
            st["cash"] = str(Decimal(st["cash"]) - qty * px)
            st["inv"] = str(Decimal(st["inv"]) + qty)
            st["bought"] = str(Decimal(st["bought"]) + qty)
        else:
            st["cash"] = str(Decimal(st["cash"]) + qty * px)
            st["inv"] = str(Decimal(st["inv"]) - qty)
            st["sold"] = str(Decimal(st["sold"]) + qty)
        bid, ask = st.get("last_bid"), st.get("last_ask")
        ticks = (int((Decimal(ask) - Decimal(bid)) / config.DEFAULT_TICK)
                 if bid and ask else None)
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "league": st["league"], "sport": config.sport_of(st["league"]),
            "market": slug, "side": side, "px": str(px), "qty": str(qty),
            "bid": bid, "ask": ask, "spread_ticks": ticks,
            "mid": st["last_mid"], "inv_after": st["inv"],
            "cash_after": st["cash"],
        }
        self.recent.appendleft(row)
        if self.on_fill:
            try:
                self.on_fill(row)
            except Exception:
                pass

    # --- trades -------------------------------------------------------------
    def on_trade(self, slug, price, qty, taker_side):
        """A taker crossing to our price trades with us."""
        st = self.books.get(slug)
        if st is None or price is None or qty is None:
            return
        side = (taker_side or "").upper()
        inv = Decimal(st["inv"])
        size = Decimal(config.MAKER_SIZE)

        if st["bid"] and side.endswith("SELL"):
            mine = Decimal(st["bid"])
            if price <= mine and inv < config.MAKER_MAX_INV:
                take = min(qty, size, config.MAKER_MAX_INV - inv)
                if take > 0:
                    self._book_fill(slug, st, "buy", mine, take)
        elif st["ask"] and side.endswith("BUY"):
            mine = Decimal(st["ask"])
            if price >= mine and inv > -config.MAKER_MAX_INV:
                take = min(qty, size, config.MAKER_MAX_INV + inv)
                if take > 0:
                    self._book_fill(slug, st, "sell", mine, take)

    # --- settlement ---------------------------------------------------------
    def reap(self, live_markets=None, settle=None):
        """Close out markets that are gone, marking inventory at settlement.

        A market marked at its last mid instead of its real settlement flatters
        leftover inventory, so which of the two was used is recorded per market
        rather than averaged away.
        """
        if live_markets is None:
            return
        for slug in [s for s in self.books if s not in live_markets]:
            st = self.books.pop(slug)
            sv = settle(slug) if settle else None
            close = sv if sv is not None else (
                Decimal(st["last_mid"]) if st["last_mid"] else None)
            if close is None:
                continue
            inv = Decimal(st["inv"])
            pnl = Decimal(st["cash"]) + inv * Decimal(close)
            self.closed.append({
                "slug": slug, "league": st["league"],
                "pnl": str(round(pnl, 4)),
                "fills": st["fills"], "quotes": st["quotes"],
                "bought": st["bought"], "sold": st["sold"],
                "undercut": st["undercut"],
                "alone_secs": st["alone_secs"],
                "marked": "settled" if sv is not None else "last_mid",
            })

    # --- reporting ----------------------------------------------------------
    def summary(self):
        net = sum(Decimal(c["pnl"]) for c in self.closed) if self.closed else ZERO
        # Count open markets as well. Everything interesting happens before a
        # market settles, so reporting only closed ones shows an active engine
        # as idle.
        fills = (sum(c["fills"] for c in self.closed)
                 + sum(st["fills"] for st in self.books.values()))
        under = (sum(c["undercut"] for c in self.closed)
                 + sum(st["undercut"] for st in self.books.values()))
        alone = (sum(c["alone_secs"] for c in self.closed)
                 + sum(st["alone_secs"] for st in self.books.values()))
        open_inv = sum(abs(Decimal(st["inv"])) for st in self.books.values())
        # Mark open markets at the last mid we saw. `net` alone counts only
        # settled markets, so early on it reads 0.00 beside hundreds of fills
        # and a live position - which looks like the engine is doing nothing.
        unreal = ZERO
        for st in self.books.values():
            if st["last_mid"]:
                unreal += (Decimal(st["cash"])
                           + Decimal(st["inv"]) * Decimal(st["last_mid"]))
        quoting = sum(1 for st in self.books.values() if st["bid"] or st["ask"])
        return {
            "open": len(self.books), "quoting_now": quoting,
            "closed": len(self.closed), "net": str(round(net, 2)),
            "fills": fills, "undercut": under,
            "open_inventory": str(open_inv),
            "unrealised": str(round(unreal, 2)),
            "net_incl_open": str(round(net + unreal, 2)),
            "alone_secs": round(alone, 1),
            "recent": list(self.recent)[:25],
            "positions": sorted(
                ({"market": k, "league": v["league"], "inv": v["inv"],
                  "cash": v["cash"], "mid": v["last_mid"], "fills": v["fills"],
                  "bid": v["bid"], "ask": v["ask"],
                  "mark": str(round(Decimal(v["cash"]) + Decimal(v["inv"])
                                    * Decimal(v["last_mid"]), 2))
                          if v["last_mid"] else None}
                 for k, v in self.books.items()
                 if Decimal(v["inv"]) != 0),
                key=lambda r: -abs(Decimal(r["inv"])))[:20],
            "settled_frac": (
                round(sum(1 for c in self.closed if c["marked"] == "settled")
                      / len(self.closed), 3) if self.closed else None),
        }
