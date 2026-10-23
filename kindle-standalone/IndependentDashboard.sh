#!/bin/sh
BASE=/mnt/us/kindle-standalone
[ -r "$BASE/start.sh" ] || exit 1
mkdir -p "$BASE/data" || exit 1
if command -v setsid >/dev/null 2>&1; then
    nohup setsid sh "$BASE/start.sh" >> "$BASE/data/bootstrap.log" 2>&1 < /dev/null &
else
    nohup sh "$BASE/start.sh" >> "$BASE/data/bootstrap.log" 2>&1 < /dev/null &
fi
exit 0
