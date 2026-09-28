#!/usr/bin/env python3
"""Explore tennis longshot entry signals from trade state and quote tapes.

This is deliberately descriptive: tape-derived candidates are marked as such
and are not mixed into the live signal ledger. Tape rows are deduplicated quote
changes, so their recent-row activity is only a proxy for websocket activity.
"""

import argparse
import csv
import gzip
import json
import math
import re
import zlib
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

from bot import config


def wilson(successes, n, z=1.96):
    if not n:
        return "n/a"
    p = successes / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return f"{100 * p:.1f}% [{100 * (mid-half):.1f}, {100 * (mid+half):.1f}]"


def fnum(row, key, default=0.0):
    try:
        return float(row.get(key) or default)
    except (TypeError, ValueError):
        return default


def entry_price(row, side):
    return fnum(row, "ask") if side == "long" else 1 - fnum(row, "bid")


def exit_price(row, side):
    return fnum(row, "bid") if side == "long" else 1 - fnum(row, "ask")


def read_tape():
    """One first 1-5c candidate per market-side; keep its later best exit."""
    candidates = {}
    recent = defaultdict(deque)
    files = sorted(config.DATA.glob("tape-tennis-*.csv.gz"))
    for path in files:
        try:
            fh = gzip.open(path, "rt", newline="")
            rows = csv.DictReader(fh)
            for row in rows:
                slug = row.get("market") or ""
                if not slug.startswith("aec-"):
                    continue
                try:
                    ts = datetime.fromisoformat(row["ts"].replace("Z", "+00:00")).timestamp()
                except (ValueError, KeyError):
                    continue
                q = recent[slug]
                q.append(ts)
                while q and ts - q[0] > 60:
                    q.popleft()
                for side in ("long", "short"):
                    key = (slug, side)
                    px = entry_price(row, side)
                    ex = max(0.0, exit_price(row, side))
                    if key in candidates:
                        c = candidates[key]
                        if ex > c["peak_exit"]:
                            c["peak_exit"] = ex
                        c["last_ts"] = ts
                        continue
                    if not (0.01 <= px <= 0.05):
                        continue
                    depth = fnum(row, "ask_depth") if side == "long" else fnum(row, "bid_depth")
                    total = fnum(row, "ask_total") if side == "long" else fnum(row, "bid_total")
                    imb = fnum(row, "imbalance") * (1 if side == "long" else -1)
                    candidates[key] = {
                        "source": "tape_first_touch", "slug": slug,
                        "ts": ts, "last_ts": ts, "league": row.get("league", ""),
                        "side": side, "entry_px": px, "peak_exit": ex,
                        "peak_mult": ex / px if px else 0,
                        "spread": fnum(row, "spread"),
                        "spread_pct": fnum(row, "spread") / px if px else 0,
                        "entry_depth": depth, "same_side_total": total,
                        "directional_imbalance": imb,
                        "change_rows_last_60s": len(q),
                        "period": row.get("period", ""),
                        "score": row.get("score", ""),
                    }
        except (OSError, EOFError, gzip.BadGzipFile, zlib.error, csv.Error) as e:
            print(f"WARNING: could not fully read {path.name}: {e}")
        finally:
            try:
                fh.close()
            except UnboundLocalError:
                pass
    return list(candidates.values()), files


def summarize(label, rows):
    n = len(rows)
    ge2 = sum(r["peak_mult"] >= 2 for r in rows)
    ge5 = sum(r["peak_mult"] >= 5 for r in rows)
    never1 = sum(r["peak_mult"] <= 1.001 for r in rows)
    print(f"{label}: n={n}; peak <=1x {never1}; >=2x {ge2} ({wilson(ge2,n)}); "
          f">=5x {ge5} ({wilson(ge5,n)})")


