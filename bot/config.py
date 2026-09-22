"""Central configuration."""

from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOGS = ROOT / "logs"
DATA.mkdir(exist_ok=True)
# Drop-box for commands aimed at the running engine. The state file cannot be
# used for this: the recorder rewrites it in full and would overwrite them.
REQUESTS = DATA / "requests"
LOGS.mkdir(exist_ok=True)

STATE_FILE = DATA / "paper_state.json"
LONGSHOT_STATE = DATA / "longshot_state.json"

# --- longshot convexity engine ------------------------------------------------
# Buy cheap and hold: loss is capped at the premium paid, so there is no stop to
# gap through. Sized small and wide because most expire worthless.
LS_BAND_LO = Decimal("0.01")
LS_BAND_HI = Decimal("0.05")
LS_STAKE = Decimal("1.00")
LS_MAX_POSITIONS = 100
# Take-profit multiples. Untimed sports get a HIGHER bar than timed (a
# comeback there has no clock working against it) but not an infinite one:
# holding tennis to settlement with no exit rule rode a 6.8x peak back to zero
# on itfwo-jiahan-rentan, where tp5x would have returned +$4 on a $1 stake.
LS_TAKE_PROFIT = Decimal("4")      # timed sports (clock closes the window)
LS_TAKE_PROFIT_UNTIMED = Decimal("5")

# Trailing exit, from lssim.py on 90 tennis positions. A fixed take-profit
# caps winners that run to 15-36x, while holding gives them all back. Arming
# late and trailing loosely captured +41% against -7% for tp5x and +25% for
# hold. The whole arm>=4x / drawdown 40-60% block scored +31..+46%, so this is
# a plateau rather than a tuned point.
LS_USE_TRAIL = True
# 3.6x rather than a round 4x: most entries are at 0.05, so a 4x arm lands on
# exactly 0.20 - a round number where these thin books visibly stall. Six
# tennis positions peaked in 3.5-4.0x against only two in 4.0-4.5x, and peak
# price 0.19 recurred four times. Backtest is flat across 2x-4x, so this costs
# nothing historically and avoids placing the trigger on a resistance level.
LS_TRAIL_ARM = Decimal("3.6")      # arm once price reaches this multiple
LS_TRAIL_DRAWDOWN = Decimal("0.25")  # then sell on this drawdown from the peak

# When the trailing rule went live (UTC epoch). Positions opened before this
# ran under the old fixed take-profit / hold-forever rules, so mixing them into
# the results would misreport how the current strategy is doing.
LS_TRAIL_SINCE = 1790010000.0      # 2026-09-21 17:00:00 UTC

# Sports the longshot engine will TRADE. Measured on 27 positions opened under
# the trailing rule: tennis +8% at the old settings, esports -46% and negative
# under every arm/drawdown combination tried (best case -23%). Setka Cup table
# tennis was 18 of 27 trades and dominated the losses.
# Everything is still RECORDED; this only limits what we buy.
LS_SPORTS = {"tennis", "football"}

# Per-sport rules. Anything a sport does not override falls back to the
# LS_BAND_*/LS_TRAIL_* values below, so tennis behaves exactly as before.
#
# Football is not a second copy of the tennis rule, it is close to its
# opposite, and the numbers behind that are:
#
#   spread at the entry price (25k-92k quotes, so this part is solid)
#       NFL   1-5c  bid/ask 83%  -> break-even 1.20x
#       NFL 10-20c  bid/ask 96%  -> break-even 1.04x   <- nearly frictionless
#       tennis 1-5c bid/ask 67%  -> break-even 1.50x
#
#   fair-odds test, P(ever reach Nx) against the martingale 1/N
#       NFL 10-20c  P(2x) 47% (fair 50)   P(3x) 41% (fair 33)   +8
#       NFL   1-5c  P(2x) 16%             P(3x)  7%             awful
#       tennis 1-5c P(2x) 24%             P(3x) 17%             -16
#
#   momentum, P(3x | already doubled), fair = 67%
#       NFL 10-20c  88%   <- once an NFL longshot doubles it keeps going
#       tennis 1-5c 73%
#
# So: enter higher (10-20c, where the odds are fair and the book is tight),
# and give it more room (35% rather than 25%), because a price that trends
# this strongly would be stopped out early by the tennis drawdown.
#
# WARNING: this rests on ONE weekend and 14 NFL moneyline markets. The direct
# backtest of it is 13 trades at -6%, t=-0.16, which confirms nothing. The
# justification is the structural measurements above, which have 100-90,000
# observations, plus a mechanism (scoring drives compound). Revisit after
# three or four more NFL weeks before believing any of it.
LS_SPORT_RULES = {
    "football": {"band_lo": Decimal("0.10"), "band_hi": Decimal("0.20"),
                 "arm": Decimal("2.0"), "drawdown": Decimal("0.35")},
}


