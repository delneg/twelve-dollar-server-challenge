#!/usr/bin/env bash
# island.sh up|down: on the rig, CPU 0 plays the 1-vCPU box. Everything the box's kernel does for the server runs on it:
# unbound workqueues (writeback, ext4), jbd2, kswapd, kcompactd, RCU threads, disk IRQs. Our shells and the generator stay off it.
case "${1:-up}" in
up) M=1; UM="1-7";;
down) M=ff; UM="";;
esac
echo $M > /sys/devices/virtual/workqueue/cpumask
for pat in kswapd0 'jbd2/' kcompactd0 khugepaged rcu_preempt rcu_exp rcub rcu_tasks oom_reaper 'ext4-rsv' writeback 'kworker/u'; do
  for p in $(pgrep -f "^\[?$pat" 2>/dev/null); do taskset -p $M $p >/dev/null 2>&1; done
done
for p in $(ps -eo pid,comm | awk '$2 ~ /^(kswapd0|jbd2|kcompactd0|khugepaged|rcu_preempt|rcu_exp_gp_kthr|rcu_exp_par_gp_|oom_reaper)/{print $1}'); do taskset -p $M $p >/dev/null 2>&1; done
for i in $(grep -E 'virtio[0-9]+-(request|config)' /proc/interrupts | awk -F: '{print $1}'); do echo $M > /proc/irq/$i/smp_affinity 2>/dev/null; done
if [ "$1" = up ]; then systemctl set-property --runtime user.slice AllowedCPUs=1-7; else systemctl set-property --runtime user.slice AllowedCPUs=; fi
echo "island $1: wq $(cat /sys/devices/virtual/workqueue/cpumask) kswapd0 $(taskset -p $(pgrep -x kswapd0) | awk '{print $NF}') user.slice $(systemctl show -p AllowedCPUs --value user.slice)"