def actual_trades():
    state = json.loads(config.LONGSHOT_STATE.read_text())
    rows = [x for x in state.get("closed", [])
            if x.get("sport") == "tennis"
            and x.get("entry_rule") == config.rule_label("tennis")]
    cooked = []
    for x in rows:
        ep = float(x["entry_px"])
        peak = float(x.get("peak_px", ep)) / ep if ep else 0
        pnl = float(x["pnl"])
        stake = ep * int(x["qty"])
        cooked.append((x, peak, pnl, stake))
    wins = sum(pnl > 0 for _, _, pnl, _ in cooked)
    pnl = sum(x[2] for x in cooked)
    stake = sum(x[3] for x in cooked)
    print("\nACTUAL CURRENT-RULE CLOSED TENNIS TRADES")
    print(f"n={len(cooked)}; wins={wins}; P&L=${pnl:.2f}; staked=${stake:.2f}; "
          f"ROI={100*pnl/stake:.1f}%" if stake else f"n={len(cooked)}")
    for name, pred in (("peak <=1x", lambda m: m <= 1.001),
                       ("1-2x", lambda m: 1.001 < m < 2),
                       ("2-5x", lambda m: 2 <= m < 5),
                       ("5x+", lambda m: m >= 5)):
        grp = [(x,m,p,s) for x,m,p,s in cooked if pred(m)]
        gp = sum(y[2] for y in grp); gs = sum(y[3] for y in grp)
        gw = sum(y[2] > 0 for y in grp)
        print(f"  {name:10} n={len(grp):3} wins={gw:3} pnl=${gp:8.2f} "
              f"ROI={100*gp/gs:7.1f}%" if gs else f"  {name:10} n=0")

    print("\nCAN AN ENTRY FEATURE SEPARATE NEVER-1x FROM 2x+ PEAKS?")
    compare = [(x, m) for x, m, _, _ in cooked if m <= 1.001 or m >= 2]
    n_never = sum(m <= 1.001 for _, m in compare)
    n_two = sum(m >= 2 for _, m in compare)
    print(f"Contrast sample: {n_never} never above entry; {n_two} reached 2x+; "
          f"excludes {sum(1.001 < m < 2 for _,m,_,_ in cooked)} trades peaking 1-2x.")

    def oriented_score(x):
        """Parse current-game and completed-set lead from the recorded score."""
        raw = str(x.get("entry_score") or "")
        chunks = [c.strip() for c in raw.split(",") if c.strip()]
        pairs = []
        for c in chunks:
            m = re.match(r"\s*(\d+)\s*-\s*(\d+)", c)
            if m:
                pairs.append((int(m.group(1)), int(m.group(2))))
        if not pairs:
            return None, None
        side_sign = 1 if x.get("side") == "long" else -1
        game_diff = (pairs[-1][0] - pairs[-1][1]) * side_sign
        # All but the last pair are completed sets in this score format.
        set_diff = 0
        for left, right in pairs[:-1]:
            if left > right:
                set_diff += 1
            elif right > left:
                set_diff -= 1
        set_diff *= side_sign
        return game_diff, set_diff

    def show_category(name, fn):
        labels = sorted({fn(x) for x, _ in compare}, key=lambda z: str(z))
        print(f"  {name}")
        for val in labels:
            group = [(x,m) for x,m in compare if fn(x)==val]
            lose = sum(m <= 1.001 for _,m in group)
            big = sum(m >= 2 for _,m in group)
            print(f"    {str(val):24} n={len(group):3} never1={lose:3} "
                  f"{wilson(lose,len(group)):20}  2x+={big:3} {wilson(big,len(group))}")

    for name, fn in (
        ("entry price", lambda x: f"{float(x['entry_px']):.2f}"),
        ("league", lambda x: x.get("league", "?")),
        ("side", lambda x: x.get("side", "?")),
        ("set / period", lambda x: x.get("entry_period") or "missing"),
        ("entry game lead (oriented to chosen side)",
         lambda x: (lambda v: "missing" if v[0] is None else
                    "trailing" if v[0] < 0 else "tied" if v[0] == 0 else "leading")(oriented_score(x))),
        ("completed-set lead (oriented)",
         lambda x: (lambda v: "missing" if v[1] is None else
                    "trailing" if v[1] < 0 else "tied" if v[1] == 0 else "leading")(oriented_score(x))),
    ):
        show_category(name, fn)

    print("\nACTUAL-TRADE BREAKDOWNS BY LEAGUE")
    leagues = sorted({x[0].get("league", "?") for x in cooked})
    for league in leagues:
        grp = [y for y in cooked if y[0].get("league", "?") == league]
        gp = sum(y[2] for y in grp); gs = sum(y[3] for y in grp)
        print(f"  {league:24} n={len(grp):3} wins={sum(y[2]>0 for y in grp):3} "
              f"ROI={100*gp/gs:7.1f}%")

    print("\nRETROSPECTIVE ENTRY FILTER SCREEN (in-sample; do not treat as validated)")
    filters = (
        ("all", lambda x: True),
        ("short side", lambda x: x.get("side") == "short"),
        ("exclude S3", lambda x: x.get("entry_period") != "S3"),
        ("short and not S3", lambda x: x.get("side") == "short" and x.get("entry_period") != "S3"),
        ("ATP/WTA only", lambda x: x.get("league") in {"ATP", "WTA"}),
        ("exclude UTR and ITF-MENS", lambda x: x.get("league") not in {"UTR", "ITF-MENS"}),
    )
    print(f"  {'filter':29} {'n':>5} {'wins':>5} {'P&L':>9} {'ROI':>8} "
          f"{'never1':>8} {'peak2+':>8} {'peak5+':>8}")
    for name, pred in filters:
        g = [y for y in cooked if pred(y[0])]
        gs = sum(y[3] for y in g); gp = sum(y[2] for y in g)
        never = sum(m <= 1.001 for _,m,_,_ in g)
        peak2 = sum(m >= 2 for _,m,_,_ in g)
        peak5 = sum(m >= 5 for _,m,_,_ in g)
        roi = 100 * gp / gs if gs else 0
        print(f"  {name:29} {len(g):5} {sum(y[2]>0 for y in g):5} "
              f"{gp:9.2f} {roi:7.1f}% {never:8} {peak2:8} {peak5:8}")

    print("\nEXIT VARIANTS ON THE SAME ENTRY SET (screen only; unhit targets fall back to actual close)")
    staked = sum(y[3] for y in cooked)
    variant_names = sorted({key for x,_,_,_ in cooked for key in (x.get("variants") or {})})
    for name in variant_names:
        values = [float((x.get("variants") or {}).get(name, x["pnl"])) for x,_,_,_ in cooked]
        total = sum(values)
        print(f"  {name:10} pnl=${total:8.2f} ROI={100*total/staked:7.1f}% "
              f"profitable trades={sum(v>0 for v in values):3}/{len(values)}")
    return cooked


