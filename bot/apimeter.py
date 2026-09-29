"""Count every REST call the bot makes, by kind, and every rate-limit reply.

All engines share one API key, and a real-money engine that gets
rate-limited mid-requote is left with orders it cannot move. So measure the
budget instead of guessing at it: calls per minute by category, 429s, and
round-trip time, from both the sync client (discovery, settlement) and the
async one (orders).
"""

import collections
import functools
import time


def _category(path):
    p = path or ""
    if "/orders" in p or "/order/" in p:
        return "orders"
    if "/settlement" in p:
        return "settlement"
    if "/portfolio" in p or "/account" in p:
        return "portfolio"
    if "/events" in p or "/markets" in p:
        return "discovery"
    return "other"


class ApiMeter:
    def __init__(self, window=3600):
        self.window = window
        self.calls = collections.deque()          # (t, category, status, ms)
        self.total = collections.Counter()
        self.limited_total = collections.Counter()

    def record(self, path, status, ms):
        cat = _category(path)
        now = time.time()
        self.calls.append((now, cat, status, ms))
        self.total[cat] += 1
        if status == 429:
            self.limited_total[cat] += 1
        while self.calls and now - self.calls[0][0] > self.window:
            self.calls.popleft()

    def wrap(self, client):
        """Meter a PolymarketUS or AsyncPolymarketUS client in place."""
        orig = client._request
        meter = self

        def status_of(e):
            r = getattr(e, "response", None)
            return getattr(r, "status_code", None) or getattr(e, "status_code", None) or "err"

        if _is_coroutine(orig):
            @functools.wraps(orig)
            async def metered(method, path, **kw):
                t0 = time.time()
                try:
                    out = await orig(method, path, **kw)
                except Exception as e:
                    meter.record(path, status_of(e), (time.time() - t0) * 1000)
                    raise
                meter.record(path, 200, (time.time() - t0) * 1000)
                return out
        else:
            @functools.wraps(orig)
            def metered(method, path, **kw):
                t0 = time.time()
                try:
                    out = orig(method, path, **kw)
                except Exception as e:
                    meter.record(path, status_of(e), (time.time() - t0) * 1000)
                    raise
                meter.record(path, 200, (time.time() - t0) * 1000)
                return out
        client._request = metered
        return client

    def summary(self):
        now = time.time()
        cats = collections.defaultdict(lambda: {"last_min": 0, "last_hour": 0,
                                                "limited_hour": 0, "ms": []})
        for t, cat, status, ms in self.calls:
            c = cats[cat]
            c["last_hour"] += 1
            if now - t <= 60:
                c["last_min"] += 1
            if status == 429:
                c["limited_hour"] += 1
            c["ms"].append(ms)
        out = {}
        for cat, c in cats.items():
            ms = sorted(c.pop("ms"))
            c["p50_ms"] = round(ms[len(ms) // 2], 1) if ms else None
            c["total"] = self.total[cat]
            c["limited_total"] = self.limited_total[cat]
            out[cat] = c
        return {
            "by_category": out,
            "last_min": sum(c["last_min"] for c in out.values()),
            "last_hour": sum(c["last_hour"] for c in out.values()),
            "limited_hour": sum(c["limited_hour"] for c in out.values()),
        }


def _is_coroutine(f):
    import inspect
    return inspect.iscoroutinefunction(f)
