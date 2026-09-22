#!/bin/bash
# Keeps run.py alive for a multi-day collection run.
# Restarts on crash; caffeinate stops the Mac sleeping.
#
#   ./supervise.sh          # run until stopped
#   ./supervise.sh 72       # stop after 72 hours

cd "$(dirname "$0")" || exit 1
HOURS="${1:-72}"
DEADLINE=$(( $(date +%s) + HOURS*3600 ))
mkdir -p logs

echo "$(date -u '+%F %T') supervisor start, running for ${HOURS}h" | tee -a logs/supervisor.log

while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    echo "$(date -u '+%F %T') launching run.py" | tee -a logs/supervisor.log
    caffeinate -is .venv/bin/python run.py >> logs/run.out 2>&1
    CODE=$?
    if [ "$CODE" -eq 0 ]; then
        echo "$(date -u '+%F %T') clean exit, stopping supervisor" | tee -a logs/supervisor.log
        break
    fi
    echo "$(date -u '+%F %T') run.py exited ($CODE), restarting in 30s" | tee -a logs/supervisor.log
    sleep 30
done
echo "$(date -u '+%F %T') supervisor done" | tee -a logs/supervisor.log
