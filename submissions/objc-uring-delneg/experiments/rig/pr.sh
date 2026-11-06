#!/usr/bin/env bash
# pr.sh <perf.data> [n]: inclusive (children) by symbol, and self by symbol
F=$1; N=${2:-70}
echo "--- children"; perf report -q -i $F --children --no-demangle --sort sym -g none --percent-limit 0.4 2>/dev/null | head -$N | cut -c1-90
echo "--- self"; perf report -q -i $F --no-children --no-demangle --sort comm,dso,sym -g none --percent-limit 0.5 2>/dev/null | head -$N | cut -c1-110
