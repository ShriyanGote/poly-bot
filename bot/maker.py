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
import csv
import gzip
import json
import time
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
        # Markets that have finished but whose settlement the venue has not
        # published yet. They wait here rather than being closed at the last
        # mid, because marking an unsettled binary at its mid is the bias that
        # once produced a fake +323% return at a 100% win rate.
        self.pending = {}
        # Markets the last complete discovery sweep listed. None until the
        # first one, so a restart does not stall quoting while it runs.
        self.live = None
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
        self.pending = d.get("pending") or {}
        # gone_at used to be stamped with the event loop's clock, which counts
        # from boot rather than 1970, while the requeue below stamps wall time.
        # Mixed, the wait came out ~56 years or negative. Move the old stamps
        # onto the wall clock.
        skew = time.time() - time.monotonic()
        for st in self.pending.values():
            g = st.get("gone_at")
            if g is not None and g < 1e9:
                st["gone_at"] = g + skew
        # One-time repair. The first version of reap() closed a market the
        # instant it left the subscription list, marking it at the last mid
        # because the venue had not published a settlement yet - so every
        # market was scored on a price instead of an outcome. Those records are
        # identifiable: marked "last_mid" but with no waited_hours, which only
        # the grace-expiry path writes. Put them back in the queue so they get
        # scored properly if the settlement has since landed.
        # An earlier pass through this migration wrote those same records as
        # "last_mid" with waited_hours 0.0, which no longer matches the requeue
        # test below. They are still identifiable: a genuine grace expiry only
        # happens after MAKER_SETTLE_GRACE, so zero hours waited means it was
        # never waited for at all.
        for c in self.closed:
            if (c.get("marked") == "last_mid"
                    and c.get("waited_hours") == 0.0):
                c["marked"] = "pre_fix"
        requeue = [c for c in self.closed
                   if c.get("marked") == "last_mid" and "waited_hours" not in c]
        if requeue:
            for c in requeue:
                self.closed.remove(c)
                self.pending.setdefault(c["slug"], {
                    "league": c.get("league", "?"),
                    "inv": "0", "cash": c["pnl"],
                    "bought": c.get("bought", "0"), "sold": c.get("sold", "0"),
                    "bid": None, "ask": None,
                    "fills": c.get("fills", 0), "quotes": c.get("quotes", 0),
                    "undercut": c.get("undercut", 0),
                    "alone_secs": c.get("alone_secs", 0.0),
                    "last_seen": None, "last_mid": None,
                    "last_bid": None, "last_ask": None,
                    "gone_at": time.time(),
                    # The position is gone; only its P&L survives, so re-score
                    # is impossible. Keep it flagged rather than pretend.
                    "unrecoverable": True,
                })
            self.log(f"maker: requeued {len(requeue)} market(s) that were "
                     f"closed at the last mid before settlement was available")
        self._backfill_costs()

    def _backfill_costs(self):
        """Recover what each market's shares cost, for records made before
        buy_cost and sell_proc were tracked.

        `cash` alone cannot say whether +$10 came from selling 100 at 0.60
        against 100 bought at 0.50, or from one lucky fill. The fill log has
        every price, but it only started partway through: fills before it are
        known only as totals. Those split exactly when they all went one way,
        and are left unknown (None) otherwise rather than guessed.
        """
        closed = [c for c in self.closed if c.get("marked") != "pre_fix"]
        need = [st for st in (*self.books.values(), *self.pending.values(),
                              *closed) if "buy_cost" not in st]
        if not need:
            return
        try:
            sets = json.loads((config.DATA / "settlements.json").read_text())
        except (OSError, ValueError):
            sets = {}
        for c in closed:
            if "close" not in c:
                v = sets.get(c["slug"]) if c.get("marked") == "settled" else None
                inv = Decimal(c.get("bought", "0")) - Decimal(c.get("sold", "0"))
                c["inv"] = str(inv)
                c["close"] = v
                c["cash"] = (str(Decimal(c["pnl"]) - inv * Decimal(v))
                             if v is not None else None)
        log = {}
        for f in sorted(config.DATA.glob("mmfills-*.csv.gz")):
            for r in _read_gz_csv(f):
                if r.get("market") and r.get("qty"):
                    log.setdefault(r["market"], []).append(r)
        slug_of = {id(st): k for k, st in (*self.books.items(),
                                           *self.pending.items())}
        slug_of.update({id(c): c["slug"] for c in closed})
        for st in need:
            if not st.get("fills"):
                st["buy_cost"], st["sell_proc"] = "0", "0"
                continue
            rows = sorted(log.get(slug_of[id(st)], []), key=lambda r: r["ts"])
            lb = ls = lc = lp = Decimal(0)
            for r in rows:
                q, px = Decimal(r["qty"]), Decimal(r["px"])
                if r["side"] == "buy":
                    lb, lc = lb + q, lc + q * px
                else:
                    ls, lp = ls + q, lp + q * px
            last = rows[-1] if rows else None
            if last and (st.get("cash") is None
                         or abs(Decimal(last["inv_after"]) - Decimal(st["inv"])) > Decimal("0.000001")
                         or abs(Decimal(last["cash_after"]) - Decimal(st["cash"])) > Decimal("0.000001")):
                # Fills missing after the log's last row (a crash lost its
                # unflushed tail), so the gap is not all before the log.
                st["buy_cost"] = st["sell_proc"] = None
                continue
            if rows:
                r = rows[0]
                q, px = Decimal(r["qty"]), Decimal(r["px"])
                cash0 = Decimal(r["cash_after"]) + (q * px if r["side"] == "buy"
                                                    else -q * px)
            elif st.get("cash") is not None:
                cash0 = Decimal(st["cash"])
            else:
                cash0 = None
            pre_b = Decimal(st.get("bought", "0")) - lb
            pre_s = Decimal(st.get("sold", "0")) - ls
            eps = Decimal("0.000001")
            if abs(pre_b) < eps and abs(pre_s) < eps:
                pc = pp = Decimal(0)
            elif cash0 is not None and abs(pre_s) < eps:
                pc, pp = -cash0, Decimal(0)
            elif cash0 is not None and abs(pre_b) < eps:
                pc, pp = Decimal(0), cash0
            else:
                st["buy_cost"] = st["sell_proc"] = None
                continue
            st["buy_cost"], st["sell_proc"] = str(pc + lc), str(pp + lp)

    def save(self):
        try:
            config.MAKER_STATE.write_text(json.dumps(
                {"books": self.books, "closed": self.closed,
                 "pending": self.pending}, indent=1))
        except OSError as e:
            self.log(f"maker save failed: {type(e).__name__}")

    def _state(self, slug, league):
        st = self.books.get(slug)
        if st is None and slug in self.pending:
            # Parked by a sweep that missed it (a one-sided book drops out of
            # discovery), yet it is still trading. Resume its position; a
            # fresh book here would later overwrite the parked one on reap.
            st = self.books[slug] = self.pending.pop(slug)
            st.pop("gone_at", None)
        if st is None:
            st = self.books[slug] = {
                "league": league,
                "inv": "0", "cash": "0",
                "bought": "0", "sold": "0",
                "buy_cost": "0", "sell_proc": "0",
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
        if self.live is not None and slug not in self.live:
            # A finished match keeps streaming, often a gutted book whose
            # "mid" is meaningless; quoting or marking it is fiction.
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
            if st.get("buy_cost") is not None:
                st["buy_cost"] = str(Decimal(st["buy_cost"]) + qty * px)
        else:
            st["cash"] = str(Decimal(st["cash"]) + qty * px)
            st["inv"] = str(Decimal(st["inv"]) - qty)
            st["sold"] = str(Decimal(st["sold"]) + qty)
            if st.get("sell_proc") is not None:
                st["sell_proc"] = str(Decimal(st["sell_proc"]) + qty * px)
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
    def reap(self, live_markets=None, settle=None, now=None):
        """Retire finished markets, but only score them once they settle.

        A market leaves the subscription list as soon as its match ends, while
        the venue does not answer for its settlement until some time later. The
        first version closed immediately at the last mid, so every market was
        scored on a price rather than an outcome - which is the exact bias that
        has flattered this project before. So park them and keep asking.
        """
        if live_markets is None:
            return
        self.live = set(live_markets)
        now = now or time.time()
        for slug in [s for s in self.books if s not in live_markets]:
            st = self.books.pop(slug)
            st["gone_at"] = now
            self.pending[slug] = st

        for slug, st in list(self.pending.items()):
            sv = settle(slug) if settle else None
            waited = now - (st.get("gone_at") or now)
            if st.get("unrecoverable"):
                # Its position was already collapsed into a P&L by the old
                # code, so there is nothing left to re-mark. Keep the number,
                # keep the flag, and let settled_frac show it honestly.
                self.closed.append({
                    "slug": slug, "league": st["league"], "pnl": st["cash"],
                    "fills": st["fills"], "quotes": st["quotes"],
                    "bought": st["bought"], "sold": st["sold"],
                    "undercut": st["undercut"], "alone_secs": st["alone_secs"],
                    "marked": "pre_fix", "waited_hours": 0.0,
                })
                self.pending.pop(slug)
                continue
            if sv is None and waited < config.MAKER_SETTLE_GRACE:
                continue                     # still worth waiting for
            close = sv if sv is not None else (
                Decimal(st["last_mid"]) if st["last_mid"] else None)
            if close is None:
                self.pending.pop(slug)       # nothing to score it on at all
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
                "waited_hours": round(waited / 3600, 2),
                "inv": st["inv"], "cash": st["cash"], "close": str(close),
                "buy_cost": st.get("buy_cost"), "sell_proc": st.get("sell_proc"),
                "closed_at": datetime.now(timezone.utc).isoformat(),
            })
            self.pending.pop(slug)

    # --- reporting ----------------------------------------------------------
    def summary(self, title=None):
        title = title or (lambda slug: None)
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
        scorable = [c for c in self.closed if c["marked"] != "pre_fix"]
        inv_pending = sum(1 for st in self.pending.values()
                          if Decimal(st["inv"]) != 0)
        return {
            "open": len(self.books), "quoting_now": quoting,
            "awaiting_settlement": len(self.pending),
            "awaiting_with_inventory": inv_pending,
            "closed": len(self.closed), "net": str(round(net, 2)),
            "fills": fills, "undercut": under,
            "open_inventory": str(open_inv),
            "unrealised": str(round(unreal, 2)),
            "net_incl_open": str(round(net + unreal, 2)),
            "alone_secs": round(alone, 1),
            "recent": [{**r, "title": title(r.get("market", ""))}
                       for r in list(self.recent)[:25]],
            "settled": [_explain(c, title(c["slug"]))
                        for c in reversed(self.closed)][:200],
            "positions": sorted(
                ({"market": k, "title": title(k),
                  "league": v["league"], "inv": v["inv"],
                  "cash": v["cash"], "mid": v["last_mid"], "fills": v["fills"],
                  "bid": v["bid"], "ask": v["ask"],
                  "mark": str(round(Decimal(v["cash"]) + Decimal(v["inv"])
                                    * Decimal(v["last_mid"]), 2))
                          if v["last_mid"] else None}
                 for k, v in self.books.items()
                 if Decimal(v["inv"]) != 0),
                key=lambda r: -abs(Decimal(r["inv"])))[:20],
            # Scored over markets the current logic actually handled; the
            # pre-fix records are reported separately rather than folded in.
            "settled_frac": (
                round(sum(1 for c in scorable if c["marked"] == "settled")
                      / len(scorable), 3) if scorable else None),
            "pre_fix_closed": sum(1 for c in self.closed
                                  if c["marked"] == "pre_fix"),
        }


