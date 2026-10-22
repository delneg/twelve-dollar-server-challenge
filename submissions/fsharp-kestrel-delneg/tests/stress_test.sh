#!/usr/bin/env bash
# Transport stress: starts this folder's out/Server (target, pinned to CPU 0 with DOTNET_PROCESSOR_COUNT=1 like
# the 1-vCPU droplet) and a reference server REF_BIN (normally a Kestrel-sockets build), each on a fresh copy of
# the seed database, then runs tests/stress.py against the target and compares reads with the reference.
# Usage: REF_BIN=/path/to/sockets/out/Server bash tests/stress_test.sh [epoll|sockets] [stress.py args...]
# env: SEED (feed.db), TOKENS (tokens.json)
set -euo pipefail
cd "$(dirname "$0")/.."
want=${1:-epoll}; shift || true
SEED=${SEED:-../../seed/feed.db}; TOKENS=${TOKENS:-../../seed/tokens.json}
: "${REF_BIN:?set REF_BIN to the reference server binary}"
ulimit -n "$(ulimit -Hn)"
tmp=$(mktemp -d); cp "$SEED" "$tmp/t.db"; cp "$SEED" "$tmp/r.db"
export JWT_SECRET=twelve-dollar-challenge
SQLITE_PATH="$tmp/t.db" PORT=3101 DOTNET_PROCESSOR_COUNT=1 taskset -c 0 out/Server > "$tmp/t.log" 2>&1 < /dev/null &
tp=$!
SQLITE_PATH="$tmp/r.db" PORT=3102 "$REF_BIN" > "$tmp/r.log" 2>&1 < /dev/null &
rp=$!
trap 'rc=$?; kill $tp $rp 2>/dev/null; wait $tp $rp 2>/dev/null || true; rm -rf "$tmp"; exit $rc' EXIT
for _ in $(seq 200); do curl -sf localhost:3101/health > /dev/null && curl -sf localhost:3102/health > /dev/null && break; sleep 0.1; done
echo "target transport: $(bash tests/transport_of.sh $tp) (want $want); reference: $(bash tests/transport_of.sh $rp)"
[ "$(bash tests/transport_of.sh $tp)" = "$want" ] || { echo "STRESS FAILED: wrong transport"; exit 1; }
taskset -c 1-63 python3 tests/stress.py --target 127.0.0.1:3101 --ref 127.0.0.1:3102 --pid $tp --tokens "$TOKENS" "$@"
