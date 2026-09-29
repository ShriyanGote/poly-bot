# Real-money maker test ($20 cap)

## Why

The paper maker credits itself a fill whenever a real trade prints at or
through its price. That can't tell us:

- **Queue position.** When a trade prints at our price, were we first in line,
  or did someone already resting there take it?
- **Latency.** How long an order takes to land, and how stale the book is by
  the time it does.
- **Adverse selection.** Whether the people who actually trade with a resting
  order are the ones who know something.
- **Our own effect.** Whether our order changes what others do. The paper
  maker is invisible; a real order is not.

The only way to find out is with real orders, at a size where the answer
costs next to nothing.

## What it does

`bot/realmaker.py` shadows the paper maker in a few markets at once.

| | |
|---|---|
| Rule | Same as paper: quote 1 tick inside the touch, only when the spread is ≥ 6 ticks |
| Markets | Up to 3 at once, tennis and esports, only where the paper maker is already quoting |
| Size | 5 shares per quote (the venue takes whole shares); at most 10 shares held per market, either direction |
| Order type | Limit, good-till-cancel, **post-only** (`participateDontInitiate`), so it can never take liquidity |
| Requotes | Cancel, then place again, at most once a second per market and 4 order actions a second overall |

Paper keeps running beside it in the same markets. The page compares the two
over exactly the window the real engine was in each market.

### The book without us

Once our bid lands, it is the best bid in the feed. If either maker quoted off
that, it would step a tick above its own order again and again. So both makers
now read the book with our resting orders subtracted (`RealMaker.ex_self`).
The recorded tape still holds the true book.

### Accounting

Everything is kept in long-outcome terms, the way the venue prices orders.
Buying the short side at 0.30 is recorded as selling the long at 0.70. A
market's P&L is `cash + n × outcome`, the same formula the paper maker uses,
so the two compare directly. The unit tests check a short opened at 0.60 and
bought back at 0.52 for 5 shares: +$0.40.

## The $20 cap

Before any order that opens or adds to a position goes out, it computes the
**worst case across all markets**. That assumes every resting opening order
fills and every match settles against us:

```
worst(market) = −min over {which orders fill} × {settles 0, settles 1} of (cash + n × outcome)
total worst ≤ REALMM_BUDGET ($20)
```

- Orders that reduce a position are always allowed; they can only lower the
  worst case.
- If an order's reply is lost, the order is still counted as resting until the
  venue says otherwise.
- The $20 is the most that can be **lost**. The money tied up at any moment is
  about the same, because on this venue a position's cost equals its worst
  case.

## Kill switches

- **Master switch.** It's off unless `REALMM_ENABLED=1` is set in the server's
  `.env`. Remove the line and restart to turn it off.
- **Order stream drops.** It relies on the private order stream to see fills.
  If the stream disconnects, every resting order is cancelled and nothing new
  is placed until the stream is back.
- **Venue disagrees with us.** Every 30 seconds it compares its records with
  the venue's open orders and positions. Orders it doesn't recognize in its
  markets are cancelled. If the venue's position differs from ours twice in a
  row, it **halts**: it cancels everything and places nothing new.
- **Too many errors.** 10 order errors within 5 minutes also halts it.
- **Clearing a halt.** A halt lasts through restarts. To clear it, delete
  `"halted"` from `data/realmm_state.json`, after checking why it happened.
- **Startup and shutdown.** Every resting order in its markets is cancelled on
  startup and on shutdown.
- **What stays open after a halt.** Positions still held when it halts or a
  match ends are left to settle. They are already inside the $20.

## What it measures

Every event is logged to `data/realmm-<sport>-<date>.csv.gz`. Each row has a
`kind` and the fields that event fills in:

| kind | what it records |
|---|---|
| `order` | order acknowledged: price, quantity, intent, the book (without us), **send→ack ms**, and **book update→ack ms** (our whole reaction time) |
| `fill` | our fill: price, quantity, book and spread at the time, venue `transactTime`, **how late we heard** |
| `print` | a public trade at or through our resting price, which paper counts as a fill, and **how much of it actually filled us** |
| `rejected` | post-only rejections: the book moved into our price before the order landed |
| `rate_limited`, `halt`, `start` | what they say |

The status page summary (`realmm` in `status.json`) has:

- **Latency:** p50 and p90 for order ack, cancel, book→ack, and fill-heard
  delay.
- **Print hit rate:** real fills divided by the prints paper would have
  counted. This is the key number. Paper assumes 100%. If it comes back at
  20%, the paper P&L is roughly 5x too high on the spread side.
- **Real vs paper per market:** fills, shares and marked P&L over the same
  window.
- **Totals:** settled P&L and current worst-case exposure against the $20.

## What would change the plan

- **Print hit rate near 100% and spreads held at fill:** paper is a fair model.
  Scale up slowly.
- **Low hit rate:** someone is queued ahead of us at the same price. The edge
  is smaller than paper says, by about the hit rate.
- **Many post-only rejections, or book→ack in the hundreds of ms:** we're too
  slow for these books.
- **Fills cluster right before the price moves against us:** that's adverse
  selection, and paper can't see it. Check the mid 30–60 seconds after each
  `fill` row against the tape.

## Status

- **Done:** the engine (`bot/realmaker.py`), config (`bot/config.py`, off by
  default), the event log (`bot/storage.py`), and offline tests of the budget
  math, the book filter, sizing and accounting.
- **Not done:** connecting it to `bot/recorder.py`. That means passing the book
  without our orders to both makers, sending public trades to it, stopping it
  when markets end, adding its summary to the status file, and calling
  `start()` in `run()`. Also a status-page section. This connection is the step
  that lets it place real orders, so it waits for an explicit go-ahead.
- **Worth doing before turning it on:** a **separate API key**. The existing
  key is already shared by discovery and settlement lookups and hits the rate
  limit, and order traffic shouldn't compete with them.
