#!/usr/bin/env bash
# ci.sh <srcdir> <label>: clean ubuntu:24.04, 1 CPU, 2 GB: install.sh as root, then test/run.sh (build + 42 tests) as user deploy.
S=$1; L=$2; C=/root/objc3/ci/$L; rm -rf $C; mkdir -p $C/submissions/objc-uring-delneg $C/seed
cp -r $S/. $C/submissions/objc-uring-delneg/; cp -r /root/objc2/repo/test $C/; cp /root/watlab/seed/feed.db $C/seed/feed.db
chmod -R a+rX $C; chown -R 1500:1500 $C
docker run --rm --name ci-$L --cpus=1 --memory=2g --memory-swap=2g --security-opt seccomp=unconfined --ulimit memlock=8388608 -v $C:/w ubuntu:24.04 bash -c '
set -e; cd /w; useradd -u 1500 -m deploy
export DEBIAN_FRONTEND=noninteractive
t0=$(date +%s); bash submissions/objc-uring-delneg/install.sh > /w/install.log 2>&1; t1=$(date +%s); echo "install.sh $((t1-t0)) s"
which curl jq >/dev/null 2>&1 || { apt-get install -y -q curl jq >/dev/null 2>&1; }
su deploy -c "cd /w && bash test/run.sh submissions/objc-uring-delneg" > /w/run.log 2>&1 && rc=0 || rc=$?; t2=$(date +%s); echo "run.sh $((t2-t1)) s rc=$rc"; tail -2 /w/run.log; kernel=$(uname -r); echo kernel $kernel; cat /etc/os-release | head -1'