def rules_for(sport):
    """Entry band and trailing stop for a sport, falling back to the globals."""
    r = LS_SPORT_RULES.get(sport) or {}
    return (r.get("band_lo", LS_BAND_LO), r.get("band_hi", LS_BAND_HI),
            r.get("arm", LS_TRAIL_ARM), r.get("drawdown", LS_TRAIL_DRAWDOWN))
# Seconds of a silent book before we ask the API what happened. This used to
# be 600, from when absence-of-feed was the only evidence and guessing wrong
# booked a live position as a loss. It is no longer a guess: _settlement_of()
# returns a value only for a market the exchange reports as settled, so asking
# early costs a request and nothing else. A match that ends now clears in a
# few minutes instead of thirteen.
LS_SETTLE_AFTER = 120
SETTLE_INTERVAL = 30               # how often to run the sweep, not just at discovery
SETTLE_RECHECK = 60                # per-position backoff between lookups
STARTUP_GRACE = 120                # quiet period after launch before settling anything

# Discovery keeps two bars. RECORD_STARTED_UNKNOWN subscribes to a game that
# has started and is quoting two sides even when its period is blank or
# unrecognised, so the tape is complete; is_live() still gates what we BUY.
# A sweep audited on 2026-09-21 returned 270 events, kept 27 and dropped 243 -
# among them 16 that had started with open two-sided markets, including an ITF
# mens tennis match and two esports games marked "Live" and "Map 1".
# Recording them costs nothing. Trading them would mean betting on games whose
# state we cannot read, which is how we bought a finished match at 3%.
RECORD_STARTED_UNKNOWN = True

# How often to re-check which games are actually running. The full discovery
# sweep is every DISCOVERY_INTERVAL (180s), and that gap is exactly where we
# bought a decided match: utr-puilav-smirac was already final when we opened
# at 3%, and settled against us for the whole stake. This poll reads only
# live/ended and costs the same few requests as a sweep.
LIVENESS_INTERVAL = 30
# If liveness cannot be refreshed for this long, stop opening rather than
# trade on a stale picture. Managing existing positions continues regardless.
LIVENESS_MAX_AGE = 600

# --- shadow stop-loss (measurement only; does NOT close anything) ------------
# Losers are not as binary as they look: replayed against the tape, holding a
# tennis loser to settlement averaged -0.88 while a -40% stop recovered it to
# -0.55, about $0.33 a loser. The catch is the tail. One winner in that sample
# was worth $19, so saving $0.33 on 25 losers ($8.25) only pays if the stop
# also spares the winner more than 57% of the time.
#
# A stop on the raw bid cannot do that: we buy the ask and can only sell the
# bid, and at these prices the bid is a median 50% of the ask (25th pct: 20%).
# A -20% and a -40% stop fired on the SAME 23 of 26 positions - both trigger at
# entry, on the spread, not on any price move. Waiting out that opening gap
# turned -$2.97 into -$0.04, but that was the best of 11 variants over 26
# positions holding a single winner, which is not evidence of anything.
#
# So it is recorded alongside the take-profit ladder rather than traded, and
# gets revisited at 100+ trades.
# Do not open during a tiebreak. This is the one match-state cut the data
# supports outright: 0 of 5 such entries ever ran, and it costs ~5% of volume.
# Entering by SET was tempting - set 1 returned -31% against set 2's -72% -
# but that is the best of four buckets over ~100 entries, and set 2 still
# reaches 3x 13% of the time. Every position now records the set it was
# opened in, so that call can be made on real numbers later instead.
LS_SKIP_TIEBREAK = True

LS_STOP_GRACE = 300                # seconds before a shadow stop can arm
LS_STOP_LADDER = (Decimal("0.40"), Decimal("0.60"))

