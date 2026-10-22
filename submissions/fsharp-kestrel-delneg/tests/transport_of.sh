#!/usr/bin/env bash
# Prints which transport a running Server uses, from its thread names: "epoll" (our loop thread, Transport.fs)
# or "sockets" (Kestrel's socket transport runs on .NET's "SocketEng"/".NET Sockets" threads). Run after at least
# one request, since .NET starts its socket threads lazily. Usage: bash tests/transport_of.sh <pid>
set -euo pipefail
names=$(cat /proc/"$1"/task/*/comm)
e=$(grep -cx 'epoll' <<<"$names" || true)
s=$(grep -c -e '\.NET Sockets' -e 'SocketEng' <<<"$names" || true)
if [ "$e" -ge 1 ] && [ "$s" -eq 0 ]; then echo epoll
elif [ "$e" -eq 0 ] && [ "$s" -ge 1 ]; then echo sockets
else echo "unknown (epoll threads $e, socket threads $s)"; fi
