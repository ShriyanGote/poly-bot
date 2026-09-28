"""Persistent candidate snapshots and outcomes for longshot research."""

import sqlite3
import threading
import time


class SignalLedger:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self.lock = threading.RLock()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS signals (
            signal_id TEXT PRIMARY KEY, ts REAL NOT NULL, slug TEXT NOT NULL,
            league TEXT, sport TEXT, side TEXT NOT NULL, rule TEXT,
            candidate_px REAL NOT NULL, bid REAL, ask REAL, spread REAL,
            spread_pct REAL, entry_depth REAL, bid_total REAL, ask_total REAL,
            activity INTEGER, period TEXT, score TEXT, first_status TEXT,
            first_reason TEXT, entered INTEGER NOT NULL DEFAULT 0,
            actual_entry_px REAL, actual_entry_ts REAL, peak_exit_px REAL,
            peak_ts REAL, settlement REAL, side_settlement REAL, settled_ts REAL
        )""")
        self.db.execute("CREATE INDEX IF NOT EXISTS signals_slug ON signals(slug)")
        # Frozen, paper-only tennis experiment. One row per market/side at the
        # first eligible 1-5c quote; every later quote advances the simulated
        # 5x arm / 20% trail and peak, then settlement closes any remainder.
        self.db.execute("""CREATE TABLE IF NOT EXISTS shadow_tennis (
            signal_id TEXT PRIMARY KEY, ts REAL NOT NULL, slug TEXT NOT NULL,
            league TEXT, side TEXT NOT NULL, entry_px REAL NOT NULL,
            bid REAL, ask REAL, spread REAL, entry_depth REAL, bid_total REAL,
            ask_total REAL, activity INTEGER, period TEXT, score TEXT,
            vol120 REAL, is_doubles INTEGER NOT NULL DEFAULT 0,
            peak_exit REAL NOT NULL DEFAULT 0, trail_armed INTEGER NOT NULL DEFAULT 0,
            simulated_exit REAL, exit_reason TEXT, settlement REAL,
            side_settlement REAL, settled_ts REAL)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS shadow_slug ON shadow_tennis(slug)")
        self.db.commit()
        self.tracked_slugs = {row[0] for row in
                              self.db.execute("SELECT DISTINCT slug FROM signals")}
        self.shadow_slugs = {row[0] for row in
                             self.db.execute("SELECT DISTINCT slug FROM shadow_tennis")}

    @staticmethod
    def signal_id(slug, side, rule):
        return f"{slug}|{side}|{rule}"

    def observe(self, *, slug, league, sport, side, rule, price, exit_px,
                bid, ask, spread, entry_depth, bid_total, ask_total,
                activity, period, score, status="candidate", reason=""):
        """Insert the first candidate snapshot and advance its peak mark."""
        with self.lock:
            now = time.time()
            sid = self.signal_id(slug, side, rule)
            pct = float(spread) / float(price) if price else None
            self.db.execute("""INSERT OR IGNORE INTO signals
                (signal_id,ts,slug,league,sport,side,rule,candidate_px,bid,ask,
                 spread,spread_pct,entry_depth,bid_total,ask_total,activity,period,
                 score,first_status,first_reason,peak_exit_px,peak_ts)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sid, now, slug, league, sport, side, rule, float(price), float(bid),
                 float(ask), float(spread), pct, float(entry_depth), float(bid_total),
                 float(ask_total), int(activity), str(period or ""), str(score or ""),
                 status, reason, max(0.0, float(exit_px)), now))
            self.tracked_slugs.add(slug)
            if status != "candidate" or reason:
                self.db.execute("""UPDATE signals SET first_status=?, first_reason=?
                    WHERE signal_id=? AND entered=0 AND first_status='candidate'""",
                    (status, reason, sid))
            px = max(0.0, float(exit_px))
            self.db.execute("""UPDATE signals SET peak_exit_px=MAX(COALESCE(peak_exit_px,0),?),
                peak_ts=CASE WHEN ?>COALESCE(peak_exit_px,0) THEN ? ELSE peak_ts END
                WHERE signal_id=?""", (px, px, now, sid))
            self.db.commit()
            return sid

    def mark_reason(self, sid, status, reason):
        if not sid:
            return
        with self.lock:
            self.db.execute("""UPDATE signals SET first_status=?, first_reason=?
                WHERE signal_id=? AND entered=0 AND first_status IN ('candidate','pending')""",
                (status, reason, sid))
            self.db.commit()

    def mark_entered(self, sid, price, opened):
        if not sid:
            return
        with self.lock:
            self.db.execute("""UPDATE signals SET entered=1, first_status='entered',
                first_reason='', actual_entry_px=?, actual_entry_ts=? WHERE signal_id=?""",
                (float(price), float(opened), sid))
            self.db.commit()

    def update_peak(self, slug, long_exit, short_exit):
        if slug not in self.tracked_slugs and slug not in self.shadow_slugs:
            return
        now = time.time()
        with self.lock:
            # Continue updating prior candidates, even if the legacy signal
            # table has no row for this market.
            if slug in self.tracked_slugs:
                for side, value in (("long", long_exit), ("short", short_exit)):
                    px = max(0.0, float(value))
                    self.db.execute("""UPDATE signals SET peak_ts=CASE WHEN ?>COALESCE(peak_exit_px,0)
                        THEN ? ELSE peak_ts END, peak_exit_px=MAX(COALESCE(peak_exit_px,0),?)
                        WHERE slug=? AND side=? AND settlement IS NULL""",
                        (px, now, px, slug, side))
            for side, value in (("long", long_exit), ("short", short_exit)):
                px = max(0.0, float(value))
                row = self.db.execute("""SELECT signal_id,entry_px,peak_exit,trail_armed,
                    simulated_exit FROM shadow_tennis WHERE slug=? AND side=?
                    AND settlement IS NULL""", (slug, side)).fetchall()
                for sid, entry, peak, armed, simulated_exit in row:
                    peak = max(float(peak or 0), px)
                    if simulated_exit is None:
                        armed = bool(armed) or px >= float(entry) * 5.0
                        sold = armed and px <= peak * 0.8
                        self.db.execute("""UPDATE shadow_tennis SET peak_exit=?,trail_armed=?,
                            simulated_exit=CASE WHEN ? THEN ? ELSE simulated_exit END,
                            exit_reason=CASE WHEN ? THEN 'trail_5x_20pct' ELSE exit_reason END
                            WHERE signal_id=?""", (peak, int(armed), int(sold), px,
                                                    int(sold), sid))
                    else:
                        # Continue measuring eventual peak for classification,
                        # but do not change the already-realized shadow exit.
                        self.db.execute("UPDATE shadow_tennis SET peak_exit=? WHERE signal_id=?",
                                        (peak, sid))
            self.db.commit()

    def observe_shadow(self, *, slug, league, side, price, bid, ask, spread,
                       entry_depth, bid_total, ask_total, activity, period,
                       score, vol120):
        if not (0 < float(price) <= .05):
            return
        sid = f"{slug}|{side}|shadow-tennis-v1"
        is_doubles = int(any(x in (slug or "").lower() for x in
                             ("dbl", "double", "wtadb", "itfdb"))
                         or "double" in (league or "").lower())
        with self.lock:
            self.db.execute("""INSERT OR IGNORE INTO shadow_tennis
                (signal_id,ts,slug,league,side,entry_px,bid,ask,spread,entry_depth,
                 bid_total,ask_total,activity,period,score,vol120,is_doubles,peak_exit)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sid, time.time(), slug, league, side, float(price), float(bid),
                 float(ask), float(spread), float(entry_depth), float(bid_total),
                 float(ask_total), int(activity), str(period or ""), str(score or ""),
                 float(vol120) if vol120 is not None else None, is_doubles,
                 float(bid if side == "long" else 1 - float(ask))))
            self.shadow_slugs.add(slug)
            self.db.commit()

    def update_settlements(self, settlements):
        now = time.time()
        with self.lock:
            for slug, value in settlements.items():
                try:
                    long_value = float(value)
                except (TypeError, ValueError):
                    continue
                self.db.execute("""UPDATE signals SET settlement=?,
                    side_settlement=CASE WHEN side='long' THEN ? ELSE 1-? END,
                    settled_ts=? WHERE slug=? AND settlement IS NULL""",
                    (long_value, long_value, long_value, now, slug))
                self.db.execute("""UPDATE shadow_tennis SET settlement=?,
                    side_settlement=CASE WHEN side='long' THEN ? ELSE 1-? END,
                    simulated_exit=COALESCE(simulated_exit,CASE WHEN side='long' THEN ? ELSE 1-? END),
                    exit_reason=COALESCE(exit_reason,'settlement'), settled_ts=?
                    WHERE slug=? AND settlement IS NULL""",
                    (long_value, long_value, long_value, long_value, long_value, now, slug))
            self.db.commit()

    def close(self):
        with self.lock:
            self.db.commit()
            self.db.close()
