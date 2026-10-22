#!/usr/bin/env bash
# End to end: start out/Server on a fresh copy of the seed database, run the challenge's test/test.sh against it,
# and fail unless every check passes AND the server runs the expected transport (default: epoll).
# Usage: bash tests/e2e_test.sh [epoll|sockets]    env: SEED (feed.db), TESTSH (test/test.sh), PORT
set -euo pipefail
cd "$(dirname "$0")/.."
want=${1:-epoll}
SEED=${SEED:-../../seed/feed.db}; TESTSH=${TESTSH:-../../test/test.sh}; PORT=${PORT:-3000}
tmp=$(mktemp -d); cp "$SEED" "$tmp/feed.db"
SQLITE_PATH="$tmp/feed.db" PORT=$PORT JWT_SECRET=twelve-dollar-challenge out/Server > "$tmp/server.log" 2>&1 < /dev/null &
pid=$!
trap 'rc=$?; kill $pid 2>/dev/null; wait $pid 2>/dev/null || true; rm -rf "$tmp"; exit $rc' EXIT
for _ in $(seq 200); do curl -sf "localhost:$PORT/health" > /dev/null && break; sleep 0.1; done
out=$(bash "$TESTSH" "http://127.0.0.1:$PORT" 2>&1 || true)
summary=$(tail -1 <<<"$out")
transport=$(bash tests/transport_of.sh $pid)
echo "test.sh: $summary"
echo "transport: $transport (want $want)"
grep -q 'FAIL' <<<"$out" && grep 'FAIL' <<<"$out"
if [[ "$summary" =~ passed=([0-9]+)\ failed=0 ]] && [ "${BASH_REMATCH[1]}" -ge 42 ] && [ "$transport" = "$want" ]; then
  echo "E2E OK"
else
  echo "E2E FAILED"; exit 1
fi
