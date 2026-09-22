"""Tape storage: dedup, daily rotation, gzip. Books, trades and excursions."""

import csv
import gzip
import time
from datetime import datetime, timezone

from . import config

BOOK_HEADER = ["ts", "league", "sport", "event", "market", "period", "bid", "ask", "mid",
               "spread", "short_px", "bid_depth", "ask_depth", "bid_total", "ask_total",
               "imbalance", "state", "bid_levels", "ask_levels",
               # Appended, never inserted: a file opened earlier today already
               # has its header row written, and shifting a column would
               # misalign every row appended afterwards.
               "score"]

TRADE_HEADER = ["ts", "league", "sport", "market", "price", "qty", "taker_side",
                "taker_intent", "maker_side", "trade_id", "trade_time"]

EXC_HEADER = ["ts", "kind", "league", "sport", "market", "side", "threshold",
              "start_px", "min_px", "peak_px", "cur_px", "recovered_ticks",
              "ticks_off_low", "dip_secs", "secs_since_low"]


class _Rolling:
    """Daily-rotated gzip CSVs, one file per sport.

    Files are named <prefix>-<sport>-<YYYY-MM-DD>.csv.gz so a single sport can
    be analysed without reading every other sport's tape.
    """

    def __init__(self, prefix, header, dedup=False, flush_secs=30):
        self.prefix, self.header, self.dedup = prefix, header, dedup
        self.flush_secs = flush_secs
        self._files: dict[tuple, tuple] = {}   # (sport, day) -> (fh, writer)
        self._last: dict[str, tuple] = {}
        self._last_flush = time.time()
        self.written = self.skipped = 0

    def _writer(self, sport):
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = (sport, day)
        if key in self._files:
            return self._files[key][1]
        # a new day has started: close yesterday's handles
        for k in [k for k in self._files if k[1] != day]:
            fh, _ = self._files.pop(k)
            try:
                fh.close()
            except Exception:
                pass
        path = config.DATA / f"{self.prefix}-{sport}-{day}.csv.gz"
        new = not path.exists()
        fh = gzip.open(path, "at", newline="", compresslevel=6)
        w = csv.writer(fh)
        if new:
            w.writerow(self.header)
        self._files[key] = (fh, w)
        return w

    def write(self, row: dict, dedup_key=None, dedup_state=None):
        if self.dedup and dedup_key is not None:
            if self._last.get(dedup_key) == dedup_state:
                self.skipped += 1
                return False
            self._last[dedup_key] = dedup_state
        sport = (row.get("sport") or "other").replace("/", "_")
        w = self._writer(sport)
        w.writerow([row.get(k, "") for k in self.header])
        self.written += 1
        now = time.time()
        if now - self._last_flush >= self.flush_secs:
            self.flush()
            self._last_flush = now
        return True

    def flush(self):
        for fh, _ in self._files.values():
            try:
                fh.flush()
            except Exception:
                pass

    def close(self):
        for fh, _ in self._files.values():
            try:
                fh.flush()
                fh.close()
            except Exception:
                pass
        self._files.clear()


class Store:
    """All three tapes together."""

    def __init__(self):
        self.books = _Rolling("tape", BOOK_HEADER, dedup=config.DEDUP)
        self.trades = _Rolling("trades", TRADE_HEADER)
        self.exc = _Rolling("excursions", EXC_HEADER, flush_secs=10)

    def flush(self):
        for t in (self.books, self.trades, self.exc):
            t.flush()

    def close(self):
        for t in (self.books, self.trades, self.exc):
            t.close()

    @property
    def written(self):
        return self.books.written

    @property
    def skipped(self):
        return self.books.skipped