# A position is only abandoned after its market is missing from this many
# CONSECUTIVE sweeps. After a restart the subscription set starts empty, so a
# single-sweep test marked live positions as dead: one restart closed five at
# -$1.00 each while they sat at 1.4x-2.5x.
REAP_GRACE_SWEEPS = 3
REAP_GRACE_SECS = 900              # and never before this long off-feed
LS_SETTLE_PER_SWEEP = 8            # settlement calls per sweep (rate limit)

# Exit rules evaluated in parallel on the SAME positions. Because a take-profit
# rule only ever fires when the price reaches its multiple, "did the peak reach
# Mx" plus the settlement value is enough to score every rule exactly - no need
# to run separate engines.
LS_TP_LADDER = (2, 3, 5, 10, 20)

# --- market universe ---------------------------------------------------------
TENNIS = {16: "ATP", 17: "WTA", 55: "ITFM", 56: "ITFW", 79: "ITFME", 80: "ITFWO",
          156: "ATPCQ", 230: "TSL", 266: "ATPDB", 275: "WTADB", 321: "CMPCUP", 330: "UTR"}

SOCCER = {10: "MLS", 11: "EPL", 12: "UCL", 18: "BUN", 19: "SEA", 20: "LAL", 73: "GENESCOT",
          74: "ISCOCHAM", 77: "CHAM", 78: "CORAPUNT", 111: "LMX", 113: "BRA", 114: "BRB",
          115: "KL1", 116: "ELS", 117: "CSL", 118: "LPA", 119: "PL1", 120: "SLR", 121: "LCO",
          122: "FLC", 123: "RPL", 125: "NLS", 126: "SLD", 127: "LNG", 128: "LPC", 129: "SCP",
          130: "UEL", 131: "UECL", 132: "SUD", 133: "AUC", 134: "OFB", 135: "NWSL", 136: "UWCL",
          158: "SWSL", 164: "USLC", 165: "CLBF", 166: "ALSV", 167: "NOR1", 168: "ECU1",
          169: "DEN1", 170: "IRLP", 171: "IRL1", 172: "VKL", 173: "YKK", 174: "UZB1",
          175: "SWE2", 176: "SWCL", 177: "URU1", 178: "LEXP", 179: "ARG2", 180: "ISL1",
          181: "PAR2", 182: "BTLA", 206: "CDB", 209: "MLSAS", 213: "LGSCUP", 215: "ICAA",
          217: "COP", 219: "LIG2", 220: "LIB", 223: "LIGPOR", 224: "EFLC", 228: "SPL",
          229: "EFLCH", 232: "LAL2", 233: "DFB", 235: "SRB", 238: "TDP", 241: "WSL",
          242: "CSHIELD", 243: "TDC", 244: "DFLSC", 274: "UAEPL", 300: "EGPL", 301: "ERE",
          308: "NB1", 309: "ACLE", 312: "ETPL", 313: "USOC"}

ESPORTS = {24: "VALETEXA", 32: "CS2", 33: "LOL", 34: "COD", 63: "VALORANT", 64: "DOTA2",
           103: "ITTF", 104: "TTELITE", 105: "CZECHLIGAPRO", 106: "RUSSIALIGAPRO",
           107: "TTCUP", 108: "SETKACUP", 109: "TTCHALLENGER", 110: "WINCUP",
           149: "SETKAMEUA", 150: "SETKAMEMD", 151: "SETKAMECZ", 152: "SETKAWOUA",
           153: "RL", 154: "OW", 155: "R6", 212: "WTT", 346: "EBATTLESEFIA",
           347: "EBATTLESEFIB", 348: "EBATTLESEFCWC", 349: "EBATTLESEFWCA",
           350: "EBATTLESEFWCB", 351: "EBATTLESEFPL", 352: "EBATTLESEFSA"}

# Major US/international sports. Basketball and baseball matter most here:
# continuous scoring means many in-play price swings per game, and the books
# are far deeper than the South American soccer that dominated early data.
MAJOR = {2: "NFL", 4: "NBA", 6: "NHL", 7: "CBB", 8: "UFC", 15: "MLB", 21: "WCBB",
         30: "IPL", 49: "WNBA", 75: "BOXING", 81: "KBO", 82: "NPB", 86: "MLC",
         87: "T20BLAST", 89: "T20WORLDCUPW", 90: "T20I", 91: "T20IW", 92: "CPL",
         98: "FIBAWCQ", 100: "NBASL", 102: "CPBL", 124: "PDC", 185: "HUNDRED",
         210: "PDCDARTS", 214: "MLP", 225: "CFB", 254: "MODUS", 282: "COUNTY",
         283: "DWCS", 307: "FIBAWWC", 319: "NCAAMS", 320: "NCAAWS", 329: "NBL",
         331: "BBL", 336: "LNBP", 337: "BCL", 338: "KHL", 340: "EUROLG",
         343: "PLL", 344: "PPA", 345: "ACB"}