def tape_analysis(rows, outfile):
    settlements_path = config.SETTLEMENTS
    settlements = json.loads(settlements_path.read_text()) if settlements_path.exists() else {}
    resolved = [r for r in rows if r["slug"] in settlements]
    # An available settlement is not sufficient evidence that the whole tape
    # interval through settlement was observed; use it only as coverage metadata.
    summarize("TAPE FIRST-IN-BAND SIGNALS (all, path observed to last quote)", rows)
    summarize("  with settlement available", resolved)
    labelled = [r for r in rows if r["peak_mult"] <= 1.001 or r["peak_mult"] >= 2]
    print("\nTAPE FEATURE COMPARISON (first 1-5c quote; descriptive, no fitted model)")
    print(f"{'feature':28} {'never >1x':>19} {'reached >=2x':>19} {'all'}")
    groups = [("never >1x", [r for r in labelled if r["peak_mult"] <= 1.001]),
              ("reached >=2x", [r for r in labelled if r["peak_mult"] >= 2])]
    features = ("entry_px", "spread_pct", "entry_depth", "same_side_total",
                "directional_imbalance", "change_rows_last_60s")
    for f in features:
        vals = []
        for _, g in groups:
            a = sorted(float(r[f]) for r in g if r.get(f) is not None)
            if not a:
                vals.append("n/a")
            else:
                vals.append(f"{a[len(a)//2]:.4g} / {sum(a)/len(a):.4g}")
        allv = sorted(float(r[f]) for r in rows if r.get(f) is not None)
        vals.append(f"{allv[len(allv)//2]:.4g} / {sum(allv)/len(allv):.4g}" if allv else "n/a")
        print(f"{f:28} {vals[0]:>19} {vals[1]:>19} {vals[2]}")
    print("  values are median / mean; activity is deduplicated quote changes in prior 60 sec")

    by_league = defaultdict(list)
    for r in rows:
        by_league[r["league"]].append(r)
    print("\nTAPE OUTCOME RATES BY LEAGUE (peak >=2x, 95% Wilson interval)")
    for lg, g in sorted(by_league.items(), key=lambda kv: -len(kv[1])):
        succ = sum(r["peak_mult"] >= 2 for r in g)
        print(f"  {lg:24} n={len(g):4} >=2x={succ:4} rate={wilson(succ,len(g))}")

    if outfile:
        keys = list(rows[0]) if rows else []
        with outfile.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=keys)
            w.writeheader()
            w.writerows(rows)
        print(f"\nWrote tape reconstruction to {outfile}")


