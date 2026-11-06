# ObjC #154: B-tree reads on stock SQLite (worklog, 2026-10-31)

Local numbers only (Synteq 8 vCPU Xeon 8260 box, kernel 6.8.0-147; server in Docker on 1 CPU / 2 GB, kload
`bench/load.js` model on 4 other CPUs, host network). Not the organizer's numbers.

## Setup

- Head `d02f205` (SQLite patch removed). On this box's 6.8.0-147 kernel it exited at startup:
  `setup_buf_ring: Invalid argument` (Ubuntu's inverted reserved-words check, Launchpad #2162843; the grader ran
  6.8.0-117). Fix `39bcc9d`: register the buffer ring by hand and retry with `resv[0]=1`. 42/42.
- Builds on the box host with the pinned clang 23.1.4 (official tarball, sha256 checked), libobjc2/liburing reused from
  the earlier build; the server runs in `ubuntu:24.04` with the build mounted.

## Study: the B-tree entries (#217 C, #191 V, #224 Mojo, #153 Lean)

- All compile a C file that `#include`s the stock amalgamation, so SQLite's file-private B-tree calls
  (`sqlite3BtreeCursor`, `TableMoveto`, `IndexMoveto`, `First/Last/Next/Previous`, `PayloadFetch`) are reachable.
  Root pages come from `sqlite_schema` at startup; #217 also checks the schema cookie per call.
