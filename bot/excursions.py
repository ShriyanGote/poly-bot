"""Dip-and-recover tracking - the core hypothesis, measured.

For each market side we watch for the price dropping below a threshold
(3%, 4%, 5%, ...) and then record whether it climbed back and by how much.
Every excursion is written out, and a per-market profile is accumulated so
markets can be ranked by how often they actually produce the pattern.
"""

import time
from collections import defaultdict
from decimal import Decimal

TICK = Decimal("0.01")

# Thresholds to mark. Each is watched independently on each side.
THRESHOLDS = [Decimal(x) for x in ("0.02", "0.03", "0.04", "0.05", "0.08", "0.10", "0.15")]

# A dip counts as "recovered" once it climbs this many ticks off its low.
RECOVERY_TICKS = [1, 2, 3]

# Abandon a dip that never resolves.
MAX_DIP_SECS = 7200


class Excursions:
    """Tracks dips below each threshold and their recoveries."""

    def __init__(self, on_event=None):
        self.on_event = on_event
        # (market, side, threshold) -> active dip dict
        self.active: dict[tuple, dict] = {}
        # (market, side) -> profile counters
        self.profile: dict[tuple, dict] = defaultdict(self._new_profile)
        self.events = 0

    @staticmethod
    def _new_profile():
        p = {"samples": 0, "min_px": None, "max_px": None, "league": "?", "period": "?"}
        for t in THRESHOLDS:
            k = f"{t}"
            p[f"dips_{k}"] = 0
            p[f"died_{k}"] = 0
            for r in RECOVERY_TICKS:
                p[f"rec{r}_{k}"] = 0
        return p

    def update(self, market, side, px, league, period, now=None):
        """Feed one price observation for one side of one market."""
        if px is None or px <= 0:
            return
        now = now or time.time()
        pkey = (market, side)
        prof = self.profile[pkey]
        prof["samples"] += 1
        prof["league"] = league
        prof["period"] = period
        prof["min_px"] = px if prof["min_px"] is None else min(prof["min_px"], px)
        prof["max_px"] = px if prof["max_px"] is None else max(prof["max_px"], px)

        for t in THRESHOLDS:
            key = (market, side, t)
            dip = self.active.get(key)

            if dip is None:
                # Start a dip when we cross below the threshold.
                if px < t:
                    self.active[key] = {
                        "market": market, "side": side, "league": league, "threshold": t,
                        "start_ts": now, "start_px": px, "min_px": px, "min_ts": now,
                        "peak_px": px, "recorded": set(), "ever": set(),
                    }
                continue

            # Inside a dip.
            if px < dip["min_px"]:
                dip["min_px"] = px
                dip["min_ts"] = now
                dip["peak_px"] = px          # reset the climb from the new low
                dip["recorded"].clear()
            if px > dip["peak_px"]:
                dip["peak_px"] = px

            ticks_up = (dip["peak_px"] - dip["min_px"]) / TICK
            for r in RECOVERY_TICKS:
                if ticks_up >= r and r not in dip["recorded"]:
                    dip["recorded"].add(r)
                    dip["ever"].add(r)
                    self._emit("recover", dip, px, now, recovered_ticks=r)

            # Dip resolves when price climbs back above the threshold.
            if px >= t:
                self._credit(prof, dip)
                self._emit("exit_above", dip, px, now)
                del self.active[key]
            elif now - dip["start_ts"] > MAX_DIP_SECS:
                self._credit(prof, dip)
                self._emit("timeout", dip, px, now)
                del self.active[key]

    @staticmethod
    def _credit(prof, dip):
        """Score a finished dip episode: one count per tier it ever reached."""
        prof[f"dips_{dip['threshold']}"] += 1
        for r in dip["ever"]:
            prof[f"rec{r}_{dip['threshold']}"] += 1

    def kill(self, market, reason="closed", now=None):
        """Market went away - close out any open dips as deaths."""
        now = now or time.time()
        for key in [k for k in self.active if k[0] == market]:
            dip = self.active.pop(key)
            prof = self.profile[(market, dip["side"])]
            prof[f"died_{dip['threshold']}"] += 1
            for r in dip["ever"]:
                prof[f"rec{r}_{dip['threshold']}"] += 1
            self._emit(reason, dip, Decimal("0"), now)

    def _emit(self, kind, dip, px, now, recovered_ticks=0):
        self.events += 1
        if not self.on_event:
            return
        self.on_event({
            "ts": now, "kind": kind, "market": dip["market"], "league": dip["league"],
            "side": dip["side"], "threshold": str(dip["threshold"]),
            "start_px": str(dip["start_px"]), "min_px": str(dip["min_px"]),
            "peak_px": str(dip["peak_px"]), "cur_px": str(px),
            "recovered_ticks": recovered_ticks,
            "ticks_off_low": str((dip["peak_px"] - dip["min_px"]) / TICK),
            "dip_secs": round(now - dip["start_ts"], 1),
            "secs_since_low": round(now - dip["min_ts"], 1),
        })

    def scorecard(self, min_samples=50):
        """Per market-side recovery stats, best first."""
        rows = []
        for (market, side), p in self.profile.items():
            if p["samples"] < min_samples:
                continue
            for t in THRESHOLDS:
                k = f"{t}"
                dips = p[f"dips_{k}"] + p[f"died_{k}"]
                if not dips:
                    continue
                rows.append({
                    "market": market, "side": side, "league": p["league"],
                    "threshold": k, "dips": dips,
                    "rec1": p[f"rec1_{k}"], "rec2": p[f"rec2_{k}"], "rec3": p[f"rec3_{k}"],
                    "died": p[f"died_{k}"],
                    "rec2_rate": (p[f"rec2_{k}"] / dips) if dips else 0.0,
                    "samples": p["samples"],
                })
        rows.sort(key=lambda r: (-r["rec2_rate"], -r["dips"]))
        return rows
