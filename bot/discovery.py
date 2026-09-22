"""Discovery of in-play markets.

Originally this swept each series with search.query(limit=20). That silently
missed live games: for any series where season-long props outnumber fixtures
(NFL had 46 fixtures behind ~50 prop markets) the games fell outside the first
page and were never seen. We ran an entire NFL Sunday with zero football.

events.list with a start-date window returns live games across every series in
one call, ordered by start time, so nothing hides behind props.
"""

import re
import time
from decimal import Decimal
from datetime import datetime, timedelta, timezone

from polymarket_us.errors import RateLimitError

from . import config

_YEAR = re.compile(r"-\d{4}$")


def league_of(series_slug: str) -> str:
    """'nfl-2025' -> 'NFL', 'ligpor' -> 'LIGPOR'."""
    if not series_slug:
        return "?"
    return _YEAR.sub("", series_slug).upper()


class Discovery:
    """Finds in-play markets, paging a time window rather than per-series."""

    def __init__(self, client, log):
        self.client = client
        self.log = log
        self.backoff = 0.0
        self.spacing = config.REQUEST_SPACING
        self.last_request = 0.0
        self.sweeps = 0
        self.rate_limit_hits = 0

    def _wait(self):
        gap = time.time() - self.last_request
        if gap < self.spacing:
            time.sleep(self.spacing - gap)
        self.last_request = time.time()

    @staticmethod
    def _state_class(e, period) -> str:
        """Live/dead/unknown, preferring what the API states outright.

        The event carries `live` and `ended` booleans (and a `score`), either
        at the top level or inside `eventState`. We were reading none of them
        and inferring everything from the `period` string, which is how a
        finished match kept looking tradeable. `ended` is authoritative and
        beats any period value; `live` is trusted next; the period regex is
        only the fallback for events with no state attached.
        """
        st = e.get("eventState") or {}
        ended = e.get("ended")
        if ended is None:
            ended = st.get("ended")
        if ended is True:
            return "dead"
        live = e.get("live")
        if live is None:
            live = st.get("live")
        if live is True:
            return "live"
        cls = config.period_class(period)
        if live is False and cls != "live":
            # State says not live and the period does not contradict it.
            return "dead"
        return cls

    @staticmethod
    def _started(e) -> bool:
        sd = e.get("startDate")
        if not sd:
            return False
        try:
            t = datetime.fromisoformat(str(sd).replace("Z", "+00:00"))
        except ValueError:
            return False
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t <= datetime.now(timezone.utc)

    @staticmethod
    def _two_sided(e) -> bool:
        for m in e.get("markets", []):
            if m.get("status") != "MARKET_STATUS_OPEN":
                continue
            try:
                bid = float((m.get("bestBidQuote") or {}).get("value") or 0)
                ask = float((m.get("bestAskQuote") or {}).get("value") or 0)
            except (TypeError, ValueError):
                continue
            if bid > 0 and ask > 0 and ask > bid:
                return True
        return False

    def _note_unknown(self, period):
        """Warn once per unrecognised period so a new marker cannot go unseen."""
        key = str(period or "").strip() or "(empty)"
        seen = getattr(self, "_unknown_periods", None)
        if seen is None:
            seen = self._unknown_periods = set()
        if key not in seen:
            seen.add(key)
            self.log(f"PERIOD unrecognised {key!r} - recorded but not traded; "
                     f"add it to _LIVE_PATTERNS if it means in-play")

    # {event_slug: "6-3, 2-1:30-15"} as of the last sweep. Kept beside the
    # meta tuple rather than inside it, so nothing that unpacks meta breaks.
    scores: dict = {}

    def liveness(self):
        """{market_slug: bool} - is this market's game actually running now.

        A full sweep runs every few minutes; a tennis match can finish and be
        bought inside that gap. This asks the same endpoint but does none of
        the market ranking or subscription bookkeeping, so it can run often.
        Returns None if the call fails, so the caller can tell "nothing is
        live" apart from "we could not find out".
        """
        now = datetime.now(timezone.utc)
        lo = (now - timedelta(hours=config.LOOKBACK_HOURS)).isoformat()
        hi = (now + timedelta(hours=config.LOOKAHEAD_HOURS)).isoformat()
        out, ok = {}, False
        for page in range(1, config.MAX_DISCOVERY_PAGES + 1):
            self._wait()
            params = {"limit": 100, "closed": False, "startDateMin": lo,
                      "startDateMax": hi, "orderBy": ["startDate"],
                      "orderDirection": "asc"}
            if page > 1:
                params["offset"] = (page - 1) * 100
            try:
                res = self.client.events.list(params)
            except Exception:
                break
            events = res.get("events", [])
            ok = True
            for e in events:
                live = (not e.get("closed")
                        and self._state_class(e, e.get("period")) == "live")
                for m in e.get("markets", []):
                    if m.get("slug"):
                        out[m["slug"]] = live
            if len(events) < 100:
                break
        return out if ok else None

    def sweep(self):
        """Returns {market_slug: (league, event_slug, period)} for live games."""

        found = {}
        if self.backoff:
            self.log(f"discovery backing off {self.backoff:.0f}s after rate limit")
            time.sleep(self.backoff)
            self.backoff = 0.0

        now = datetime.now(timezone.utc)
        lo = (now - timedelta(hours=config.LOOKBACK_HOURS)).isoformat()
        hi = (now + timedelta(hours=config.LOOKAHEAD_HOURS)).isoformat()

        pages = 0
        for page in range(1, config.MAX_DISCOVERY_PAGES + 1):
            self._wait()
            params = {"limit": 100, "closed": False, "startDateMin": lo,
                      "startDateMax": hi, "orderBy": ["startDate"],
                      "orderDirection": "asc"}
            if page > 1:
                params["offset"] = (page - 1) * 100
            try:
                res = self.client.events.list(params)
            except RateLimitError:
                self.rate_limit_hits += 1
                self.spacing = min(self.spacing * 1.5, 10.0)
                self.backoff = min(max(config.RATE_LIMIT_BACKOFF, self.backoff * 2),
                                   config.MAX_BACKOFF)
                self.log(f"RATE LIMITED during discovery; spacing -> {self.spacing:.1f}s")
                break
            except Exception as e:
                self.log(f"discovery page {page} failed: {type(e).__name__}")
                break

            events = res.get("events", [])
            if not events:
                break
            pages += 1

            for e in events:
                if e.get("closed"):
                    continue
                period = e.get("period")
                cls = self._state_class(e, period)
                if cls == "unknown":
                    # Do not drop it in silence - that is exactly how "Live"
                    # and "Map 1" went missing. Report each new one once.
                    self._note_unknown(period)
                if cls == "dead":
                    continue
                if cls == "unknown":
                    # Record it only if it is actually under way and quoting.
                    if not (config.RECORD_STARTED_UNKNOWN
                            and self._started(e) and self._two_sided(e)):
                        continue
                league = league_of(e.get("seriesSlug", ""))
                st = e.get("eventState") or {}
                sc = e.get("score") or st.get("score")
                if sc:
                    self.scores[e.get("slug", "")] = str(sc)
                keep = []
                ticks = {}
                for m in e.get("markets", []):
                    if m.get("status") != "MARKET_STATUS_OPEN" or not m.get("slug"):
                        continue
                    if config.REQUIRE_TWO_SIDED:
                        try:
                            bid = float((m.get("bestBidQuote") or {}).get("value") or 0)
                            ask = float((m.get("bestAskQuote") or {}).get("value") or 0)
                        except (TypeError, ValueError):
                            continue
                        if bid <= 0 or ask <= 0 or ask <= bid:
                            continue
                        spread = ask - bid
                    else:
                        spread = 1.0
                    # prefer moneyline, then tightest spread as a liquidity proxy
                    try:
                        ticks[m["slug"]] = Decimal(str(m.get("orderPriceMinTickSize")
                                                       or config.DEFAULT_TICK))
                    except Exception:
                        ticks[m["slug"]] = config.DEFAULT_TICK
                    rank = 0 if m["slug"].startswith(config.MARKET_PRIORITY) else 1
                    keep.append((rank, spread, m["slug"]))
                keep.sort()
                for _r, _s, slug in keep[:config.MARKETS_PER_GAME]:
                    found[slug] = (league, e.get("slug", ""), e.get("period"),
                                   ticks.get(slug, config.DEFAULT_TICK))

            if len(events) < 100:
                break

        self.sweeps += 1
        if not self.backoff and self.spacing > config.REQUEST_SPACING:
            self.spacing = max(config.REQUEST_SPACING, self.spacing * 0.9)
        return found
