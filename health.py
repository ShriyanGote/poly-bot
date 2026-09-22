#!/usr/bin/env python
"""One-shot health check for the collection run.

    .venv/bin/python health.py          # full report
    .venv/bin/python health.py -q       # one line, exit 0 OK / 1 WARN / 2 FAIL

Checks every failure mode this run has actually hit: dead process, duplicate
recorders, silently dead websocket, rate limiting, stalled tape, disk, sleep.
"""

import argparse
import csv
import gzip
import json
import os
import re
import subprocess
import sys
import zlib
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bot import config  # noqa: E402

OK, WARN, FAIL = "OK", "WARN", "FAIL"
RANK = {OK: 0, WARN: 1, FAIL: 2}
ICON = {OK: "  ok  ", " WARN ": " WARN ", WARN: " WARN ", FAIL: " FAIL "}


class Report:
    def __init__(self):
        self.rows = []

    def add(self, status, name, detail):
        self.rows.append((status, name, detail))

    @property
    def worst(self):
        return max((r[0] for r in self.rows), key=lambda s: RANK[s], default=OK)

    def render(self):
        w = max(len(r[1]) for r in self.rows)
        out = []
        for status, name, detail in self.rows:
            out.append(f"  [{ICON.get(status, status):^6}] {name:<{w}}  {detail}")
        return "\n".join(out)


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except Exception:
        return ""


def age(path):
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return None


def human(secs):
    if secs is None:
        return "n/a"
    if secs < 90:
        return f"{secs:.0f}s"
    if secs < 5400:
        return f"{secs/60:.0f}m"
    return f"{secs/3600:.1f}h"


def newest_tape():
    """Freshest tape file of any sport. Tapes are per-sport now, so checking a
    single fixed filename reports a false failure."""
    files = list(config.DATA.glob("tape-*.csv.gz"))
    if not files:
        return None
    return max(files, key=lambda f: f.stat().st_mtime)