def _read_gz_csv(path):
    """Rows of a gzipped CSV, including one still being written to (its
    stream has no end marker yet, which gzip reports as an error at the end)."""
    rows = []
    try:
        with gzip.open(path, "rt", newline="") as f:
            for r in csv.DictReader(f):
                rows.append(r)
    except (EOFError, OSError, gzip.BadGzipFile, csv.Error):
        pass
    return rows


def _explain(c, title):
    """One closed market, told as what happened to it.

    P&L splits into what the two-sided trading earned on shares we both bought
    and sold (the spread) and what the result did to the shares left over.
    Computed from average prices, then checked against the recorded total:
    if the two do not add back up, the split is withheld rather than shown.
    """
    out = {k: c.get(k) for k in ("slug", "league", "pnl", "fills", "bought",
                                 "sold", "inv", "close", "marked",
                                 "waited_hours", "closed_at")}
    out["title"] = title
    b, s = Decimal(c.get("bought") or 0), Decimal(c.get("sold") or 0)
    bc, sp = c.get("buy_cost"), c.get("sell_proc")
    out["avg_buy"] = str(round(Decimal(bc) / b, 4)) if bc is not None and b else None
    out["avg_sell"] = str(round(Decimal(sp) / s, 4)) if sp is not None and s else None
    out["spread_pnl"] = out["result_pnl"] = None
    if bc is None or sp is None or c.get("close") is None:
        return out
    v, bc, sp = Decimal(c["close"]), Decimal(bc), Decimal(sp)
    pb = bc / b if b else Decimal(0)
    ps = sp / s if s else Decimal(0)
    matched = min(b, s)
    spread = matched * (ps - pb)
    left = b - s
    result = left * (v - pb) if left > 0 else -left * (ps - v)
    if abs(spread + result - Decimal(c["pnl"])) > Decimal("0.01"):
        return out
    out["spread_pnl"] = str(round(spread, 2))
    out["result_pnl"] = str(round(result, 2))
    return out
