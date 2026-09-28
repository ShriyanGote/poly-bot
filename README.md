# Polymarket US in-play data collection

Records live order books for tennis and soccer, and paper-trades a
longshot-scalp strategy against them. **No real orders are ever placed.**

## Layout

    bot/config.py      series universe, thresholds, rate-limit settings
    bot/discovery.py   paced REST sweep for in-play markets (429-aware)
    bot/storage.py     dedup + daily-rotated gzip tape
    bot/signals.py     entry gates and EV math
    bot/paper.py       paper trading engine (maker-side fills)
    bot/recorder.py    websocket ingest + orchestration, auto-reconnect
    run.py             entrypoint
    analyze.py         offline analysis of collected tape
    supervise.sh       multi-day supervisor (restart + caffeinate)

## Running

    ./supervise.sh 72          # 3-day collection run
    .venv/bin/python run.py --status
    .venv/bin/python analyze.py

Data lands in `data/tape-YYYY-MM-DD.csv.gz`, logs in `logs/`.

## Tennis shadow experiment

The paper-only shadow ledger records the first eligible 1-5c moneyline quote
for each tennis market/side, including price, spread, displayed depth, recent
activity, score, period, and rolling 120-second mid-price volatility. It
simulates a fixed 5x arm / 20% trailing exit and tracks the eventual peak and
settlement. It never submits orders. The volatility cutoff and exploratory
league exclusions are frozen in `research/shadow_report.py`; run
`.venv/bin/python research/shadow_report.py` for overall and daily results.
Rankings and tournament round are not currently present in the recorder's
signal metadata, so they cannot be tested from this ledger.

## Why the entry band is 0.05-0.35, not 0.01-0.05

The tick is a fixed 1 cent, so one tick of spread costs `0.01/price` of your
stake: 50% at 2c, 10% at 10c, 3% at 30c. Under a driftless price,
P(reach target before zero) = entry/target, which makes gross EV exactly zero
at every price. So the band cannot create edge - only transaction cost
differs, and cheap prices are the expensive ones. Entries are placed as
resting bids (maker, zero commission) rather than crossing the spread.

## Rate limits

gateway.polymarket.us sits behind Cloudflare and returns error 1015 (temporary
IP ban) aggressively - observed at 5 requests in 5 seconds. Discovery paces
requests 1.5s apart and backs off on 429. Price data comes over the websocket,
which is push-based and not rate limited.
