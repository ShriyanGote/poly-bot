"""Longshot convexity engine - the opposite shape to the scalper.

Buy cheap sides (1-5c) and hold. Most go to zero; a few pay 20-30x. Loss is
capped at what you paid, so the gap-fills that destroyed the scalper cannot
hurt here - being filled by a gap just means buying cheaper.

Timed vs untimed matters:
  untimed (tennis, baseball, cricket, darts, table tennis) - you must win the
    last point/out/wicket, so a comeback never runs out of time. Hold.
  timed (football, basketball, soccer, hockey) - the clock closes the comeback
    window, so a spike is likely the best you will get. Take profit.

Resolution comes from markets.settlement(), not the last price we happened to
see, so winners are not mis-measured when a market leaves our subscription.
"""

import json
import time
from datetime import datetime, timezone
from decimal import Decimal

from . import config

UNTIMED = {"tennis", "baseball", "cricket", "darts", "esports", "pickleball"}
TIMED = {"football", "basketball", "soccer", "hockey", "lacrosse"}


def league_from_slug(slug: str) -> str:
    """League from the market slug: <prefix>-<league>-<teams>-<date>.

    The engine must not depend on the recorder's league map being populated -
    a tennis match that arrived as "?" would be tagged "other" and then skipped
    entirely once LS_SPORTS is set.
    """
    parts = (slug or "").split("-")
    return parts[1].upper() if len(parts) > 1 else "?"


def market_kind(slug: str) -> str:
    """Moneyline vs prop: a prop going 3c -> 95c is the event happening,
    not a comeback, so they must be measured separately."""
    return "moneyline" if slug.startswith("aec-") else "prop"