SERIES = {**TENNIS, **SOCCER, **ESPORTS, **MAJOR}

# Which bucket a league belongs to, for analysis grouping.
BASKETBALL = {"NBA", "CBB", "WCBB", "WNBA", "NBASL", "NBL", "ACB", "LNBP",
              "BCL", "EUROLG", "FIBAWCQ", "FIBAWWC", "NCAAMS", "NCAAWS"}
BASEBALL = {"MLB", "KBO", "NPB", "CPBL"}
CRICKET = {"IPL", "MLC", "T20BLAST", "T20I", "T20IW", "CPL", "BBL", "COUNTY",
           "HUNDRED", "T20WORLDCUPW"}

def _sport(code):
    if code in BASKETBALL:
        return "basketball"
    if code in BASEBALL:
        return "baseball"
    if code in CRICKET:
        return "cricket"
    return {"NFL": "football", "CFB": "football", "NHL": "hockey", "KHL": "hockey",
            "UFC": "fighting", "BOXING": "fighting", "DWCS": "fighting",
            "PDC": "darts", "PDCDARTS": "darts", "MODUS": "darts",
            "MLP": "pickleball", "PPA": "pickleball", "PLL": "lacrosse"}.get(code, "other")

# Leagues now come from the event's seriesSlug (nfl-2025 -> NFL), which does
# not always match the hardcoded codes: "itf-womens" produced ITF-WOMENS where
# the table had ITFWO, so its rows fell through to "other". Match on a
# normalised key and fall back to keyword rules.
_KEYWORDS = (
    (("itf", "atp", "wta", "tennis", "utr", "challenger"), "tennis"),
    (("setka", "ttcup", "ttelite", "ligapro", "ittf", "wtt", "tabletennis"), "esports"),
    (("nfl", "cfb", "ncaaf"), "football"),
    (("nba", "wnba", "cbb", "ncaab", "euroleag", "basket", "nbl", "acb", "bcl"), "basketball"),
    (("mlb", "kbo", "npb", "cpbl", "baseball"), "baseball"),
    (("nhl", "khl", "hockey"), "hockey"),
    (("ipl", "t20", "odi", "cricket", "hundred", "bbl"), "cricket"),
    (("ufc", "boxing", "dwcs", "mma"), "fighting"),
    (("pdc", "darts", "modus"), "darts"),
    (("epl", "uefa", "ucl", "uel", "liga", "serie", "bundes", "mls", "soccer",
      "fc", "cup"), "soccer"),
)


def _norm(name: str) -> str:
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def sport_of(league: str) -> str:
    """Sport for a league label, tolerant of slug spelling differences."""
    if not league:
        return "other"
    direct = SPORT_OF.get(league) or SPORT_OF.get(league.upper())
    if direct:
        return direct
    n = _norm(league)
    for code, sport in _NORM_SPORT.items():
        if n == code:
            return sport
    for keys, sport in _KEYWORDS:
        if any(k in n for k in keys):
            return sport
    return "other"


SPORT_OF = {**{v: "tennis" for v in TENNIS.values()},
            **{v: "soccer" for v in SOCCER.values()},
            **{v: "esports" for v in ESPORTS.values()},
            **{v: _sport(v) for v in MAJOR.values()}}

_NORM_SPORT = {_norm(k): v for k, v in SPORT_OF.items()}

# Periods meaning "no live trading" - not started, or finished.
import re as _re

