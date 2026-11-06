#!/usr/bin/env bash
# hs.sh <label> <build> <users> [hold_s=120]: rig v3. Like /root/objc2/hs.sh, plus: CPU 0 island (island.sh), k6-view percentiles over the whole run
# (ramp + hold + ramp-down, connect time excluded), per-second series, kernel thread CPU, no TD_STATS unless STATS=1.
# Env: SRVMB (1850), MEMMAX (1750M), RAMP (60), ARGS, EXTRA, BURN, STATS, RTT (us, netem on client->server), PERF (start,secs), BIN.
L=$1; BLD=$2; U=$3; H=${4:-120}; M=$((H-10)); W=/root/watlab; X=/root/objc3; D=/home/deploy; B=${BDIR:-$X/out/$BLD}
LOGD=$X/w; mkdir -p $LOGD; LOG=$LOGD/$L; bash $W/ns.sh up >/dev/null; sysctl -qp $W/w3/sysctl.cal; ip netns exec srvns sysctl -qw net.ipv4.tcp_rmem="4096 131072 6291456" net.ipv4.tcp_wmem="4096 16384 4194304"; SIP=10.77.0.2
bash $X/island.sh up >/dev/null
systemctl stop nginx ModemManager udisks2 unattended-upgrades 2>/dev/null
tc qdisc del dev veth0 root 2>/dev/null; [ -n "${RTT:-}" ] && tc qdisc add dev veth0 root netem delay ${RTT}us limit 200000
systemctl stop objsrv 2>/dev/null; systemctl reset-failed objsrv 2>/dev/null; sleep 1
rm -rf $D/wb $D/run/cur; mkdir -p $D/wb $D/run/cur/bin $D/run/cur/deps/objc; cp $W/seed/feed.db $D/wb/feed.db; cp $B/bin/server $D/run/cur/bin/; cp -r $B/deps/objc/lib $D/run/cur/deps/objc/ 2>/dev/null; chown -R deploy: $D/wb $D/run
sync; echo 3 > /proc/sys/vm/drop_caches; dmesg -c >/dev/null
systemctl stop hog 2>/dev/null; systemctl reset-failed hog 2>/dev/null
sleep 1; AV=${AV0:-15470}; HOG=$(( AV - ${SRVMB:-1850} - U * ${KBU:-115} / 10240 ))
HOGC="$W/hog.py $HOG"; [ -n "${FRAG:-}" ] && HOGC="$X/fhog.py $HOG $((AV-HOG-300))"; HOGADJ=-1000; [ -n "${FRAG:-}" ] && HOGADJ=0; HT0=$(date "+%F %T"); systemd-run -q --unit=hog -p OOMScoreAdjust=$HOGADJ -p AllowedCPUs=1-7 python3 $HOGC; for i in $(seq 120); do if [ -n "${FRAG:-}" ]; then journalctl -u hog -q --no-pager --since "$HT0" | grep -q "hog holds" && break; else journalctl -u hog -q --no-pager | grep -q "hog holds" && break; fi; sleep 1; done
echo "hog ${HOG} MiB, avail now $(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo) MiB" > $LOG.hog
STATSENV=""; [ -n "${STATS:-}" ] && STATSENV="-E TD_STATS=1"
systemd-run -q --unit=objsrv -p User=deploy -p LimitNOFILE=65535 -p CPUAffinity=0 -p MemoryMax=${MEMMAX:-1750M} -p MemorySwapMax=0 -p NetworkNamespacePath=/run/netns/srvns \
  -p WorkingDirectory=$D/run/cur ${EXTRA:-} -p MemoryAccounting=yes -p IOAccounting=yes $STATSENV -E SQLITE_PATH=$D/wb/feed.db -E JWT_SECRET=twelve-dollar-challenge \
  -E HOST=$SIP -E PORT=3000 $D/run/cur/bin/server ${ARGS:-}
