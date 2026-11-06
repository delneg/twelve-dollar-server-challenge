#!/usr/bin/env bash
# ns.sh [up|down]: server netns "srvns" for harness v2. Created after tcp_child_ehash_entries=16384, so its established hash
# has the bucket count of a 2 GB box. veth pair: veth0 (root, 10.77.0.1 + client sources 10.77.1.2-33) <-> veth1 (srvns,
# 10.77.0.2). RPS: srvns rx on CPU0 (the server CPU pays its TCP receive in softirq, as on a 1-vCPU droplet), root rx on CPUs 1-7.
set -e
if [ "${1:-up}" = down ]; then ip netns del srvns 2>/dev/null || true; ip link del veth0 2>/dev/null || true; exit 0; fi
ip netns list | grep -q "^srvns" && { echo "srvns already up"; exit 0; }
sysctl -qw net.ipv4.tcp_child_ehash_entries=16384
ip netns add srvns
sysctl -qw net.ipv4.tcp_child_ehash_entries=0
ip link add veth0 type veth peer name veth1
ip link set veth1 netns srvns
ip addr add 10.77.0.1/16 dev veth0
for i in $(seq 2 33); do ip addr add 10.77.1.$i/16 dev veth0; done
ip link set veth0 up
# the load generator side is untracked, like lo (Docker NAT rules turn conntrack on in the root namespace): its
# entries (~67 MB at 220k) would take memory a droplet never spends on its clients
iptables -t raw -C PREROUTING -i veth0 -j NOTRACK 2>/dev/null || iptables -t raw -A PREROUTING -i veth0 -j NOTRACK
iptables -t raw -C OUTPUT -o veth0 -j NOTRACK 2>/dev/null || iptables -t raw -A OUTPUT -o veth0 -j NOTRACK
ip netns exec srvns ip addr add 10.77.0.2/16 dev veth1
ip netns exec srvns ip link set veth1 up
ip netns exec srvns ip link set lo up
echo fe > /sys/class/net/veth0/queues/rx-0/rps_cpus
ip netns exec srvns sh -c "echo 1 > /sys/class/net/veth1/queues/rx-0/rps_cpus; cat /sys/class/net/veth1/queues/rx-0/rps_cpus"
ip netns exec srvns sysctl net.ipv4.tcp_max_syn_backlog net.core.somaxconn net.ipv4.tcp_rmem net.ipv4.tcp_wmem net.ipv4.ip_local_port_range