# Deciding "is this match live" by blocklist kept failing: CAN, PRE and POST
# all slipped through and we subscribed to dead markets. Allowlist instead -
# a live period is a clock reading or a known in-play marker, anything else
# (NS, FT, PRE, POST, CAN, ABD, ...) is not tradeable.
_LIVE_PATTERNS = (
    r"^\d{1,3}\+?\d*'?$",      # 45, 45', 90+5, 45+1'
    r"^(1H|2H|HT)$",            # halves
    r"^S\d$",                   # tennis/volleyball sets
    r"^TB\d$",                  # tiebreaks
    r"^Q\d$",                   # basketball quarters
    r"^P\d$",                   # hockey periods
    # Baseball innings arrive as IN2/IN3 (NPB), and can be TOP/BOT prefixed.
    r"^(IN|INN)\d{1,2}$",
    r"^(TOP|BOT)\s?\d{1,2}$",
    r"^(OT|ET|SO|BRK|BREAK|T\d+|B\d+|EX\d*)$",
    # Found by auditing what discovery was throwing away: esports report
    # "Map 1" / "Game 2" and some feeds just say "Live" or "In Play". The
    # allowlist silently dropped every one of them.
    r"^(LIVE|IN[\s_-]?PLAY|IN[\s_-]?PROGRESS|PLAYING|STARTED)$",
    r"^(MAP|GAME|SET|ROUND|RD|FRAME|END|LEG)\s?\d{1,2}$",
)
_LIVE_RE = _re.compile("|".join(_LIVE_PATTERNS), _re.I)

# Periods that definitely do NOT mean in-play. Anything that is neither these
# nor _LIVE_RE is unrecognised, and gets logged rather than silently dropped -
# that is how "Live" and "Map 1" went unnoticed in the first place.
_DEAD_PATTERNS = (
    r"^(NS|TBD|POSTP|POSTPONED|CANC|CAN|CANCELLED|ABD|ABANDONED|SUSP|INT)$",
    r"^(FT|AET|PEN|AP|FINAL|ENDED|AOT|AWARDED|WO|WALKOVER|RET|RETIRED)$",
)
_DEAD_RE = _re.compile("|".join(_DEAD_PATTERNS), _re.I)


def is_live(period) -> bool:
    """True only for periods that mean the match is actually in play.

    This gates TRADING. An unrecognised or missing period is not in-play as
    far as this is concerned - we do not bet on a game whose state we cannot
    read.
    """
    if not period:
        return False
    return bool(_LIVE_RE.match(str(period).strip()))


def is_tiebreak(period) -> bool:
    """A tiebreak is a handful of points from the end of a set.

    Measured on the tape: 0 of 5 longshots entered during one ever reached
    even 2x, against 29% for a set-1 entry. There is almost no match left for
    a comeback, so we record these but do not buy them.
    """
    return str(period or "").strip().upper().startswith("TB")


def period_class(period) -> str:
    """'live', 'dead' or 'unknown' - for deciding what to record and to warn
    about periods no pattern recognises."""
    p = str(period or "").strip()
    if not p:
        return "unknown"
    if _LIVE_RE.match(p):
        return "live"
    if _DEAD_RE.match(p):
        return "dead"
    return "unknown"


# --- rate limiting -----------------------------------------------------------
# We were banned (Cloudflare 1015) at 5 requests in 5 seconds. Stay well under.
REQUEST_SPACING = 1.5        # seconds between REST calls
LOOKBACK_HOURS = 8       # games already under way
LOOKAHEAD_HOURS = 6      # about to start
MAX_DISCOVERY_PAGES = 8  # 100 events per page

DISCOVERY_INTERVAL = 180     # seconds between full sweeps (121 series x 1.5s = ~3min)
RATE_LIMIT_BACKOFF = 120     # seconds to pause after a 429
MAX_BACKOFF = 900

# --- trading params ----------------------------------------------------------
TICK = Decimal("0.01")          # default; per-market tick overrides it

# Tick size is NOT uniform: NFL markets quote in 0.005, half the 0.01 used by
# soccer/tennis/basketball. Treating them alike made our stop and target twice
# as wide on NFL, and overstated the spread cost there by 2x.
DEFAULT_TICK = Decimal("0.01")
STAKE = Decimal("3.00")
EXIT_TICKS = 2
MAX_POSITIONS = 5
MAX_HOLD_SECS = 3600

# Stop loss, in ticks below entry. Chosen from stoploss.py, not guessed:
# P(reach target | already down D ticks) vs the break-even D/(D+2) needed to
# justify holding. At D=1 (0.65 vs 0.33) and D=2 (0.63 vs 0.50) holding still
# pays; at D=3 they cross (0.60 vs 0.60) and beyond it holding is clearly
# negative. A tighter stop would cut trades that statistically recover.
STOP_TICKS = 3

