"""Paper trading engine. Never places a real order."""

import json
import time
from datetime import datetime, timezone
from decimal import Decimal

from . import config
from .signals import PriceHistory, evaluate


class Paper:
    """Simulates maker-side entries and tracks fills against the live book.

    Entry model: we rest a bid at `entry` and only count a fill when the book
    actually trades through it (best ask <= our bid). That is the honest
    version - crossing the spread instead would guarantee a one-tick loss.
    """

    def __init__(self, log):
        self.log = log
        self.hist = PriceHistory()
        self.positions: dict[str, dict] = {}
        self.closed: list[dict] = []
        self.resting: dict[str, dict] = {}
        self.rejects: dict[str, int] = {}
        self.cooldown: dict[str, float] = {}   # key -> earliest re-entry time
        self._load()

    def _load(self):
        if config.STATE_FILE.exists():
            try:
                s = json.loads(config.STATE_FILE.read_text())
                self.positions = s.get("positions", {})
                now = time.time()
                for _p in self.positions.values():
                    _p["last_seen"] = now
                    _p["resumed"] = True
                self.closed = s.get("closed", [])
                self.resting = s.get("resting", {})
                self.rejects = s.get("rejects", {})
                self.cooldown = s.get("cooldown", {})
                self.log(f"resumed: {len(self.positions)} open, {len(self.closed)} closed")
            except Exception as e:
                self.log(f"state load failed ({type(e).__name__}); starting fresh")

    def save(self):
        tmp = config.STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "positions": self.positions, "closed": self.closed,
            "resting": self.resting, "rejects": self.rejects,
            "saved": datetime.now(timezone.utc).isoformat(),
        }, indent=2, default=str))
        tmp.replace(config.STATE_FILE)

    def on_book(self, slug, league, bid, ask, bid_levels, ask_levels,
                tick=None):
        """Called on every book update."""
        tick = tick or config.TICK
        now = time.time()
        tradeable = (config.TRADE_SPORTS is None
                     or config.sport_of(league) in config.TRADE_SPORTS)
        for side, px in (("long", ask), ("short", Decimal("1") - bid)):
            self.hist.add(f"{slug}|{side}", now, px)

        for side, mark in (("long", bid), ("short", Decimal("1") - ask)):
            pos = self.positions.get(f"{slug}|{side}")
            if pos is not None:
                pos["mark"] = str(mark)

        self._check_fills(slug, bid, ask, now)
        self._check_exits(slug, league, bid, ask, now)
        # Existing positions still exit normally; only new entries are blocked.
        if tradeable and len(self.positions) + len(self.resting) < config.MAX_POSITIONS:
            self._consider(slug, league, bid, ask, bid_levels, ask_levels, now, tick)

    def _consider(self, slug, league, bid, ask, bid_levels, ask_levels, now,
                  tick=None):
        tick = tick or config.TICK
        for side in ("long", "short"):
            key = f"{slug}|{side}"
            if key in self.positions or key in self.resting:
                continue
            # Do not immediately re-enter a market that just stopped us out.
            if now < self.cooldown.get(key, 0):
                self.rejects["cooldown after stop"] = \
                    self.rejects.get("cooldown after stop", 0) + 1
                continue
            # Maker entry: we join the bid, so our price is one tick inside.
            entry = bid if side == "long" else Decimal("1") - ask
            if not (config.BAND_LO <= entry <= config.BAND_HI):
                continue
            qty = int(config.STAKE / entry) if entry > 0 else 0
            if qty <= 0:
                continue
            target = entry + config.EXIT_TICKS * tick
            exit_levels = bid_levels if side == "long" else ask_levels
            ok, reasons, m = evaluate(entry, target, Decimal(qty), exit_levels,
                                      self.hist, key, now, (config.BAND_LO, config.BAND_HI))
            if not ok:
                for r in reasons:
                    tag = r.split("(")[0].strip()[:40]
                    self.rejects[tag] = self.rejects.get(tag, 0) + 1
                continue
            self.resting[key] = {
                "slug": slug, "side": side, "league": league, "px": str(entry),
                "qty": qty, "target": str(target), "placed": now, "tick": str(tick),
                "metrics": {k: str(v) for k, v in m.items()},
            }
            self.log(f"  REST {side:5} {qty:>5} @ {entry}  target {target}  "
                     f"depth {m['exit_depth']:.0f}  osc {m['oscillations']}  {slug[:36]}")

    def _check_fills(self, slug, bid, ask, now):
        """A resting bid fills when the market trades down through it."""
        for key, o in list(self.resting.items()):
            if o["slug"] != slug:
                continue
            px = Decimal(o["px"])
            touched = (ask <= px) if o["side"] == "long" else ((Decimal("1") - bid) <= px)
            if touched:
                self.positions[key] = {**o, "filled": now, "entry_px": o["px"],
                                   "last_seen": now}
                del self.resting[key]
                self.log(f"BUY   | {o.get('league','?'):9} {o['side']:5} {o['qty']:>5} @ {px} "
                     f"= ${px*o['qty']:>6.2f}  target {o['target']}  {slug[:36]}")
            elif now - o["placed"] > 300:
                del self.resting[key]

    def _check_exits(self, slug, league, bid, ask, now):
        for key, p in list(self.positions.items()):
            if p["slug"] != slug:
                continue
            entry = Decimal(p["entry_px"])
            target = Decimal(p["target"])
            qty = p["qty"]
            # What we could sell into right now.
            cur = bid if p["side"] == "long" else Decimal("1") - ask
            # A stale position means we missed the path and cannot honestly
            # model the exit - flag it rather than bank a fictional price.
            gap = now - p.get("last_seen", p["filled"])
            p["last_seen"] = now

            reason = None
            if cur >= target:
                reason = "target"
            elif cur <= 0:
                reason = "zero"
            elif (entry - cur) >= config.STOP_TICKS * Decimal(p.get("tick") or config.TICK):
                reason = "stop"
            elif now - p["filled"] > config.MAX_HOLD_SECS:
                reason = "timeout"
            if reason:
                # A resting sell limit fills AT the limit, never better. Only a
                # forced exit (timeout/zero) takes the live price.
                fill = target if reason == "target" else cur
                pnl = (fill - entry) * qty
                if gap > config.MAX_GAP_SECS:
                    self.log(f"  (gap {gap:.0f}s before exit - trade flagged suspect)")
                if reason in ("stop", "zero"):
                    self.cooldown[key] = now + config.REENTRY_COOLDOWN_SECS
                self.closed.append({**p, "exit_px": str(fill), "pnl": str(pnl),
                                    "reason": reason, "closed": now,
                                    "suspect": gap > config.MAX_GAP_SECS,
                                    "gap_secs": round(gap, 1)})
                del self.positions[key]
                self.log(f"  CLOSE {reason:7} @ {cur}  pnl {pnl:+.2f}  {slug[:36]}")

    def unrealised(self):
        """Mark-to-market on open positions.

        Without this the headline is survivorship: winners hit target and close
        quickly while losers sit open until timeout, so realised P&L alone looks
        like a 100% win rate.
        """
        tot = Decimal("0")
        for p in self.positions.values():
            mark = p.get("mark")
            if mark is None:
                continue
            tot += (Decimal(mark) - Decimal(p["entry_px"])) * p["qty"]
        return tot

    def reap(self, live_markets=None, now=None, settle=None):
        """Expire positions and orders whose market has gone quiet.

        Exit checks only run on book updates, so once a game ends and its feed
        stops, positions there never hit their timeout and resting orders never
        expire - they sit forever holding MAX_POSITIONS slots. This runs on a
        timer instead, independent of the feed.
        """
        now = now or time.time()
        reaped = 0

        # Ignore the first sweeps after a restart: self.subscribed is empty or
        # partial then, so every live position looks abandoned.
        self._sweeps = getattr(self, "_sweeps", 0) + 1
        warming = self._sweeps <= config.REAP_GRACE_SWEEPS

        for key, o in list(self.resting.items()):
            gone = live_markets is not None and o["slug"] not in live_markets
            if gone or now - o["placed"] > 300:
                del self.resting[key]
                reaped += 1

        for key, p in list(self.positions.items()):
            last = p.get("last_seen", p.get("filled", now))
            missing = live_markets is not None and p["slug"] not in live_markets

            # Count consecutive sweeps the market has been absent; a single
            # absence is not evidence the game is over.
            p["miss"] = (p.get("miss", 0) + 1) if missing else 0
            gone = (not warming
                    and p["miss"] >= config.REAP_GRACE_SWEEPS
                    and now - last >= config.REAP_GRACE_SECS)

            if not gone and now - last < config.MAX_HOLD_SECS:
                continue
            # Mark out at the last price we actually saw; if we never saw one,
            # the entry is all we can honestly claim.
            mark = Decimal(p.get("mark") or p["entry_px"])
            reason = "market_gone" if gone else "timeout"
            # A finished market has a real answer, so use it rather than the
            # last quote. Marking a loser out at the price it was showing
            # before the book emptied understated these losses.
            if gone and settle is not None:
                val = settle(p["slug"])
                if val is not None:
                    mark = val if p["side"] == "long" else Decimal("1") - val
                    reason = "settled"
            pnl = (mark - Decimal(p["entry_px"])) * p["qty"]
            self.log(f"SELL  | {p.get('league','?'):9} {p['side']:5} {p['qty']:>5} @ {mark} "
                     f"= ${mark*p['qty']:>6.2f}  pnl {pnl:+6.2f}  {reason:11} "
                     f"stale {now-last:.0f}s  {p['slug'][:32]}")
            self.closed.append({**p, "exit_px": str(mark), "pnl": str(pnl),
                                "reason": reason, "closed": now,
                                "suspect": p.get("mark") is None,
                                "gap_secs": round(now - last, 1)})
            del self.positions[key]
            reaped += 1

        return reaped

    def summary(self):
        allc = self.closed
        real = sum(Decimal(c["pnl"]) for c in allc) if allc else Decimal("0")
        wins = sum(1 for c in allc if Decimal(c["pnl"]) > 0)
        unreal = self.unrealised()
        gapped = sum(1 for c in allc if c.get("suspect"))
        s = (f"open {len(self.positions)} resting {len(self.resting)} "
             f"| closed {len(allc)} {wins}W/{len(allc)-wins}L real {real:+.2f} "
             f"unreal {unreal:+.2f} TOTAL {real + unreal:+.2f}")
        return s + (f" ({gapped} gapped)" if gapped else "")
