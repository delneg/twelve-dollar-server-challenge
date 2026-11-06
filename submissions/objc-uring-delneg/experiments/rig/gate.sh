#!/usr/bin/env bash
# gate.sh: gates for the push candidate v28f, then E179. Log w/gate-v28f.log
cd /root/objc3; chmod -x /etc/update-motd.d/*
S=/root/objc3/src/v28f; rm -rf $S; mkdir -p $S; tar xzf v28f.tgz -C $S
{ date -u; echo "== mk"; bash mk.sh v28f $S; date -u; echo "== ci"; bash ci.sh $S v28f; date -u; cd /root/objc3; echo "== crash"; ulimit -n 65535; python3 crash.py /root/objc3/out/v28f/start.sh 3; date -u; echo GATES-DONE; } > w/gate-v28f.log 2>&1
bash q.sh g8 "e179 v28f 290000 300 BURN=26 BCPU=1 ARGS=--answers=64+--wwin=10+--defacc=1"
