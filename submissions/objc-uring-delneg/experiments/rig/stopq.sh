#!/usr/bin/env bash
# stopq.sh: stop a running queue, its run, the generator and the units
for p in $(pgrep -x bash); do c=$(tr '\0' ' ' < /proc/$p/cmdline); case "$c" in "bash q.sh"*|"bash hs.sh"*) [ "$p" != "$$" ] && kill -9 $p;; esac; done
pkill -9 -x ul3; pkill -x timeout; sleep 1; systemctl stop objsrv hog burn 2>/dev/null; echo stopped
