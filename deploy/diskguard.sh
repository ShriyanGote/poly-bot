#!/bin/bash
# A full disk stops the tapes silently: gzip writes fail, the recorder keeps
# running, and the data is simply gone. Archive oldest-first before that.
set -euo pipefail
# Follow the symlink: data/ lives on the attached volume, and df on the
# symlink path reports the volume, which is what we want to watch.
DATA=/home/poly/Polymarket/data
KEEP_FREE_PCT=15

used=$(df --output=pcent "$DATA" | tail -1 | tr -dc '0-9')
free=$((100 - used))
[ "$free" -ge "$KEEP_FREE_PCT" ] && exit 0

echo "$(date -u +%FT%TZ) disk ${used}% used, only ${free}% free - pruning oldest tapes"
# Oldest first, stop as soon as we are back above the threshold.
find "$DATA" -name 'tape-*.csv.gz' -printf '%T@ %p\n' | sort -n | while read -r _ f; do
    used=$(df --output=pcent "$DATA" | tail -1 | tr -dc '0-9')
    [ $((100 - used)) -ge "$KEEP_FREE_PCT" ] && break
    echo "  removing $f"
    rm -f "$f"
done
