#!/bin/bash
# Safe restart. Never delete the lockfile to force a start - that is what the
# lockfile exists to prevent, and doing so produced two concurrent recorders
# writing the same tapes.
cd "$(dirname "$0")" || exit 1

echo "stopping supervisor(s)..."
pkill -f "supervise.sh" 2>/dev/null
sleep 1

# Stop the recorder the lockfile names, then any stragglers.
if [ -f data/recorder.pid ]; then
    PID=$(cat data/recorder.pid)
    if kill -0 "$PID" 2>/dev/null; then
        echo "stopping recorder $PID..."
        kill "$PID" 2>/dev/null
    fi
fi
pkill -f "Python run.py" 2>/dev/null
pkill -f "caffeinate -is" 2>/dev/null

# Wait for a genuine exit rather than assuming one.
for i in $(seq 1 30); do
    REMAIN=$(pgrep -f "Python run.py" | wc -l | tr -d ' ')
    [ "$REMAIN" -eq 0 ] && break
    sleep 1
done

REMAIN=$(pgrep -f "Python run.py" | wc -l | tr -d ' ')
if [ "$REMAIN" -ne 0 ]; then
    echo "ERROR: $REMAIN recorder(s) still alive after 30s - not starting a second one"
    pgrep -f "Python run.py"
    exit 1
fi

# Only now is a stale lockfile safe to clear.
[ -f data/recorder.pid ] && rm -f data/recorder.pid

HOURS="${1:-72}"
echo "starting supervisor for ${HOURS}h..."
nohup ./supervise.sh "$HOURS" > /dev/null 2>&1 &
sleep 3
echo "supervisors: $(pgrep -f 'supervise.sh' | wc -l | tr -d ' ')  recorders: $(pgrep -f 'Python run.py' | wc -l | tr -d ' ')"
