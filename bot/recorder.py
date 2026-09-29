"""WebSocket recorder: books + trades, excursion tracking, paper trading."""

import asyncio
import csv
import json
import shutil
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
from .maker import Maker
from .paper import Paper
from .realmaker import RealMaker
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
        # paper_enabled is the master off switch (--no-paper); each engine also
        # has its own config flag, so the losing scalper can be stopped without
        # stopping the exit logic that sells real positions.
        on = paper_enabled
        self.paper = Paper(self.log) if (on and config.PAPER_SCALPER) else None
        # Second, independent engine. The scalper stays on as a known-losing
        # control so the two can be compared on identical data.
        self.longshot = (Longshot(self.log, self.client)
                         if (on and config.LONGSHOT_ENABLED) else None)
        self.maker = (Maker(self.log, on_fill=self.store.mmfills.write)
                      if (on and config.MAKER_ENABLED) else None)
        # Real orders at the paper maker's prices. It shadows the maker, so it
        # only exists alongside it, and stays inert unless REALMM_ENABLED.
        self.realmm = (RealMaker(self.log, write=self.store.realmm.write,
                                 title=self._title)
                       if self.maker and config.REALMM_ENABLED else None)
        self.meta = {}
        # request_id -> the markets that request carried. The per-connection
        # cap is reported as an async error frame AFTER the subscribe call
        # returns, so without this we mark markets live and then hear nothing.
        self._req_chunks: dict[str, list] = {}
        self.cap_dropped = 0
        self._last_liveness = 0.0
        self._last_realguard = 0.0
        self._last_status = 0.0
        self._last_settle = 0.0
        # {slug: {"since": first queued, "next": earliest retry, "tries": n}}
        # Persisted: a market drops out of discovery exactly once, so a queue
        # lost to a restart is never rebuilt and its outcome never recorded.
        self._pending_settle = self._load_pending()
        config.REQUESTS.mkdir(exist_ok=True)
        self.subscribed = set()
        self.conns = []          # [{"ws": ws, "slugs": set()}]
        self.seen_events = {}    # event_slug -> league
        self.msgs = self.trades = self.req = self.reconnects = 0
        self.started = datetime.now(timezone.utc)
        self._last_scorecard = 0.0
        self.last_msg = time.time()
        self.stale_reconnects = 0
        self.stale_conns = 0
        self.settle_queue = (0, 0)   # (pending, backed off) for the status line
        self.tick_at = {}        # slug -> last time real data arrived
        self.sub_at = {}         # slug -> when we last subscribed it
        self.dark_found = 0
        self._live_found = set()
        self._last_reconcile = 0

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
    def on_message(self, payload, *_, ws=None):
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
            # Per-connection, not just global: one dead socket among five
            # leaves the global clock fresh while its markets go silent.
            if ws is not None:
                for c in self.conns:
                    if c["ws"] is ws:
                        c["seen"] = self.last_msg
                        break

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
        if self.maker:
            try:
                px = (t.get("price") or {}).get("value")
                qty = (t.get("quantity") or {}).get("value")
                self.maker.on_trade(
                    slug, d(px) if px is not None else None,
                    d(qty) if qty is not None else None,
                    (t.get("taker") or {}).get("side"))
                if self.realmm:
                    self.realmm.on_trade(
                        slug, d(px) if px is not None else None,
                        d(qty) if qty is not None else None,
                        (t.get("taker") or {}).get("side"))
            except Exception as e:
                self.log(f"maker trade error: {type(e).__name__} {str(e)[:80]}")

    def _on_book(self, md):
        slug = md.get("marketSlug")
        bids, offers = md.get("bids") or [], md.get("offers") or []
        if not slug or not bids or not offers:
            return
        bid, ask = d(bids[0]["px"]["value"]), d(offers[0]["px"]["value"])
        if bid <= 0 or ask <= 0 or ask <= bid:
            return
        self.msgs += 1
        self.tick_at[slug] = time.time()
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
                # The venue stamps every book frame; we were dropping it, which
                # is why our own staleness could only be measured from a live
                # 30-second probe instead of from history.
                "transact_time": md.get("transactTime", ""),
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
        if self.maker:
            try:
                # Both makers read the book with our real orders taken out;
                # otherwise our own bid is "the best bid" and we quote off it.
                mb, ma = bid, ask
                if self.realmm:
                    xb, xo = self.realmm.ex_self(slug, bids, offers)
                    mb = d(xb[0]["px"]["value"]) if xb else None
                    ma = d(xo[0]["px"]["value"]) if xo else None
                if mb is not None and ma is not None and ma > mb:
                    self.maker.on_book(slug, league, mb, ma, tick, time.time())
                    if self.realmm:
                        self.realmm.on_book(slug, league, mb, ma, tick,
                                            self.maker.books.get(slug))
            except Exception as e:
                self.log(f"maker error: {type(e).__name__} {str(e)[:80]}")
        if self.longshot:
            try:
                self.longshot.on_book(slug, league, bid, ask, tick, period,
                                      self.disc.scores.get(event),
                                      book={"bids": bids, "offers": offers,
                                            "bid_total": bt, "ask_total": at})
            except Exception as e:
                self.log(f"longshot error: {type(e).__name__} {str(e)[:80]}")

    # --- connection ----------------------------------------------------------
    async def _connect(self):
        ws = self.client.ws.markets()
        ws.on("message", lambda payload, *a: self.on_message(payload, ws=ws))
        ws.on("error", lambda *a: self._on_ws_error(ws, a))
        ws.on("close", lambda *a: self.log("WS CLOSED by peer"))
        await ws.connect()
        return ws

    @staticmethod
    def _load_pending():
        try:
            raw = json.loads(config.PENDING_SETTLE.read_text())
        except (OSError, ValueError):
            return {}
        out = {}
        for slug, v in (raw or {}).items():
            if isinstance(v, dict):
                out[slug] = {"since": float(v.get("since", 0)),
                             "next": float(v.get("next", 0)),
                             "tries": int(v.get("tries", 0))}
            else:                                  # pre-backoff format
                out[slug] = {"since": float(v), "next": 0.0, "tries": 0}
        return out

    def _save_pending(self):
        try:
            tmp = config.PENDING_SETTLE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._pending_settle))
            tmp.replace(config.PENDING_SETTLE)
        except OSError as e:
            self.log(f"could not save pending settlements: {e}")

    def _queue_settle(self, slug, now):
        self._pending_settle.setdefault(
            slug, {"since": now, "next": 0.0, "tries": 0})

    def _settlement_of(self, slug):
        """What a finished market paid, from the harvested file.

        The maker needs this to mark leftover inventory, and it must not depend
        on the longshot engine being switched on - that engine owns the API
        lookups, but the harvester writes SETTLEMENTS regardless, so read the
        file. Only a real settlement closes a market; a market absent here is
        marked at its last mid instead, and which of the two was used is
        recorded per market so the difference is never averaged away.
        """
        try:
            st = config.SETTLEMENTS.stat().st_mtime
        except OSError:
            return None
        if getattr(self, "_settle_mtime", None) != st:
            try:
                self._settle_cache = json.loads(config.SETTLEMENTS.read_text())
            except (OSError, ValueError):
                self._settle_cache = {}
            self._settle_mtime = st
        v = self._settle_cache.get(slug)
        return Decimal(str(v)) if v is not None else None

    def _harvest_settlements(self):
        """Record what finished markets actually paid, permanently.

        markets.settlement() stops answering for older markets, and the ones
        it still answers for skew heavily to winners, so a backtest scored on
        whatever happens to resolve is badly biased. Capturing the outcome
        while it is still available is the only way to score a replay on what
        really happened rather than on an assumption.
        """
        # Anything the API still shows as ended is worth queuing too, but the
        # main source is markets that dropped out of discovery.
        for event in list(self.disc.ended):
            self._queue_settle(f"aec-{event}", time.time())
            self.disc.ended.discard(event)
        path = config.SETTLEMENTS
        try:
            have = json.loads(path.read_text()) if path.exists() else {}
        except ValueError:
            have = {}
        if self.longshot:
            self.longshot.signals.update_settlements(have)
        if not self._pending_settle:
            return
        now = time.time()
        done = 0
        wrote = False
        # Only markets whose backoff has elapsed compete for the budget, and
        # among those the oldest goes first. A market that keeps missing backs
        # off further, so it cannot hold a slot that a just-finished match
        # needs.
        due = [(v["since"], slug) for slug, v in self._pending_settle.items()
               if v["next"] <= now]
        for since, slug in sorted(due):
            if slug in have:
                self._pending_settle.pop(slug, None)
                continue
            if now - since > config.SETTLE_GIVE_UP:
                self._pending_settle.pop(slug, None)
                continue
            if done >= config.SETTLE_HARVEST:
                break
            done += 1
            ent = self._pending_settle[slug]
            try:
                v = self.client.markets.settlement(slug).get("settlement")
            except Exception:
                v = None
            if v is None:         # not published yet - wait longer next time
                ent["tries"] += 1
                step = config.SETTLE_BACKOFF[min(ent["tries"] - 1,
                                                 len(config.SETTLE_BACKOFF) - 1)]
                ent["next"] = now + step
                continue
            have[slug] = str(v)
            self._pending_settle.pop(slug, None)
            wrote = True
        self._save_pending()
        if self._pending_settle:
            waiting = sum(1 for v in self._pending_settle.values() if v["next"] > now)
            self.settle_queue = (len(self._pending_settle), waiting)
        if wrote:
            try:
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(have))
                tmp.replace(path)
                self.log(f"settlements recorded: {len(have)} total")
            except OSError:
                pass

    def _title(self, slug):
        """The match a market belongs to, as people would name it."""
        if not hasattr(self, "_titles"):
            try:
                self._titles = json.loads(
                    (config.DATA / "event_titles.json").read_text())
            except (OSError, ValueError):
                self._titles = {}
        event = slug[4:] if slug.startswith("aec-") else slug
        return self.disc.titles.get(event) or self._titles.get(event)

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
        self._titles = merged
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
        if not self.longshot:
            # Nothing owns positions, so a stale request cannot be honoured.
            # Clear it rather than leaving it to be retried forever.
            for f in sorted(config.REQUESTS.glob("close-*.json")):
                try:
                    f.unlink()
                except OSError:
                    pass
                self.log("MANUAL sell ignored: longshot engine is off")
            return
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

    def _retire(self, conn, why):
        """Drop a dead socket and hand its markets back for resubscription.

        Retiring used to overwrite conn["slugs"] with filler to mark the
        socket full. That threw away the only record of what was on it: the
        markets stayed in self.subscribed, so no sweep ever re-subscribed
        them, and they went dark on a socket nobody was reading. Thirty-four
        tennis markets were lost that way in one afternoon.
        """
        orphans = set(conn.get("slugs") or ())
        if conn in self.conns:
            self.conns.remove(conn)
        self.subscribed -= orphans
        ws = conn.get("ws")
        if ws is not None:
            asyncio.ensure_future(self._close_quietly(ws))
        self.log(f"retired connection ({why}); {len(orphans)} market(s) "
                 f"handed back for resubscription")
        return orphans

    @staticmethod
    async def _close_quietly(ws):
        try:
            await ws.close()
        except Exception:
            pass

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
                conn = {"ws": ws, "slugs": set(), "seen": time.time()}
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
                orphans = self._retire(conn, type(e).__name__)
                # Both the markets already on the socket and the ones we were
                # mid-way through adding need a fresh home.
                pending = chunk + [x for x in orphans if x not in chunk] + pending
                continue
            conn["slugs"] |= set(chunk)
            self.subscribed |= set(chunk)
            now_sub = time.time()
            for slug in chunk:
                self.sub_at[slug] = now_sub
            placed += len(chunk)
        self.last_msg = time.time()
        return placed

    def _guard_real(self):
        """Keep real positions managed even when their feed is quiet.

        The websocket is the normal path, but a position holding actual money
        cannot depend on it. Anything real that has not ticked recently is
        priced over REST and run through the same exit logic, so a dark market
        cannot strand an armed position below its stop.
        """
        ls = self.longshot
        if not ls or not getattr(ls, "broker", None):
            return
        now = time.time()
        stale = [(k, p) for k, p in list(ls.positions.items())
                 if p.get("real")
                 and now - float(p.get("last_seen") or 0) > config.REAL_GUARD_STALE]
        for key, pos in stale:
            slug = pos["slug"]
            try:
                md = self.client.markets.bbo(slug).get("marketData") or {}
                bid = Decimal(str((md.get("bestBid") or {}).get("value") or 0))
                ask = Decimal(str((md.get("bestAsk") or {}).get("value") or 0))
            except Exception as e:
                self.log(f"REAL GUARD: no quote for {slug[:40]} "
                         f"({type(e).__name__}) - still holding")
                continue
            if bid <= 0 or ask <= 0:
                continue
            exit_px = bid if pos["side"] == "long" else Decimal("1") - ask
            quiet = now - float(pos.get("last_seen") or 0)
            self.log(f"REAL GUARD: {slug[:40]} quiet {quiet:.0f}s - pricing over "
                     f"REST at {float(exit_px):.4f}")
            ls._manage(key, pos, exit_px, pos.get("sport") or "tennis", now)

    def _audit_real(self):
        """Does anything at the venue lack a position here, or vice versa?"""
        ls = self.longshot
        if not ls or not getattr(ls, "broker", None):
            return
        held, _ = {}, None
        try:
            held = (self.client.portfolio.positions() or {}).get("positions") or {}
        except Exception:
            return
        ours = {p["slug"] for p in ls.positions.values() if p.get("real")}
        for slug, e in held.items():
            if abs(float(e.get("netPosition") or 0)) > 0 and slug not in ours:
                self.log(f"REAL AUDIT: venue holds {e.get('netPosition')} of "
                         f"{slug[:40]} with no position here - NOT BEING MANAGED")
        for slug in ours - set(held):
            self.log(f"REAL AUDIT: we think we hold {slug[:40]} but the venue "
                     f"shows nothing")

    async def _reconcile(self):
        """Verify that markets we believe we are subscribed to are ticking.

        Two bugs now have had the same shape: we trusted self.subscribed
        instead of checking whether data was arriving. The subscription cap
        marked markets subscribed that the server had refused, and retiring a
        socket orphaned the markets on it. Both were invisible because the
        bookkeeping said everything was fine.

        So check the claim directly. A live market that has produced nothing
        since well after we subscribed it is not subscribed, whatever our
        records say - resubscribe it and say so.
        """
        cutoff = time.time() - config.RECONCILE_MAX_AGE
        dark = [s for s in self.subscribed
                if s in self._live_found
                # Never ticked? Then judge from when we subscribed it, which
                # is what catches a subscribe the server silently refused.
                and max(self.tick_at.get(s, 0), self.sub_at.get(s, 0)) < cutoff]
        if not dark:
            return
        self.dark_found += len(dark)
        never = sum(1 for s in dark if s not in self.tick_at)
        self.log(f"RECONCILE: {len(dark)} live market(s) marked subscribed but "
                 f"silent >{config.RECONCILE_MAX_AGE}s ({never} never ticked) "
                 f"- resubscribing [{self.dark_found} total]")
        for s in dark[:5]:
            self.log(f"  dark: {s[:60]}")
        # Free the slots before asking for them again, or _subscribe finds
        # every connection full and opens pointless new ones.
        gone = set(dark)
        self.subscribed -= gone
        for c in self.conns:
            c["slugs"] -= gone
        await self._subscribe(dark)

    def _prune_seen(self):
        """Keep the tick/subscribe stamps bounded."""
        keep = self.subscribed | self._live_found
        for d in (self.tick_at, self.sub_at):
            if len(d) > config.META_MAX:
                for slug in [k for k in d if k not in keep]:
                    del d[slug]

    async def run(self):
        self.log(f"start | {len(config.SERIES)} series "
                 f"(tennis {len(config.TENNIS)}, soccer {len(config.SOCCER)}, "
                 f"esports {len(config.ESPORTS)}, major {len(config.MAJOR)}) "
                 f"| paper={'on' if self.paper else 'off'} "
                 f"| band {config.BAND_LO}-{config.BAND_HI}")
        backoff = 5
        first_guard = True
        if self.realmm:
            await self.realmm.start()
        while True:
            try:
                for c in self.conns:
                    try:
                        await c["ws"].close()
                    except Exception:
                        pass
                self.conns.clear()
                self.subscribed.clear()
                self.sub_at.clear()
                self.tick_at.clear()
                self.last_msg = time.time()
                backoff = 5
                # Resubscribe what we already know about right away; a fresh
                # REST sweep takes ~3 minutes we do not want to lose.
                # A real position may have gone past its stop while we were
                # down. Price it now rather than waiting for a tick.
                if first_guard and self.longshot and getattr(self.longshot, "broker", None):
                    first_guard = False
                    await asyncio.to_thread(self._audit_real)
                    await asyncio.to_thread(self._guard_real)
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

            # A single socket can go quiet while the others keep the global
            # clock fresh, so STALE_SECS above never fires. Retire any
            # connection that has heard nothing while its peers are healthy;
            # the next sweep resubscribes its markets on a live socket.
            if len(self.conns) > 1:
                fresh = max((c.get("seen", 0) for c in self.conns), default=0)
                for c in list(self.conns):
                    quiet = fresh - c.get("seen", 0)
                    if c["slugs"] and quiet > config.CONN_STALE_SECS:
                        self.stale_conns += 1
                        self._retire(c, f"silent {quiet:.0f}s while peers live")

            if (self.subscribed and self._live_found
                    and now - self._last_reconcile >= config.RECONCILE_INTERVAL):
                self._last_reconcile = now
                await self._reconcile()
                self._prune_seen()

            # Real money first: this runs before anything that might raise.
            if (self.longshot and getattr(self.longshot, "broker", None)
                    and now - self._last_realguard >= config.REAL_GUARD_INTERVAL):
                self._last_realguard = now
                await asyncio.to_thread(self._guard_real)

            if now - self._last_status >= config.STATUS_INTERVAL:
                self._last_status = now
                self._write_status()

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
                    # Queue its outcome. Waiting to see `ended` in the live
                    # query does not work: events.list is called with
                    # closed=False, so a finished match vanishes from view
                    # before its settlement is published. A market dropping
                    # out of discovery is the signal that it is over.
                    if slug.startswith("aec-"):
                        self._queue_settle(slug, now)
                # Those markets keep streaming (nothing unsubscribes them), so
                # tell the longshot which ones are still live before it can
                # open anything else into a finished game.
                if self.longshot:
                    self.longshot.set_live(found)
                # Do NOT prune meta. There is no unsubscribe, so a market that
                # drops out of a sweep keeps streaming; forgetting its league
                # sent 49,407 rows to the "?" tape. Keep a bounded history.
                self._live_found = set(found)
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
                if self.maker and self.disc.complete:
                    # Judge liveness by discovery, not by `subscribed`. There
                    # is no unsubscribe, so `subscribed` keeps a finished match
                    # for as long as its socket lives: markets that ended hours
                    # earlier stayed in the open book marked at a dead mid, and
                    # a restart then parked them all at once. Only trust a
                    # sweep that read every page, or a short one parks live
                    # markets.
                    self.maker.reap(
                        now=time.time(),
                        live_markets=set(found),
                        settle=(self.longshot._settlement_of
                                if self.longshot else self._settlement_of))
                    # Everything the maker is waiting on must be asked for.
                    # The queue above only catches a market that drops out of
                    # a sweep while still subscribed; one lost to a retired
                    # socket or a restart never shows up in `gone`, so its
                    # settlement was never fetched and it would have been
                    # scored at its last mid after the grace period.
                    for slug in self.maker.pending:
                        if slug.startswith("aec-"):
                            self._queue_settle(slug, time.time())
                    if self.realmm:
                        self.realmm.reap(set(found), self._settlement_of)
                        for slug, m in self.realmm.mk.items():
                            if not m["active"]:
                                self._queue_settle(slug, time.time())
                    self.maker.save()
                if self.longshot:
                    k = await asyncio.to_thread(self.longshot.settle_gone,
                                                self.subscribed)
                    if k:
                        self.log(f"settled {k} longshot position(s)")
                    self.longshot.save()
                self._save_titles()
                await asyncio.to_thread(self._harvest_settlements)
                self.store.flush()
                if now - self._last_scorecard >= config.SCORECARD_INTERVAL:
                    self._dump_scorecard()
                    self._last_scorecard = now
            # Flush as soon as anything actually changed, so the viewer is
            # seconds behind the log rather than up to a sweep behind.
            if self.longshot and self.longshot.dirty:
                self.longshot.save()
            await asyncio.sleep(2)

    def _write_status(self):
        """Publish a machine-readable snapshot for the status page.

        Everything here is already computed for the heartbeat line; this just
        writes it somewhere a browser can read, plus the per-sport market
        breakdown that answers the actual question - are we capturing every
        game for this sport right now.
        """
        by_sport = {}
        for slug in self.subscribed:
            league, event, period, _tick = self.meta.get(
                slug, ("?", "?", "?", None))
            sport = config.sport_of(league)
            d = by_sport.setdefault(sport, {"markets": 0, "events": set(),
                                            "leagues": {}, "sample": []})
            d["markets"] += 1
            if event and event != "?":
                d["events"].add(event)
            d["leagues"][league] = d["leagues"].get(league, 0) + 1
            if len(d["sample"]) < 40:
                d["sample"].append({"slug": slug, "event": event,
                                    "title": self.disc.titles.get(event, ""),
                                    "period": period,
                                    "score": self.disc.scores.get(event, "")})
        sports = {}
        for sp, d in sorted(by_sport.items()):
            sports[sp] = {"markets": d["markets"], "events": len(d["events"]),
                          "leagues": dict(sorted(d["leagues"].items(),
                                                 key=lambda kv: -kv[1])),
                          "sample": d["sample"]}
        try:
            usage = shutil.disk_usage(config.DATA)
            disk = {"free_gb": round(usage.free / 1e9, 1),
                    "total_gb": round(usage.total / 1e9, 1)}
            tape_gb = round(sum(f.stat().st_size
                                for f in config.DATA.glob("*.gz")) / 1e9, 2)
        except OSError:
            disk, tape_gb = {}, None
        status = {
            "generated": datetime.now(timezone.utc).isoformat(),
            "started": self.started.isoformat(),
            "uptime_hours": round(
                (datetime.now(timezone.utc) - self.started).total_seconds()/3600, 2),
            "subscribed": len(self.subscribed),
            "connections": len(self.conns),
            "book_updates": self.msgs,
            "trades": self.trades,
            "rows_written": self.store.written,
            "rows_deduped": self.store.skipped,
            "quiet_seconds": round(time.time() - self.last_msg, 1),
            "dead_connections": self.stale_conns,
            "dark_recovered": self.dark_found,
            "rate_limit_hits": self.disc.rate_limit_hits,
            "settle_queue": self.settle_queue[0],
            "disk": disk,
            "tape_gb": tape_gb,
            "sports": sports,
            "maker": self.maker.summary(self._title) if self.maker else None,
            "realmm": (self.realmm.summary(self.maker.books) if self.realmm
                       else {"enabled": False}),
            # What is actually armed, so the page never has to guess. The whole
            # point of showing this is that "which engine is running" was
            # previously only answerable by reading config on the box.
            "engines": {
                "scalper": bool(self.paper),
                "longshot": bool(self.longshot),
                "maker": bool(self.maker),
                "real_money": bool(config.LS_REAL_ENABLED
                                   and config.LS_REAL_NEW_ENTRIES),
            },
            "maker_rule": {
                "min_spread_ticks": config.MAKER_MIN_SPREAD,
                "inside_ticks": config.MAKER_INSIDE,
                "size": str(config.MAKER_SIZE),
                "max_inventory": str(config.MAKER_MAX_INV),
                "prefixes": list(config.MAKER_PREFIXES),
            } if self.maker else None,
        }
        try:
            tmp = config.STATUS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(status, indent=1))
            tmp.replace(config.STATUS_FILE)
        except OSError as e:
            self.log(f"could not write status: {e}")

    def _heartbeat(self):
        self._write_status()
        hrs = (datetime.now(timezone.utc) - self.started).total_seconds() / 3600
        parts = [f"up {hrs:.1f}h", f"{len(self.subscribed)} mkts/{len(self.conns)}conn",
                 f"books {self.msgs}", f"trades {self.trades}",
                 f"tape {self.store.written}w/{self.store.skipped}dup",
                 f"exc {self.exc.events}"]
        if self.maker:
            m = self.maker.summary()
            parts.append(f"mm {m['quoting_now']}q/{m['fills']}f/"
                         f"{m['undercut']}beat net{m['net_incl_open']}")
        if self.disc.rate_limit_hits:
            parts.append(f"429x{self.disc.rate_limit_hits}")
        parts.append(f"quiet {time.time()-self.last_msg:.0f}s")
        if self.stale_conns:
            parts.append(f"deadconn {self.stale_conns}")
        if self.dark_found:
            parts.append(f"dark {self.dark_found}")
        if self.settle_queue[0]:
            parts.append(f"settleq {self.settle_queue[0]}")
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
        self._save_pending()
        if self.realmm:
            self.realmm.save()
        if self.maker:
            self.maker.save()
            self.log(f"maker {self.maker.summary()}")
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
        if self.longshot:
            self.longshot.close()
        self.store.close()
        self.log(f"stopped | books {self.msgs} | trades {self.trades} | "
                 f"rows {self.store.written} | excursions {self.exc.events}")