- #191/#217 (no invariants): walk `posts_created_at_id_idx` from the first entry (it is DESC, so newest first) for
  the 20 ids, one `likes_post_id_idx` range scan for the like counts when the ids sit in a narrow span, then a rowid
  seek per post (#191 steps back with `Previous` when ids are consecutive) and a rowid seek into `users`.
- #224/#153: the feed is the last 20 rows of `posts` by rowid when ids are dense and `created_at` never decreases with
  id; checked at startup with SQL and after every insert; a boolean flips to the SQL plan for good if a check fails.
  #224 opens cursors once per batch.
- Reads run in the batch's transaction; #191/#224 call `sqlite3BtreeBeginTrans(bt, 0, 0)` themselves.
- Other ideas: #224 VFS `unix-excl` (WAL index in heap), checkpoint thread; one-shot accepts (#224) instead of multishot.

## Experiments

| # | Change | Result | Kept |
|---|---|---|---|
| 0 | Baseline `d02f205` + buffer-ring fix (A1), 300k users × 3 | 28.70 / 29.14 / 27.54 µs/req, CPU 88–92 %, 31.3–31.8k req/s; pass 2 of 3 (one p95 717 ms) | — |
| 1 | B-tree reads for `/feed` and `/posts/:id` (`src/td_btree.c`, B1) | wrk `/feed` 23.1–25.9 → 18.3–19.8 µs/req (−21 %), instructions 127k → 85k; `/posts/:id` 10.2 → 9.55 µs (−6.5 %), 29.1k → 25.9k ins. Diff test vs `--btree=0`: 711 requests (reads, 404s, writes, likes, unicode/escapes), 0 differences | yes |
| 2 | Write-only holds: during a WAL hold, reads are answered; a write and every later request on its connection stay at the front of the batch (B3) | 300k users: hold p95 19.8 / 23.4 ms, p99 47.9 / 56.1 ms against B1 62.2 / 288.8 ms; CPU 27.06 / 27.47 µs/req against 26.77 (+1–2 %, batches after a hold are smaller). Diff test with `--hold=8` (214 holds), pipelined writes and `Connection: close`: 0 differences in 1,191 requests | yes |
| 3 | WAL hold at 16,384 frames instead of 8,192 (`--hold=16384`) | 300k users: 28.43 / 26.47 µs/req against 26.32 / 27.69 (noise is about ±3 % between runs); p99 75 / 54 ms against 51 / 69 | no |
| 4 | WAL hold at 4,096 frames (`--hold=4096`) | 300k users: 26.29 / 26.46 µs/req against 26.11; p99 50 / 50 against 54 ms | no |
| 5 | SQLite page cache 16 MB instead of 32 MB (`--cache=16384`; smaller hash for SQLite's per-commit truncate scan) | 300k users: 26.51 / 26.17 µs/req against 25.81 | no |
| 6 | Batching window 2 ms / 256 completions instead of 1 ms / 128 (`--bwait=2000 --batch=256`) | 300k users: 28.09 / 27.52 µs/req against 27.28 / 26.98; p99 71 / 93 against 43 / 56 ms | no |
| 7 | Read cursors stay open for the batch (closed before COMMIT); the newest post found with `sqlite3BtreeLast` (B5) | wrk: `/feed` 85.4k → 84.8k instructions, `GET /posts/:id` 26.0k → 22.9k (−12 %); cycles too noisy to read. Diff tests 0/1,191 (also with `--hold=8`) | pending |

Profile of B1 `/feed` (wrk): kernel 57 %, server 41 %. Kernel `clear_page_erms` 4.2 % is `tcp_sendmsg` →
`skb_page_frag_refill` (Ubuntu zeroes allocated pages, init_on_alloc): scales with bytes sent.

E1 with the bench/load.js model, 300k users, interleaved: B1 26.54, 26.04 µs/req (CPU 84 %, 82.5 %; p99 268, 171 ms)
against A1 28.49 (90 %, p99 272 ms). Kept.

Hold log (`TD_CKLOG=1`, B2 = B1 + counters), 300k users: 102 WAL holds in ~4.5 min, one every ~2.7 s, each stopping
every request: p50 51 ms, p90 116 ms, max 206 ms, plus one 250 ms timeout. 1,083 receives ended in `ENOBUFS`.

From E3 on the test server runs with `--security-opt apparmor=unconfined`: Docker's default AppArmor profile put
`apparmor_socket_sendmsg/recvmsg` and `aa_inet_msg_perm` at 4.4 % of CPU, which a plain systemd service (the grader)
does not pay. B3 went from 27.06 / 27.47 to 26.32 / 27.69 µs/req.

Profile of B3 at 300k users (perf, 15 s of the hold): kernel 72 %, server 24 %, libc 4 %. Top server symbols: feed 6.4 %,
feed JSON 2.4 %, `sqlite3BtreeIndexMoveto` 1.9 %, `GET /posts/:id` 1.8 %, `TableMoveto` 1.6 %, `onCQE` 1.6 %,
`pcache1TruncateUnsafe` 1.4 %, flush 1.3 %; libc `memmove` 2.4 %. Kernel: `__inet_lookup_established` 6.0 % (600k
loopback sockets in the established hash), TCP send/ack. `pcache1TruncateUnsafe` is SQLite's own commit path
(`pager_end_transaction` → `sqlite3PcacheTruncate`): after `balance_nonroot` rekeys a page to the pending-byte page
number, the next commit scans the whole page-cache hash. That is what the removed patch avoided; stock SQLite pays it.

Line profile of `/feed` (debug-info build, saturated wrk): inside the feed function, SQLite's `sqlite3BtreeTableMoveto`
binary search (findCell + varint key per probe) is ~19 % plus ~9 % varint decoding: 20 author seeks into `users` per
feed, each from the root (TableMoveto only shortcuts the same key, the next key, or a cursor at the last row). JSON
output (`feed_sink`) 10 % of the samples, mostly the 8-byte escape scan. `likes_count` and `getCellInfo` (SQLite's cell
parse per index entry) about 9 %.

Validation of `1fe6941` (B3): 300k users with a 5-minute hold pass (26.54 µs/req, 84 % CPU, p95 20 ms, p99 52 ms,
0 failed). 340k (2-minute hold) pass at 95 % CPU, p95 346 / p99 502 ms. 370k fail at 98.5 % CPU (p95 2.1 s), 0 errors and
no refused connections: the local knee is CPU, about 340k on this box.

## E8: JSON escaper that copies while it scans (B6 = B5 + E8), dropped

`td_buf_json_str` tested 8 bytes, then copied runs with `td_buf_put`; B6 stores each clean 8-byte block as it tests it
and goes byte by byte through a block that holds an escape. Same output as the old escaper on 2M random strings (ASan
clean), 42/42, B-tree vs SQL diff 0 of 1,191 (difftest now also sends control bytes and many quotes). wrk `/feed`,
4 interleaved rounds: B5 84.59–84.65k instructions per request, B6 86.17–86.28k (+1.9 %); cycles noisy (52.8–58.3k
vs 54.7–59.7k). The stores per block cost more than the one `memcpy` per run. Reverted.

Not tried, rule question for Opt: the `pcache1TruncateUnsafe` cost (1.4 %) could be avoided without editing SQLite
through `sqlite3_config(SQLITE_CONFIG_PCACHE2)`, a wrapper around pcache1 that parks `balance_nonroot`'s swap page
under a free key just above the cache's largest key instead of the pending-byte page number. It is public API, but
it does what the removed patch did, and #158 lost its row over a pcache change, so it needs Arjay's word first.

## E7 result: users A/B, B5 vs B3 (300k users, 2 interleaved pairs)

B5 24.27 / 25.95 µs/req (CPU 77.0 / 82.3 %, hold p99 42 / 120 ms), B3 25.09 / 25.75 (79.6 / 81.7 %, 47 / 93 ms). Mean
−1.2 %, inside the run-to-run noise; instructions say B5 is cheaper (`GET /posts/:id` −12 %, `/feed` −0.7 %). Kept, and
measured again together with E9 (chain-u8, 3 more pairs against B3).

## E9: header end found by stepping memchr('\r') instead of memmem (B7 = B5 + E9)

`memmem(p, n, "\r\n\r\n", 4)` was 0.65 % of the 300k profile (glibc's general memmem). `find_crlf2` jumps from one
`\r` to the next with memchr and checks the next three bytes: the same first match (5M random strings against memmem,
ASan clean). 42/42, diff 0 of 1,191. wrk `GET /posts/1`, 4 rounds: B5 23.24–23.26k instructions, B7 23.03–23.07k
(−0.9 %); cycles noisy. Kept.

## E7+E9 users A/B (chain-u8, 300k users, 3 interleaved pairs, B7 vs B3)

B3 27.24 / 27.90 / 26.23 µs/req (hold p95 27.0 / 22.1 / 16.5 ms, p99 83 / 47 / 48 ms); B7 27.36 / 26.96 / 25.85
(p95 29.1 / 21.1 / 15.8, p99 74 / 66 / 60). Mean −1.5 % CPU, tails mixed. The box drifted ~2 µs between chains, more
than the effect. `--cache` (E5's flag) taken out again; it was never kept.

From 1 Nov the bar for a push (Opt): clearly lower p95/p99 at load, with at most ~2 % more µs/req, over 2+ runs, 42/42.
So the next A/B runs at the knee (340k), where the tails come from CPU.

## E10: 340k A/B, B8 (= 4b18d6d: E7 + E9) vs B3 (1fe6941); E11: checkpoint pass every 2000 frames (`--ckpt=2000`)

At 340k the run is a closed loop at the CPU limit: response time is about N/X − think time, so a 1 % capacity shortfall
turns straight into latency and runs come out bimodal (they keep up or tip over). Hold p95/p99 ms:

| run | B3 | B8 | B8 `--ckpt=2000` |
|---|---|---|---|
| 1 | 26.19 µs, 93.5 %, 416 / 766 | 25.16 µs, 90.4 %, 32 / 95 | 23.78 µs, 85.5 %, 31 / 144 |
| 2 | 26.60 µs, 95.0 %, 140 / 545 | 26.12 µs, 91.9 %, 979 / 3,064 FAIL (steal 74 ticks vs ~35) | 25.47 µs, 91.3 %, 183 / 338 |
| 3 | | 26.35 µs, 93.5 %, 553 / 1,028 | |
| 4 | | 25.87 µs, 92.8 %, 130 / 222 | |

TCP retransmits and timeouts are the same in every run (10–20 SYN retransmits). `--ckpt` is the number of new WAL frames
between passive checkpoint requests to the checkpointer process; 250 was chosen when checkpoints still ran inside the
loop (shorter stalls). In the child, each pass costs a WAL read, page writes and two fsyncs in the same 1-CPU cgroup,
and a longer interval copies a hot page once instead of once per pass. chain-u11: `--ckpt=4000`, B8, `--ckpt=2000`.

## E11 result and push: fac5f79 (B9 = B8 + `--ckpass=2000`)

More 340k runs: B8 26.10 / 25.84 (p95 432 / 35); `--ckpt=2000` 24.88 / 26.26 (p95 88 / 440); `--ckpt=4000` 24.37 /
25.73 (p95 41 / 202). The interval became its own flag, `--ckpass` (default 2000); `--ckpt` stays 250 for the inline
fallback. 42/42; diff 0 of 1,191, also with `--hold=40` (54 holds).

Final interleaved A/B at 340k (chain-u12), B9 vs B3 (`1fe6941`, the pushed head), µs/req, CPU, hold p95 / p99 ms:
pair 1 25.50, 91.6 %, 41 / 117 vs 27.01, 96.9 %, 61 / 165; pair 2 26.42, 94.9 %, 55 / 112 vs 27.33, 97.9 %, 161 / 251
(steal 98 ticks in that B3 run); pair 3 25.53, 91.7 %, 52 / 212 vs 26.97, 96.8 %, 71 / 216. All pass. Lower p95 and p99
in every pair and −4.7 % µs/req: meets the 1 Nov bar. Pushed.

## 1 Nov, after the merge: the accept side (PR #154 scored 280k at 1fe6941; 290k/300k/320k: ~77 % "connection refused")

What the grader's notes say: the same ~77 % refused shape was seen for 10067c1 at 260k+, and for asm #207 it came with
"app cgroup memory down to 0", i.e. the app died; a dead process leaves no listener, so every later connect is refused.
~77 % errors is what a 60 s ramp + 120 s probe gives if serving stops near the end of the ramp. Mojo #224 passed a 290k
probe, so the load side reaches past 290k; #233 (790 MB) and Lean #153 also collapse to refused at 280k+.

Checked on Synteq (fac5f79 + TD_STATS diagnostics, B10):
- RLIMIT_NOFILE: main() raises soft to hard; under systemd LimitNOFILE=65535 that is 65,535 = slots per ring (the
  kernel caps a fixed-file table at RLIMIT_NOFILE). Accepted sockets never enter the fd table.
- listen(65535) is capped by somaxconn (4096 stock).
- 8 rings × 65,535 fixed files, high-water 61,439 per ring: 340k uses 6 rings.
- CQ 16,384 entries per ring: at 340k no pass ever saw IORING_SQ_CQ_OVERFLOW, 0 CQEs dropped.
- Accept errors at 340k: 5, all -ECANCELED (the high-water hand-off); multishot accept is re-armed after any ending.
- No conntrack on the grader (no UFW); the server uses no ephemeral ports.
- 300k, 340k pass locally; 370k fails on CPU with 0 errors. The refused collapse does not reproduce on this 16 GB box.

Memory at 340k users (MEMSTAT, 30 s into the hold): app cgroup anon 168 MB, file 50 MB, kernel 552 MB (slab 545:
sock_inode, dentry, filp, io_kiocb). Outside the cgroup the kernel's TCP slab holds 768 MB for the 340k server
sockets (2.26 KB each; on 6.8 an accepted socket's tcp_sock is allocated in softirq and not charged to the app).
Idle-connection test (100k): our process adds 78 B per idle connection (TDConn) and ~130 B after the first request.
On the 2 GB grader the app peaked at 1,245 MB at 280k; with ~2.3 KB/connection of TCP slab outside the cgroup that is
about the whole box, so the likely death is the global OOM killer (largest process) or our own exit(1) on an
io_uring wait error (-EBADR when the kernel cannot queue overflowing CQEs, -ENOMEM). Not confirmed: needs the grader's
kernel log / memory.stat for a 290k probe.

E12: io_uring wait errors are no longer fatal (logged; back-off 1 ms if they repeat). TD_STATS=1 prints a line a
second: live connections, accept errors, CQ-overflow passes, dropped CQEs, ENOBUFS.

E13: SQLite's pcache1 behind SQLITE_CONFIG_PCACHE2 (allowed since 31 Oct, like #233): skips xTruncate when no page at or
past the limit can be cached (balance_nonroot's pending-byte parking otherwise makes the next commit walk the whole
hash). TD_PCHECK=1 verifies every skip against pcache1's hash: a 150k users run never aborted, integrity_check ok
(514k posts, 2.1M likes); diff vs --pcache=0 0 of 1,191. Write-only wrk (70 % likes, 30 % creates): with the old PGO
profile no gain (the profile's indirect-call promotion inlined pcache1Fetch; the wrapper's fetch went out of line,
pcache1Fetch 4.4 % against pcache1TruncateUnsafe 5.3 % without the wrapper); trained with the wrapper (pgo/train.m
installs it), 4 rounds: 20.5 / 19.9 / 18.0 / 20.2 µs/req against 20.1 / 20.2 / 21.6 / 22.5 with --pcache=0.

E12+E13 at 340k (B12 = fac5f79 + E12 + E13, PGO trained with the wrapper), interleaved with B9 (fac5f79), µs/req, CPU,
hold p95 / p99 ms: B12 25.42, 90.7 %, 346 / 553; 24.75, 88.0 %, 528 / 1,155 FAIL; 26.26, 93.6 %, 403 / 791. B9 25.35,
90.8 %, 127 / 399; 25.44, 91.3 %, 92 / 222; 26.23, 93.8 %, 218 / 350. Same binary, wrapper off (`--pcache=0`, B12n) vs on:
off 26.48, 95.0 %, 97 / 173 and 25.51, 91.4 %, 179 / 297; on 25.61, 88.8 %, 1,630 / 5,538 FAIL (cgroup 737 → 1,061 MB
2.5 min in with RSS flat: socket buffers piling up behind a stall). Four wrapper runs, all with worse tails, two fails;
CPU per request the same. Cause not found (every skipped truncate verified at 150k). E13 dropped; E12 kept (no effect
locally, it only matters when the kernel reports an error).
Fifth wrapper run (u14-b12-340k-2): 24.75 µs, 88.9 %, 42 / 210, pass. Wrapper: 2 fails in 5; without: 0 in 5.

## 5 Nov (ObjcTop): C0, C1: calibration, and the refusals reproduced (`/root/objc2/hs.sh` on Synteq)

Harness: Synteq 8 vCPU, kernel 6.8.0-147, the WAT agents' netns + veth + RPS (server softirq on CPU 0), systemd unit as `deploy`,
LimitNOFILE=65535, MemoryMax=1750M (the rig's cap, LEADERBOARD note ᵐ), CPU 0, no Restart, fresh seed DB, tcp_mem 45000/60000/90000,
memory hog leaving SRVMB plus the load generator's own 11.5 KB/user, ul2 (`bench/load.js` model) on CPUs 1-7, 60 s ramp + 120 s.
ul2 plateau rate matches the scorer: the scorer's 26,741 req/s is averaged with the ramp in; 79 % CPU / 27 µs = ~29.3k req/s, here 29.6k.

| run | SRVMB | users | result |
|---|---|---|---|
| base (`1fe6941`) | 1900 | 280k | pass, p95 7.6 / p99 26, app 71 % of CPU 0 |
| base | 1900 | 290k | pass, p95 75 / p99 510 |
| base | 1900 | 300k | **OOM-killed**, 63 % errors, 2.78M connect errors |
| head (`6b50ca8`) | 1900 | 300k × 2 | **OOM-killed at the end of the ramp**, 99.6-99.9 % errors |
| base | 1850 | 280k | pass, p95 10.5 / p99 33 |
| base | 1850 | 290k | **OOM-killed**, 98.7 % errors |

**SRVMB=1850 reproduces the scorer's cliff: 280k holds, 290k dies with connection refused.** Latency here is lower than the
scorer's (p95 10 vs 35, p99 33 vs 227) and the app takes 71-73 % of CPU against 79 %, so CPU is ~8 % generous.

Proven cause on this harness: **the global OOM killer** (`oom-kill:constraint=CONSTRAINT_NONE ... global_oom`, `Out of memory: Killed
process (server)`, unit Result=oom-kill). Not our exit, not an accept failure, not a stall. Timeline in the 300k base run: file
cache gone (cgroup file 10 MB), loop in D state reading the DB, requests queue in sockets (cgroup sock 222 → 904 MB in 12 s, TCP
57k → 217k pages), RSS 206 → 260 MB, kill. Kernel memory per server socket from slabtop: TCP 2.31 + sock_inode 0.81 + filp 0.25 +
dentry 0.19 + io_kiocb 0.25 + others ~0.15 KB = ~4 KB; 1.2 GB at 300k, which leaves very little on a 1,967 MB box.

Amplifier seen with TD_STATS: ENOBUFS endings 592 until 218k users, then 76,820 at t=53 s, 572,899 at t=59, 1,207,438 at t=65,
1,821,603 at t=71, CQ overflows in 109 passes. That is the re-arm storm #245 and #264 describe. Every re-arm finds its request
queued and no buffer, and ends again.

## E14-E18: bounded re-arm + 6 KB buffers + locked code (v1), then SQLite page cache (5 Nov, SRVMB=1850, 2 min)

v1 = head + `--rearm=1024` FIFO per ring + output buffers 16 KB → 6 KB, free list 8,192 + CQ 65,536 + mlock of code. All at 300k:
| run | result |
|---|---|
| v1 | OOM-killed at ~130 s, 75 % errors; 2nd run 84 % |
| v1 `--rearm=0` | OOM-killed, 48 % errors |
| v1 at 320k | OOM-killed in the ramp |
| v1 `--cache=128000` | **alive**, p95/p99 5,320 (bad stall at the end of the ramp, WAL 113 MB, then clean), rdMB 227 |
| v1 `--cache=256000` | **pass, p95 13 / p99 143**, rdMB 13, majf 212, 73 % of CPU 0, RSS 163 MB, peak 1,629 MB |

Bounded re-arm alone doesn't save it: nobufs fell to 448k-834k and no CQ overflow, but the loop stays in D state reading the DB (rdMB 60-70 per 10 s) and sockets fill. With the hot set in SQLite's own page cache the loop stops reading the disk at the wall, and the spiral never starts. RSS stays low, so the cache limit isn't what costs memory: the touched set is about 100 MB. The kernel would evict its own copy of those pages anyway. Rule 5 allows SQLite's page cache by name.

## E19-E24: drain first, and what the loop actually reads (5 Nov, SRVMB=1850, 2 min)

v2 = v1 + 256 MB cache default + receive buffers 2 KB × 2,048 → 1 KB × 4,096 + at most 2,048 answers per pass, the rest parked as records.
320k: alive, 0 errors, TCP ≤ 4.5k pages, nobufs 0, but p50 2.3 s at 23k req/s, 170k parked: each pass walked every parked request and
dirty connection, a cost I added. 340k: OOM-killed, and a build ran during it, so void.
Don't build on Synteq during a run: the hog leaves no memory for it (linker OOM-killed, plain non-PGO binary, runs disturbed).

v3 = v2, parked requests untouched: `reqs[reqHead..nreq)` FIFO, a pass looks at most at what it answers, per-connection `queued` count replaces `inBatch`, arena kept until the queue is empty (compacted past 16 MB). 42/42.
| run | result |
|---|---|
| v3 300k | **p95 12.2 / p99 34**, peak 1,334 MB, RSS 138 MB, 77 % of CPU 0, rdMB 23 |
| v3 320k (with bpftrace attached, so not a fair run) | alive, 0 errors, p50 750 ms, WAL grew to ~1 GB |

Page-cache trace (`mm_filemap_add_to_page_cache` by pid and inode, 15 s): main process 7,291 pages of **feed.db-wal**, 16 of -shm, 0 of feed.db; checkpointer child 1,034 of feed.db. So with the larger SQLite cache the loop no longer reads the DB; it reads the WAL. Reason: WAL frames are 4,120 bytes, not page-aligned, and the file is overwritten from the start after a rewind. Overwriting part of a file page that reclaim evicted makes the kernel read it from disk first, on the loop (D state). New: H13 `PRAGMA journal_size_limit=0`.

## C2: CPU handicap; consult (5 Nov)

`BURN=8` (a process taking 8 % of CPU 0 in 20 ms cycles) on `1fe6941` at 280k, SRVMB=1850: p95 37.8 / p99 350, app 75 %, against the scorer's 35 / 227, 79 %. Calibration from here: **SRVMB=1850 BURN=8**. 290k dies with or without it.

Consult 1105-144212-3a93 (DeepSeek; Gemini was overloaded), what I did with it:
- "tcp_mem is per netns": checked, wrong. `/proc/sys/net/ipv4/tcp_mem` doesn't exist inside srvns; the limit is global.
- "Drop WAL, journal_mode=MEMORY, synchronous=OFF": rule 6 forbids it.
- `locking_mode=EXCLUSIVE`: rejected, the checkpointer child needs shared memory (and the README's A/B: 11.56 vs 11.54-11.60 µs/req).
- `mmap_size` instead of page cache: rejected. Mapped pages are reclaimable, which is exactly the major-fault stall at the wall; also measured slower.
- Kept: the harness is fitted at one point, so trust A/B deltas more than absolute numbers (agrees with WAT: 260k local → 220k scored); watch the 5-minute hold for page-cache growth; target rdMB ≈ 0 over the full hold; hold under slower I/O as well (add an IOPS cap run); count TIME_WAIT (0 here: no reconnects).

## E25: 5-minute hold, v4 at 320k (no burner)

p95 840 / p99 995, 0 errors, peak 1,318 MB, app 89 % of CPU 0. Memory flat (avail ~720 MB), WAL 2-32 MB, TCP ≤ 2k pages except one 4.4k. Latency and CPU rise through the hold: 85 % in the first minutes, 92 % at the end, rdMB per 10 s from 0 to ~12, maxBatch from ~1k to 27k. So a slow slide: the loop reads ~1.2 MB/s, likely cold index leaves (writes land on random leaves of `sqlite_autoindex_likes_1` 29.5 MB and `posts_user_id_idx` 5.3 MB, authors on `users` 2.5 MB) now that the file cache is gone, and kswapd scans ~5k pages/s.
v5: read those three at startup into the page cache (`--warm`), and the checkpointer drops feed.db's clean file pages after each pass (`posix_fadvise DONTNEED`, `--ckdrop`), on a descriptor it keeps open because closing one would drop its POSIX locks.

## E26-E30: v5 under the calibrated burner (5 Nov, SRVMB=1850 BURN=8)

| run | result |
|---|---|
| v4 330k, 5 min, no burner | pass, p95 665 / p99 970, 94 % of CPU 0, no OOM |
| v4 320k, 5 min, BURN=8 | **fail on p99** 1,315 (p95 910), 0 errors, CPU 0 busy 99 % |
| v5 320k, 2 min | fail, p95 1,240 / p99 1,390, 0 errors |
| v5 340k, 2 min | fail, p95 2,210 / p99 2,395, 0 errors, no OOM, peak 1,826 MB, MemoryMax reached (memory.events max 21,987) |
| v5 310k, 2 min | pass, p95 305 / p99 465 |
| v5 310k, 5 min | **fail**, p95 1,255 / p99 1,530, 30.6k req/s of ~32.8k asked, 86 % + burner, CPU 0 busy 95 % |

No deaths and no errors from 310k to 340k now: **the limit moved from memory to CPU**. On this harness at BURN=8 the knee is near 300-310k. `miss` in TD_STATS stays at 9,507 through the hold, so SQLite's page cache misses nothing; cache grows ~0.2 MB/s from new rows (112 MB after 5 min).

Clean install (opt-box docker `ubuntu:24.04`, --cpus=1 --memory=2g, kernel 7.0, head `921063f`): install.sh 69 s, test/run.sh 61 s including the PGO build, 42/42.
Profile at 310k (perf on CPU 0, 15 s): server kernel 69 %, server user 20 %, libc 2.4 %, burner 6 %. `__inet_lookup_established` 14 % (310k sockets in 16k buckets, the 2 GB box's size, not ours to tune), post_view 6 %, feed 2.8 %, AppArmor send hooks 2.8 %.

## E31-E37: v6 and v7 at 300k and 310k, SRVMB=1850 BURN=8 (5 Nov)

| run | users | rps | p50 | p95 | p99 | note |
|---|---|---:|---:|---:|---:|---|
| v6 | 300k | 31,404 | 11 | 355 | 670 | traced, unfair |
| v6 `--answers=0` | 300k | 31,490 | 8 | 225 | 440 | max 2,939, RSS 220 MB |
| v6 `--cknice=10` | 300k | 31,652 | 6 | 35 | 158 | one good rep |
| v6 | 300k | 31,656 | 7.5 | 180 | 955 | |
| v6 `--ckdrop=0` | 300k | 31,655 | 5.9 | 151 | 245 | |
| base `1fe6941` | 280k | 4,702 | 1,370 | 5,320 | 5,320 | **collapsed**: global OOM again, the earlier 37.8/350 rep at BURN=8 is one side of its edge |

310k, MEAS=200, interleaved:

| run | rps | p50 | p95 | p99 | us/req | softirq % |
|---|---:|---:|---:|---:|---:|---:|
| v7 | 32,661 | 7.6 | 89 | 365 | 17.5 | 36 |
| v7 `--cknice=10` | 31,959 | 64 | 900 | 1,205 | 20.5 | 43 |
| v6 `--cknice=10` | 32,669 | 8.2 | 158 | 240 | 18.0 | 35 |
| v7 | 31,718 | 240 | 900 | 1,095 | 21.2 | 44 |
| v7 `--cknice=10` | 31,611 | 295 | 885 | 1,110 | 22.1 | 57 |
| v6 `--cknice=10` | 31,514 | 355 | 815 | 1,015 | 21.2 | 44 |

So 310k is **bistable**, whatever the variant. Same load, either p50 ~8 ms and ~17.5 us/req, or p50 240-355 ms and ~21 us/req. The nice value and v7's one-write commit don't decide it; at this rep count they can't be told apart. Differences above between variants aren't evidence.

## Bistability: late answers cost the kernel a bare ACK per request

v7 at 310k, counters from the server's netns over the hold:

| run | p50 | OutSegs/s | data segs/s | bare ACKs/s | DelayedACKs/s |
|---|---:|---:|---:|---:|---:|
| v7 | 12.8 | 77,449 | 62,274 | 15,175 | 4,786 |
| v7 | 47.8 | 81,858 | 61,066 | 20,792 | 2,945 |
| v8 fifo, 2,048 | 16.3 | 77,833 | 61,928 | 15,905 | 3,841 |
| v8 lifoage=300 | 390 | 86,573 | 60,602 | 25,971 | 4,923 |

bpftrace on `tcp_send_ack`, 8 s, FIFO 2,048: nearly all of them come from `tcp_rcv_established` (an immediate ACK when the request arrives), ~9,100 from the delayed-ACK timer. Reading of kernel logic, unverified against the 6.8 source on this box: a reply later than `ato` (40 ms) doesn't count as ping-pong, a missed delayed ACK leaves ping-pong mode, and a socket out of ping-pong with quick-ACK credit ACKs each request at once. So every late reply adds a packet out, and CPU per request rises about a fifth. Above ~83 % that extra cost keeps the backlog alive: one stall can flip the run.

That's a new reading of the limit: capacity with most answers under 40 ms is ~20 % above capacity with most over it.

v8: H15, newest first past one pass of backlog. First try, `--lifoage=300`, `--answers=512`: p50 390, p95 1,020, p99 1,215. Its age rule sends everything through the old end once a queue stands at 300 ms, so it behaves as FIFO with a floor. Next: a much longer age limit.

## E38-E60: newest first (v8-v10), SRVMB=1850 BURN=8 (5 Nov)

310k, MEAS=200:

| run | rps | p50 | p95 | p99 | us/req | bare ACKs/s |
|---|---:|---:|---:|---:|---:|---:|
| v8 `--lifoage=1000` | 32,731 | 9 | 27 | 215 | 17.9 | |
| v8 `--lifoage=1000 --answers=256` | 32,637 | 8 | 275 | 1,015 | 18.7 | |
| v8 `--lifoage=1000` | 31,910 | 17 | 1,160 | 1,380 | 19.8 | |
| v8 `--lifoage=1000 --answers=256` | 32,672 | 8 | 83 | 695 | 18.2 | |
| v9 `--lifoage=1000` | 32,752 | 8 | 27 | 390 | 17.7 | |
| v9 `--lifoage=1000` | 32,730 | 7 | 22 | 141 | 17.0 | |
| v9 `--lifoage=1000` | 32,678 | 9 | 75 | 1,045 | 18.9 | |
| v10 (old end gets half past 500 ms) | 32,625 | 9 | 400 | 1,015 | 18.1 | |
| v10 | 32,731 | 8 | 24 | 161 | 17.4 | |

Against FIFO at 310k (p50 8-355, p95 89-1,040): p50 stays low in every rep, the run no longer goes into the p50 240+ state, and bare ACKs fall to 2-4k/s from 15-26k/s. Steal is 0 in all of them, checked with the new sampler column. The child takes 3.4 % of the CPU.

v10 330k: p95 5,320, memory wall (file cache down to 1-5 MB, kswapd scan and major faults explode 3 minutes in, the WAL reaches 415 MB). Second rep 330k: p95 3,235, rps 32,797. 350k: dead. So on this harness the memory wall sits between 310k and 330k.

5-minute holds, v10:

| users | rps | p50 | p95 | p99 |
|---|---:|---:|---:|---:|
| 310k | 32,335 | 14 | 650 | 2,895 |
| 320k | 31,926 | 25 | 2,240 | 3,350 |
| 310k | 31,535 | 24 | 1,195 | 2,265 |
| 320k | 31,414 | 36 | 2,525 | 2,910 |

All fail. The queue is near zero for long stretches, then rises slowly over 10-20 s to 40-68k parked requests and falls again. That holds up to 20 % of users, so arrivals drop too.

Phase timing per second (TD_STATS): flush cost per answer tracks pass size, 9 us at ~190 passes/s, 12.5 us at ~55 passes/s. perf on CPU 0, in and out of a backlog, has the same shape (kernel 69 %, user 20 %), which reads as a uniform slowdown. My guess, unverified: passes of 512 answers walk more memory than the 1 MB L2. A/B on the pass size is queued.

## Page cache: PENDING_BYTE page forces full hash scans

`pcache1TruncateUnsafe` 1.5 % of CPU normally, 3.1 % in a backlog. bpftrace, about 8 s after the server started: ~30 calls/s from commits, 256-512 us each, `iMaxKey` 262,145, the pending-byte page: `balance_nonroot` parks a page under it, so the next commit scans the whole hash (this is the line #154's old SQLite patch changed). That patch is forbidden. The public `SQLITE_CONFIG_PCACHE2` is named as allowed in the leaderboard's rulings, so: `td_pcache` in td_btree.c, a dense array by key, a clock instead of an LRU list, truncate looks only at keys added above the last limit. Stress test against stock pcache1 under ASan and UBSan, 150-400 rounds of inserts, updates, deletes, rollbacks, VACUUM, checkpoint, reopen, at caches of 64 KB, 500 KB, 2 MB, 256 MB, WAL and rollback journal: same content hash, integrity_check ok, no sanitizer report.

## E61-E66: v11 and v12 (5 Nov, SRVMB=1850 BURN=8)

v11 310k, MEAS=200, A/B of pass size and page cache:

| flags | p50 | p95 | p99 | us/req |
|---|---:|---:|---:|---:|
| `--answers=512` | 6.6 | 24 | 480 | 17.59 |
| `--answers=128` | 3.8 | 23 | 220 | 17.58 |
| `--answers=64` | 2.2 | 565 | 935 | 17.80 |
| `--answers=512 --pcache=0` | 11 | 550 | 1,170 | 18.55 |
| `--answers=512` | 5.3 | 19 | 76 | 17.17 |
| `--answers=128` | 3.9 | 42 | 375 | 18.53 |
| `--answers=64` | 2.0 | 103 | 480 | 17.84 |
| `--answers=512 --pcache=0` | 8.7 | 38 | 530 | 18.00 |

H16: about 4 % less CPU per request, two pairs. H17: no clear gain from smaller passes; 512 stays.

5-minute holds:

| build | users | p50 | p95 | p99 | max | note |
|---|---:|---:|---:|---:|---:|---|
| v11 | 310k | 12 | 540 | 1,070 | 1,372 | fail |
| base | 280k | 3.6 | 54 | 315 | 2,027 | pass |
| v12 lifoage=400 | 310k | 6.5 | 1,010 | 1,945 | 2,415 | fail; quiet until t≈340, then kswapd scan, major faults, queue to 52k |
| v12 lifoage=250 | 310k | 5.9 | 103 | 235 | 609 | **pass**, one rep, 58.7 % cgroup CPU |
| v12 lifoage=400 | 320k | 22 | 1,190 | 1,495 | 1,801 | fail |

Consult 1105-192451-31a6 (DeepSeek): a fixed 1/8 share never shrinks a backlog, so the share now grows with age. Its claims on `msg_name` and registering files don't apply: sends have no address and sockets are direct descriptors.

Costs so far: DeepSeek consults $0.0193 + $0.0229 + $0.0126, a few `ds` searches at ~$0.00075 each (count not logged). About $0.06 of the $1 limit.

PR #326 opened on `197ad57` at Denis's order (5 Nov), before a repeated 310k hold. Clean install on that head: opt-box Docker `ubuntu:24.04`, 1 CPU, 2 GB, kernel 7.0: install.sh 71 s, 42/42. v10 at 350k: cgroup OOM kill of the server at MemoryMax=1750M, so the OOM limit moved up rather than away.

## E67-E71: holds after the PR (5 Nov, SRVMB=1850 BURN=8, MEAS=300)

| build | users | rps | p50 | p95 | p99 | max | result |
|---|---:|---:|---:|---:|---:|---:|---|
| v12 lifoage=400, bufs=4096 | 310k | 32,361 | 10 | 695 | 1,220 | 1,546 | fail; RSS 197 MB against 185 |
| v12 lifoage=250 | 320k | 32,734 | 235 | 1,205 | 1,375 | 1,643 | fail, stuck at FIFO at 250 ms, ~20 us/req |
| v12 lifoage=250 (head of #326) | 310k | 32,637 | 7 | 380 | 575 | 821 | **pass**, second of two |
| v13 cap 1/2 until 2×lifoage, 250 | 320k | 33,601 | 10 | 525 | 620 | 905 | p95 just over; first run that reaches the 320k rate |
| v13 250 | 310k | 32,601 | 8 | 515 | 725 | 994 | p95 just over |
| v13 300 | 320k | 33,362 | 16 | 715 | 1,370 | 1,792 | fail |

With all of a pass going to the old end at the age limit, a run past capacity stays in the slow state: all replies late, ~20 us/req, 32.7k req/s at most. v13 caps the old end at half up to twice the limit, keeps half the replies fast, and the queue empties again after each rise (peaks 37k, 16k, 8k, then back under 2k). p95 sits at about twice the limit, so a lower limit is next.

## E72-E76 (5 Nov, same harness, MEAS=300)

| build | users | rps | p50 | p95 | p99 | max | result |
|---|---:|---:|---:|---:|---:|---:|---|
| v13 250, `--cache=80000` | 310k | 32,109 | 15 | 925 | 1,050 | 1,283 | fail, the cache cap costs more than it saves (RSS 156 MB, 19.7 us/req) |
| v13 200 | 320k | 32,292 | 430 | 1,330 | 1,670 | 1,921 | fail, stuck: all of a pass goes to the old end at 400 ms |
| v14 150, seven eighths at 4×limit | 320k | 31,130 | 625 | 2,040 | 5,320 | 6,032 | fail |
| v14 200 | 320k | 32,824 | 22 | 1,065 | 1,705 | 1,949 | fail |
| v14 150 | 310k | 32,606 | 8 | 395 | 655 | 942 | pass |

Run-to-run spread is wide, so one rep of a setting says little. Not pushed: v13 and v14 aren't clearly better than the #326 head at 310k (103/235, 380/575). Three experiments left.

## E77-E80: last holds (5 Nov, same harness, MEAS=300)

E77 v14 150 at 330k: stopped early, void, and counted anyway.

| build | users | rps | p50 | p95 | p99 | max | result |
|---|---:|---:|---:|---:|---:|---:|---|
| v13 250 | 320k | 33,235 | 16 | 940 | 1,475 | 1,801 | fail |
| v13 225 | 320k | 33,570 | 10 | 500 | 905 | 1,170 | on the p95 limit (p95 must be under 500) |
| v13 225 | 310k | 32,728 | 5.7 | 53 | 425 | 704 | pass, 17.32 us/req |

v13 with 225 becomes the default. v14 dropped. At 320k reclaim starts near the end of a hold (scanK 370k and majf 6k per 44 s in the v14 150 run), so the memory wall is still close there.

## E80 and the scorer's result (5 Nov)

E80, head `2b4d6d8` (v15), 320k, 5 min: rps 32,676, p50 25, p95 1,135, p99 1,260, max 1,540. Fail. So v13 at 225 and 320k is one rep on the limit (500/905) and one clear fail. The 80 experiments are used up.

**Scorer on `197ad57` (arjaythedev on #326, 19:38Z):** held 280,000, 26,714 req/s, p95 189.9, p99 348.5, 0 errors, 79 % CPU, 27 us/req, 807 MB peak (was 1,245). Probes: 240k and 280k pass; 290k fails on p99 (1.05 s at 84 % CPU, 0 errors); 300k fails on p99 (2.33 s at 84 % CPU); 320k CPU-bound (97 %). Review clean, PCACHE2 and newest-first accepted. Tied the merged row, so not merged; the PR stays open and a version that beats 280,000 gets a re-run.

What that says about this harness: its OOM story held (memory down, no refusals, 0 errors at 290k and 300k), but it's too kind on tails. It passed that build at 310k twice, while the scorer fails it on p99 at 290k with CPU at 84 %, so the scorer's limit there isn't CPU saturation and something the rig doesn't reproduce holds requests back. Its p95 at 280k (190) is worse than #154's (35), and CPU per request at 280k didn't move (27 us both), against the ~5 % this rig showed for the page cache. Unverified guesses for the stall: slower disk on the VPS (WAL writes or dirty throttling blocking the loop, checkpoint holds), or the age rule itself. Next step would be to recalibrate on these points before any more tuning.

Not verified: whether `2b4d6d8`, pushed about a minute after that comment and without having read it, helps on the scorer at all.

## 6 Nov (ObjcTop2): rig v3 and the tail's real cause (Synteq `167.17.74.246`, `/root/objc3`, `hs.sh`, `ul3`)

Rig v3, what changed from `/root/objc2/hs.sh`:
- `ul3` (ul2 + k6's view): percentiles over the whole run (60 s ramp + hold + 30 s ramp-down + graceful 10 s, connect time excluded like `http_req_duration`), plus a per-second series. The old `ul2` measured only the hold from ramp + 10 s. **That omission was the main reason it overstated.**
- CPU 0 island (`island.sh`): unbound workqueues (writeback, ext4), jbd2, kswapd, kcompactd, RCU threads and disk IRQs all on CPU 0, as on a 1-vCPU box. Before, they ran free on CPUs 1-7. `user.slice` (shells, generator) on 1-7.
- Sampler: CPU 0 breakdown, app cgroup, ksoftirqd, kernel threads' ticks, TCP counters, in 5 s rows.
- No TD_STATS by default. drop_caches before each run. Memory: SRVMB=1850 as before.

Scorer vs rig v3, 2-minute probes (`197ad57` = a197, `2b4d6d8` = b2b4; K6 = whole run, Hold = hold only; ms):

| build | users | BURN | K6 p95 / p99 | Hold p95 / p99 | scorer |
|---|---:|---:|---|---|---|
| a197 (E81) | 280k | 0 | 22 / 133 | 12 / 39 | pass (5 min 190 / 348) |
| a197 (E82) | 290k | 0 | 70 / 450 | 12 / 41 | fail p99 1.05 s |
| a197 (E83) | 300k | 0 | 395 / 1,010 | 16 / 58 | fail p99 2.33 s |
| a197 (E85) | 290k | 6 | 395 / 960 | 14 / 52 | fail p99 1.05 s |
| a197 (E87) | 280k | 6 | 18 / 128 | 18 / 175 | pass |
| b2b4 (E86) | 290k | 6 | 24 / 425 | 14 / 62 | fail p95 1.13 s |
| b2b4 (E88) | 280k | 6 | 123 / 590 | 11 / 33 | 2-min p95 438 / p99 728 pass |
| b2b4 (E89, perf on) | 290k | 8 | 1,435 / 1,755 | 25 / 415 | fail p95 1.13 s |

E84 was a void repeat (burner refused by the island's cpuset; ran as BURN=0). App CPU per request here 24-25 us against the scorer's 27, so BURN=6-8 stands in for the missing cost (virtio exits, fq_codel; unverified which). Not done: running other entries as reference points; the harness refused running other contestants' scripts, so #264 and #245 are read, not run.

**Proven on the rig: the tail isn't in the hold, it's in the last 15 s of the ramp.** Hold-only p99 stays under 100 ms at 290k and 300k while the whole-run p99 fails. In E89 all of t=50-65 s sits at mean 0.5-1.7 s; the rest of the run is clean. In a 2-minute probe 1 % of requests is only ~1.65 s of traffic, so one such spike fails it, and the search never gets to try a hold at that level. That also explains why the scorer's 2-min p95/p99 are worse than its 5-min ones at the same users.

Why the spike happens (E90 counters, 290k, t=50-60): connections 5.1k/s still arriving (perf: accept + handshake about 10 % of CPU), the request rate at its peak, CPU 100 %. Once replies pass 40 ms the delayed-ACK timer fires per request: DelayedACKs 24k/s, OutSegs 87k/s against 60-65k/s, so capacity falls about 20 % and requests fall behind further (23-25k req/s served against 30k asked). Then a 35k req/s catch-up and echoes at t+20 s (one cycle), e.g. busy 97 % at t=100.

## 6 Nov (ObjcTop2): first code runs on rig v3 (BURN=8 unless noted)

| run | build | users | length | K6 p95 / p99 | Hold p95 / p99 | note |
|---|---|---:|---|---|---|---|
| E89 | b2b4 | 290k | 2 min, perf | 1,435 / 1,755 | 25 / 415 | |
| E90 | v16 (all rings deferred) | 290k | 2 min, perf | 885 / 1,720 | 19 / 73 | p50 1.15 vs 3.85, app CPU same 74.8 |
| E91 | v16 | 290k | 2 min | 870 / 2,180 | 13 / 33 | |
| E92 | v16 --lifoage=2000 | 290k | 2 min | 24.9 / 430 | 13.6 / 33 | pass |
| E93 | v16 --lifo=0 | 290k | 2 min | 820 / 1,285 | 100 / 265 | oldest-first fails |
| E94 | v17 --lifoage=2000 | 300k | 2 min | 90 / 3,830 | 15.6 / 36 | collapse t=54-67 |
| E95 | v17 --lifoage=60000 --lifomin=0 | 300k | 5 min | 22 / 128.6 | 14 / 30 | max 7.8 s, pass, one rep |
| E96 | v17 --lifoage=2000 --wwin=20 | 300k | 2 min | 960 / 3,685 | 38 / 1,245 | app 77.5 vs 74; wrMB -30 %, child ticks 569 vs 660; no gain seen, not a clean A/B |
| E97 | v17 strict newest-first | 310k | 5 min | 78 / 5,320 | 35 / 420 | app 84.6, iow 5: saturated, max 17 s |
| E98 | v17 strict newest-first | 320k | 5 min | 4,600 / 5,320 | 765 / 5,320 | memory wall: scanK 424k, majf 8.9k per 5 s |
| E99 | stopped | 330k | | | | void, counted |
| E100 | v18 (lifoage 8000, lifomin 0), perf | 300k | 2 min | 35.4 / 5,320 | 22.8 / 93 | max 8.8 s |

Profile, v16 at 290k in the hold (E90): io_send 38 % with nested softirq, net_rx_action 33 %, __inet_lookup_established 12.5-14 %, pwrite 5.2 %, checkpointer child about 4.7 % of app, post_view 7.7 %, TDStore feed 3.5 %, onCQE 2.2 %, io_cqring_wait 2.6 %, clear_page_erms 2 %, AppArmor hooks about 2.8 %, fdatasync 0.8 %, timers 2 %. Ramp window (t=45-55): io_accept 2.8 %, tcp_v4_conn_request 1.7 %, tcp_check_req 1.55 %, tcp_rcv_state_process 2.3 %.

E100 is the first clean look at what the probe weighs. 2 minutes at 300k are 5.43M requests, so 1 % is 54k. Requests over 1 s at t=60-70 sum to about 91k, so a 2-minute probe fails on p99, while the same parked set in a 5-minute hold (about 11M requests, 1 % = 110k) passes. Capacity at t=45-60, with 5k new connections per second: about 27k req/s. Right after the ramp, with none: 35k req/s. So the question is the size of the parked set at ramp end.

The scorer probes for 2 minutes first, so the parked set needs to stay under 1 % of a 2-minute run.

v18 = strict newest-first, old end from 8 s (half a pass, all of it at 16 s), every ring deferred. BURN=8:

| run | build | users | length | K6 p95 / p99 | Hold p95 / p99 | max s | note |
|---|---|---:|---|---|---|---:|---|
| E101 | v18 | 290k | 5 min | 24.5 / 179 | 20.6 / 73 | 8.7 | |
| E102 | v18 | 300k | 5 min | 23.1 / 114 | 20 / 50 | 8.3 | |
| E103 | v18, 512 answers | 300k | 2 min | 31.2 / 850 | 24.6 / 93 | 8.4 | |
| E104 | v18 `--answers=256` | 300k | 2 min | 12.6 / 134 | | 6.5 | |
| E105 | v18 `--answers=128` | 300k | 2 min | 7.7 / 116.6 | | 3.2 | |
| E106 | v18, 512 answers | 300k | 2 min | 34.3 / 910 | | 9.5 | |
| E107 | v18 `--answers=256` | 300k | 2 min | 16.7 / 290 | | 8.4 | |
| E108 | v18 `--answers=128` | 300k | 2 min | 8.2 / 130.5 | | 6.6 | |

H27 confirmed (new, not on the 12:30Z list, so a post-hoc addition): sends go out at the end of a pass, so a request that comes in during one pass is answered at the end of the next, between one and two pass lengths later. 512 answers at 100 % CPU take about 25 ms, so between 25 and 50 ms: half the replies are past the 40 ms delayed-ACK timer even under newest-first. 128 answers keep them far under it. The earlier A/B of pass size (E63, 310k, hold only, old rig) measured us/req in the hold and saw no gain, since the hold isn't where the tail is.

Annotate of `post_view` (E100): the time is the inlined binary search of `sqlite3BtreeTableMoveto` (load of a cell pointer, then the varint at the cell), not record decoding. A faster descent would need our own walk of the page or writes to `BtCursor` fields, so H26 is dropped: ruling 8. `clear_page_erms`: 0.78 of its 1.25 % comes from ext4 page-cache allocation for WAL appends.

## 6 Nov (ObjcTop2): what the OOMs at 300-320k were

E109 (v19 walbuf 300k), E111 (310k), E112 (320k) ended in global OOM and 75-81 % errors. Not a memory wall: a bug in my VFS. `unixWrite` keeps the low 17 bits of a write's length (`seekAndWriteFd`: `nBuf &= 0x1ffff`), so a buffered commit of 128 KB or more wrote part of it, then a zero-length write, then returned `SQLITE_FULL`. strace at 200k (E114): `pwrite64(6, ..., 55096, 70072) = 55096`, `pwrite64(6, ..., 0, 125168) = 0`, and the same offset written again after the rollback. The rollback emptied the page cache (957 DB reads in 8 s, none with `--walbuf=0`) and dropped the batch's connections (err 1.6-2 % at 200k), and at 300k the reconnect storm ran the box out of memory. Fixed by a 131,071-byte cap (v20).

Rig note: hs.sh sized the memory hog from MemAvailable right after the last run's teardown, which varied the budget by up to 300 MB. Since E116 it uses a fixed 15,400 MiB (rig v3.1).

| run | build | users | length | K6 p95 / p99 | app us/req | note |
|---|---|---:|---|---|---:|---|
| E109, E111, E112 | v19 | 300-320k | 2 min | | | void: the bug above |
| E110 | v19 `--walbuf=0`, perf | 300k | 2 min | 21.8 / 5,320 | | perf at t=100 costs the tail; don't compare tails of perf runs |
| E113, E114 | v19 | 200k | 1 min, ramp 30 | | | err 2.0, 1.6 %: the bug |
| E115 | v19 `--walbuf=0` | 200k | 1 min, ramp 30 | 8.6 / 74 | | err 0 |
| E116, E118 | v20 | 300k | 2 min | 13.4 / 595, 8.8 / 153 | 24.69, 24.01 | |
| E117, E119 | v20 `--walbuf=0` | 300k | 2 min | 8.2 / 115, 10 / 173 | 24.53, 25.36 | |
| E120 | v20 | 310k | 2 min | 12.8 / 545 | | pass |
| E121 | v20 | 320k | 2 min | 5,320 / 5,320 | | 100 % CPU at 33.9k req/s, reclaim from t=130 |
| E122, E124 | v21 huge | 300k | 2 min | 22.7 / 870, 11.4 / 370 | 25.72, 24.79 | |
| E123, E125 | v21 `--huge=0` | 300k | 2 min | 16.8 / 3,700, 11.8 / 335 | 25.25, 24.38 | |
| E126 | v21, perf and bpftrace | 300k | 2 min | 10.2 / 215 | 24.68 | |
| E127 | v21 | 120k | void | | | bpftrace syntax error |
| E128 | v21, bpftrace | 120k | 40 s | 4.0 / 52 | | item A below |
| E129 | v21 | 300k | 5 min | 7.1 / 102 | 23.20 | max 5.4 s |

**Item A (Opt): is the rig's profile true?** E128, bpftrace for 8 s at 12.6k req/s. `tcp_v4_rcv` on CPU 0: `veth1` only, 201k (2 per request). `veth0` (the generator's receive): 14.4k on each of CPUs 1-7, none on CPU 0. `io_send` on CPU 0: 100.8k. So no generator TCP receive is charged to the server's CPU; the kernel's ~70 % is server-side send and server-side receive nested in it. What the rig still lacks: virtio kicks and virtio IRQs (it has RPS IPIs instead, `asm_sysvec_call_function_single`), which the 8 % burner stands in for. Not measured on a droplet.

Memory at 300k, 100 s into E126, /proc/slabinfo halved where both sides count: TCP 2,368 B (not charged to the app's cgroup, it's allocated in softirq), sock_inode_cache 832, filp 256, dentry 192, lsm_file 32, plus io_kiocb 256 on our side only: about 3.75 KB per connection, 1.13 GB. Cgroup: anon 106 MB, file 42 MB, slab 480 MB.

p1 = minimal head: `2b4d6d8` + all rings deferred + strict newest-first with an 8 s bound + 128 answers; no VFS, no huge pages. Clean install of p1: Docker `ubuntu:24.04` on Synteq, `--cpus=1 --memory=2g`, kernel 6.8.0-147, install.sh 73 s, build + 42 tests 88 s, 42/42.

| run | build | users | length | K6 p95 / p99 | app us/req by window (85-180, 180-270, 270-359 s) | note |
|---|---|---:|---|---|---|---|
| E129 | v21 VFS + huge | 300k | 5 min | 7.1 / 102 | 23.02, 23.15, 23.44 | max 5.4 s |
| E130 | v21 VFS only | 300k | 5 min | 8.8 / 125 | 24.53, 25.31, 25.13 | max 2.5 s |
| E131 | p1, `perf stat -p` with tracepoints | 300k | 5 min | 4,055 / 5,320 | | void: softirq up by half for exactly the perf window, t=120-240 |
| E135 | p1 | 300k | 5 min | 11.9 / 245 | 25.05, 26.12, 26.48 | |
| E136 | p1 | 300k | 5 min | 18.9 / 1,110 | 26.13, 27.09, 26.70 | fail on p99: spikes through the whole hold at 100 % busy |
| E137 | p1 | 290k | 2 min | 9.6 / 156 | | |
| E138 | p1 | 300k | 2 min | 10.0 / 164 | | |

Corrects two earlier calls. The 2-minute A/Bs (E116-E125) saw huge pages and the VFS as noise, since a 2-minute run has about 95 s of steady hold and the differences grow with time. Over 5 minutes both are real: about -1.2 us/req for the VFS (E130 against E135, E136) and about -2 us/req for huge pages (E129 against E130), one or two reps each. So p1 isn't the head to push. Repeats of E129 are queued against the next idea.

Annotate-driven idea (own, H41): `sqlite3BtreeTableMoveto` returns at once from a cursor already on the key, so one cursor per position in the feed (20 for posts, 20 for authors) skips the descents for all feeds of a pass but the first. Same public cursor API, no private state written, each request still decodes its rows from SQLite's page.


## 6 Nov, one cursor per feed position (H41), and the candidate head p3

Scorer's box, re-read in LEADERBOARD.md: a Synteq 1 vCPU / 2 GB VPS (KVM, Xeon Platinum 8260, kernel 6.8), so our rig's host is the same vendor. README rule 8 still says DigitalOcean.

v25 = v21 + H41 + experimental flags, all off. `--walbuf=1 --huge=1` needed there, they default to 0. p3 = `2b4d6d8` + rings deferred + strict newest-first + 128 answers + VFS + huge pages + H41, with those defaults on.

| run | build | users | length | K6 p95 / p99 | app us/req by window (85-180, 180-270, 270-359 s) | note |
|---|---|---:|---|---|---|---|
| E139 | v25 | 300k | 5 min | 9.2 / 152 | 24.3 | |
| E140 | v21 | 300k | 5 min | 10.2 / 193 | 25.0 | |
| E141 | v25 | 300k | 5 min | 6.9 / 96.4 | 23.3 | max 1.17 s |
| E142 | v21 | 300k | 5 min | 12.8 / 255 | 25.3 | |
| E143, E145 | v25, `perf stat -C 0` | 300k | 2 min | 17.7 / 4,805, | | counts only: perf raises softirq (si 44-51 against 33) and hurts tails |
| E144, E146 | v21, `perf stat -C 0` | 300k | 2 min | 41.7 / 5,320, | | counts only |

Counts per request on CPU 0, burner included: instr:u 34.1-34.3k (v25) against 41.8k (v21); cycles:u 19.6k against 21.4k; cycles:k 59.4k, 57.8k against 61.0k, 59.1k; instr:k 27.7k both. So H41 is about -4 % of total cycles. Kernel IPC about 0.46, user about 1.74.

Note on method: an identical build varies by about 1 us/req between 5-minute holds (E129 23.2, E140 25.0, E142 25.3). Single-run CPU claims are weak, including the "VFS -1.2, huge -2" above; instruction counts are stable.

p3 gates: 42/42. `crash.py` 3 reps PASS (400 writers, `kill -9` with the child, integrity_check ok, acked posts 12,634, 21,836, 31,897, 0 missing). Clean install: Docker `ubuntu:24.04`, `--cpus=1 --memory=2g`, kernel 6.8.0-147, install.sh 69 s, build + tests 91 s, 42/42.

Own errors, this stretch: a `pkill -f` in crash.py matched my own ssh command and left the island down; removed, children killed via /proc. Edited hs.sh in place while a run read it; restored at once, and scripts now go through a new file and `mv`.

## 6 Nov, p3 holds on rig v3.1, BURN=8, solo (`/root/objc3/w/p3h.sum`)

| run | build | users | length | K6 p95 / p99 | max ms | hold rps | app us/req (85-180, 270-359 s) | note |
|---|---|---:|---|---|---:|---:|---|---|
| E147 | p3 | 300k | 5 min | 8.7 / 240 | 10,276 | 31,705 | 23.61, 23.22 | pass |
| E148 | p3 | 300k | 5 min | 7.8 / 131.8 | 5,435 | 31,728 | 24.16, 23.18 | pass |
| E149 | p3 | 310k | 5 min | 10.4 / 177.2 | 4,887 | 32,778 | 23.28, 24.31 | pass, one rep; iowait 11 % left on CPU 0 |
| E150 | p3 | 290k | 2 min | 8.9 / 155.9 | 6,280 | 30,663 | | pass |
| E151 | p3 | 300k | 2 min | 8.7 / 138.1 | 2,141 | 31,740 | | pass |
| E152 | p3 | 310k | 2 min | 10.7 / 196.8 | 5,520 | 32,765 | | pass |

0 errors in all six. `2b4d6d8` on the same rig: 280k 123 / 590 (burner 6), 290k 1,435 / 1,755 (burner 8). The rig's rate is 105.8 req/s per 1,000 users against the scorer's 95.5 at 280k, cause unknown.

E153 (p3, 300k, 2 min, `FRAG=1`): `fhog.py` maps 15,100 MiB, then frees single pages evenly until only the hog's size is held, so no free 2 MB block exists without compaction. Why: `defrag` is `madvise`, so a `MADV_HUGEPAGE` fault can reclaim and compact in the loop's own time, and the rig's 15 GB never fragments (`thp_fault_fallback` 2 in all runs since boot, `compact_stall` 315) while a 2 GB box might. The box stopped answering ping and ssh at about 17:13 UTC, during this queue. Dashboard (Denis, 17:36 UTC): Running, vCPU load 13 % (about one of 8), memory 15.4 / 16 GB, network flat since the same time. So the VM hangs inside the guest; my stress test is the likely cause, not proved. E154-E156 did not run.

The hang, read from Denis's console screenshot and the journal of the previous boot: `Kernel panic - not syncing: System is deadlocked on memory` at 17:10:15 UTC, 28 s after `fhog.py 10181 4919` started. My rig's fault, not p3's. Two errors of mine: hs.sh waits for the hog with `journalctl -u hog | grep "hog holds"`, which matches any of 380 old lines and so never waits, so server and generator started while the hog was still touching its 15,100 MiB peak (free 1,241 MiB at t=25, falling 1.7 GB/s); and the hog and generator run at `oom_score_adj=-1000`, so once the killer ran out of other victims the kernel had nobody left and panicked (`kernel.panic=0`, so no reboot). Denis restarted the box at 17:41 UTC. Lost: nothing on disk; netns, sysctls and island come back through hs.sh. E153 void. Regular runs were safe from this only because the plain hog's peak is 5 GB lower, but note their hog was still allocating during the first seconds of the ramp too.

Instead of a stress test, first the plain question: are huge pages worth having at all? The evidence is thin (E129, E140, E142 with: 23.2, 25.0, 25.3; E130 without: 25.0). Interleaved 5-minute holds of p3 with and without, E154-E157. If the difference stays inside the 1 us/req noise, huge pages come out of the head and the fragmentation risk goes with them.

Pushed p3 as `1ae8cff` at about 17:47 UTC on Denis's order ("can you push your changes BEFORE rerunning the expiremnts ?"), PR body updated, no PR comment. Source checked by sha256 against `/root/objc3/src/p3` on Synteq (14 files equal), diff only in `submissions/objc-uring-delneg/`. Bar met: 5-minute holds at 300k twice and 310k once, 2-minute probes at 290k, 300k, 310k, 42/42, clean install, crash test, rules check. Open at the push: whether huge pages help (E154-E157 running) and how they behave on a fragmented 2 GB box (unmeasured). PR was last commented on 5 Nov 23:10 UTC, so no run seen in progress; a run can't be seen from here except by that.

Correction to the "105.8 req/s per 1,000 users, cause unknown" above: that's the hold's rate. Over a whole run E147 did 11,149,149 requests in 390 s, 28.6k req/s, 95.3 per 1,000 users; the scorer's 26,702 at 280k is 95.4. So the rig's load matches the scorer's, and the scorer's req/s is a whole-run figure. PR body corrected (edit only, no comment).

## 6 Nov, huge pages A/B after the reboot, and the rig's budget

| run | build | users | length | K6 p95 / p99 | max ms | avail before hog, MiB | app us/req (85-180, 180-270, 270-359 s) | note |
|---|---|---:|---|---|---:|---:|---|---|
| E154 | p3 `--huge=0` | 300k | 5 min | 16.8 / 435 | 8,306 | 15,231 | 23.25, 22.83, 23.94 | |
| E155 | p3 | 300k | 5 min | 5.8 / 71.8 | 1,573 | 15,185 | 22.59, 21.83, 22.54 | best hold so far |
| E156 | p3 `--huge=0` | 300k | 5 min | 15.5 / 1,145 | 14,772 | 15,213 | 24.36, 24.25, 24.05 | fails p99; direct reclaim 568 |
| E157 | p3 | 300k | 5 min | 5,320 / 5,320 | | 15,103 | 30-34 | void: global OOM, `pgmajfault` 873k by t=324, reclaim from t=60-119; server killed at t=390 when my ssh login ran landscape-sysinfo |

Why E157 is void, and partly E154-E156: the reboot brought back nginx (off before), ModemManager, udisks2, unattended-upgrades and more, so MemAvailable before the hog fell from 15,407-15,464 to 15,103-15,231 with the hog's size fixed: 180-360 MiB off the calibrated budget. Also found: every ssh login runs `update-motd` scripts, landscape-sysinfo among them, on the rig's memory. Fix: those services stopped and disabled, `chmod -x /etc/update-motd.d/*`, hs.sh stops the four services each run, AV0 = 15,470 for MemAvailable 15,502 now (budget as before the reboot). So p3 doesn't get its 300k pass back from E157 by argument; E158 repeats it on the corrected budget.

Huge pages, on what's clean: on 22.3 (E155), 23.4, 23.7 (E147, E148) us/req, p99 72, 240, 132; off 23.3, 24.2, p99 435, 1,145. They stay. Counters per run: `thp_fault_alloc` 51, `thp_fault_fallback` 0. Even in E157's thrash only 9 fallbacks and 5 direct compactions, all successful, which is the only data on that risk so far.

The cliff is sharp: 15,185 MiB clean, 15,103 thrash. In the rig's terms the server needs about 1,550-1,630 of the calibrated 1,850 MiB at 300k (`SRVMB` minus the shortfall). The unknown is how well 1,850 maps to the scorer's box; `1fe6941` dying at 290k fitted it.

## 6 Nov, p3's limit on the corrected budget (AV0 15,470, H45)

| run | build | users | length | K6 p95 / p99 | hold p99 | app us/req | note |
|---|---|---:|---|---|---:|---:|---|
| E158 | p3 | 300k | 5 min | 9.9 / 179 | 107 | | pass, repeat of the void E157 |
| E159 | p3 | 320k | 2 min | 250 / 5,320 | 5,320 | 24.71 | fail: t=148-190 falls into all-late (mean 3.2 s at t=160), CPU 88-97 %; before that clean. free at the floor (206-233 MiB) from t=70, file 4-40 MiB, kswapd scanning |
| E160 | p3 | 330k | 2 min | 28.1 / 1,050 | 470 | 23.21 | just fails p99 |
| E161 | p3 | 340k | 2 min | 5,320 / 5,320 | | | global OOM, server killed |

So the rig's wall for p3 is 320-330k, with memory and CPU arriving together: hold app 83 %, iowait 7 % at 320k, free at the floor from t=70. E159 against E160 shows the all-late state is still a coin toss near the wall. Whether the scorer's memory wall sits in the same place is unknown; the budget was fitted to `1fe6941` dying at 290k.

Rig notes: q.sh's `head -1` picked up the new `vm:` line, fixed. Tl2.py prints the per-10 s series.

Reading the loop for H44: with more than one ring active, `ready` (any other ring has a CQE) skips ring 0's pause, so the 128-completions-or-1-ms wait hardly ever runs at 300k. New flag `--ready=0` in v26 (H46), with TD_STATS for passes per second.

## 6 Nov, the scorer on `1ae8cff`: 280,000 again

Arjay's comment, 18:49 UTC: 280,000 x 5 min: 26,731 req/s, p95 25.4 / p99 236.4 ms, 0 errors, 83 % CPU (28 us/req), 629 MB peak. 290,000 and 300,000 failed the 2-minute probe on p99 (2.52 s, 2.99 s, at 86-87 % CPU, 0 errors). 42/42, CI green. A tie, so not merged; the PR stays open.

So the rig is still too kind, again on the 2-minute whole-run p99: E150 290k 8.9 / 156 against the scorer's p99 2,520; E151 300k 8.7 / 138 against 2,990. My push bar was met and the forecast was wrong. App CPU per request: rig 23.5 us, scorer 28 us; the same ratio for `2b4d6d8` was about 24.5 to 26.

What the shape says, an estimate: p95 fine and p99 over 1 s in a 2-minute probe means more than 1 % of the run's requests (53k at 290k) waited over 1 s, while the 5-minute hold's 1 % (107k) at 280k stays clean. That's the parked set at the ramp's end, as before, only bigger on the real box: real virtio makes a connection and a request dearer than veth does. With hold CPU 86 % capacity is about 35.7k req/s; if a connection costs about 80 us there (a guess; 40 us on the rig), 4.8k conn/s takes 39 % at t=60, capacity falls to 21.8k against 30.7k asked, and the deficit from t=42 to t=60 is about 80k requests. That fits 53k being exceeded at 290k and 107k not at 280k.

Next, with 15 experiments left (85 used): make the rig fail p3 at 290k like the scorer (higher burner), then what cuts that deficit: accept gate (H30), checkpointer at nice 19 during it (H21), `TCP_DEFER_ACCEPT` (H35).

## 6 Nov, H46 and recalibration after the scorer's 1ae8cff

| run | build | users | length | burner hold, ramp | K6 p95 / p99 | hold p99 | app us/req | note |
|---|---|---:|---|---|---|---:|---:|---|
| E162 | v26, STATS | 300k | 2 min | 8 | 22.1 / 620 | 230 | 23.74 | 1,620-1,890 passes/s, about 17 answers a pass; enter 0.75, flush 0.21, reap 0.03 of each second |
| E163 | v26 `--ready=0`, STATS | 300k | 2 min | 8 | 20.0 / 2,765 | 205 | 23.87 | passes the same, p50 4.3 ms against 1.25 |
| E164 | v26 `--ready=0` | 320k | 2 min | 8 | 79.8 / 5,320 | 995 | 22.28 | |
| E165 | v26 `--ready=0 --bwait=2000` | 320k | 2 min | 8 | 85.7 / 2,395 | 460 | 23.00 | |
| E166 | p3 | 290k | 2 min | 16 | 10.5 / 160 | 86 | 23.79 | |
| E167 | p3 | 280k | 2 min | 16 | 10.4 / 151 | 93 | | |
| E168 | p3 | 290k | 2 min | 16, 36 for the first 60 s | 16.5 / 350 | 178 | | `burn2.py`; mean 123 ms at t=60 |

H46 killed: no fewer passes, no less CPU, latency worse. A second burner step isn't enough to make p3 fail 290k; the scorer's p99 there is 2,520.

Real NIC on Synteq, for the record: virtio-net, 1 queue, RX ring 1,024, TX ring 256, `rx-frames` 1, fq_codel on eth0 (target 5 ms). The rig's veth has neither ring nor qdisc, so it can't lose packets at CPU 0, and `netdev_max_backlog` is 250,000 from an old `/etc/sysctl.d/90-million.conf`. Open idea, untested: RX overflow or CoDel drops on the real box when softirq falls behind at the ramp's end, RTOs of 200 ms and up. That would give a p99 like the scorer's that no burner reproduces.

| run | build | users | length | burner hold, ramp | K6 p95 / p99 | hold p99 | note |
|---|---|---:|---|---|---|---:|---|
| E169 | p3 | 290k | 2 min | 20, 50 | 18.9 / 275 | 245 | still passes; hold 17.6 / 245 is close to the scorer's 5-minute 25 / 236 at 280k |
| E170 | v27 `--gate=2 --backlog=256` | 290k | 2 min | 20, 50 | 15.5 / 270 | 225 | 107k SYNs dropped, `Possible SYN flooding`, max 13.6 s, 0 errors; neutral where the rig has no deficit |
| E171 | v27 `--cknice=19` | 290k | 2 min | 20, 50 | 56.1 / 5,320 | 5,120 | killed: the checkpointer starves behind the burner and writes wait on the WAL (H21) |

v25-v26's gate was a no-op: the main loop re-armed accept at once when nothing was armed. Fixed in v27 (`gateRing < 0 &&`).

A burner doesn't reproduce the scorer's 2-minute p99 at 290k. Reasoning from the scorer's own history, which is better data than the rig here:
- `197ad57` (whole pass to the old end at 250 ms, so near oldest-first in a backlog): 290k p99 1.05 s, just over, p95 under 500.
- `2b4d6d8`: 290k p95 1.13 s.
- `1ae8cff` (strict newest-first, 8 s bound): 290k p99 2.52 s, p95 under 500.
An oldest-first run that peaks near 1 s means the backlog there peaks at about 35k requests. Then 1 % of a 2-minute run (53k) should have held under `1ae8cff` unless its deficit is bigger than `197ad57`'s was. Unknown why; 128 answers at saturation mean more passes, commits and enters than 512 did, and I never measured that on the real box.

#264's README, read for this: single-shot accepts, at most 512 in flight, re-armed only when a pass has seen them complete, so accept rate falls when passes get long; 8 `SO_REUSEPORT` listeners. Our multishot accept drains the queue at every enter, whatever the load. Same idea as the gate without its SYN drops.

| run | build | users | length | burner | K6 p95 / p99 | hold p99 | note |
|---|---|---:|---|---|---|---:|---|
| E172 | v27 `--gate=2 --backlog=256` | 320k | 2 min | 8 | 54.6 / 1,330 | 705 | fail: 7,531 connect errors (0.169 %), max 145 s, 329k dials, hold rate down to 31.9k |
| E173 | v27 `--gate=2 --backlog=256` | 330k | 2 min | 8 | 5,320 / 5,320 | 5,320 | collapse: hold sys 63 %, 17k req/s, SYN cookies |

H30 killed: worse than none at 320k (E159 was bad by chance, E160 at 330k nearly passed without it). Same lesson as #319 tells about #257's accept pause.

Rig flaw found: `burn.py` and `burn2.py` spin on `perf_counter`, wall time. When app and softirq contend, the burner's window passes without its getting the CPU, so it steals less exactly at saturation. That's why BURN=8 to 20, and even 50 in the ramp, barely hurt. All "burner N %" so far were a tax only below saturation. `burn3.py` counts CPU time (`BCPU=1`). E174 tests it.

| run | build | users | length | burner, CPU time | K6 p95 / p99 | hold p99 | app us/req | note |
|---|---|---:|---|---|---|---:|---:|---|
| E174 | p3 | 290k | 2 min | 16 | 18.4 / 305 | 168 | | hold busy 88 % |
| E175 | p3 | 290k | 2 min | 26 | 53.7 / 5,320 | 415 | 21.39 | the scorer's shape: p95 fine, p99 over 1 s. t=60: 19.5k req/s, mean 1.1 s. Hold busy 93 % |

Rig v3.2 = v3.1 + `burn3.py`, CPU time, 26 %. It reproduces `1ae8cff` failing 290k on p99. Not checked: 280k passing at this setting. It says the real box may have about 7 % spare at 290k, not the 14 % its "86 % CPU" looks like. Plausible reason, unverified: softirq taken while the app sleeps isn't charged to the app's cgroup.

At saturation in E175: 74 % left after the burner, a connection 40 us x 4.8k/s = 19 %, so 55 % / 28 us = 19.6k req/s, matching the 19.5k seen at t=60. Against 30k asked, the deficit is 10k/s for about 12 s, about 120k requests; 1 % of the run is 53k. CPU per request is the lever: each us/req is about 3.5 %.

E176, v28 (branch counters), `--ready=0`, 300k, 2 min, wall-clock burner 8: 30.1 / 870, hold 18.4 / 275, app 24.91 us/req. Passes 258-362 a second, about 100 answers a pass, timed wait about 1.4 ms, against E162's 1,620-1,890 passes and 17 answers a pass at 23.74 us/req. (E163's "same passes" were sampled at the wrong lines.) So the pause works, and bigger passes cost more per answer, as the older note had it (9 us at 190 passes/s, 12.5 at 55). H46 stays killed, and it points the other way: smaller is cheaper. At saturation the 128-answer cap sets pass size, so E177 tries 64 on the case that fails.

| run | build | users | length | burner, CPU time | K6 p95 / p99 | hold p95 / p99 | app us/req | note |
|---|---|---:|---|---|---|---|---:|---|
| E177 | p3 `--answers=64` | 290k | 2 min | 26 | 24.1 / 5,320 | 14.5 / 235 | 21.49 | against E175 53.7 / 5,320, 15.8 / 415; t=65 mean 1 s, then clean |

## E178-E180, the push candidate, end of the run

| run | build | users | length | burner, CPU time | K6 p95 / p99 | hold p95 / p99 | max | note |
|---|---|---:|---|---|---|---|---:|---|
| E178 | v27 `--answers=64 --wwin=10 --defacc=1` | 290k | 2 min | 26 | 24.0 / 355 | 19.85 / 345 | 18.3 s | pass, 30.6k req/s |
| E179 | v28 + same flags | 290k | 5 min | 26 | void | | | my bug: `gate.sh` ran `ulimit -n 65535` in its own shell, `ul3` raises soft to hard, so the generator stopped at about 65k connections. The server was healthy |
| E180 | v28 + same flags, the candidate tree | 290k | 5 min | 26 | 42.6 / 5,320 | 28.95 / 44.75 for what finished | 162 s | **fail**, 0 errors, 209,258 users waiting at the end, 208,858 of them for over 5 s |

E180's req/s: 27.0k at t=70-100, 22.0k, 16.7k, 12.4k, 9.3k, 7.0k at t=300-350, with CPU at 100 % throughout and app 75-80 %. Mean latency of what finishes stays 5-15 ms. It left the ramp with a spike at t=60-65 (mean 2.5 s), as E178 did on a smaller scale (t=75, 629 ms), but never got back to 30k.

Post-mortem on the run's database, no new run: 250,484 new rows (27,186 posts, 223,298 likes) in 5,696,708 answers, 4.40 % writes against the 7.8 % the mix gives. That's about 194k writes short, about the number of users stuck. So users end up parked on a write one by one, and reads keep flowing.

The reading of `flush`, not proved (no TD_STATS in that run): with `--wwin` a write is answered only on a pass at least 10 ms after the last write pass, at most 64 of them; every pass inside the window walks the held writes and copies them, so a pass costs O(held). Once a backlog forms, the write rate falls under demand, the queue grows and passes get longer. That's a one-way trap. E178 got away with it in 2 minutes. I didn't separate `--wwin` from `--defacc`, and `--answers=64` alone (E177) failed p99 without this collapse, so `--wwin` is the suspect.

Gates on the candidate tree (v28 source, p3's `td_btree.c` comment, flags in `start.sh`), log in `logs/gate-v28f.log`:

- clean `ubuntu:24.04` in Docker, `--cpus=1 --memory=2g`, kernel 6.8.0-147: install.sh 71 s, build + tests 87 s, passed=42 failed=0, through `start.sh`
- `crash.py`, 3 reps of `kill -9`, through `start.sh`: 14,397, 25,437, 33,100 acked posts, 0 missing, integrity ok
- rules check: see `rules-check.md`

v27 got no gates of its own. It's the same logic as v28 (`patches/v27-to-v28.diff` is counters under `statsOn`), so E180 applies to it too, and nothing was going to be pushed.

Decision: pushed nothing. Denis's order had four gates, which all passed, and "v28 if it holds up, else v27". v28 didn't hold up, and v27 only looks better because it never ran 5 minutes. A push starts another rescore on a public PR for a config I've just seen collapse at the level it's aimed at, so that goes back to Denis with the numbers. Ready to go if he wants it: `patches/candidate-v28-flags-over-1ae8cff.diff`, local commit `1baccb2` on branch `objc-uring-delneg-2-candidate` in `/tmp/tdsc-objc2`.

Budget: E81-E180, 100 of 100, voids included. Money: DeepSeek about $0.023, Gemini via grsai unlisted, nothing else.

State of Synteq at the end: no load, server, hog or burner running, no namespaces, no containers. The sysctls I changed are back at the values seen at the start, nginx, ModemManager, udisks2 and unattended-upgrades are enabled and running again, motd scripts executable again. Stock `vm.min_free_kbytes` was never captured, it's at 67584, the rig's value. `tcp_max_orphans=1048576` and `netdev_max_backlog=250000` come from the old `/etc/sysctl.d/90-million.conf`.
