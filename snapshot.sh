#!/bin/bash
# Snapshot data/ before any destructive operation.
# Uses APFS clones (cp -c) so it is near-instant and costs almost no disk
# until files diverge.
cd "$(dirname "$0")" || exit 1
SNAP="backups/snapshot-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$SNAP"
cp -c data/*.csv.gz data/*.json "$SNAP"/ 2>/dev/null || \
  cp data/*.csv.gz data/*.json "$SNAP"/ 2>/dev/null
echo "$SNAP  ($(ls "$SNAP" | wc -l | tr -d ' ') files, $(du -sh "$SNAP" | awk '{print $1}'))"

# keep the 8 most recent
ls -1dt backups/snapshot-* 2>/dev/null | tail -n +9 | while read -r old; do
    rm -rf "$old"; echo "pruned $old"
done
