#!/usr/bin/env bash
# t42.sh <label>: 42/42 for an already built variant, on CPU 7 (build skipped: start.sh of out/<label>)
L=$1; O=/root/objc3/out/$L; R=/root/objc2/repo
export JWT_SECRET=twelve-dollar-challenge HOST=127.0.0.1 PORT=${PORT:-3999}
T=$(mktemp -d); export SQLITE_PATH=$T/feed.db; cp /root/watlab/seed/feed.db $SQLITE_PATH
ulimit -n 65535
taskset -c 7 $O/bin/server > $T/log 2>&1 &
P=$!
for i in $(seq 60); do curl -sf http://$HOST:$PORT/health >/dev/null && break; sleep 0.5; done
bash $R/test/test.sh http://$HOST:$PORT 2>&1 | tail -${TAILN:-3}
kill $P; rm -rf $T
