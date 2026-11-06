#!/usr/bin/env bash
# q.sh <name> "label build users hold [K=V ...]"...: runs each through hs.sh in order, results in w/<label>.out, one-liners in w/<name>.sum. K=V: env for hs.sh; ARGS with + for spaces.
N=$1; shift; cd /root/objc3; mkdir -p w
for line in "$@"; do
  set -- $line; L=$1; B=$2; U=$3; H=$4; shift 4
  ( for e in "$@"; do case $e in ARGS=*) export ARGS="$(echo ${e#ARGS=} | tr + ' ')";; *) export "$e";; esac; done
    timeout $((H+500)) bash hs.sh $L $B $U $H > w/$L.out 2>&1 )
  { grep -h "^$L " w/$L.out | head -1 | cut -c1-260; grep -E '^  (ramp|hold):|dmesg' w/$L.out | head -4 | cut -c1-200; } >> w/$N.sum
done
echo DONE >> w/$N.sum
