"""Real-money shadow of the paper maker, capped at a few dollars.

The paper maker assumes a fill whenever a trade prints at or through our
price. It cannot know where we would sit in the queue, how long our orders
take to land, or whether the flow that hits a resting order is the flow that
loses to it. This engine answers those questions with real orders, at a size
where the answer costs almost nothing:

    - the same rule as paper (quote 1 tick inside when the spread is >= 6
      ticks), in a few markets the paper maker is already quoting
    - post-only (participateDontInitiate), so it can never take liquidity
    - worst-case loss across every position AND every resting order capped at
      REALMM_BUDGET, checked before each order goes out

and it writes down, for each order, how long the venue took to acknowledge
it; for each fill, how late we heard; and for each public trade printing at
our price, whether we actually got filled. That last one is the number paper
cannot produce: paper counts every such print as ours.

Accounting is in long-outcome terms throughout, the way the venue prices
every order: buying the short side at 0.30 is selling the long at 0.70. So
a position is `n` long-equivalent shares plus `cash`, and it settles to
cash + n * outcome, exactly like the paper maker's books.

Safety, in the order it matters:
    - off unless REALMM_ENABLED
    - the budget check assumes every resting opening order fills and the
      match goes the wrong way
    - if the private order stream drops, or the venue's positions disagree
      with ours twice running, everything resting is cancelled and the
      engine halts until a person clears data/realmm_state.json's "halted"
    - on shutdown and on startup, every resting order in our markets is
      cancelled
"""

import asyncio
import collections
import json
import os
import statistics
import time
from datetime import datetime, timezone
from decimal import Decimal

from polymarket_us import AsyncPolymarketUS
from polymarket_us.errors import NotFoundError, RateLimitError

from . import config

ZERO = Decimal(0)
BUY_INTENTS = ("ORDER_INTENT_BUY_LONG", "ORDER_INTENT_SELL_SHORT")


