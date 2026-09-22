"""Entry gates and EV estimation for longshot scalping.

The core fact this module encodes: the tick is a fixed 1 cent, so the
round-trip spread cost as a fraction of stake is 0.01/price. That is 50% at
2c and 5% at 20c. Cheap prices are the EXPENSIVE ones to scalp.

Under a fair martingale, P(reach target T before 0 | start S) = S/T, which
makes gross EV exactly zero at every price. So no band is inherently better;
edge can only come from (a) paying less spread than you capture, or
(b) the price being genuinely wrong. These gates target (a).
"""

from collections import deque
from decimal import Decimal

TICK = Decimal("0.01")

# --- tunable gates -----------------------------------------------------------
MAX_SPREAD_TICKS = 1          # never pay more than one tick of spread
MAX_SPREAD_COST_PCT = Decimal("20")   # 1 tick must be <= this % of entry -> price >= 0.05
MIN_EXIT_DEPTH_MULT = Decimal("3")    # resting size at target >= 3x our size
MIN_OSCILLATIONS = 2          # direction changes in the window
VOL_WINDOW_SECS = 180
MIN_REALIZED_TICKS = 2        # price must have actually moved this many ticks
MAX_TIME_IN_BAND_SECS = 900   # a price parked in the band for ages is decaying, not oscillating


def spread_cost_pct(entry: Decimal) -> Decimal:
    """One tick as a percentage of the entry price."""
    if entry <= 0:
        return Decimal("999")
    return TICK / entry * 100


def martingale_p_win(entry: Decimal, target: Decimal) -> Decimal:
    """P(hit target before zero) for a driftless price."""
    if target <= 0:
        return Decimal("0")
    return min(Decimal("1"), entry / target)


def net_ev(entry: Decimal, target: Decimal, p_win: Decimal | None = None) -> Decimal:
    """EV per share after paying one tick of spread to get in.

    Taking the offer means our effective cost is `entry` but our immediate
    mark is `entry - TICK`. Gross EV is ~0, so net EV is roughly -TICK.
    """
    p = martingale_p_win(entry, target) if p_win is None else p_win
    gross = p * (target - entry) - (Decimal("1") - p) * entry
    return gross - TICK


def depth_at_or_better(levels, price: Decimal, side: str) -> Decimal:
    """Total resting size we could sell into at `price` or better."""
    total = Decimal("0")
    for lv in levels or []:
        try:
            px = Decimal(str(lv["px"]["value"]))
            qty = Decimal(str(lv["qty"]))
        except Exception:
            continue
        if side == "bid" and px >= price:
            total += qty
        elif side == "ask" and px <= price:
            total += qty
    return total


class PriceHistory:
    """Rolling per-market price history for volatility and oscillation counts."""

    def __init__(self, window_secs: int = VOL_WINDOW_SECS):
        self.window = window_secs
        self.pts: dict[str, deque] = {}

    def add(self, key: str, ts: float, px: Decimal):
        dq = self.pts.setdefault(key, deque())
        dq.append((ts, px))
        while dq and ts - dq[0][0] > self.window:
            dq.popleft()

    def _changes(self, key):
        dq = self.pts.get(key) or deque()
        out, prev = [], None
        for _, px in dq:
            if px != prev:
                out.append(px)
                prev = px
        return out

    def oscillations(self, key) -> int:
        """Direction reversals - the quantified version of 'goes up and down'."""
        seq = self._changes(key)
        if len(seq) < 3:
            return 0
        flips, last_dir = 0, 0
        for i in range(1, len(seq)):
            dirn = 1 if seq[i] > seq[i - 1] else -1 if seq[i] < seq[i - 1] else 0
            if dirn and last_dir and dirn != last_dir:
                flips += 1
            if dirn:
                last_dir = dirn
        return flips

    def realized_range_ticks(self, key) -> Decimal:
        seq = self._changes(key)
        if len(seq) < 2:
            return Decimal("0")
        return (max(seq) - min(seq)) / TICK

    def time_in_band(self, key, lo: Decimal, hi: Decimal, now: float) -> float:
        dq = self.pts.get(key) or deque()
        entered = None
        for ts, px in dq:
            if lo <= px <= hi:
                if entered is None:
                    entered = ts
            else:
                entered = None
        return (now - entered) if entered else 0.0


def evaluate(entry: Decimal, target: Decimal, our_qty: Decimal, exit_levels,
             hist: PriceHistory, key: str, now: float, band=(Decimal("0.01"), Decimal("0.05"))):
    """Run every gate. Returns (ok, reasons, metrics)."""
    reasons, m = [], {}

    m["spread_cost_pct"] = spread_cost_pct(entry)
    if m["spread_cost_pct"] > MAX_SPREAD_COST_PCT:
        reasons.append(f"spread is {m['spread_cost_pct']:.0f}% of stake (max {MAX_SPREAD_COST_PCT}%)")

    m["exit_depth"] = depth_at_or_better(exit_levels, target, "bid")
    if m["exit_depth"] < our_qty * MIN_EXIT_DEPTH_MULT:
        reasons.append(f"exit depth {m['exit_depth']:.0f} < {MIN_EXIT_DEPTH_MULT}x our {our_qty:.0f}")

    m["oscillations"] = hist.oscillations(key)
    if m["oscillations"] < MIN_OSCILLATIONS:
        reasons.append(f"only {m['oscillations']} direction flips (need {MIN_OSCILLATIONS})")

    m["range_ticks"] = hist.realized_range_ticks(key)
    if m["range_ticks"] < MIN_REALIZED_TICKS:
        reasons.append(f"range {m['range_ticks']:.1f} ticks (need {MIN_REALIZED_TICKS})")

    m["time_in_band"] = hist.time_in_band(key, band[0], band[1], now)
    if m["time_in_band"] > MAX_TIME_IN_BAND_SECS:
        reasons.append(f"parked in band {m['time_in_band']:.0f}s - decaying, not oscillating")

    m["p_win"] = martingale_p_win(entry, target)
    m["net_ev"] = net_ev(entry, target)
    m["net_ev_pct"] = (m["net_ev"] / entry * 100) if entry else Decimal("0")

    return (not reasons), reasons, m


if __name__ == "__main__":
    print("Spread drag by entry price (1 tick round trip):\n")
    print(f"{'entry':>8}{'tick as % of stake':>22}{'net EV/share':>15}{'net EV %':>11}")
    for p in ["0.02", "0.03", "0.05", "0.08", "0.10", "0.15", "0.20", "0.30"]:
        e = Decimal(p)
        t = e + 2 * TICK
        print(f"{e:>8}{spread_cost_pct(e):>21.0f}%{net_ev(e, t):>15.4f}{net_ev(e,t)/e*100:>10.0f}%")
