# Polymarket longshot bot - handover (2026-09-21, ~21:30 UTC)

## What this is

Two paper-trading engines run off one WebSocket recorder:

- **longshot** (`bot/longshot.py`) - the live strategy. Buys either side of a
  sports market when it prices at 1-5%, holds for a multiple. Tennis only.
- **paper** (`bot/paper.py`) - the original scalper, kept as a known-losing
  control at about -$830. Do not tune it; it exists for comparison.

Everything is paper money. No real orders are placed.

## Run / inspect

    ./restart.sh            # stop cleanly, restart under supervisor (72h)
    ./snapshot.sh           # ALWAYS before touching state or restarting
    ./ls.py [--all] [--open] [--sport tennis] [--watch]
    ./lsstop.py [sport]     # replay closed positions vs stop-loss variants
    tail -f logs/run-$(date +%F).log

`data/longshot_state.json` is written by the running recorder. **Never edit it
while the recorder is up** - it gets overwritten on shutdown. Two tapes were
destroyed that way earlier in the project.

## Where the strategy stands

Roughly 24 closed tennis trades under the current rule, hovering near
break-even, and **one 16.3x trade is more than the whole profit**. t-statistic
was +0.49; about 233 trades are needed for significance. There is no
demonstrated edge. Treat any positive ROI you see as noise until the trade
count is far higher.

**Rules are now per-sport** (`LS_SPORT_RULES`, with the globals as fallback):

    tennis    buy 1-5%, but only after the price dips to <=0.04 and then
              HOLDS at >=0.05 for 60s. Arm the trail at 3.6x, exit on a 25%
              drawdown. Skip tiebreaks.
    football  buy 10-20% on first touch. Arm at 2.0x, exit on 35%.
    both      moneyline (aec-) markets only.

Football is close to the opposite of tennis on purpose: NFL books at 10-20c
break even at 1.04x against tennis's 1.50x at 1-5c, price at 10-20c in NFL is
at or above fair odds, and once an NFL longshot doubles it triples 88% of the
time against a fair 67%. It is based on ONE weekend and 14 markets; the direct
backtest is 13 trades at -6%. Revisit after three or four more NFL weeks.

Old settings (still the fallback for any sport without an override):

    LS_BAND_LO/HI     0.01 / 0.05
    LS_STAKE          $1.00
    LS_TRAIL_ARM      3.6x        (3.6 not 4.0: a 4x arm on a 0.05 entry lands
                                   on 0.20, a round number where thin books stall)
    LS_TRAIL_DRAWDOWN 25%
    LS_SPORTS         {"tennis"}
    LS_TRAIL_SINCE    1790010000.0  (2026-09-21 17:00 UTC; gates the closed-trade
                                     sample only, NOT the open list)

## Changed today, all deployed and tested

1. **Settlement no longer waits for a reconnect.** `settle_gone()` judged
   positions by membership in `self.subscribed`, which only ever grows - so a
   finished match still counted as live and decided positions sat open until
   the next restart. Now it judges on data staleness, and falls back to
   `markets.bbo()['marketData']['settlementPx']` when `markets.settlement()`
   404s. This was hiding losses: winners exit fast via the trail, losers can
   only exit at settlement, so the open list filled with losers-in-waiting.

2. **Shadow stop-loss.** `_mark_stops()` records where a stop WOULD have sold
   (`LS_STOP_LADDER` 40%/60%, after `LS_STOP_GRACE` 300s) without selling
   anything. Scored into each closed record's `variants`. Measurement only.

3. **Dead-market entry filter.** `Longshot.set_live()` is fed the discovery
   result each sweep; entries are refused for markets discovery has dropped.
   Existing positions are still managed. Counter in the heartbeat.

4. **Period allowlist widened.** `Live`, `In Play`, `Map N`, `Game N`,
   `Round N`, `Frame N` were being silently dropped by `_LIVE_PATTERNS`.
   Unrecognised periods now log once each (`PERIOD unrecognised ...`) so a new
   marker can never go unnoticed again - that is how this went undetected.

5. **The API's own game state is now used.** `Discovery._state_class()` reads
   `ended` / `live` (top-level or nested under `eventState`) in preference to
   the period string. `ended: true` beats any period value. Tennis populates
   these, including UTR and ITF, along with a live `score`. We had been
   ignoring all of it and inferring from `period` alone.

6. **Entry-time liveness check.** `Discovery.liveness()` polls every
   `LIVENESS_INTERVAL` (30s) between the 180s sweeps and feeds the live set to
   the longshot. `on_book` is the WebSocket hot path, so the check cannot
   block there - the poll runs off-thread via `asyncio.to_thread` and the
   engine reads a cached set. Verified live: 2917 markets mapped, 49 live,
   3.5s per poll, ~0.1 req/sec. Also a staleness guard: if liveness cannot be
   refreshed for `LIVENESS_MAX_AGE` (600s) the engine stops OPENING rather
   than trade blind. Existing positions are managed throughout.