def _dec(x):
    if isinstance(x, dict):
        x = x.get("value")
    return Decimal(str(x)) if x not in (None, "") else None


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _venue_secs(iso):
    """Venue timestamp -> epoch seconds, or None."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def worst_loss(n, cash, bid=None, ask=None):
    """Most this market can lose: every combination of our opening orders
    filling, times either outcome. `bid`/`ask` are (price, qty) or None."""
    worst = ZERO
    for fill_bid in ((False, True) if bid else (False,)):
        for fill_ask in ((False, True) if ask else (False,)):
            nn, cc = n, cash
            if fill_bid:
                nn, cc = nn + bid[1], cc - bid[0] * bid[1]
            if fill_ask:
                nn, cc = nn - ask[1], cc + ask[0] * ask[1]
            worst = min(worst, cc, cc + nn)        # settles 0, settles 1
    return -worst


class RealMaker:
    def __init__(self, log, write=None, title=None):
        self.log = log
        self.write = write or (lambda row: None)
        self.title = title or (lambda slug: None)
        self.enabled = config.REALMM_ENABLED
        self.client = None
        self.mk = {}                 # slug -> market state (persisted)
        self.closed = []             # settled markets (persisted)
        self.halted = None           # reason string (persisted)
        self.seen_exec = collections.deque(maxlen=5000)
        self.lat = {k: collections.deque(maxlen=1000)
                    for k in ("ack", "cancel", "decide", "fill_heard", "ws_new")}
        self.prints = []             # public trades at our price, awaiting a verdict
        self.print_stats = {"hit": 0, "missed": 0, "shares_printed": ZERO,
                            "shares_filled": ZERO}
        self.counts = collections.Counter()
        self.recent = collections.deque(maxlen=40)
        self.pws = None
        self.pws_ok = False
        self._kick = {}
        self._tasks = {}
        self._action_times = collections.deque(maxlen=64)
        self._backoff_until = 0.0
        self._drift = {}
        self._errors = collections.deque(maxlen=20)
        self._load()

    # --- persistence ----------------------------------------------------------
    def _load(self):
        try:
            d = json.loads(config.REALMM_STATE.read_text())
        except (OSError, ValueError):
            return
        self.mk = d.get("markets") or {}
        self.closed = d.get("closed") or []
        self.halted = d.get("halted")
        self.counts.update(d.get("counts") or {})
        ps = d.get("print_stats") or {}
        for k in ("hit", "missed"):
            self.print_stats[k] = int(ps.get(k, 0))
        for k in ("shares_printed", "shares_filled"):
            self.print_stats[k] = Decimal(str(ps.get(k, 0)))
        for m in self.mk.values():
            m["orders"] = {"bid": None, "ask": None}    # re-learned from venue
            m["want"] = {"bid": None, "ask": None}

    def save(self):
        d = {"markets": {k: {**v, "want": None} for k, v in self.mk.items()},
             "closed": self.closed, "halted": self.halted,
             "counts": dict(self.counts),
             "print_stats": {k: str(v) for k, v in self.print_stats.items()}}
        try:
            tmp = config.REALMM_STATE.with_suffix(".tmp")
            tmp.write_text(json.dumps(d, indent=1, default=str))
            tmp.replace(config.REALMM_STATE)
        except OSError as e:
            self.log(f"REALMM save failed: {e}")

    # --- lifecycle ------------------------------------------------------------
    async def start(self):
        if not self.enabled:
            return
        self.client = AsyncPolymarketUS(
            key_id=os.environ["POLYMARKET_KEY_ID"],
            secret_key=os.environ["POLYMARKET_SECRET_KEY"])
        await self._cancel_everything("startup")
        asyncio.ensure_future(self._private_loop())
        asyncio.ensure_future(self._housekeeping())
        self.log(f"REALMM armed: budget ${config.REALMM_BUDGET}, "
                 f"{config.REALMM_SIZE} shares/quote, max "
                 f"{config.REALMM_MAX_MARKETS} markets"
                 + (f" - HALTED: {self.halted}" if self.halted else ""))

    async def stop(self):
        if not self.enabled or self.client is None:
            return
        await self._cancel_everything("shutdown")
        self.save()

    async def _cancel_everything(self, why):
        """Cancel every resting order in any market we have touched. Orders
        in other markets are not ours and are left alone."""
        slugs = list(self.mk)
        try:
            if slugs:
                await self.client.orders.cancel_all({"slugs": slugs})
            left = (await self.client.orders.list({"slugs": slugs}) if slugs
                    else {"orders": []}).get("orders") or []
        except Exception as e:
            self.log(f"REALMM cancel-all ({why}) FAILED: {type(e).__name__}: "
                     f"{str(e)[:100]} - CHECK OPEN ORDERS BY HAND")
            return False
        for m in self.mk.values():
            m["orders"] = {"bid": None, "ask": None}
        if left:
            self.log(f"REALMM cancel-all ({why}): {len(left)} order(s) STILL "
                     f"OPEN - CHECK BY HAND")
            return False
        self.log(f"REALMM cancel-all ({why}): nothing resting")
        return True

    async def halt(self, reason):
        if self.halted:
            return
        self.halted = reason
        self.log(f"REALMM HALT: {reason}")
        self.row("halt", "", detail=reason)
        await self._cancel_everything("halt")
        self.save()

    # --- the book without us --------------------------------------------------
    def ex_self(self, slug, bids, offers):
        """The book as everyone else sees it minus our own orders.

        Our resting bid IS the best bid once it lands. Quote off that and we
        chase ourselves a tick at a time; the paper maker, reading the same
        book, would do the same and count us as a competitor.
        """
        m = self.mk.get(slug)
        if not m:
            return bids, offers
        out = []
        for levels, key in ((bids, "bid"), (offers, "ask")):
            o = m["orders"].get(key)
            if not o or not o.get("leaves"):
                out.append(levels)
                continue
            px, mine = Decimal(o["px"]), Decimal(o["leaves"])
            kept = []
            for lv in levels:
                p = _dec(lv.get("px"))
                q = _dec(lv.get("qty")) or ZERO
                if p == px:
                    q -= mine
                    if q <= 0:
                        continue
                    lv = {**lv, "qty": str(q)}
                kept.append(lv)
            out.append(kept)
        return out[0], out[1]

    # --- decisions --------------------------------------------------------------
    def on_book(self, slug, league, bid, ask, tick, paper_st):
        """Called with the ex-self touch after the paper maker has quoted."""
        if not self.enabled or self.client is None:
            return
        m = self.mk.get(slug)
        if m is None:
            if (self.halted or not self.pws_ok
                    or config.sport_of(league) not in config.REALMM_SPORTS
                    or not paper_st or not (paper_st.get("bid") or paper_st.get("ask"))
                    or sum(1 for x in self.mk.values() if x["active"])
                    >= config.REALMM_MAX_MARKETS
                    or self.exposure() + self._one_quote_risk(bid, ask)
                    > config.REALMM_BUDGET):
                return
            m = self.mk[slug] = self._new_market(slug, league, paper_st)
            self.log(f"REALMM start {slug}  ({self.title(slug) or '?'})")
            self.row("start", slug, league=league)
        if not m["active"]:
            return
        m["book"] = (str(bid), str(ask))
        tick = tick or config.DEFAULT_TICK
        ticks = int((ask - bid) / tick)
        want = {"bid": None, "ask": None}
        if ticks >= config.MAKER_MIN_SPREAD and not self.halted and self.pws_ok:
            step = tick * min(config.MAKER_INSIDE, (ticks - 1) // 2)
            want = self._sized(m, bid + step, ask - step)
        want["t_book"] = time.time()
        m["want"] = want
        self._wake(slug)

    def _new_market(self, slug, league, paper_st):
        return {
            "league": league, "active": True, "started_at": _now_iso(),
            "n": "0", "cash": "0", "fills": 0,
            "bought": "0", "sold": "0", "buy_cost": "0", "sell_proc": "0",
            "orders": {"bid": None, "ask": None},
            "want": {"bid": None, "ask": None}, "book": None,
            # Paper's counters at the moment we joined, so the two can be
            # compared over exactly the same window.
            "paper_base": {k: paper_st.get(k) for k in
                           ("fills", "inv", "cash", "bought", "sold")},
        }

    def _one_quote_risk(self, bid, ask):
        q = Decimal(config.REALMM_SIZE)
        return max(bid * q, (1 - ask) * q)

    def _sized(self, m, bp, ap):
        """Desired orders: price, quantity, intent. Closing orders never add
        risk; opening orders must fit the budget with everything else."""
        n = Decimal(m["n"])
        size = Decimal(config.REALMM_SIZE)
        cap = Decimal(config.REALMM_MAX_INV)
        out = {"bid": None, "ask": None}
        # Bid: buy back a short first, else open/add long.
        if n < 0:
            out["bid"] = {"px": str(bp), "qty": int(min(size, -n)),
                          "intent": "ORDER_INTENT_SELL_SHORT"}
        elif n + size <= cap:
            out["bid"] = {"px": str(bp), "qty": int(size),
                          "intent": "ORDER_INTENT_BUY_LONG"}
        if n > 0:
            out["ask"] = {"px": str(ap), "qty": int(min(size, n)),
                          "intent": "ORDER_INTENT_SELL_LONG"}
        elif -n + size <= cap:
            out["ask"] = {"px": str(ap), "qty": int(size),
                          "intent": "ORDER_INTENT_BUY_SHORT"}
        # Budget: drop opening sides until the worst case fits.
        for side in ("ask", "bid"):
            if self._exposure_with(m, out) <= config.REALMM_BUDGET:
                break
            o = out[side]
            if o and o["intent"] in ("ORDER_INTENT_BUY_LONG", "ORDER_INTENT_BUY_SHORT"):
                out[side] = None
        if self._exposure_with(m, out) > config.REALMM_BUDGET:
            out = {k: (v if v and v["intent"] in ("ORDER_INTENT_SELL_LONG",
                                                   "ORDER_INTENT_SELL_SHORT")
                       else None) for k, v in out.items()}
        return out

    def _risk(self, m, orders):
        b, a = orders.get("bid"), orders.get("ask")
        return worst_loss(Decimal(m["n"]), Decimal(m["cash"]),
                          (Decimal(b["px"]), Decimal(b["qty"])) if b else None,
                          (Decimal(a["px"]), Decimal(a["qty"])) if a else None)

    def _exposure_with(self, m, want):
        """Total worst case if market m's orders were `want`, counting the
        larger of live and wanted while a replace is in flight."""
        tot = ZERO
        for x in self.mk.values():
            if x is m:
                tot += max(self._risk(x, want), self._risk(x, self._live(x)))
            else:
                tot += self._risk(x, self._live(x))
        return tot

    @staticmethod
    def _live(m):
        return {k: ({"px": o["px"], "qty": o["leaves"]} if o and o.get("leaves")
                    else None) for k, o in m["orders"].items()}

    def exposure(self):
        return sum((self._risk(m, self._live(m)) for m in self.mk.values()), ZERO)

    # --- order management -----------------------------------------------------
    def _wake(self, slug):
        ev = self._kick.get(slug)
        if ev is None:
            ev = self._kick[slug] = asyncio.Event()
            self._tasks[slug] = asyncio.ensure_future(self._sync(slug))
        ev.set()

    async def _sync(self, slug):
        """One worker per market, so its orders change strictly in sequence."""
        ev = self._kick[slug]
        while True:
            await ev.wait()
            ev.clear()
            m = self.mk.get(slug)
            if m is None:
                return
            try:
                await self._apply(slug, m)
            except Exception as e:
                self._error(f"sync {slug[:30]}: {type(e).__name__}: {str(e)[:90]}")
            # Do not requote faster than this; changes meanwhile coalesce.
            await asyncio.sleep(config.REALMM_MIN_REQUOTE_SECS)

    async def _apply(self, slug, m):
        want = m.get("want") or {}
        for side in ("bid", "ask"):
            live, w = m["orders"].get(side), want.get(side)
            same = (live and w and live["px"] == w["px"]
                    and int(live["qty"]) == int(w["qty"])
                    and live["intent"] == w["intent"])
            if same:
                continue
            if live:
                await self._cancel(slug, m, side)
                if m["orders"].get(side):          # cancel failed
                    continue
            if w and not self.halted and self.pws_ok:
                await self._create(slug, m, side, w, want.get("t_book"))

    async def _throttle(self):
        now = time.time()
        if now < self._backoff_until:
            await asyncio.sleep(self._backoff_until - now)
        while (len(self._action_times) >= config.REALMM_MAX_ACTIONS_PER_SEC
               and time.time() - self._action_times[-config.REALMM_MAX_ACTIONS_PER_SEC] < 1):
            await asyncio.sleep(0.1)
        self._action_times.append(time.time())

    async def _create(self, slug, m, side, w, t_book):
        if self._exposure_with(m, {**self._live(m), side: w}) > config.REALMM_BUDGET \
                and w["intent"] in ("ORDER_INTENT_BUY_LONG", "ORDER_INTENT_BUY_SHORT"):
            self.counts["budget_block"] += 1
            return
        await self._throttle()
        req = {"marketSlug": slug, "intent": w["intent"],
               "type": "ORDER_TYPE_LIMIT", "price": {"value": w["px"]},
               "quantity": int(w["qty"]),
               "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
               "participateDontInitiate": True,
               "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC"}
        # Mark it live BEFORE sending: if the reply is lost we must still
        # treat the order as resting (and counted in the budget) until the
        # venue says otherwise.
        o = m["orders"][side] = {"id": None, "px": w["px"], "qty": int(w["qty"]),
                                 "leaves": int(w["qty"]), "intent": w["intent"],
                                 "sent": time.time()}
        t0 = time.time()
        try:
            resp = await self.client.orders.create(req)
        except RateLimitError:
            self._backoff_until = time.time() + 5
            self.counts["rate_limited"] += 1
            m["orders"][side] = None
            self.row("rate_limited", slug, side=side)
            return
        except Exception as e:
            self._error(f"create {slug[:30]} {side}: {type(e).__name__}: {str(e)[:90]}")
            # Unknown whether it landed: the reconcile finds out, adopting it
            # or clearing it. Until then it stays counted against the budget.
            asyncio.ensure_future(self._reconcile())
            return
        t1 = time.time()
        o["id"] = (resp or {}).get("id")
        o["acked"] = t1
        ms = (t1 - t0) * 1000
        self.lat["ack"].append(ms)
        if t_book:
            self.lat["decide"].append((t1 - t_book) * 1000)
        self.counts["orders"] += 1
        bb, ba = m.get("book") or (None, None)
        self.row("order", slug, side=side, intent=w["intent"], px=w["px"],
                 qty=w["qty"], book_bid=bb, book_ask=ba, latency_ms=round(ms, 1),
                 detail=f"book->ack {((t1 - t_book) * 1000):.0f}ms" if t_book else "")
        for ex in (resp or {}).get("executions") or []:
            self._on_execution(ex, via="rest")

    async def _cancel(self, slug, m, side):
        o = m["orders"].get(side)
        if not o:
            return
        if not o.get("id"):
            # Reply never came back; only the reconcile can find it.
            asyncio.ensure_future(self._reconcile())
            return
        await self._throttle()
        t0 = time.time()
        try:
            await self.client.orders.cancel(o["id"], {"marketSlug": slug})
        except NotFoundError:
            pass                                   # already filled or gone
        except RateLimitError:
            self._backoff_until = time.time() + 5
            self.counts["rate_limited"] += 1
            return
        except Exception as e:
            self._error(f"cancel {slug[:30]} {side}: {type(e).__name__}: {str(e)[:90]}")
            return
        self.lat["cancel"].append((time.time() - t0) * 1000)
        self.counts["cancels"] += 1
        if m["orders"].get(side) is o:
            m["orders"][side] = None

    def _error(self, msg):
        self.log(f"REALMM {msg}")
        self._errors.append(time.time())
        self.counts["errors"] += 1
        if (len(self._errors) >= 10
                and time.time() - self._errors[-10] < 300):
            asyncio.ensure_future(self.halt("10 order errors within 5 minutes"))

    # --- the private stream ---------------------------------------------------
    async def _private_loop(self):
        backoff = 2
        while True:
            try:
                ws = self.client.ws.private()
                ws.on("message", self._on_private)
                closed = asyncio.Event()
                ws.on("close", lambda *a: closed.set())
                ws.on("error", lambda *a: closed.set())
                await ws.connect()
                await ws.subscribe_orders("realmm-orders")
                self.pws = ws
                self.pws_ok = True
                backoff = 2
                self.log("REALMM order stream connected")
                await closed.wait()
            except Exception as e:
                self.log(f"REALMM order stream error: {type(e).__name__}: {str(e)[:80]}")
            # Without the stream we cannot see fills, so nothing may rest.
            self.pws_ok = False
            self.log("REALMM order stream DOWN - cancelling everything")
            await self._cancel_everything("stream down")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def _on_private(self, msg, *a):
        if not isinstance(msg, dict):
            return
        up = msg.get("orderSubscriptionUpdate") or msg.get("orderUpdate")
        if up and up.get("execution"):
            self._on_execution(up["execution"], via="ws")

    def _find(self, oid):
        for slug, m in self.mk.items():
            for side, o in m["orders"].items():
                if o and o.get("id") == oid:
                    return slug, m, side, o
        return None, None, None, None

    def _on_execution(self, ex, via):
        eid = ex.get("id")
        if eid and eid in self.seen_exec:
            return
        if eid:
            self.seen_exec.append(eid)
        order = ex.get("order") or {}
        slug = order.get("marketSlug")
        kind = ex.get("type", "")
        heard = time.time()
        vt = _venue_secs(ex.get("transactTime"))
        _, m, side, o = self._find(order.get("id"))
        if m is None and slug in self.mk:
            m = self.mk[slug]
        if m is None:
            return                                  # not one of ours
        if kind == "EXECUTION_TYPE_NEW":
            if o and o.get("sent") and vt:
                self.lat["ws_new"].append((vt - o["sent"]) * 1000)
            return
        if kind in ("EXECUTION_TYPE_FILL", "EXECUTION_TYPE_PARTIAL_FILL"):
            q = Decimal(str(ex.get("lastShares") or 0))
            px = _dec(ex.get("lastPx"))
            if not q or px is None:
                return
            buy = order.get("intent") in BUY_INTENTS
            n, cash = Decimal(m["n"]), Decimal(m["cash"])
            if buy:
                n, cash = n + q, cash - q * px
                m["bought"] = str(Decimal(m["bought"]) + q)
                m["buy_cost"] = str(Decimal(m["buy_cost"]) + q * px)
            else:
                n, cash = n - q, cash + q * px
                m["sold"] = str(Decimal(m["sold"]) + q)
                m["sell_proc"] = str(Decimal(m["sell_proc"]) + q * px)
            m["n"], m["cash"] = str(n), str(cash)
            m["fills"] += 1
            if o is not None:
                leaves = order.get("leavesQuantity")
                o["leaves"] = (int(leaves) if leaves is not None
                               else max(0, int(o["leaves"]) - int(q)))
                if o["leaves"] <= 0 and m["orders"].get(side) is o:
                    m["orders"][side] = None
            if vt:
                self.lat["fill_heard"].append((heard - vt) * 1000)
            if ex.get("aggressor"):
                # Post-only should make this impossible. If it happens the
                # "never take" premise is broken and we want to know at once.
                self.counts["took_liquidity"] += 1
            self.counts["fills"] += 1
            self._resolve_prints(slug, "bid" if buy else "ask", q)
            bb, ba = m.get("book") or (None, None)
            spread = (int((Decimal(ba) - Decimal(bb)) / config.DEFAULT_TICK)
                      if bb and ba else None)
            row = {"ts": _now_iso(), "market": slug, "title": self.title(slug),
                   "side": "buy" if buy else "sell", "px": str(px), "qty": str(q),
                   "book_bid": bb, "book_ask": ba, "spread_ticks": spread,
                   "heard_ms": round((heard - vt) * 1000) if vt else None,
                   "aggressor": bool(ex.get("aggressor")), "n_after": m["n"]}
            self.recent.appendleft(row)
            self.row("fill", slug, side="bid" if buy else "ask",
                     intent=order.get("intent"), px=str(px), qty=str(q),
                     book_bid=bb, book_ask=ba, spread_ticks=spread,
                     latency_ms=row["heard_ms"], venue_time=ex.get("transactTime"),
                     detail="TOOK LIQUIDITY" if ex.get("aggressor") else "")
            self.log(f"REALMM FILL {slug[:36]} {'buy' if buy else 'sell'} "
                     f"{q} @ {px}  n={m['n']}  heard {row['heard_ms']}ms")
            self.save()
            self._wake(slug)
            return
        if kind in ("EXECUTION_TYPE_CANCELED", "EXECUTION_TYPE_REJECTED",
                    "EXECUTION_TYPE_EXPIRED"):
            if kind == "EXECUTION_TYPE_REJECTED":
                reason = ex.get("orderRejectReason") or ex.get("text") or "?"
                # Post-only orders that would have crossed come back here:
                # the book moved between our read and our order landing.
                self.counts["rejected"] += 1
                self.counts[f"reject:{str(reason)[:40]}"] += 1
                self.row("rejected", slug, side=side,
                         px=(_dec(order.get("price")) or ""), detail=str(reason)[:120])
            if o is not None and m["orders"].get(side) is o:
                m["orders"][side] = None
                self._wake(slug)

    # --- did we get the prints paper assumes? ----------------------------------
    def on_trade(self, slug, px, qty, taker_side):
        """A public trade. If it printed at or through a price we were resting
        at, paper would count it as our fill. Find out whether it was."""
        m = self.mk.get(slug)
        if not m or px is None or not qty:
            return
        side = (taker_side or "").upper()
        o_bid, o_ask = m["orders"].get("bid"), m["orders"].get("ask")
        if side.endswith("SELL") and o_bid and o_bid.get("id") \
                and px <= Decimal(o_bid["px"]):
            self.prints.append({"slug": slug, "side": "bid", "px": str(px),
                                "qty": qty, "ours": o_bid["px"],
                                "at": time.time(), "filled": ZERO})
        elif side.endswith("BUY") and o_ask and o_ask.get("id") \
                and px >= Decimal(o_ask["px"]):
            self.prints.append({"slug": slug, "side": "ask", "px": str(px),
                                "qty": qty, "ours": o_ask["px"],
                                "at": time.time(), "filled": ZERO})

    def _resolve_prints(self, slug, side, q):
        for p in self.prints:
            if p["slug"] == slug and p["side"] == side and p["filled"] < p["qty"]:
                take = min(q, p["qty"] - p["filled"])
                p["filled"] += take
                q -= take
                if q <= 0:
                    return

    def _flush_prints(self, final=False):
        """A print gets a few seconds for our fill to arrive, then a verdict."""
        now, keep = time.time(), []
        for p in self.prints:
            if not final and now - p["at"] < config.REALMM_PRINT_WAIT:
                keep.append(p)
                continue
            hit = p["filled"] > 0
            self.print_stats["hit" if hit else "missed"] += 1
            self.print_stats["shares_printed"] += min(p["qty"], Decimal(config.REALMM_SIZE))
            self.print_stats["shares_filled"] += p["filled"]
            self.row("print", p["slug"], side=p["side"], px=p["px"], qty=str(p["qty"]),
                     detail=f"ours {p['ours']} filled {p['filled']}")
        self.prints = keep

    # --- housekeeping ----------------------------------------------------------
    async def _housekeeping(self):
        last_rec = 0.0
        while True:
            await asyncio.sleep(1)
            try:
                self._flush_prints()
                if time.time() - last_rec >= config.REALMM_RECONCILE_SECS:
                    last_rec = time.time()
                    await self._reconcile()
            except Exception as e:
                self.log(f"REALMM housekeeping: {type(e).__name__}: {str(e)[:90]}")

    async def _reconcile(self):
        """The venue is the truth. Adopt its view of our orders; halt if its
        positions disagree with ours twice in a row (one miss can be a fill
        still in flight)."""
        if not self.mk:
            return
        slugs = list(self.mk)
        try:
            orders = (await self.client.orders.list({"slugs": slugs})).get("orders") or []
            pos = (await self.client.portfolio.positions()).get("positions") or {}
        except RateLimitError:
            self._backoff_until = time.time() + 5
            return
        except Exception as e:
            self.log(f"REALMM reconcile failed: {type(e).__name__}: {str(e)[:80]}")
            return
        known = {o["id"] for m in self.mk.values() for o in m["orders"].values()
                 if o and o.get("id")}
        for vo in orders:
            if vo.get("id") in known:
                continue
            slug = vo.get("marketSlug")
            m = self.mk.get(slug)
            pend = [s for s, o in (m["orders"].items() if m else [])
                    if o and not o.get("id") and o["px"] == str(_dec(vo.get("price")))
                    and time.time() - o["sent"] > 5]
            if pend:
                # The create whose reply we lost. Adopt it.
                m["orders"][pend[0]]["id"] = vo["id"]
                continue
            self.log(f"REALMM unknown resting order {vo.get('id')} in {slug} - cancelling")
            try:
                await self.client.orders.cancel(vo["id"], {"marketSlug": slug})
            except Exception:
                pass
        live_ids = {vo.get("id") for vo in orders}
        for slug, m in self.mk.items():
            for side, o in m["orders"].items():
                if o and o.get("id") and o["id"] not in live_ids \
                        and time.time() - o.get("acked", o["sent"]) > 5:
                    m["orders"][side] = None        # gone at the venue
                elif o and not o.get("id") and time.time() - o["sent"] > 30:
                    m["orders"][side] = None        # never landed
            if not m["active"]:
                # A finished match settles at the venue on its own schedule,
                # and its position vanishes there before we score it here.
                continue
            vp = pos.get(slug) or {}
            venue = abs(Decimal(str(vp.get("netPosition") or 0)))
            ours = abs(Decimal(m["n"]))
            if venue != ours:
                self._drift[slug] = self._drift.get(slug, 0) + 1
                self.log(f"REALMM position drift {slug[:36]}: venue {venue} vs ours "
                         f"{ours} ({self._drift[slug]}x)")
                if self._drift[slug] >= 2:
                    await self.halt(f"position drift in {slug}: venue {venue}, ours {ours}")
                    return
            else:
                self._drift.pop(slug, None)

    # --- endings --------------------------------------------------------------
    def reap(self, live_markets, settle):
        """Stop quoting markets discovery no longer lists; score them once the
        venue publishes the result."""
        if not self.enabled:
            return
        for slug, m in list(self.mk.items()):
            if m["active"] and slug not in live_markets:
                m["active"] = False
                m["want"] = {"bid": None, "ask": None}
                m["ended_at"] = _now_iso()
                self.log(f"REALMM stop {slug} (no longer live)")
                if self.client is not None:
                    self._wake(slug)
            if m["active"] or any(m["orders"].values()):
                continue
            n = Decimal(m["n"])
            v = settle(slug) if n != 0 else ZERO
            if v is None:
                continue
            pnl = Decimal(m["cash"]) + n * Decimal(v)
            self.closed.append({**{k: m[k] for k in (
                "league", "fills", "bought", "sold", "buy_cost", "sell_proc",
                "n", "cash", "started_at", "paper_base")},
                "slug": slug, "close": str(v), "pnl": str(round(pnl, 4)),
                "closed_at": _now_iso(), "ended_at": m.get("ended_at")})
            self.mk.pop(slug)
            self.log(f"REALMM settled {slug}: {pnl:+.2f}")
        self.save()

    # --- log rows -------------------------------------------------------------
    def row(self, kind, slug, **kw):
        self.write({"ts": _now_iso(), "kind": kind, "market": slug,
                    "sport": config.sport_of((self.mk.get(slug) or {}).get("league", ""))
                    if slug else "other", **kw})

    # --- reporting ------------------------------------------------------------
    def summary(self, paper_books=None):
        if not self.enabled:
            return {"enabled": False}
        paper_books = paper_books or {}

        def pct(xs, q):
            xs = sorted(xs)
            return round(xs[min(len(xs) - 1, int(q * len(xs)))], 1) if xs else None

        lat = {k: {"n": len(v), "p50": pct(v, .5), "p90": pct(v, .9)}
               for k, v in self.lat.items()}
        markets = []
        for slug, m in self.mk.items():
            n, cash = Decimal(m["n"]), Decimal(m["cash"])
            bb, ba = m.get("book") or (None, None)
            mid = (Decimal(bb) + Decimal(ba)) / 2 if bb and ba else None
            p = paper_books.get(slug) or {}
            base = m.get("paper_base") or {}
            paper = None
            if p and base.get("fills") is not None:
                pc = Decimal(p["cash"]) - Decimal(base["cash"] or 0)
                pn = Decimal(p["inv"]) - Decimal(base["inv"] or 0)
                paper = {"fills": p["fills"] - base["fills"],
                         "shares": str(Decimal(p["bought"]) - Decimal(base["bought"] or 0)
                                       + Decimal(p["sold"]) - Decimal(base["sold"] or 0)),
                         "mark": str(round(pc + pn * mid, 2)) if mid is not None else None,
                         "inv": str(pn)}
            markets.append({
                "market": slug, "title": self.title(slug), "league": m["league"],
                "active": m["active"], "n": m["n"], "fills": m["fills"],
                "shares": str(Decimal(m["bought"]) + Decimal(m["sold"])),
                "mark": str(round(cash + n * mid, 2)) if mid is not None else None,
                "worst": str(round(self._risk(m, self._live(m)), 2)),
                "orders": {k: ({"px": o["px"], "leaves": o["leaves"]} if o else None)
                           for k, o in m["orders"].items()},
                "book": m.get("book"), "paper": paper,
                "started_at": m["started_at"]})
        ps = self.print_stats
        seen = ps["hit"] + ps["missed"]
        return {
            "enabled": True, "halted": self.halted, "stream_ok": self.pws_ok,
            "budget": str(config.REALMM_BUDGET),
            "exposure": str(round(self.exposure(), 2)),
            "size": config.REALMM_SIZE, "max_inv": config.REALMM_MAX_INV,
            "latency_ms": lat, "counts": dict(self.counts),
            "prints": {"seen": seen, "hit": ps["hit"],
                       "hit_rate": round(ps["hit"] / seen, 3) if seen else None,
                       "shares_printed": str(ps["shares_printed"]),
                       "shares_filled": str(ps["shares_filled"])},
            "markets": markets,
            "closed": [{**c, "title": self.title(c["slug"])}
                       for c in reversed(self.closed)][:100],
            "net_settled": str(round(sum((Decimal(c["pnl"]) for c in self.closed),
                                         ZERO), 2)),
            "recent": list(self.recent)[:25],
        }
