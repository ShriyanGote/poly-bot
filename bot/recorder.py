"""WebSocket recorder: books + trades, excursion tracking, paper trading."""

import asyncio
import csv
import json
import os
import re
import time
from datetime import datetime, timezone
from decimal import Decimal

from polymarket_us import PolymarketUS

from . import config
from .discovery import Discovery
from .excursions import Excursions
from .longshot import Longshot
from .paper import Paper
from .storage import Store


def d(v):
    return Decimal(str(v or "0"))


def league_from_slug(slug):
    """Fallback when meta has no entry: slugs look like
    <prefix>-<league>-<teams>-<date>, so the league is recoverable."""
    parts = (slug or "").split("-")
    return parts[1].upper() if len(parts) > 1 else "?"


def pack(levels, n):
    out = []
    for lv in (levels or [])[:n]:
        try:
            out.append(f"{lv['px']['value']}:{lv['qty']}")
        except Exception:
            continue
    return "|".join(out)


def total_qty(levels, n=None):
    t = Decimal("0")
    for lv in (levels or [])[: (n or len(levels or []))]:
        try:
            t += Decimal(str(lv["qty"]))
        except Exception:
            continue
    return t


class Recorder:
    def __init__(self, paper_enabled=True):
        self.client = PolymarketUS(
            key_id=os.environ["POLYMARKET_KEY_ID"],
            secret_key=os.environ["POLYMARKET_SECRET_KEY"],
        )
        self.store = Store()
        self.disc = Discovery(self.client, self.log)
        self.exc = Excursions(on_event=self._on_excursion)
        self.paper = Paper(self.log) if paper_enabled else None
        # Second, independent engine. The scalper stays on as a known-losing
        # control so the two can be compared on identical data.
        self.longshot = Longshot(self.log, self.client) if paper_enabled else None
        self.meta = {}
        # request_id -> the markets that request carried. The per-connection
        # cap is reported as an async error frame AFTER the subscribe call
        # returns, so without this we mark markets live and then hear nothing.
        self._req_chunks: dict[str, list] = {}
        self.cap_dropped = 0
        self._last_liveness = 0.0
        self._last_settle = 0.0
        config.REQUESTS.mkdir(exist_ok=True)
        self.subscribed = set()
        self.conns = []          # [{"ws": ws, "slugs": set()}]
        self.seen_events = {}    # event_slug -> league
        self.msgs = self.trades = self.req = self.reconnects = 0
        self.started = datetime.now(timezone.utc)
        self._last_scorecard = 0.0
        self.last_msg = time.time()
        self.stale_reconnects = 0

    def log(self, msg):
        line = f"{datetime.now(timezone.utc).strftime('%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            with (config.LOGS / f"run-{day}.log").open("a") as fh:
                fh.write(line + "\n")
        except Exception:
            pass

    def _on_excursion(self, ev):
        ev = dict(ev)
        ev["ts"] = datetime.fromtimestamp(ev["ts"], timezone.utc).isoformat()
        ev["sport"] = config.sport_of(ev.get("league", ""))
        self.store.exc.write(ev)
        # Surface the interesting ones: a real climb off the low.
        if ev["kind"] == "recover" and ev["recovered_ticks"] >= 2:
            self.log(f"  RECOVER {ev['league']:8} {ev['side']:5} thr {ev['threshold']} "
                     f"low {ev['min_px']} -> {ev['peak_px']} (+{ev['recovered_ticks']}t "
                     f"in {ev['secs_since_low']:.0f}s)  {ev['market'][:34]}")

    # --- message handling ----------------------------------------------------
    def on_message(self, payload, *_):
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                return
        if not payload:
            return
        got_data = False
        if payload.get("trade"):
            self._on_trade(payload["trade"])
            got_data = True
        md = payload.get("marketData")
        if md:
            self._on_book(md)
            got_data = True
        # Only genuine market data counts as liveness. Error frames and
        # subscription acks previously refreshed this, masking a dead feed
        # for ~9 hours.
        if got_data:
            self.last_msg = time.time()

    def _on_trade(self, t):
        slug = t.get("marketSlug")
        if not slug:
            return
        league, event, period, _tick = self.meta.get(
            slug, (league_from_slug(slug), "?", "?", config.DEFAULT_TICK))
        self.trades += 1
        self.store.trades.write({
            "ts": datetime.now(timezone.utc).isoformat(), "league": league,
            "sport": config.sport_of(league), "market": slug,
            "price": (t.get("price") or {}).get("value"),
            "qty": (t.get("quantity") or {}).get("value"),
            "taker_side": (t.get("taker") or {}).get("side"),
            "taker_intent": (t.get("taker") or {}).get("intent"),
            "maker_side": (t.get("maker") or {}).get("side"),
            "trade_id": t.get("id"), "trade_time": t.get("tradeTime"),
        })

    def _on_book(self, md):
        slug = md.get("marketSlug")
        bids, offers = md.get("bids") or [], md.get("offers") or []
        if not slug or not bids or not offers:
            return
        bid, ask = d(bids[0]["px"]["value"]), d(offers[0]["px"]["value"])
        if bid <= 0 or ask <= 0 or ask <= bid:
            return
        self.msgs += 1
        league, event, period, tick = self.meta.get(
            slug, (league_from_slug(slug), "?", "?", config.DEFAULT_TICK))
        bt, at = total_qty(bids, config.BOOK_LEVELS), total_qty(offers, config.BOOK_LEVELS)
        imb = (bt - at) / (bt + at) if (bt + at) else Decimal("0")

        self.store.books.write(
            {
                "ts": datetime.now(timezone.utc).isoformat(), "league": league,
                "sport": config.sport_of(league), "event": event, "market": slug,
                "period": period, "score": self.disc.scores.get(event, ""),
                "bid": bid, "ask": ask, "mid": (bid + ask) / 2,
                "spread": ask - bid, "short_px": Decimal("1") - bid,
                "bid_depth": bids[0]["qty"], "ask_depth": offers[0]["qty"],
                "bid_total": bt, "ask_total": at, "imbalance": round(imb, 4),
                "state": md.get("state", ""),
                "bid_levels": pack(bids, config.BOOK_LEVELS),
                "ask_levels": pack(offers, config.BOOK_LEVELS),
            },
            dedup_key=slug,
            dedup_state=(str(bid), str(ask), bids[0]["qty"], offers[0]["qty"]),
        )

        # Excursions watch both tradeable sides.
        self.exc.update(slug, "long", ask, league, period)
        self.exc.update(slug, "short", Decimal("1") - bid, league, period)

        if self.paper:
            try:
                self.paper.on_book(slug, league, bid, ask, bids, offers, tick)
            except Exception as e:
                self.log(f"paper error: {type(e).__name__} {str(e)[:80]}")
        if self.longshot:
            try:
                self.longshot.on_book(slug, league, bid, ask, tick, period,
                                      self.disc.scores.get(event))
            except Exception as e:
                self.log(f"longshot error: {type(e).__name__} {str(e)[:80]}")

    # --- connection ----------------------------------------------------------
    async def _connect(self):
        ws = self.client.ws.markets()
        ws.on("message", self.on_message)
        ws.on("error", lambda *a: self._on_ws_error(ws, a))
        ws.on("close", lambda *a: self.log("WS CLOSED by peer"))
        await ws.connect()
        return ws

    def _save_titles(self):
        """Write the event-title cache the viewers read.

        ls.py used to fetch these itself, which meant a read-only viewer made
        blocking API calls with no timeout and could hang for a minute. We are
        already pulling every event here, so the titles come free.
        """
        if not self.disc.titles:
            return
        path = config.DATA / "event_titles.json"
        try:
            have = json.loads(path.read_text()) if path.exists() else {}
        except ValueError:
            have = {}
        merged = {**have, **self.disc.titles}
        if merged == have:
            return
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(merged))
            tmp.replace(path)
        except OSError:
            pass

    def _drain_requests(self):
        """Pick up sell orders left by ./sell.py.

        They arrive as files rather than edits to longshot_state.json, because
        the engine rewrites that file wholesale and would discard them.
        """
        for f in sorted(config.REQUESTS.glob("close-*.json")):
            try:
                want = json.loads(f.read_text()).get("match") or []
            except Exception:
                want = []
            try:
                f.unlink()
            except OSError:
                pass
            if not want:
                continue
            hit = self.longshot.request_close(want)
            if hit:
                self.log("MANUAL sell requested for " + ", ".join(hit))
            else:
                self.log(f"MANUAL sell: nothing open matches {want}")

    def _on_ws_error(self, ws, args):
        """Act on the per-connection subscription cap instead of just logging.

        The server accepts the subscribe call and only then sends an error
        frame, so the try/except around the call never fires: we recorded
        those markets as subscribed and quietly received no data for them.
        This un-marks them so the next sweep resubscribes on a fresh socket.
        """
        text = str(args)[:300]
        self.log(f"WS ERROR {text[:120]}")
        if "max subscriptions" not in text.lower():
            return
        conn = next((c for c in self.conns if c["ws"] is ws), None)
        if conn is not None:
            conn["full"] = True
        m = re.search(r"request_id='([^']+)'", text)
        chunk = self._req_chunks.pop(m.group(1), None) if m else None
        if chunk:
            self.subscribed -= set(chunk)
            self.cap_dropped += len(chunk)
            self.log(f"cap hit: {len(chunk)} market(s) un-marked; a new "
                     f"connection picks them up next sweep")

    async def _subscribe(self, slugs):
        """Spread slugs over connections, opening more as capacity runs out.

        The server refuses subscriptions past a per-connection cap, and each
        market needs two (book + trades), so we never put more than
        MARKETS_PER_CONN markets on one socket.
        """
        pending = list(slugs)
        placed = 0
        while pending:
            conn = next((c for c in self.conns
                         if not c.get("full")
                         and len(c["slugs"]) < config.MARKETS_PER_CONN), None)
            if conn is None:
                if len(self.conns) >= config.MAX_CONNECTIONS:
                    self.log(f"SUBSCRIPTION CAP: {len(pending)} markets dropped "
                             f"({len(self.conns)} connections all full)")
                    break
                ws = await self._connect()
                conn = {"ws": ws, "slugs": set()}
                self.conns.append(conn)
                self.log(f"opened connection #{len(self.conns)}")

            room = config.MARKETS_PER_CONN - len(conn["slugs"])
            chunk, pending = pending[:room], pending[room:]
            try:
                for i in range(0, len(chunk), 50):
                    part = chunk[i:i + 50]
                    self.req += 1
                    self._req_chunks[f"b{self.req}"] = list(part)
                    await conn["ws"].subscribe_market_data(f"b{self.req}", part)
                    self.req += 1
                    self._req_chunks[f"t{self.req}"] = list(part)
                    await conn["ws"].subscribe_trades(f"t{self.req}", part)
                    if len(self._req_chunks) > 4000:      # keep it bounded
                        for k in list(self._req_chunks)[:2000]:
                            del self._req_chunks[k]
            except Exception as e:
                # Refused (usually the per-connection cap). Retire this socket
                # and put the work back so a fresh connection picks it up.
                self.log(f"subscribe refused on connection "
                         f"{self.conns.index(conn) + 1} ({type(e).__name__}); "
                         f"retiring it")
                conn["slugs"] = set(range(config.MARKETS_PER_CONN))  # mark full
                pending = chunk + pending
                continue
            conn["slugs"] |= set(chunk)
            self.subscribed |= set(chunk)
            placed += len(chunk)
        self.last_msg = time.time()
        return placed

    async def run(self):
        self.log(f"start | {len(config.SERIES)} series "
                 f"(tennis {len(config.TENNIS)}, soccer {len(config.SOCCER)}, "
                 f"esports {len(config.ESPORTS)}, major {len(config.MAJOR)}) "
                 f"| paper={'on' if self.paper else 'off'} "
                 f"| band {config.BAND_LO}-{config.BAND_HI}")
        backoff = 5
        while True:
            try:
                for c in self.conns:
                    try:
                        await c["ws"].close()
                    except Exception:
                        pass
                self.conns.clear()
                self.subscribed.clear()
                self.last_msg = time.time()
                backoff = 5
                # Resubscribe what we already know about right away; a fresh
                # REST sweep takes ~3 minutes we do not want to lose.
                known = list(self.meta)[:config.MAX_TOTAL_MARKETS]
                if known:
                    n = await self._subscribe(known)
                    self.log(f"fast resubscribe {n} markets "
                             f"over {len(self.conns)} connection(s)")
                await self._loop()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.reconnects += 1
                self.log(f"connection lost ({type(e).__name__}); reconnect in {backoff}s "
                         f"[#{self.reconnects}]")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)

    async def _loop(self):
        last_sweep = 0
        while True:
            now = asyncio.get_event_loop().time()

            # Watchdog: the socket can die silently (observed after a laptop
            # sleep) - no exception, no close event, just no data. If we are
            # subscribed and hear nothing, force a reconnect.
            silent = time.time() - self.last_msg
            if self.subscribed and silent > config.STALE_SECS:
                self.stale_reconnects += 1
                self.log(f"FEED STALE {silent:.0f}s with {len(self.subscribed)} markets "
                         f"subscribed - forcing reconnect [#{self.stale_reconnects}]")
                raise ConnectionError(f"feed stale {silent:.0f}s")

            if self.longshot:
                self._drain_requests()

            # Settle finished matches on their own timer. Tying this to the
            # discovery sweep meant a match that ended could sit open for
            # another three minutes after it was already known to be over.
            if (self.longshot
                    and now - self._last_settle >= config.SETTLE_INTERVAL):
                self._last_settle = now
                k = await asyncio.to_thread(self.longshot.settle_gone,
                                            self.subscribed)
                if k:
                    self.log(f"settled {k} longshot position(s)")

            if (self.longshot and self.subscribed
                    and now - self._last_liveness >= config.LIVENESS_INTERVAL
                    and now - last_sweep < config.DISCOVERY_INTERVAL):
                m = await asyncio.to_thread(self.disc.liveness)
                self._last_liveness = time.time()
                if m is not None:
                    self.longshot.set_live({s for s, ok in m.items() if ok})

            if now - last_sweep >= config.DISCOVERY_INTERVAL or not self.subscribed:
                sweep_started = time.time()
                found = await asyncio.to_thread(self.disc.sweep)
                # Credit the time the blocking sweep consumed.
                self.last_msg = max(self.last_msg, sweep_started)
                # Announce newly discovered games (one line per game, not per market).
                for slug, (league, event, period, _tick) in found.items():
                    if event and event not in self.seen_events:
                        self.seen_events[event] = league
                        self.log(f"GAME  | {league:9} {event[:44]:46} [{period}]")

                gone = self.subscribed - set(found)
                for slug in gone:
                    self.exc.kill(slug, "market_gone")
                # Those markets keep streaming (nothing unsubscribes them), so
                # tell the longshot which ones are still live before it can
                # open anything else into a finished game.
                if self.longshot:
                    self.longshot.set_live(found)
                # Do NOT prune meta. There is no unsubscribe, so a market that
                # drops out of a sweep keeps streaming; forgetting its league
                # sent 49,407 rows to the "?" tape. Keep a bounded history.
                self.meta.update(found)
                if len(self.meta) > config.META_MAX:
                    for slug in list(self.meta)[:len(self.meta) - config.META_MAX]:
                        if slug not in self.subscribed:
                            del self.meta[slug]
                new = [s for s in found if s not in self.subscribed]
                room_left = config.MAX_TOTAL_MARKETS - len(self.subscribed)
                if len(new) > room_left:
                    self.log(f"market ceiling: taking {max(0, room_left)} of "
                             f"{len(new)} new (cap {config.MAX_TOTAL_MARKETS})")
                    new = new[:max(0, room_left)]
                if new:
                    n = await self._subscribe(new)
                    self.log(f"subscribed +{n} -> {len(self.subscribed)} markets "
                             f"over {len(self.conns)} connection(s)")
                last_sweep = now
                self._last_liveness = now
                self._heartbeat()
                if self.paper:
                    # Expire anything stranded on a finished game.
                    n = self.paper.reap(
                        live_markets=self.subscribed,
                        settle=(self.longshot._settlement_of
                                if self.longshot else None))
                    if n:
                        self.log(f"reaped {n} stale position(s)/order(s)")
                    self.paper.save()
                if self.longshot:
                    k = await asyncio.to_thread(self.longshot.settle_gone,
                                                self.subscribed)
                    if k:
                        self.log(f"settled {k} longshot position(s)")
                    self.longshot.save()
                self._save_titles()
                self.store.flush()
                if now - self._last_scorecard >= config.SCORECARD_INTERVAL:
                    self._dump_scorecard()
                    self._last_scorecard = now
            # Flush as soon as anything actually changed, so the viewer is
            # seconds behind the log rather than up to a sweep behind.
            if self.longshot and self.longshot.dirty:
                self.longshot.save()
            await asyncio.sleep(2)

    def _heartbeat(self):
        hrs = (datetime.now(timezone.utc) - self.started).total_seconds() / 3600
        parts = [f"up {hrs:.1f}h", f"{len(self.subscribed)} mkts/{len(self.conns)}conn",
                 f"books {self.msgs}", f"trades {self.trades}",
                 f"tape {self.store.written}w/{self.store.skipped}dup",
                 f"exc {self.exc.events}"]
        if self.disc.rate_limit_hits:
            parts.append(f"429x{self.disc.rate_limit_hits}")
        parts.append(f"quiet {time.time()-self.last_msg:.0f}s")
        if self.reconnects:
            parts.append(f"reconn {self.reconnects}")
        if self.stale_reconnects:
            parts.append(f"stale {self.stale_reconnects}")
        if self.paper:
            parts.append(self.paper.summary())
        if self.longshot:
            parts.append(self.longshot.summary())
        self.log(" | ".join(parts))

    def _dump_scorecard(self):
        rows = self.exc.scorecard()
        if not rows:
            return
        path = config.DATA / "scorecard.csv"
        with path.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        top = [r for r in rows if r["dips"] >= 3][:3]
        for r in top:
            self.log(f"  TOP {r['league']:8} {r['side']:5} thr {r['threshold']} "
                     f"dips {r['dips']:>3} rec2 {r['rec2']:>3} "
                     f"({r['rec2_rate']*100:.0f}%)  {r['market'][:32]}")

    def shutdown(self):
        if self.longshot:
            self.longshot.save()
            self.log(self.longshot.summary())
        if self.paper:
            self.paper.save()
            self.log(self.paper.summary())
            if self.paper.rejects:
                top = sorted(self.paper.rejects.items(), key=lambda x: -x[1])[:5]
                self.log("gate rejections: " + ", ".join(f"{k} x{v}" for k, v in top))
        self._dump_scorecard()
        self.store.close()
        self.log(f"stopped | books {self.msgs} | trades {self.trades} | "
                 f"rows {self.store.written} | excursions {self.exc.events}")