class Longshot:
    def __init__(self, log, client=None):
        self.log = log
        self.client = client
        self.positions: dict[str, dict] = {}
        self.closed: list[dict] = []
        self.peak: dict[str, str] = {}
        # Markets discovery still rates as live. None until the first sweep
        # reports in, so a wiring failure cannot silently stop all trading.
        self._live: set[str] | None = None
        self._live_at = 0.0
        # Distinct markets, not ticks. Counting ticks made these look enormous
        # and meant nothing: one busy market can add hundreds a minute.
        self.skipped_dead: set[str] = set()
        self.skipped_unknown: set[str] = set()
        self.skipped_stale: set[str] = set()
        self.skipped_tiebreak: set[str] = set()
        # key -> when the price was last seen at or below the dip threshold.
        # Pruned by age and size so it cannot grow without bound.
        self.dipped: dict[str, float] = {}
        self.skipped_prop: set[str] = set()
        # State is what ./ls.py reads. Saving only on the 180s discovery sweep
        # meant a sale showed in the log seconds after it happened but took
        # minutes to appear in the viewer.
        self.dirty = False
        self._load()

    def _prune_dipped(self, now):
        """Drop stale dips. Every market that ever trades under the threshold
        lands here, not just the ones we buy, so it needs a ceiling."""
        if len(self.dipped) <= config.LS_DIP_MAX:
            self.dipped = {k: t for k, t in self.dipped.items()
                           if now - t["dip"] <= config.LS_DIP_TTL}
            return
        keep = sorted(self.dipped.items(), key=lambda kv: -kv[1]["dip"])
        self.dipped = dict(keep[:config.LS_DIP_MAX // 2])

    def request_close(self, want):
        """Mark positions to be sold on their next book update.

        `want` is a list of substrings matched against the position key
        (slug|side). Returns the keys marked. Nothing is closed here: the sale
        happens on the next real tick so the fill is a price that existed.
        """
        hit = []
        for key, pos in self.positions.items():
            if any(w in key for w in want):
                pos["close_now"] = True
                hit.append(key)
        if hit:
            self.dirty = True
        return hit

    def set_live(self, slugs):
        """Tell the engine which markets are still live.

        There is no unsubscribe, so a market whose game has finished keeps
        streaming and we keep buying into it. Discovery already applies
        is_live() every sweep and the recorder already computes the set that
        dropped out - it just was not reaching the entry path.
        """
        self._live = set(slugs)
        self._live_at = time.time()

    # --- state ---------------------------------------------------------------
    def _load(self):
        if config.LONGSHOT_STATE.exists():
            try:
                s = json.loads(config.LONGSHOT_STATE.read_text())
                self.positions = s.get("positions", {})
                self.closed = s.get("closed", [])
                self.peak = s.get("peak", {})
                # Drop dips already past their TTL rather than carrying them in.
                now = time.time()
                # Older states stored a bare timestamp per key; treat those
                # as a dip with the hold clock not yet started.
                self.dipped = {}
                for k, t in (s.get("dipped") or {}).items():
                    d = t if isinstance(t, dict) else {"dip": float(t), "up": None}
                    if now - float(d["dip"]) <= config.LS_DIP_TTL:
                        self.dipped[k] = d
                self.log(f"longshot resumed: {len(self.positions)} open, "
                         f"{len(self.closed)} closed")
            except Exception as e:
                self.log(f"longshot state load failed ({type(e).__name__})")

    def save(self):
        self.dirty = False
        tmp = config.LONGSHOT_STATE.with_suffix(".tmp")
        # `dipped` is persisted because we restart often: without it, a market
        # that already dipped would have to dip again before it could be
        # bought, and the entry would simply be missed.
        tmp.write_text(json.dumps({
            "positions": self.positions, "closed": self.closed, "peak": self.peak,
            "dipped": self.dipped,
            "saved": datetime.now(timezone.utc).isoformat(),
        }, indent=2, default=str))
        tmp.replace(config.LONGSHOT_STATE)

    # --- trading -------------------------------------------------------------
    def on_book(self, slug, league, bid, ask, tick=None, period=None, score=None):
        if not league or league == "?":
            league = league_from_slug(slug)
        sport = config.sport_of(league)
        now = time.time()

        # Existing positions are always managed; only NEW entries are gated by
        # sport, so nothing already open gets stranded.
        can_open = (config.LS_SPORTS is None or sport in config.LS_SPORTS)
        if can_open and config.LS_MONEYLINE_ONLY and market_kind(slug) != "moneyline":
            can_open = False
            self.skipped_prop.add(slug)
        if (can_open and self._live is not None
                and now - self._live_at > config.LIVENESS_MAX_AGE):
            # We cannot see which games are running. Opening on a stale
            # picture is how a finished match gets bought.
            can_open = False
            self.skipped_stale.add(slug)
        if can_open and self._live is not None and slug not in self._live:
            # Open positions here are still managed below; only new ones stop.
            can_open = False
            self.skipped_dead.add(slug)
        if can_open and config.LS_SKIP_TIEBREAK and config.is_tiebreak(period):
            can_open = False
            self.skipped_tiebreak.add(slug)
        if can_open and period is not None and not config.is_live(period):
            # Discovery records games whose state it cannot read, so that the
            # tape is complete. We do not bet on them: a blank period is how a
            # finished match looks, and we cannot tell those apart.
            can_open = False
            self.skipped_unknown.add(slug)

        for side, price, exit_px in (("long", ask, bid),
                                     ("short", Decimal("1") - bid, Decimal("1") - ask)):
            key = f"{slug}|{side}"
            pos = self.positions.get(key)

            if pos is not None:
                self._manage(key, pos, exit_px, sport, now)
                continue

            if not can_open:
                continue
            if key in self.peak:          # already traded this side once
                continue
            r = config.rules_for(sport)
            band_lo, band_hi, arm, draw = r.band_lo, r.band_hi, r.arm, r.drawdown
            if r.dip_to is not None:
                # Wait for a recovery that holds. The price must trade down to
                # the dip level, come back to the buy-back level, and STAY
                # there - a single tick up is noise at these prices, not a
                # bounce, and it happens constantly on the way down.
                st = self.dipped.get(key)
                if band_lo <= price <= r.dip_to:
                    self.dipped[key] = {"dip": now, "up": None}
                    continue
                if not isinstance(st, dict) or now - st["dip"] > config.LS_DIP_TTL:
                    continue
                if price < r.buy_back:
                    st["up"] = None               # fell back; restart the clock
                    continue
                if st.get("up") is None:
                    st["up"] = now
                    continue
                if now - st["up"] < r.hold_secs:
                    continue
            if not (band_lo <= price <= band_hi):
                continue
            if exit_px <= 0:              # no bid to ever sell into
                continue
            if len(self.positions) >= config.LS_MAX_POSITIONS:
                continue

            qty = int(config.LS_STAKE / price) if price > 0 else 0
            if qty <= 0:
                continue
            self.positions[key] = {
                "slug": slug, "side": side, "league": league, "sport": sport,
                "kind": market_kind(slug), "entry_px": str(price), "qty": qty,
                "opened": now, "timed": sport in TIMED, "peak_px": str(price),
                "last_seen": now, "peak_ts": now, "ticks": 0,
                # Match state when we bought, so entry conditions can be
                # compared later on a real sample rather than guessed at.
                "entry_period": str(period or ""), "entry_score": str(score or ""),
                # Pinned at entry: changing LS_SPORT_RULES later must not
                # retroactively move the stop on a position already open.
                "arm": str(arm), "drawdown": str(draw),
            }
            self.peak[key] = str(price)
            self.dipped.pop(key, None)
            self._prune_dipped(now)
            self.dirty = True
            self.log(f"LSBUY | {league:9} {sport:10} {side:5} {qty:>4} @ {price} "
                     f"= ${price*qty:>5.2f}  {'timed' if sport in TIMED else 'hold'}  "
                     f"{slug[:34]}")

    def _manage(self, key, pos, exit_px, sport, now):
        pos["last_seen"] = now
        pos["ticks"] = pos.get("ticks", 0) + 1
        if pos.get("close_now"):
            # Asked for by hand. Sell into the live bid on this tick, which is
            # what a market order would actually get - not the last price we
            # happened to see, and not the mid.
            self._close(key, pos, exit_px, "manual", now)
            return
        if exit_px > Decimal(pos["peak_px"]):
            pos["peak_px"] = str(exit_px)
            pos["peak_ts"] = now

        entry = Decimal(pos["entry_px"])
        self._mark_stops(pos, exit_px, entry, now)
        if exit_px <= 0:
            return                                    # let it die at settlement

        peak = Decimal(pos["peak_px"])

        if config.LS_USE_TRAIL:
            # Let winners run, but never give back more than the drawdown.
            # A fixed multiple capped 15-36x winners at 5x; holding returned
            # them to zero.
            sr = config.rules_for(sport)
            arm = Decimal(pos.get("arm") or sr.arm)
            draw = Decimal(pos.get("drawdown") or sr.drawdown)
            if peak >= entry * arm:
                floor = peak * (Decimal("1") - draw)
                if exit_px <= floor:
                    self._close(key, pos, exit_px, "trail", now)
            return

        target = (config.LS_TAKE_PROFIT if pos["timed"]
                  else config.LS_TAKE_PROFIT_UNTIMED)
        if exit_px >= entry * target:
            self._close(key, pos, exit_px, "take_profit", now)

    @staticmethod
    def _mark_stops(pos, exit_px, entry, now):
        """Note where a stop-loss WOULD have sold, without selling anything.

        Nothing here closes a position - it only records the first price each
        stop level would have got, so the rule accumulates a track record on
        the same trades the live rule is taking. The grace window exists
        because the bid sits far below the ask at these prices, so an
        ungraced stop fires on the spread the moment we enter rather than on
        anything the market did.
        """
        if now - pos["opened"] < config.LS_STOP_GRACE:
            return
        fired = pos.setdefault("stops", {})
        for frac in config.LS_STOP_LADDER:
            name = f"stop{int(frac * 100)}"
            if name in fired:
                continue
            if exit_px <= entry * (Decimal("1") - frac):
                fired[name] = str(exit_px)

    def _close(self, key, pos, px, reason, now):
        entry = Decimal(pos["entry_px"])
        pnl = (Decimal(px) - entry) * pos["qty"]
        rec = {**pos, "exit_px": str(px), "pnl": str(pnl),
               "reason": reason, "closed": now,
               "held_secs": round(now - pos["opened"], 1),
               "mult": float(Decimal(px) / entry) if entry else 0,
               "variants": self._variants(pos, Decimal(px))}
        self.closed.append(rec)
        del self.positions[key]
        self.dirty = True
        self.log(f"LSSELL| {pos['league']:9} {pos['sport']:10} {pos['side']:5} "
                 f"@ {px}  pnl {pnl:+7.2f}  {Decimal(px)/entry:>5.1f}x  {reason:11} "
                 f"{pos['slug'][:30]}")

    @staticmethod
    def _variants(pos, final_px):
        """P&L this position would have returned under each take-profit rule.

        A rule selling at Mx fires exactly when the price reaches M * entry, so
        the recorded peak tells us whether it fired; otherwise the position ran
        to its actual exit. Scoring every rule off one position avoids running
        parallel engines on different trades, which would not be comparable.
        """
        entry = Decimal(pos["entry_px"])
        qty = pos["qty"]
        peak = Decimal(pos.get("peak_px", pos["entry_px"]))
        out = {"hold": str((final_px - entry) * qty)}
        for m in config.LS_TP_LADDER:
            target = entry * m
            hit = peak >= target
            exitp = target if hit else final_px
            out[f"tp{m}x"] = str((exitp - entry) * qty)
        # A stop that never fired leaves the position to the live rule, so its
        # result is whatever actually happened.
        fired = pos.get("stops") or {}
        for frac in config.LS_STOP_LADDER:
            name = f"stop{int(frac * 100)}"
            exitp = Decimal(fired[name]) if name in fired else final_px
            out[name] = str((exitp - entry) * qty)
        return out

    # --- settlement ----------------------------------------------------------
    def _settlement_of(self, slug):
        """What a finished market paid, or None if it is not decided yet.

        markets.settlement() 404s for a while after a match ends, but bbo
        already carries settlementPx once the market reaches EXPIRED, so ask
        both before giving up. Only a real settlement value closes a position
        - never the last price we happened to see.
        """
        try:
            v = self.client.markets.settlement(slug).get("settlement")
            if v is not None:
                return Decimal(str(v))
        except Exception:
            pass
        try:
            md = self.client.markets.bbo(slug).get("marketData") or {}
            if md.get("state") != "MARKET_STATE_EXPIRED":
                return None
            px = (md.get("settlementPx") or {}).get("value")
            if px is None:
                return None
            val = Decimal(str(px))
            # settlementPx on this endpoint is a placeholder until the real
            # result lands, and the placeholder is not always the same: 0.5
            # booked a 0.02 entry as +$24, and 0.0 booked a losing short as
            # +$19. So it is only believed when the last traded price agrees
            # with it - a market that settles at 1 was trading high, and one
            # that settles at 0 was trading low. Anything else waits for
            # markets.settlement(), which is authoritative.
            if val not in (Decimal(0), Decimal(1)):
                return None
            last = ((md.get("lastTradePx") or {}).get("value")
                    or (md.get("currentPx") or {}).get("value"))
            if last is None:
                return None
            last = Decimal(str(last))
            if (val == 1 and last >= Decimal("0.5")) or \
               (val == 0 and last <= Decimal("0.5")):
                return val
        except Exception:
            pass
        return None

    def settle_gone(self, live_markets, now=None):
        """Markets that left the feed are finished: ask the API what happened
        rather than guessing from the last price we saw."""
        now = now or time.time()
        done = 0
        # Time-based, not sweep-based: this runs every SETTLE_INTERVAL now, so
        # counting sweeps would burn the grace in seconds. The real safeguard
        # is that nothing closes without a settlement value from the exchange.
        self._started_at = getattr(self, "_started_at", None) or now
        if now - self._started_at < config.STARTUP_GRACE:
            return 0

        tries = 0
        for key, pos in list(self.positions.items()):
            quiet = now - pos.get("last_seen", pos["opened"])
            if pos["slug"] in live_markets:
                # Being in live_markets is NOT evidence the match is running.
                # There is no unsubscribe, so `subscribed` only grows: a match
                # that ended an hour ago is still "live" by that test, and the
                # position sat open until the next restart emptied the set.
                # Judge it on whether data is still arriving instead.
                pos["miss"] = 0
                if quiet < config.LS_SETTLE_AFTER:
                    continue
            else:
                pos["miss"] = pos.get("miss", 0) + 1
                if pos["miss"] < config.REAP_GRACE_SWEEPS:
                    continue
                if quiet < config.LS_SETTLE_AFTER:
                    continue
            if now - float(pos.get("settle_checked", 0) or 0) < config.SETTLE_RECHECK:
                continue            # asked recently; do not hammer the API
            if tries >= config.LS_SETTLE_PER_SWEEP:
                break                                 # stay under the rate limit
            tries += 1
            pos["settle_checked"] = now
            val = self._settlement_of(pos["slug"]) if self.client else None
            if val is None:
                continue                              # retry on a later sweep
            # settlement is for the long side; a short wins when it resolves 0
            payoff = val if pos["side"] == "long" else (Decimal("1") - val)
            self._close(key, pos, payoff, "settled", now)
            done += 1
            if done >= config.LS_SETTLE_PER_SWEEP:
                break
        return done

    def variant_table(self):
        """Realised P&L for each exit rule across all closed positions."""
        rules = (["hold"] + [f"tp{m}x" for m in config.LS_TP_LADDER]
                 + [f"stop{int(f * 100)}" for f in config.LS_STOP_LADDER])
        out = {}
        for r in rules:
            tot = Decimal("0")
            for c in self.closed:
                v = c.get("variants") or {}
                tot += Decimal(v.get(r, c["pnl"]))
            out[r] = tot
        return out

    def summary(self):
        if not self.closed:
            return f"longshot: {len(self.positions)} open, none closed yet"
        pnl = sum(Decimal(c["pnl"]) for c in self.closed)
        wins = sum(1 for c in self.closed if Decimal(c["pnl"]) > 0)
        staked = sum(Decimal(c["entry_px"]) * c["qty"] for c in self.closed)
        roi = (pnl / staked * 100) if staked else Decimal("0")
        skips = []
        if self.skipped_dead:
            skips.append(f"{len(self.skipped_dead)} dead-market")
        if self.skipped_unknown:
            skips.append(f"{len(self.skipped_unknown)} unknown-period")
        if self.skipped_stale:
            skips.append(f"{len(self.skipped_stale)} stale-liveness")
        if self.skipped_tiebreak:
            skips.append(f"{len(self.skipped_tiebreak)} tiebreak")
        if self.skipped_prop:
            skips.append(f"{len(self.skipped_prop)} non-moneyline")
        dead = f" | skipped {' + '.join(skips)}" if skips else ""
        return (f"longshot: {len(self.positions)} open | closed {len(self.closed)} "
                f"{wins}W | staked ${staked:.2f} pnl {pnl:+.2f} ({roi:+.0f}%){dead}")
