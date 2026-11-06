#!/usr/bin/env bash
# mk.sh <label> <srcdir>: build a variant on Synteq; deps shared via cache; out in /root/objc3/out/<label>
L=$1; S=$2; O=/root/objc3/out/$L; rm -rf $O; mkdir -p $O; cp -r $S/. $O/; cd $O
mkdir -p deps; cp -n /root/objc2/depcache/* deps/ 2>/dev/null
if [ -d /root/objc2/depbuilt/objc ]; then cp -r /root/objc2/depbuilt/objc /root/objc2/depbuilt/uring deps/; fi
bash build.sh > build.log 2>&1; rc=$?; tail -2 build.log
if [ ! -d /root/objc2/depbuilt/objc ] && [ -d deps/objc ]; then mkdir -p /root/objc2/depbuilt /root/objc2/depcache; cp -r deps/objc deps/uring /root/objc2/depbuilt/; cp deps/*.tar.gz deps/*.zip /root/objc2/depcache/; fi
echo "$L rc=$rc $(sha256sum bin/server | cut -c1-12) $(stat -c %s bin/server)"