7. **Scalper marks stranded positions at settlement.** `paper.reap()` takes a
   `settle` callable (the longshot's `_settlement_of`) and books a finished
   market at its real outcome instead of the last quote it happened to see.
   Its -$840 was understated before this.

8. **Subscription cap is now actionable.** The per-connection cap is reported
   as an ASYNC error frame after `subscribe_market_data` returns successfully,
   so the try/except never fired: markets were marked subscribed and then
   streamed nothing. 422 of these today. `_on_ws_error()` now retires the
   connection and un-marks those slugs so the next sweep resubscribes on a
   fresh socket. **This is probably the main reason 472 of 659 games in a 24h
   window had no data - verify that it improves.**

## Known and NOT fixed

- **The finished-match window is narrowed to ~30s, not closed.** The exchange
  reports `MARKET_STATE_OPEN` for every tick we receive, so liveness has to
  come from `live`/`ended` on the event. Those are now polled every 30s. The
  residual risk is the API's own lag: `finishedTimestamp` for
  `utr-puilav-smirac` was 21:06:55, about 24 minutes after the UI showed
  "Final" at 20:43. The gate is as fast as the data allows, not as fast as
  reality.

- **The cost of this bug is NOT measurable from the data.** `finishedTimestamp`
  marks settlement, not the final point. Scored against it, zero of 148
  positions look like they were opened late - including the Puiac trade we
  watched happen. Do not trust that zero, and do not re-derive it.

- **Trail slippage.** Configured 25% drawdown realises **44% below peak**.
  Across 10 trail exits, $9.90 was given up to gapping, ~$0.99 each - a full
  stake. The bid jumps the floor rather than walking through it (one exit went
  0.20 -> 0.15 with nothing in between). Tuning the drawdown does not fix this.
  The asymmetry worth exploring: a resting limit sell fills on the way UP where
  buyers exist; a trail must cross a vanishing bid. The `tp` ladder in
  `variants` already models resting limits correctly.


## What the bugs actually cost

Close to nothing in P&L, and it is worth being precise about why:

- Settlement lag cost **$0** - positions settled at the same price either way.
  It distorted REPORTING: winners exit in seconds via the trail, losers can
  only exit at settlement, so at any snapshot the open list was loser-heavy
  and the closed P&L flattered the strategy. A figure of +$1.10 was really
  -$1.89 once the pending losses booked.
- The subscription cap and the allowlist gaps cost **data, not money**. And at
  -52% ROI, more coverage would most likely have meant more losses.
- Buying decided matches: **unmeasurable** (see above). One confirmed case,
  `utr-puilav-smirac`, -$0.99.

Across 148 positions matched to real outcomes: **-$76.79 on $147.87 staked,
-52% ROI, 16 winners.** That is the strategy, not the bugs. Fixing measurement
did not and will not make a negative edge positive.

## Checking back (the run started 2026-09-21 ~21:33 UTC, 72h deadline)

Rate is about 8 tennis trades/hour, so 100 trades lands roughly 8h in and 200
roughly 21h in. Nothing will notify you; the monitors only live inside a
session.

    ./ls.py --sport tennis          # current-strategy trades and open book
    ./lsstop.py tennis              # replay vs stop variants
    grep "up .*h |" logs/run-*.log | tail -1     # health + skipped counters

**Decide from the variant table, not from any single trade.** Each closed
position records what every rule would have returned on that same trade, so
they are directly comparable:

    hold / tp2x / tp3x / tp5x / tp10x / tp20x / stop40 / stop60   vs   ACTUAL

Criteria set in advance, deliberately, so a vivid trade cannot move them:

- **Switch the trail to a take-profit ladder** only if `tp3x` or `tp5x` beats
  the live trail across 100+ trades by more than the spread (median spread at
  these prices is roughly half the entry price, so the margin needs to be
  wide, not marginal).
- **Turn the shadow stop on** only if `stop40` beats `hold` across 100+
  trades. As of 8 trades it is behind (-$0.40 vs +$0.40) and fires on 6 of 8,
  which matches the replay: it mostly triggers on the spread, not on a move.
- **Do not move the entry band down.** Measured: 0.05 returns -35% with 17 of
  20 winners; 0.04 returns -100% with zero winners across 16 trades. Cheaper
  longshots are worse value, not better - favourite-longshot bias strengthens
  as price falls.
- **Do not conclude anything from ROI alone at any sample size below ~230
  trades.** The t-statistic was +0.49 at 19 trades and a single 16.3x trade
  has been more than the whole profit all day.

Two live observations worth checking against the accumulated data:

1. `aec-utr-hamzec-palluk` (2026-09-21 21:41) exited on the trail at 0.23 from
   a 0.31 peak for +$3.60. Two and a half minutes later the same side was
   0.56; holding was worth +$10.20. The trail's 25% drawdown is ordinary noise
   in a UTR book. `tp10x` would have sold at 0.50.
2. The trail fills about 19 points below its own trigger because the bid gaps
   rather than walking. Realised drawdown is 44% against 25% configured.

Both point the same way - toward resting limit sells over reacting to a move -
but one trade is one trade. Let the table decide.

## Tools

    ./ls.py [--all] [--open] [--sport tennis] [--watch]   the book, reads local files only
    ./sell.py <name|--all|--min-mult N> [-n]              sell by hand; -n dry run
    ./entryrules.py [--since HH:MM] [--trades]            replay entry rules on the tape
    ./lsstop.py [sport]                                   replay stop-loss variants

`entryrules.py` is the evaluator: it replays first-touch / bounce / bounce+hold
against recorded book data. Prefer adding an analysis there over adding one to
the live engine - the recorder has enough moving parts.

## Scoring a replay: the thing that made every comparison wrong

A replay has to decide what a position was worth when it never exited. Scoring
it at zero looked conservative and is catastrophically wrong: `staced-mcdcia`
entered short at 0.02, the match settled against the long side, and the real
payoff was **+$49 on a $1 stake**. The replay booked it as -$1.00. In a
strategy whose entire return lives in the tail, that assumption deletes the
only trades that matter, and it makes tighter entry rules look better than
they are because they take fewer of them.

Scoring on real settlements instead has its own trap. markets.settlement()
stops answering for older markets, and what it still answers for is not a
random sample: of 163 real tennis trades, the 24 it could resolve returned
+27% while the 139 it could not returned -42%. Scoring only the resolvable
ones turned a -38% strategy into +60%.

`data/settlements.json` now fixes this going forward. A market dropping out of
discovery is the signal it is over (events.list is called with closed=False,
so a finished match vanishes before its settlement publishes - keying off
`ended` in the live feed records almost nothing). Those markets are queued and
retried each sweep until the outcome publishes, then stored permanently.

It starts from 2026-09-22 05:00 UTC. Anything recorded before that cannot be
scored on real outcomes, so replays over the first four days keep the bias.

## Traps - read this before running any analysis

Four separate times today a result was wrong in the same direction, and the
error always flattered the strategy:

1. Reporting P&L while only winners had closed.
2. Filtering closed trades on `reason == "trail"`, which excludes every loser.
3. Excluding "suspect" gapped trades - winners exit in under a second, losers
   straddle restarts, so this removed losses selectively.
4. **Measuring how long a book kept moving AFTER entry.** This looked like a
   spectacular filter (31 frozen-book trades, 0 winners, -100%) and was pure
   lookahead: a book stops moving because the match ended. Measured backward
   from entry, which is what the engine can actually see, it does not
   discriminate at all. The +26% that came with it is not real.

Also: a flat spread assumption in cross-validation manufactured a +88% edge
that was really -23%, and 8.8s sample spacing against a 60s horizon inflated
t-statistics ~2.6x.

5. **Scoring unexited replay positions at zero**, which deletes the tail - see
   the section above. This one runs the OTHER way: it makes the strategy look
   worse, and makes selective entry rules look better than they are.

6. **A settled longshot pays 0 or 1.** Two fabricated settlements booked +$24
   and +$19 on 2-5c entries because bbo's `settlementPx` is a placeholder
   until the real result lands - 0.5 once, 0.0 the other time. It is now only
   believed when the last traded price agrees with it. A large payout on a
   tiny entry with a flat peak is a bug, not a win.

**Before believing any number, ask what it would look like if losers were
systematically missing, whether the measure uses information available at
decision time, and what happened to the positions that never exited.**

## Two pythons

The recorder runs under `.venv` (3.11). The CLI tools run under the system
python (3.9) via `#!/usr/bin/env python`. Anything in `bot/config.py` must
import on both - `Decimal | None` in a NamedTuple body broke every tool at
once while the recorder carried on fine.

## Established negative results - do not re-litigate

- Scalping is structurally negative; 112 stop/target combinations all lose
  after real spreads.
- Order flow predicts direction (AUC 0.55-0.65) but never beats the spread.
- Esports longshots: -46% under every parameter tried.
- Time-based bail-outs make things worse - exiting costs the spread.
- Gross EV of the martingale framing is exactly zero at every price:
  P(reach target before 0) = entry/target.

## Market structure that drives everything

At these prices the bid is a **median 50% of the ask** (25th percentile: 20%).
You are down ~50% on paper the instant you enter. A -20% and a -40% stop fired
on the identical 23 of 26 positions - both trigger at entry, on the spread, not
on any price move. Losers frequently die at the minimum tick (0.01) with no
intermediate print, which is why both shadow stop levels record the same fill.

A stop saves ~$0.33 per loser. One winner was worth $19. So a stop only pays if
it spares the winner more than 57% of the time.
