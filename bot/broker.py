"""Real-money order placement, behind hard limits.

Everything here is off unless LS_REAL_ENABLED is true, and every order passes
the same set of guards. The guards are deliberately dumb and independent: a
master switch, a cap on how many real entries may ever be opened, a cap on
total dollars deployed, a sport allowlist, and a refusal to hold two real
positions in one market.

The trade counter is persisted and incremented BEFORE the order is sent. If
the process dies mid-order we lose a slot rather than reuse one - the safe
direction when the failure mode is spending real money twice.
"""

import json
import time
from decimal import Decimal

from . import config


class Broker:
    def __init__(self, client, log):
        self.client = client
        self.log = log
        self.state = self._load()

    # --- persisted counters -------------------------------------------------
    def _load(self):
        try:
            d = json.loads(config.LS_REAL_STATE.read_text())
        except (OSError, ValueError):
            d = {}
        return {"entries": int(d.get("entries", 0)),
                "spent": float(d.get("spent", 0.0)),
                "orders": list(d.get("orders", []))}

    def _save(self):
        try:
            tmp = config.LS_REAL_STATE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, indent=1))
            tmp.replace(config.LS_REAL_STATE)
        except OSError as e:
            self.log(f"REAL: could not save broker state: {e}")

    # --- guards -------------------------------------------------------------
    def why_not(self, sport, slug, stake, open_real_slugs):
        """Reason this trade may NOT go real, or None if every guard passes."""
        if not config.LS_REAL_ENABLED:
            return "real trading disabled"
        if sport not in config.LS_REAL_SPORTS:
            return f"{sport} not in the real allowlist"
        if self.state["entries"] >= config.LS_REAL_MAX_TRADES:
            return (f"cap reached ({self.state['entries']}/"
                    f"{config.LS_REAL_MAX_TRADES} real trades)")
        if Decimal(str(self.state["spent"])) + stake > config.LS_REAL_MAX_SPEND:
            return (f"spend cap reached (${self.state['spent']:.2f} + "
                    f"${float(stake):.2f} > ${float(config.LS_REAL_MAX_SPEND):.2f})")
        if slug in open_real_slugs:
            return "already holding a real position in this market"
        return None

    def remaining(self):
        return (config.LS_REAL_MAX_TRADES - self.state["entries"],
                float(config.LS_REAL_MAX_SPEND) - self.state["spent"])

    # --- orders -------------------------------------------------------------
    @staticmethod
    def _intent(side, opening):
        if side == "long":
            return "ORDER_INTENT_BUY_LONG" if opening else "ORDER_INTENT_SELL_LONG"
        return "ORDER_INTENT_BUY_SHORT" if opening else "ORDER_INTENT_SELL_SHORT"

    def _order(self, slug, side, qty, price, opening, preview=False):
        """One limit IOC order. Limit, not market: at 5c a market order can
        walk the book several ticks, which is most of the edge."""
        req = {
            "marketSlug": slug,
            "intent": self._intent(side, opening),
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": f"{float(price):.4f}"},
            "quantity": int(qty),
            "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
        }
        if preview:
            return self.client.orders.preview({"request": req})
        return self.client.orders.create(req)

    @staticmethod
    def filled(resp):
        """(shares, average price) actually executed."""
        ex = (resp or {}).get("executions") or []
        got = 0
        cash = 0.0
        for e in ex:
            if e.get("type") in ("EXECUTION_TYPE_FILL", "EXECUTION_TYPE_PARTIAL_FILL"):
                n = int(float(e.get("lastShares") or 0))
                px = float((e.get("lastPx") or {}).get("value") or 0)
                got += n
                cash += n * px
        return got, (cash / got if got else 0.0)

    def open_real(self, slug, side, qty, price, sport, open_real_slugs):
        """Place a real entry. Returns a dict to merge onto the position, or None."""
        stake = Decimal(str(qty)) * Decimal(str(price))
        stop = self.why_not(sport, slug, stake, open_real_slugs)
        if stop:
            return None
        # Reserve the slot first: dying after this costs a slot, not a double spend.
        self.state["entries"] += 1
        self.state["spent"] = round(self.state["spent"] + float(stake), 4)
        self._save()
        try:
            resp = self._order(slug, side, qty, price, opening=True)
        except Exception as e:
            # The slot stays consumed on purpose. An exception leaves it unknown
            # whether the order reached the venue, and burning a slot is cheaper
            # than risking a second order for the same signal. A definitive
            # no-fill below does release the slot, because there we know.
            self.log(f"REAL: entry REJECTED {slug[:40]} {type(e).__name__}: {str(e)[:120]}"
                     f"  (slot kept - fill state unknown)")
            self.state["spent"] = round(self.state["spent"] - float(stake), 4)
            self._save()
            return None
        got, avg = self.filled(resp)
        if not got:
            self.log(f"REAL: entry unfilled {slug[:40]} (IOC, no liquidity at {float(price):.4f})")
            self.state["entries"] -= 1
            self.state["spent"] = round(self.state["spent"] - float(stake), 4)
            self._save()
            return None
        self.state["spent"] = round(self.state["spent"] - float(stake) + got * avg, 4)
        self.state["orders"].append({"slug": slug, "side": side, "qty": got,
                                     "px": avg, "ts": time.time(),
                                     "id": (resp or {}).get("id")})
        self._save()
        left, cash = self.remaining()
        self.log(f"REAL BUY | {slug[:38]} {side} {got} @ {avg:.4f} = ${got*avg:.2f}"
                 f"   [{self.state['entries']}/{config.LS_REAL_MAX_TRADES} used, "
                 f"${cash:.2f} left]")
        return {"real": True, "real_qty": got, "real_entry_px": f"{avg:.4f}",
                "real_order_id": (resp or {}).get("id")}

    def close_real(self, pos, price):
        """Sell a real position. Returns (shares, avg px) or (0, 0)."""
        slug, side = pos["slug"], pos["side"]
        qty = int(pos.get("real_qty") or 0)
        if qty <= 0:
            return 0, 0.0
        try:
            resp = self._order(slug, side, qty, price, opening=False)
        except Exception as e:
            self.log(f"REAL: EXIT FAILED {slug[:40]} {type(e).__name__}: {str(e)[:120]}"
                     f"  - position still held, will retry")
            return 0, 0.0
        got, avg = self.filled(resp)
        if not got:
            self.log(f"REAL: exit unfilled {slug[:40]} at {float(price):.4f} - will retry")
            return 0, 0.0
        self.log(f"REAL SELL| {slug[:38]} {side} {got} @ {avg:.4f} = ${got*avg:.2f}")
        return got, avg
