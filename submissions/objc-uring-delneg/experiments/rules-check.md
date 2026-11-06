# Rules check, objc-uring-delneg (5 Nov). Read upstream README rules, SPEC.md, LEADERBOARD.md notes and flags at `fe19e96`.

| trap | where it says so | status |
|---|---|---|
| Rule 2 stock SQLite, no patch | LEADERBOARD "Arjay's rulings on SQLite (2026-10-31)"; #154, #158 pulled until dropped | clear: build.sh uses the amalgamation unedited; td_btree.c only includes it |
| No own engine for the file format | same note, #210 | clear: reads go through `sqlite3Btree*` cursors |
| No writing SQLite private state (ruling 8) | flags on #296, #317, #318 (`BtCursor` fields) | clear: checked on 5 Nov. td_btree.c calls `sqlite3Btree*`, assigns only its own static `B` and local records it passes to `sqlite3BtreeIndexMoveto`, reads `schema_cookie` |
| Page cache plugin | LEADERBOARD rulings: a custom page cache through `SQLITE_CONFIG_PCACHE2` is named as allowed | `td_pcache` in td_btree.c uses only that public interface. Rule 5 names SQLite's page cache as fine. It holds what SQLite puts in it, and every read still goes through the pager |
| Newest-first order of answers | no rule on queue order; rule 7 | ordering only: applies to any backlog, no request dropped, connection order kept, nothing keys on traffic shape or client. Rule 6 unchanged: each 201 after its COMMIT |
| Rule 3 schema | README rule 3 | clear: no DDL |
| Rule 5 no cross-request cache; JWT verified each request | README rule 5; #260, #243, #277 coalescing held open | clear: no coalescing; SQLite's own page cache allowed by name, so a bigger cache_size is fine |
| Rule 6 durable before 201 | README rule 6 | clear: WAL + NORMAL, response submitted after COMMIT |
| Rule 7 no load-test detection | README rule 7; #32 evict-order flag; #112 sheds on /proc/meminfo | clear: nothing reads /proc/meminfo or keys on traffic shape |
| Rule 10 pinned, built on the box, healthy in 60 s; 1 h install timeout (#191, #143, #166) | README rule 10; LB | install.sh 40 s and PGO build ~2 min on 8 vCPU Synteq; clean `ubuntu:24.04` on 1 vCPU still to run before the PR |
| Rule 11 no kernel parameters, no pinning | README rule 11; #139 flag acts outside its folder | clear so far: start.sh only execs the binary; setrlimit and per-socket options are own-process. mlock within RLIMIT_MEMLOCK and setpriority on our own child are own-process settings (#245, #257 do both, unflagged) |
| Touch only own folder | README "Submitting"; #139 | PR must diff only `submissions/objc-uring-delneg/` |
| Copies | flags on #291, #303, #314, #271; Arjay 2026-11-04: credited reuse of helpers is fine | own code only; ideas from #245, #264, #257 credited in README |
| start.sh restart loop | flag: Deno #119, c-epoll #64, raoz #26, Antheurus #65 | clear: `exec bin/server` |
| Tokens without `exp` | flag list | clear: `exp` required |
| Follow-up merges only if it beats the merged row | #319, #278, #293 comments | a PR must score above 280,000 to merge; head must not move during a run (#304 lost its merge that way): don't push while it's being scored |

## 6 Nov, ObjcTop2, head with all rings deferred and 128 answers per pass

| trap | status |
|---|---|
| Stock SQLite, public APIs, ruling 8 | no change to td_btree.c or store.m from `2b4d6d8` in this head; the VFS experiment stays out of it |
| Rule 7, order of answers | strict newest-first past one pass of backlog, bounded (half a pass from 8 s, all of it from 16 s). No request dropped, nothing keyed on client or traffic shape; only the server's own queue. LEADERBOARD: "ordering routes or fast paths by observed traffic is fine" |
| Rule 6 | unchanged: sends after COMMIT |
| Rule 11 | no sysctls, no pinning, no capabilities beyond CAP_NET_BIND_SERVICE; nothing new in start.sh |
| Rule 10 | clean `ubuntu:24.04` in Docker on Synteq, `--cpus=1 --memory=2g`, kernel 6.8.0-147: install.sh 73 s, build + 42 tests 88 s, 42/42 (source p1) |
| Copies | idea of deferred task work on every ring from #264, read in its README and code; own code, credited in README |
| Own folder | diff touches only `submissions/objc-uring-delneg/` |

Not in this head, would need a look before any push: an accept gate (H30). It's admission control on our own queue, and k6 doesn't count connect time, so the README would have to say that plainly and let Arjay judge it.


## 6 Nov, ObjcTop2, head p3 (adds VFS, huge pages, one cursor per feed position to p1)

| trap | status |
|---|---|
| Rule 2 and rulings of 2026-10-31, stock SQLite | amalgamation unpatched. The VFS uses `sqlite3_vfs_register` and only wraps the default VFS's own methods for files opened with `SQLITE_OPEN_WAL`. LEADERBOARD: "SQLite's public plugin APIs (a custom page cache through `SQLITE_CONFIG_PCACHE2`, a custom VFS)" are fine; #325's scorer note says the same of a WAL shim VFS |
| Ruling 8, no writes to private state | the VFS and page cache see only what the plugin APIs hand them. Cursors are driven with `sqlite3BtreeCursor`, `TableMoveto`, `First`, `Next`, `PayloadFetch`, `CloseCursor`; no `BtCursor` field is written. `grep -n 'curFlags\|->ix\b\|info\.n\|->eState\|pPage\|apPage' src/td_btree.c` finds nothing |
| Rule 5, no caching across requests | each `/feed` still walks the index, counts likes and fetches and decodes each row from SQLite's page. The per-position cursor keeps only a cursor's position within one pass, the same thing the merged code does with its four cursors, and the shortcut is stock `sqlite3BtreeTableMoveto`. No row, result or JWT is kept. #260 and #243 share one query's result between requests; here each request does its own read. Disclosed in the README and PR body so Arjay can judge it |
| Rule 6, durable before 201 | a commit frame flushes the buffer with `pwrite` before `COMMIT` returns, and sends come after that: same state as stock WAL with `synchronous=NORMAL`. `crash.py` 3 reps with `kill -9` of server and child: 0 acked posts missing, integrity ok |
| Rule 7 | answer order uses only the server's own queue; nothing keyed on client or traffic shape |
| Rule 10 | clean `ubuntu:24.04` in Docker on Synteq, `--cpus=1 --memory=2g`, kernel 6.8.0-147: install.sh 69 s, build + 42 tests 91 s, 42/42 |
| Rule 11 | no sysctls, no pinning. `madvise(MADV_HUGEPAGE)` acts on our own mappings, and Ubuntu's `enabled` is already `madvise`. Nothing new in start.sh |
| Capabilities | runs as `deploy` with CAP_NET_BIND_SERVICE only: `hs.sh` runs it with `User=deploy`, no extra capabilities. MADV_HUGEPAGE, io_uring and `mlock` within RLIMIT_MEMLOCK need none |
| Copies | VFS idea from #245 and #325, deferred rings from #264, credited; own code |
| Own folder | diff touches only `submissions/objc-uring-delneg/` |


## 6 Nov, ObjcTop2, candidate v28 + flags in `start.sh` (not pushed)

| trap | status |
|---|---|
| Rule 5, no caching across requests | clear: the diff adds no data held between requests. `--wwin` delays writes, `--answers` caps a pass, stats are counters |
| Rule 6, durable before 201 | clear: held writes stay in the queue and are answered only in a pass that commits, sends go out after `COMMIT`, as in the WAL hold. README rule 6 allows group commit if each response waits for its commit. `crash.py` 3 reps through `start.sh`: 0 acked posts missing, integrity ok |
| Rule 7, nothing load-test specific | clear: the three settings are fixed, keyed on nothing. `--gate` is admission control, off by default and not in `start.sh`; the README says so |
| Rule 8, 1 vCPU, 2 GB, no swap | E180: peak 1,419 MB under `MemoryMax=1750M`, no OOM. But the same run shows a collapse under load, a quality problem and no rule problem |
| Rule 10 | clean install in Docker, 71 s + 87 s, 42/42 |
| Rule 11 | clear: `TCP_DEFER_ACCEPT` is per-socket on our own listener, no sysctl, no pinning. `grep` finds only `/proc/self/maps` (mlock) and `setpriority` on our own child, both already there |
| Scorer's `deploy` user, CAP_NET_BIND_SERVICE only | `hs.sh` runs `User=deploy` with no capabilities at all on port 3000, CI as `deploy`. `TCP_DEFER_ACCEPT` needs none |
| `start.sh`, "configured only by the env vars" | fixed flags, no new env; other submissions' `start.sh` pass fixed flags too (`java-flash-relism`, `erlang-cowboy-tbsvttr`). Only the comment changed besides the flags |
| Own folder | diff touches only `submissions/objc-uring-delneg/` (4 files against `1ae8cff`) |
