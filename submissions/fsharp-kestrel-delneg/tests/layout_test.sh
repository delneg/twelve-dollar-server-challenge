#!/usr/bin/env bash
# Layout test. Run on the target Linux box after build.sh: bash tests/layout_test.sh
set -euo pipefail
cd "$(dirname "$0")/.."
tmp=$(mktemp -d)
cc -O0 tests/native_layout.c -o "$tmp/native_layout"
"$tmp/native_layout" > "$tmp/native.txt"
dotnet fsi tests/layout_check.fsx out/Server.dll "$tmp/native.txt" Transport.fs