def last_heartbeat():
    """Most recent heartbeat line and its parsed fields."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log = config.LOGS / f"run-{day}.log"
    if not log.exists():
        logs = sorted(config.LOGS.glob("run-*.log"))
        if not logs:
            return None, {}
        log = logs[-1]
    hb = None
    try:
        for line in log.read_text(errors="replace").splitlines():
            if "quiet" in line and "mkts" in line:
                hb = line
    except OSError:
        return None, {}
    if not hb:
        return None, {}
    f = {}
    for key, pat in (("mkts", r"(\d+) mkts"), ("books", r"books (\d+)"),
                     ("trades", r"trades (\d+)"), ("quiet", r"quiet (\d+)s"),
                     ("reconn", r"reconn (\d+)"), ("stale", r"stale (\d+)"),
                     ("exc", r"exc (\d+)")):
        m = re.search(pat, hb)
        if m:
            f[key] = int(m.group(1))
    m = re.search(r"net ([+-][\d.]+)", hb)
    if m:
        f["net"] = m.group(1)
    return hb, f


def proc_start(pid):
    """Seconds since the recorder process started, via ps etime."""
    out = sh(f"ps -o etime= -p {pid}").strip()
    if not out:
        return None
    parts = out.replace("-", ":").split(":")
    try:
        nums = [int(x) for x in parts]
    except ValueError:
        return None
    secs, mult = 0, 1
    for n in reversed(nums[-3:]):
        secs += n * mult
        mult *= 60
    if len(nums) == 4:
        secs += nums[0] * 86400
    return secs


def build():
    r = Report()

    # --- process -------------------------------------------------------------
    lock = config.DATA / "recorder.pid"
    pid = None
    if lock.exists():
        try:
            pid = int(lock.read_text().strip())
            os.kill(pid, 0)
            r.add(OK, "recorder", f"pid {pid} running")
        except (ValueError, ProcessLookupError, PermissionError):
            r.add(FAIL, "recorder", f"lock says pid {pid} but it is not running")
            pid = None
    else:
        r.add(FAIL, "recorder", "no lockfile - recorder is not running")

    n_rec = len([x for x in sh("pgrep -f 'Python run.py'").split() if x])
    if n_rec > 1:
        r.add(FAIL, "duplicates", f"{n_rec} recorders running - they corrupt each other")
    elif n_rec == 1:
        r.add(OK, "duplicates", "exactly one recorder")

    n_sup = len([x for x in sh("pgrep -f 'supervise.sh'").split() if x])
    if n_sup == 0:
        r.add(WARN, "supervisor", "not running - no auto-restart on crash")
    elif n_sup > 1:
        r.add(WARN, "supervisor", f"{n_sup} supervisors - may fight over restarts")
    else:
        r.add(OK, "supervisor", "1 running, will restart on crash")

    # --- feed ----------------------------------------------------------------
    hb, f = last_heartbeat()
    if not hb:
        r.add(WARN, "heartbeat", "none found yet")
    else:
        hb_age = None
        m = re.match(r"(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)", hb)
        if m:
            mo, d, H, M, S = map(int, m.groups())
            now = datetime.now(timezone.utc)
            try:
                t = datetime(now.year, mo, d, H, M, S, tzinfo=timezone.utc)
                hb_age = (now - t).total_seconds()
            except ValueError:
                pass
        # heartbeats land once per discovery interval
        limit = config.DISCOVERY_INTERVAL * 2.5
        if hb_age is None:
            r.add(WARN, "heartbeat", "could not parse timestamp")
        elif hb_age > limit:
            r.add(FAIL, "heartbeat", f"last one {human(hb_age)} ago - loop may be stuck")
        else:
            r.add(OK, "heartbeat", f"{human(hb_age)} ago")

        up = proc_start(pid) if pid else None
        if up is not None and hb_age is not None and hb_age > up:
            r.add(WARN, "heartbeat age",
                  f"last heartbeat predates this process (restarted {human(up)} ago) "
                  f"- figures below are stale until the first sweep completes")

        quiet = f.get("quiet")
        if quiet is None:
            r.add(WARN, "websocket", "no quiet field in heartbeat")
        elif quiet > config.STALE_SECS:
            r.add(FAIL, "websocket", f"silent {quiet}s (> {config.STALE_SECS}s) - feed dead")
        elif quiet > config.STALE_SECS / 2:
            r.add(WARN, "websocket", f"silent {quiet}s - watching")
        else:
            r.add(OK, "websocket", f"live, last message {quiet}s ago")

        if f.get("mkts"):
            r.add(OK, "subscriptions", f"{f['mkts']} markets")
        else:
            r.add(WARN, "subscriptions", "0 markets subscribed")

    # --- tape freshness ------------------------------------------------------
    tape = newest_tape()
    a = age(tape) if tape else None
    if a is None:
        r.add(WARN, "tape", "today's tape not created yet")
    elif a > 300:
        r.add(FAIL, "tape", f"{tape.name} not written for {human(a)}")
    elif a > 120:
        r.add(WARN, "tape", f"last write {human(a)} ago")
    else:
        r.add(OK, "tape", f"{tape.name.replace('.csv.gz','')} written {human(a)} ago")

    # --- errors --------------------------------------------------------------
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log = config.LOGS / f"run-{day}.log"
    text = log.read_text(errors="replace") if log.exists() else ""
    # Errors from a previous process are history, not current health. Count
    # only what this process has logged since its most recent "start |" line.
    lines = text.splitlines()
    starts = [i for i, ln in enumerate(lines) if " start | " in ln]
    cur = "\n".join(lines[starts[-1]:]) if starts else text
    older = len(lines) - (starts[-1] if starts else 0)
    stale = cur.count("FEED STALE")
    rl = cur.count("RATE LIMITED")
    errs = cur.count("WS ERROR") + cur.count("paper error")
    lost = cur.count("connection lost")
    hist_errs = (text.count("WS ERROR") + text.count("paper error")) - errs

    r.add(WARN if rl else OK, "rate limits",
          f"{rl} today" + (" - discovery is backing off" if rl else ""))
    r.add(WARN if stale > 3 else OK, "stale recoveries", f"{stale} forced reconnects")
    r.add(OK if lost < 10 else WARN, "reconnects", f"{lost} connection drops (recovered)")
    r.add(WARN if errs else OK, "errors",
          f"{errs} since restart" + (f" ({hist_errs} earlier, resolved)" if hist_errs else ""))

    # --- disk ----------------------------------------------------------------
    free = sh("df -k /Users | tail -1 | awk '{print $4}'")
    try:
        free_gb = int(free) / 1024 / 1024
        used = sum(p.stat().st_size for p in config.DATA.glob("*.gz")) / 1e6
        if free_gb < 5:
            r.add(FAIL, "disk", f"{free_gb:.0f} GB free - too low")
        elif free_gb < 20:
            r.add(WARN, "disk", f"{free_gb:.0f} GB free")
        else:
            r.add(OK, "disk", f"{free_gb:.0f} GB free, {used:.0f} MB collected")
    except (ValueError, ZeroDivisionError):
        r.add(WARN, "disk", "could not read")

    # --- sleep ---------------------------------------------------------------
    pm = sh("pmset -g | grep ' sleep '")
    caf = "caffeinate" in pm
    never = sh("pmset -g custom 2>/dev/null | sed -n '/AC Power/,/^$/p' | grep -E '^ sleep'")
    ac_never = never.split()[-1] == "0" if never.split() else False
    if caf and ac_never:
        r.add(OK, "sleep", "caffeinate active, AC sleep disabled")
    elif caf:
        r.add(WARN, "sleep", "caffeinate active but AC sleep timer set - run: sudo pmset -c sleep 0")
    else:
        r.add(FAIL, "sleep", "nothing preventing sleep")

    onac = "AC Power" in sh("pmset -g batt")
    r.add(OK if onac else FAIL, "power", "on AC" if onac else "ON BATTERY - will sleep")

    return r, f


def data_summary():
    out = []
    tot_rows = tot_bytes = 0
    games, markets = set(), set()
    for f in sorted(config.DATA.glob("tape-*.csv.gz")):
        tot_bytes += f.stat().st_size
        try:
            with gzip.open(f, "rt") as fh:
                for row in csv.DictReader(fh):
                    tot_rows += 1
                    if row.get("event"):
                        games.add(row["event"])
                    markets.add(row["market"])
        except (EOFError, OSError, gzip.BadGzipFile, zlib.error, csv.Error):
            # live file mid-write, or damaged by the earlier concurrent-writer
            # incident; count what parsed rather than crash the health report
            pass
    out.append(f"  games {len(games)} | markets {len(markets)} | "
               f"book rows {tot_rows:,} | {tot_bytes/1e6:.0f} MB")

    if config.STATE_FILE.exists():
        try:
            s = json.loads(config.STATE_FILE.read_text())
            # Every closed trade counts. Gapped trades are flagged, never
            # dropped: winners exit in seconds while losers straddle restarts,
            # so filtering by gap silently deletes losses.
            closed = s.get("closed", [])
            susp = sum(1 for c in closed if c.get("suspect"))
            real = sum(Decimal(c["pnl"]) for c in closed) if closed else Decimal("0")
            wins = sum(1 for c in closed if Decimal(c["pnl"]) > 0)
            # Unrealised matters: winners close fast, losers sit open, so
            # realised-only P&L flatters the strategy badly.
            unreal = Decimal("0")
            unmarked = 0
            for p_ in s.get("positions", {}).values():
                if p_.get("mark") is None:
                    unmarked += 1
                    continue
                unreal += (Decimal(p_["mark"]) - Decimal(p_["entry_px"])) * p_["qty"]
            out.append(f"  paper: {len(s.get('positions',{}))} open, "
                       f"{len(s.get('resting',{}))} resting, "
                       f"{len(closed)} closed ({wins}W/{len(closed)-wins}L)"
                       + (f", {susp} spanned a data gap" if susp else ""))
            out.append(f"  P&L:   realised {real:+.2f}  unrealised {unreal:+.2f}  "
                       f"TOTAL {real + unreal:+.2f}"
                       + (f"   ({unmarked} open unmarked)" if unmarked else ""))
        except Exception:
            pass
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-q", "--quiet", action="store_true", help="one line + exit code")
    args = ap.parse_args()

    rep, f = build()
    worst = rep.worst

    if args.quiet:
        bad = [n for s, n, _ in rep.rows if s != OK]
        msg = "HEALTHY" if worst == OK else f"{worst}: " + ", ".join(bad)
        print(f"{msg}  |  {f.get('mkts','?')} mkts, books {f.get('books','?')}, "
              f"quiet {f.get('quiet','?')}s")
        return RANK[worst]

    print(f"\nPolymarket collector health   {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC")
    print("=" * 74)
    print(rep.render())
    print("-" * 74)
    print(data_summary())
    print("=" * 74)
    print(f"  OVERALL: {'HEALTHY' if worst == OK else worst}\n")
    return RANK[worst]


if __name__ == "__main__":
    sys.exit(main())