# Trade everything we have not measured; exclude only on evidence.
# Soccer is excluded because its gated EV is -0.33 ticks/trade against tennis
# +0.15, driven by book depth (median 1,289 vs 27,720) rather than gap
# frequency (~10% in both). Everything else stays in until its own data says
# otherwise. Set to None to trade all sports.
# After a stop-out, do not re-enter the same market/side until this elapses.
# Observed failure: a trending market produced 7 consecutive stopped shorts on
# one WNBA book (0.23 -> 0.20 -> 0.17 -> 0.14 -> 0.11 -> 0.07 -> 0.04), each
# re-entered instantly after the previous stop.
REENTRY_COOLDOWN_SECS = 600

# Reject entries when the price has net-drifted against us over the window.
# The oscillation gate alone passes a staircase downtrend, since each small
# bounce counts as a direction reversal.
MAX_ADVERSE_DRIFT_TICKS = 2

TRADE_SPORTS = {"tennis", "basketball", "baseball", "football", "hockey",
                "esports", "cricket", "darts", "fighting", "pickleball",
                "lacrosse", "other"}

# Entry band is deliberately NOT 1-5c: a fixed 1c tick costs 50% of stake at 2c.
# See signals.MAX_SPREAD_COST_PCT - this band is a coarse prefilter only.
BAND_LO = Decimal("0.05")
BAND_HI = Decimal("0.35")

# --- storage -----------------------------------------------------------------
TAPE_PREFIX = "tape"
DEDUP = True                 # skip writes when the book state is unchanged (~55% of msgs)
BOOK_LEVELS = 10             # depth levels to persist per side
SCORECARD_INTERVAL = 900     # seconds between scorecard dumps

# Watchdog: if subscribed and no websocket message for this long, the socket is
# considered dead and a reconnect is forced. Observed failure: after a laptop
# sleep the connection stops delivering without raising or firing close.
STALE_SECS = 180

# A position unobserved for longer than this cannot have its exit modelled
# honestly (we missed the price path), so its P&L is marked suspect.
MAX_GAP_SECS = 120

# The server caps subscriptions per websocket connection (observed: 164 markets
# x 2 subscriptions = 328 refused, 161 x 2 = 322 accepted). Each market costs
# two subscriptions (market data + trades), so shard across connections well
# under the limit.
# The server's per-connection limit appears to count subscription REQUESTS
# cumulatively rather than live markets: after ~48 incremental subscribe calls
# across 3 connections we hit "max subscriptions per connection reached" while
# holding only 344 markets. Keep each connection well under, and open a fresh
# one when one is refused.
# The server's per-connection limit counts subscription REQUESTS over the life
# of the socket, not live markets, so it bound at 44-64 markets on 2026-09-21
# depending on how much that connection had churned. Set below the observed
# floor so a fresh socket is opened BEFORE the server refuses, instead of
# relying on the error path every time. Capacity is MARKETS_PER_CONN x
# MAX_CONNECTIONS, which must stay above MAX_TOTAL_MARKETS.
MARKETS_PER_CONN = 40
MAX_CONNECTIONS = 40          # 40 x 40 = 1600, comfortably over MAX_TOTAL_MARKETS

# Only subscribe to markets that actually quote two-sided. Overnight we
# discovered 1,078 markets, saturated 12 connections and dropped 1,957 - most
# of them dead exact-score props that never quote. Filtering at discovery
# keeps the connection budget for markets we could actually trade.
REQUIRE_TWO_SIDED = True
META_MAX = 8000                    # league map entries kept (never prune a live one)
MAX_TOTAL_MARKETS = 1200

# One NFL game carries hundreds of prop markets - a single Sunday produced
# 4,486, which would consume the whole budget and crowd out every other sport.
# Cap per game and prefer the liquid market types.
MARKETS_PER_GAME = 12
MARKET_PRIORITY = ("aec-",)      # moneyline first; everything else after


# Used when recovering a damaged tape whose member lost its header row.
BOOK_HEADER_FALLBACK = ("ts", "league", "sport", "event", "market", "period",
                        "bid", "ask", "mid", "spread", "short_px", "bid_depth",
                        "ask_depth", "bid_total", "ask_total", "imbalance",
                        "state", "bid_levels", "ask_levels")