for i in $(seq 600); do curl -sf http://$SIP:3000/health >/dev/null && break; sleep 0.1; done
SP=$(systemctl show -p MainPID --value objsrv); CG=/sys/fs/cgroup/system.slice/objsrv.service
for i in 1 2 3; do curl -s http://$SIP:3000/feed >/dev/null; done
python3 $X/kts.py > $LOG.kt0
grep -E "^(thp_fault_alloc|thp_fault_fallback|compact_stall|compact_fail|compact_success|allocstall_normal|allocstall_movable|pgscan_direct) " /proc/vmstat > $LOG.vm0
( a=$SP; T0=$(date +%s); while sleep 1; do
    read -r st ut stt <<< "$(awk '{print $3, $14, $15}' /proc/$a/stat 2>/dev/null)"
    echo "$(($(date +%s)-T0)) st=$st c0=$(awk '/^cpu0 /{print $2+$3","$4","$5","$6","$7","$8","$9}' /proc/stat) ucg=$(awk '/^usage_usec/{print $2}' $CG/cpu.stat 2>/dev/null) ksi=$(awk '{print $14+$15}' /proc/$(pgrep -x ksoftirqd/0)/stat) cgm=$(( $(cat $CG/memory.current 2>/dev/null || echo 0) >>20 )) sock=$(( $(awk '/^sock /{print $2}' $CG/memory.stat 2>/dev/null || echo 0) >>20 )) tcp=$(awk '/^TCP:/{print $11}' /proc/$a/net/sockstat 2>/dev/null) file=$(( $(awk '/^file /{print $2}' $CG/memory.stat 2>/dev/null || echo 0) >>20 )) wal=$(( $(stat -c %s $D/wb/feed.db-wal 2>/dev/null || echo 0) >>20 )) dirty=$(awk '/^Dirty:/{print int($2/1024)}' /proc/meminfo) free=$(awk '/MemFree/{print int($2/1024)}' /proc/meminfo) rd=$(( $(awk '/^read_bytes/{print $2}' /proc/$a/io 2>/dev/null || echo 0) >>20 )) wr=$(( $(awk '/^write_bytes/{print $2}' /proc/$a/io 2>/dev/null || echo 0) >>20 )) $(awk '/^(allocstall_normal|pgscan_direct|pgscan_kswapd|pgmajfault) /{printf "%s=%s ", $1, $2}' /proc/vmstat)$(awk '/^TcpExt:/{if(!h){split($0,k);h=1}else{for(i=2;i<=NF;i++)if(k[i]~/^(ListenOverflows|ListenDrops|DelayedACKs|TCPPureAcks|TCPTimeouts|TCPRcvQDrop|TCPBacklogDrop|PruneCalled)$/)printf "%s=%s ",k[i],$i}}' /proc/$a/net/netstat 2>/dev/null)$(awk '/^Tcp:/{if(!h){split($0,k);h=1}else{for(i=2;i<=NF;i++)if(k[i]~/^(InSegs|OutSegs|RetransSegs|ActiveOpens|PassiveOpens)$/)printf "%s=%s ",k[i],$i}}' /proc/$a/net/snmp 2>/dev/null)"
  done ) > $LOG.samp 2>&1 &
SM=$!
[ -n "${PSTAT:-}" ] && ( st=${PSTAT%,*}; se=${PSTAT#*,}; sleep $st; perf stat -x, -C 0 -o $LOG.pstat -e cycles:u,cycles:k,instructions:u,instructions:k -- sleep $se ) &
for pf in ${PERF//+/ }; do ( sleep ${pf%,*}; perf record -q -C 0 -g -F 1999 -o /tmp/$L.p${pf%,*}.data -- sleep ${pf#*,} >/dev/null 2>&1 ) & done
[ -n "${BURN:-}" ] && { systemctl stop burn 2>/dev/null; systemctl reset-failed burn 2>/dev/null; systemd-run -q --unit=burn -p CPUAffinity=0 python3 $( if [ -n "${BCPU:-}" ]; then echo "$X/burn3.py $BURN ${RBURN:-$BURN} ${RAMP:-60}"; elif [ -n "${RBURN:-}" ]; then echo "$X/burn2.py $BURN $RBURN ${RAMP:-60}"; else echo "/root/objc2/burn.py $BURN"; fi ); }
R=$(echo -1000 > /proc/self/oom_score_adj; UL_TS=$LOG.ts UL_DST=$SIP UL_SRC=10.77.1.2 taskset -c 1-7 $X/bin/ul3 2>$LOG.ulerr 3000 $U 1 ${RAMP:-60} $M $W/seed/tokens.json $SP 7)
kill $SM 2>/dev/null; systemctl stop burn 2>/dev/null
peak=$(( $(cat $CG/memory.peak 2>/dev/null || echo 0) >>20 ))
python3 $X/kts.py > $LOG.kt1
grep -E "^(thp_fault_alloc|thp_fault_fallback|compact_stall|compact_fail|compact_success|allocstall_normal|allocstall_movable|pgscan_direct) " /proc/vmstat > $LOG.vm1
echo "  vm: $(paste $LOG.vm0 $LOG.vm1 | awk '{printf "%s=%d ", $1, $4-$2}')"
echo "$L b=$BLD u=$U $(echo "$R" | tr '\n' ' ') peak=${peak}M"
grep -E "errkinds|inflight" $LOG.ulerr | sed "s/^/  /"
echo "  unit: $(systemctl show -p ActiveState -p ExecMainStatus -p ExecMainCode -p MainPID -p Result --value objsrv | tr '\n' ' ') memory.events: $(cat $CG/memory.events 2>/dev/null | tr '\n' ' ')"
journalctl -u objsrv --since "-$((H+300))s" -q --no-pager > $LOG.jrn
dmesg | grep -iE "oom|killed|out of memory|page allocation|tcp:" | head -8 | cut -c1-200 | sed 's/^/  dmesg: /'
systemctl stop objsrv hog 2>/dev/null; [ -f $LOG.hog ] && sed "s/^/  /" $LOG.hog
tc qdisc del dev veth0 root 2>/dev/null
python3 $X/sum.py $LOG $H