def write_trade_csv(cooked, path):
    keys = ["opened_utc", "closed_utc", "slug", "league", "side", "entry_px",
            "qty", "stake", "peak_px", "peak_multiple", "exit_px", "pnl",
            "period", "score", "reason", "held_secs", "peak_le_1x", "peak_ge_2x",
            "peak_ge_5x", "entry_rule"]
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        for x, mult, pnl, stake in cooked:
            opened = datetime.fromtimestamp(float(x["opened"]), timezone.utc).isoformat()
            closed = datetime.fromtimestamp(float(x["closed"]), timezone.utc).isoformat()
            w.writerow({
                "opened_utc": opened, "closed_utc": closed,
                "slug": x.get("slug", ""), "league": x.get("league", ""),
                "side": x.get("side", ""), "entry_px": x.get("entry_px", ""),
                "qty": x.get("qty", ""), "stake": f"{stake:.4f}",
                "peak_px": x.get("peak_px", ""), "peak_multiple": f"{mult:.4f}",
                "exit_px": x.get("exit_px", ""), "pnl": f"{pnl:.4f}",
                "period": x.get("entry_period", ""), "score": x.get("entry_score", ""),
                "reason": x.get("reason", ""), "held_secs": x.get("held_secs", ""),
                "peak_le_1x": int(mult <= 1.001), "peak_ge_2x": int(mult >= 2),
                "peak_ge_5x": int(mult >= 5), "entry_rule": x.get("entry_rule", ""),
            })
    print(f"\nWrote actual trade snapshot/score/outcome rows to {path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tape", action="store_true", help="attempt the incomplete/corruption-prone historical tape reconstruction")
    ap.add_argument("--csv", type=Path, default=config.DATA / "tennis_first_touch_tape_reconstruction.csv")
    ap.add_argument("--trades-csv", type=Path, default=config.DATA / "tennis_first_touch_trade_analysis.csv")
    ap.add_argument("--no-csv", action="store_true")
    args = ap.parse_args()
    cooked = actual_trades()
    write_trade_csv(cooked, args.trades_csv)
    dbpath = config.LS_SIGNAL_DB
    if dbpath.exists():
        import sqlite3
        with sqlite3.connect(dbpath) as db:
            print("\nLIVE SIGNAL LEDGER")
            print(f"database: {dbpath}")
            print(f"rows: {db.execute('select count(*) from signals').fetchone()[0]}")
            print("status / reason counts:")
            for status, reason, n in db.execute("""select first_status, first_reason, count(*)
                    from signals group by first_status, first_reason order by count(*) desc"""):
                print(f"  {status or '?':10} {reason or '-':30} {n}")
            for sport in ("tennis", "all other sports"):
                condition, params = ("sport='tennis'", ()) if sport == "tennis" else ("sport!='tennis'", ())
                group = db.execute(f"""select candidate_px, actual_entry_px,
                        peak_exit_px, entered, side_settlement, first_status, first_reason
                        from signals where {condition}""", params).fetchall()
                if not group:
                    print(f"{sport} ledger: 0 rows")
                    continue
                print(f"{sport} ledger: {len(group)} rows")
                by_reason = defaultdict(int)
                for row in group:
                    by_reason[(row[5] or "?", row[6] or "-")] += 1
                for (status, reason), n in sorted(by_reason.items(), key=lambda kv: -kv[1]):
                    print(f"  {status:10} {reason:30} {n}")
                buckets = Counter()
                for candidate_px, actual_px, peak_px, entered, settlement, _status, _reason in group:
                    base = actual_px if entered and actual_px else candidate_px
                    mult = (peak_px or 0) / base if base else 0
                    bucket = "<=1x" if mult <= 1.001 else "1-2x" if mult < 2 else ">=2x"
                    buckets[bucket] += 1
                print("  best observed executable peak multiple: " + ", ".join(
                    f"{k}={buckets[k]}" for k in ("<=1x", "1-2x", ">=2x")))
    if args.tape:
        rows, files = read_tape()
        print(f"\nRead {len(files)} tennis quote tape file(s) from {files[0].name if files else 'none'} "
              f"to {files[-1].name if files else 'none'}; reconstructed {len(rows)} first-in-band "
              "market-side paths.")
        tape_analysis(rows, None if args.no_csv else args.csv)


if __name__ == "__main__":
    main()
