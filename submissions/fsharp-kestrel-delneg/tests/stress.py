#!/usr/bin/env python3
"""Transport stress test for the server (Transport.fs epoll loop, or Kestrel's socket transport).

Every read response is compared byte for byte (minus the Date header) with the same request answered by a
reference server (normally the Kestrel-sockets build) on an identical fresh copy of the database. Writes go only to
the target and are checked for consistency. Between scenarios it waits until the server has no open connections left
and checks the server's fd count against the baseline; the soak samples RSS.

Python standard library only. Run on the same box as the server:
  python3 tests/stress.py --target 127.0.0.1:3001 --ref 127.0.0.1:3002 --pid <target pid> --tokens tokens.json
"""
import argparse
import asyncio
import json
import os
import random
import re
import resource
import socket
import struct
import subprocess
import sys
import time

TIMEOUT = 20.0  # any single wait longer than this counts as a stuck connection
CL = re.compile(rb"\r\ncontent-length: *(\d+)", re.I)
DATE = re.compile(rb"\r\nDate: [^\r]*", re.I)

args = None
errors = []
stats = {}


def err(msg):
    errors.append(msg)
    if len(errors) <= 20:
        print("  ERROR", msg, flush=True)


def get(path, extra=b""):
    return b"GET " + path.encode() + b" HTTP/1.1\r\nHost: t\r\n" + extra + b"\r\n"


def post(path, body, auth=None):
    h = b"POST " + path.encode() + b" HTTP/1.1\r\nHost: t\r\nContent-Type: application/json\r\n"
    if auth:
        h += b"Authorization: Bearer " + auth.encode() + b"\r\n"
    return h + b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body


rng = random.Random(42)
READ_IDS = rng.sample(range(1, 250001), 3000)
READS = {f"post{i}": get(f"/posts/{i}") for i in READ_IDS}
READS.update({
    "feed": get("/feed"),
    "404": get("/nope"),
    "badid": get("/posts/abc"),
    "zeroid": get("/posts/0"),
    "noid": get("/posts/999999999"),
    "query": get("/posts/12?x=1&y=2"),
    "head": b"HEAD /feed HTTP/1.1\r\nHost: t\r\n\r\n",
    "post401": post("/posts", b'{"body":"no auth, so this must not be stored"}'),
    "like401": post("/posts/5/like", b""),
    "badauth": post("/posts", b'{"body":"x"}', auth="not.a.jwt"),
    "bigheaders": get("/posts/7", b"X-Pad: " + b"a" * 8000 + b"\r\n"),
})
KEYS = list(READS)
POST_KEYS = [k for k in KEYS if k.startswith("post") and k[4:].isdigit()]
golden = {}
raw_len = {}


def parse_all(buf):
    """Splits a byte string into complete (head, body) responses."""
    out = []
    i = 0
    while True:
        he = buf.find(b"\r\n\r\n", i)
        if he < 0:
            break
        m = CL.search(buf, i, he + 4)
        end = he + 4 + int(m.group(1))
        if end > len(buf):
            break
        out.append((buf[i:he + 4], buf[he + 4:end]))
        i = end
    return out


def norm(head, body):
    return DATE.sub(b"", head) + body


async def read_resp(r):
    head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), TIMEOUT)
    m = CL.search(head)
    if not m:
        raise ValueError(f"no Content-Length in {head[:200]!r}")
    body = await asyncio.wait_for(r.readexactly(int(m.group(1))), TIMEOUT)
    return head, body


async def connect(rcvbuf=None, limit=1 << 16):
    host, port = args.target
    if rcvbuf is None:
        r, w = await asyncio.wait_for(asyncio.open_connection(host, port, limit=limit), TIMEOUT)
    else:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        s.setblocking(False)
        await asyncio.wait_for(asyncio.get_running_loop().sock_connect(s, (host, port)), TIMEOUT)
        r, w = await asyncio.open_connection(sock=s, limit=limit)
    w.get_extra_info("socket").setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return r, w


def rst(w):
    """Close with RST instead of FIN."""
    s = w.get_extra_info("socket")
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    except OSError:
        pass
    w.transport.abort()


def check(key, head, body, where):
    got = norm(head, body)
    if got != golden[key]:
        err(f"{where}: {key} differs: got {got[:160]!r} want {golden[key][:160]!r}")
        return False
    return True


async def exchange(r, w, key, where):
    w.write(READS[key])
    await w.drain()
    h, b = await read_resp(r)
    return check(key, h, b, where)


# ---------- server-side observation (same box) ----------

def fd_count():
    return len(os.listdir(f"/proc/{args.pid}/fd"))


def socket_fds():
    n = 0
    for f in os.listdir(f"/proc/{args.pid}/fd"):
        try:
            n += os.readlink(f"/proc/{args.pid}/fd/{f}").startswith("socket:")
        except OSError:
            pass
    return n


def rss_mb():
    for line in open(f"/proc/{args.pid}/status"):
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return -1


def server_sockets():
    """Server-side sockets on the target port by state (listener and TIME-WAIT excluded)."""
    out = subprocess.run(["ss", "-Htan", f"sport = :{args.target[1]}"], capture_output=True, text=True).stdout
    states = {}
    for line in out.splitlines():
        st = line.split()[0]
        if st not in ("LISTEN", "TIME-WAIT"):
            states[st] = states.get(st, 0) + 1
    return states


async def drain(name, base_fds, base_socks):
    """Waits until the server holds no connection on the port and exactly its baseline socket fds (a leaked or
    stuck socket fails). Other fds (SQLite's -wal/-shm, opened on first use) are reported, not checked."""
    t0 = time.time()
    while True:
        st = server_sockets()
        socks = socket_fds()
        if not st and socks == base_socks:
            print(f"  drained in {time.time() - t0:.1f}s: socket fds {socks} (baseline {base_socks}), "
                  f"all fds {fd_count()} (baseline {base_fds})", flush=True)
            return
        if time.time() - t0 > 30:
            err(f"{name}: after 30s the server still has sockets {st}, {socks} socket fds (baseline {base_socks})")
            return
        await asyncio.sleep(0.2)


# ---------- scenarios ----------

async def s_sequential():
    """Every read request once, on one keep-alive connection."""
    r, w = await connect()
    ok = 0
    for k in KEYS:
        ok += await exchange(r, w, k, "sequential")
    w.close()
    return {"requests": len(KEYS), "ok": ok}


async def s_concurrent():
    """N connections open at once; each makes 3 keep-alive requests, idles, makes 1 more, closes."""
    n = args.conns
    sem = asyncio.Semaphore(1000)
    conns = []
    done = 0

    async def first(i):
        nonlocal done
        async with sem:
            try:
                r, w = await connect()
            except Exception as e:
                err(f"concurrent: connect {i}: {e!r}")
                return
        try:
            for _ in range(3):
                if await exchange(r, w, random.choice(KEYS), "concurrent"):
                    done += 1
            conns.append((r, w))
        except Exception as e:
            err(f"concurrent: conn {i}: {e!r}")

    await asyncio.gather(*(first(i) for i in range(n)))
    open_now = len(conns)
    peak = {"client_open": open_now, "server_fds": fd_count(), "server_sockets": server_sockets()}
    print(f"  {open_now} connections open; server fds {peak['server_fds']}, sockets {peak['server_sockets']}", flush=True)
    await asyncio.sleep(args.idle)

    async def again(r, w):
        nonlocal done
        try:
            if await exchange(r, w, random.choice(KEYS), "concurrent-after-idle"):
                done += 1
        except Exception as e:
            err(f"concurrent: after idle: {e!r}")
        w.close()

    await asyncio.gather(*(again(r, w) for r, w in conns))
    if open_now != n:
        err(f"concurrent: only {open_now} of {n} connections worked")
    return {"conns": n, "ok_requests": done, **peak}


async def s_pipeline():
    """Each connection sends many requests in one write, then reads them in order; then reuses the connection."""
    total = 0

    async def one(i):
        nonlocal total
        try:
            r, w = await connect()
            for depth in (args.depth, 10, 1):
                keys = [random.choice(KEYS) for _ in range(depth)]
                w.write(b"".join(READS[k] for k in keys))
                await w.drain()
                for k in keys:
                    h, b = await read_resp(r)
                    total += check(k, h, b, f"pipeline depth {depth}")
            w.close()
        except Exception as e:
            err(f"pipeline conn {i}: {e!r}")

    await asyncio.gather(*(one(i) for i in range(200)))
    return {"conns": 200, "ok_responses": total}


async def s_bytewise():
    """Requests sent one byte per TCP segment; then one request split at every offset into two writes."""
    ok = 0

    async def one(i):
        nonlocal ok
        try:
            r, w = await connect()
            keys = ["post401", random.choice(POST_KEYS), "query", "badauth"]
            for byte in b"".join(READS[k] for k in keys):
                w.write(bytes([byte]))
                await w.drain()
                await asyncio.sleep(0.0005)
            for k in keys:
                h, b = await read_resp(r)
                ok += check(k, h, b, "bytewise")
            w.close()
        except Exception as e:
            err(f"bytewise conn {i}: {e!r}")

    await asyncio.gather(*(one(i) for i in range(100)))
    splits = 0
    r, w = await connect()
    for k in ("post401", "post" + str(READ_IDS[0])):
        req = READS[k]
        for cut in range(1, len(req)):
            w.write(req[:cut])
            await w.drain()
            await asyncio.sleep(0.003)
            w.write(req[cut:])
            await w.drain()
            h, b = await read_resp(r)
            splits += check(k, h, b, f"split at {cut}")
    w.close()
    return {"bytewise_ok": ok, "split_ok": splits}


async def s_slow_reader():
    """Small receive buffer, many pipelined /feed responses (MBs), read slowly: the server's send hits EAGAIN and
    must finish on EPOLLOUT. Meanwhile a probe on another connection must keep getting fast answers."""
    stop = asyncio.Event()
    lat = []

    async def probe():
        r, w = await connect()
        while not stop.is_set():
            t = time.perf_counter()
            await exchange(r, w, "post" + str(READ_IDS[1]), "slow-probe")
            lat.append(time.perf_counter() - t)
            await asyncio.sleep(0.02)
        w.close()

    got = 0
    nbytes = 0

    async def slow(i):
        nonlocal got, nbytes
        try:
            r, w = await connect(rcvbuf=4096, limit=8192)
            n = args.slow_depth
            w.write(READS["feed"] * n)
            await w.drain()
            buf = bytearray()
            want = n * raw_len["feed"]
            while len(buf) < want:
                chunk = await asyncio.wait_for(r.read(4096), TIMEOUT)
                if not chunk:
                    break
                buf += chunk
                await asyncio.sleep(0.002 + 0.002 * (i % 3))
            nbytes += len(buf)
            parts = parse_all(bytes(buf))
            good = sum(norm(h, b) == golden["feed"] for h, b in parts)
            got += good
            if len(parts) != n or good != n:
                err(f"slow reader {i}: {len(parts)} responses, {good} correct, want {n}")
            w.close()
        except Exception as e:
            err(f"slow reader {i}: {e!r}")

    p = asyncio.create_task(probe())
    await asyncio.gather(*(slow(i) for i in range(20)))
    stop.set()
    await p
    worst = max(lat) if lat else -1
    if worst > 1.0:
        err(f"slow reader: probe latency {worst:.3f}s while slow readers were pending")
    return {"slow_conns": 20, "ok_responses": got, "bytes": nbytes,
            "probe_requests": len(lat), "probe_max_ms": round(worst * 1000, 1)}


async def s_halfclose():
    """Requests, then shutdown(SHUT_WR): all responses must arrive, then the server closes. Also
    Connection: close and HTTP/1.0."""
    ok = 0

    async def one(i):
        nonlocal ok
        try:
            r, w = await connect()
            keys = [random.choice(KEYS) for _ in range(1 + i % 5)]
            w.write(b"".join(READS[k] for k in keys))
            await w.drain()
            w.write_eof()
            for k in keys:
                h, b = await read_resp(r)
                ok += check(k, h, b, "halfclose")
            rest = await asyncio.wait_for(r.read(), TIMEOUT)
            if rest:
                err(f"halfclose {i}: extra bytes {rest[:80]!r}")
            w.close()
        except Exception as e:
            err(f"halfclose {i}: {e!r}")

    async def closing(i, req):
        nonlocal ok
        try:
            r, w = await connect()
            w.write(req)
            await w.drain()
            h, b = await read_resp(r)
            if b"connection: close" not in h.lower():
                err(f"close {i}: no Connection: close header")
            rest = await asyncio.wait_for(r.read(), TIMEOUT)  # server must close
            ok += not rest
            w.close()
        except Exception as e:
            err(f"close {i}: {e!r}")

    await asyncio.gather(*(one(i) for i in range(300)))
    await asyncio.gather(*(closing(i, get(f"/posts/{READ_IDS[i]}", b"Connection: close\r\n")) for i in range(100)))
    await asyncio.gather(*(closing(i, b"GET /feed HTTP/1.0\r\n\r\n") for i in range(100)))
    return {"ok": ok}


async def s_abort():
    """Clients that go away: mid-request (FIN and RST), mid-body, mid-response (FIN and RST), right after sending."""
    feed = READS["feed"]
    half = READS["post401"][: len(READS["post401"]) - 10]
    cases = {
        "mid-headers FIN": (b"GET /posts/1 HTTP/1.1\r\nHo", False, 0),
        "mid-headers RST": (b"GET /posts/1 HTTP/1.1\r\nHo", True, 0),
        "mid-body FIN": (half, False, 0),
        "mid-body RST": (half, True, 0),
        "mid-response FIN": (feed * 200, False, 0.01),
        "mid-response RST": (feed * 200, True, 0.01),
        "sent-then-gone FIN": (feed, False, 0),
        "sent-then-gone RST": (feed, True, 0),
        "oversized headers": (b"GET / HTTP/1.1\r\nX: " + b"a" * 20000, False, 0.05),
    }
    counts = {}

    async def one(name, data, use_rst, wait):
        try:
            r, w = await connect(rcvbuf=4096 if "response" in name else None)
            w.write(data)
            await w.drain()
            if wait:
                await asyncio.sleep(wait)
            rst(w) if use_rst else w.close()
            counts[name] = counts.get(name, 0) + 1
        except Exception as e:
            err(f"abort {name}: {e!r}")

    for name, (data, use_rst, wait) in cases.items():
        await asyncio.gather(*(one(name, data, use_rst, wait) for _ in range(300)))
    return counts


async def s_burst():
    """Bursts of connections that connect and leave at once (FIN or RST), with no request."""
    made = 0
    for round_ in range(6):
        use_rst = round_ % 2 == 1

        async def one():
            nonlocal made
            try:
                r, w = await connect()
                rst(w) if use_rst else w.close()
                made += 1
            except Exception as e:
                err(f"burst: {e!r}")

        await asyncio.gather(*(one() for _ in range(1000)))
    return {"connections": made}


def jload(b):
    return json.loads(b.decode())


async def s_writes():
    """Writes on the target only, checked for consistency: concurrent likes, repeated likes, pipelined posts
    (responses wait for the loop's COMMIT), and pipelined posts to a slow reader."""
    toks = args.tokens_list
    target_post = 300001
    r, w = await connect()
    w.write(get(f"/posts/{target_post}"))
    await w.drain()
    _, b = await read_resp(r)
    before = jload(b)["post"]["like_count"]
    liked = 0
    already = 0

    async def likes(chunk, where):
        nonlocal liked, already
        try:
            r, w = await connect()
            for t in chunk:
                w.write(post(f"/posts/{target_post}/like", b"", auth=t["token"]))
                await w.drain()
                h, b = await read_resp(r)
                j = jload(b)
                if h.startswith(b"HTTP/1.1 201") and j == {"liked": True, "already_liked": False, "post_id": target_post}:
                    liked += 1
                elif h.startswith(b"HTTP/1.1 200") and j == {"liked": True, "already_liked": True, "post_id": target_post}:
                    already += 1
                else:
                    err(f"{where}: like answer {h[:40]!r} {b[:100]!r}")
            w.close()
        except Exception as e:
            err(f"{where}: {e!r}")

    users = toks[1000:3000]
    await asyncio.gather(*(likes(users[i:i + 20], "likes") for i in range(0, len(users), 20)))
    first_liked = liked
    liked = already = 0
    await asyncio.gather(*(likes(users[i:i + 20], "likes again") for i in range(0, 400, 20)))
    if liked != 0:
        err(f"likes again: {liked} new likes, want 0")
    w.write(get(f"/posts/{target_post}"))
    await w.drain()
    _, b = await read_resp(r)
    after = jload(b)["post"]["like_count"]
    if after != before + first_liked:
        err(f"likes: like_count {before} -> {after}, but {first_liked} likes were new (201)")
    w.close()

    # Pipelined posts: N POSTs in one write, N 201s in order, each readable afterwards.
    created = []

    async def posts(i, n, rcvbuf=None, slow=0.0):
        try:
            r, w = await connect(rcvbuf=rcvbuf, limit=8192 if rcvbuf else 1 << 16)
            bodies = [f"stress-{i}-{k}-" + "x" * (k % 300) for k in range(n)]
            t = toks[3000 + i]["token"]
            w.write(b"".join(post("/posts", json.dumps({"body": s}).encode(), auth=t) for s in bodies))
            await w.drain()
            for s in bodies:
                if slow:
                    await asyncio.sleep(slow)
                h, b = await read_resp(r)
                if not h.startswith(b"HTTP/1.1 201"):
                    err(f"posts {i}: status {h[:40]!r} {b[:100]!r}")
                    continue
                p = jload(b)["post"]
                if p["body"] != s:
                    err(f"posts {i}: body mismatch {p['body'][:40]!r} vs {s[:40]!r}")
                created.append((p["id"], s))
            w.close()
        except Exception as e:
            err(f"posts {i}: {e!r}")

    await asyncio.gather(*(posts(i, 30) for i in range(50)))
    await asyncio.gather(*(posts(100 + i, 300, rcvbuf=4096, slow=0.001) for i in range(5)))
    ids = [c[0] for c in created]
    if len(set(ids)) != len(ids):
        err(f"posts: duplicate ids ({len(ids) - len(set(ids))})")
    r, w = await connect()
    for pid_, s in random.sample(created, min(200, len(created))):
        w.write(get(f"/posts/{pid_}"))
        await w.drain()
        _, b = await read_resp(r)
        if jload(b)["post"]["body"] != s:
            err(f"posts: read back {pid_} differs")
    w.close()
    return {"likes_true": first_liked, "like_count": [before, after], "posts_created": len(created)}


async def s_soak():
    """Mixed load for --soak seconds: keep-alive readers, connection churn (FIN/RST, mid-request aborts),
    pipeliners, writers. RSS and fds sampled every 10 s."""
    end = time.time() + args.soak
    reqs = 0
    samples = []
    by = {"reader": 0, "churn": 0, "pipeliner": 0, "writer": 0}

    async def reader():
        nonlocal reqs
        r, w = await connect()
        while time.time() < end:
            k = random.choice(POST_KEYS)
            ok = await exchange(r, w, k, "soak reader")
            reqs += ok
            by["reader"] += 1
        w.close()

    async def churn():
        nonlocal reqs
        while time.time() < end:
            try:
                r, w = await connect()
                for _ in range(random.randint(0, 4)):
                    ok = await exchange(r, w, random.choice(POST_KEYS + ["404", "badid", "post401"]), "soak churn")
                    reqs += ok
                    by["churn"] += 1
                x = random.random()
                if x < 0.2:
                    w.write(b"GET /posts/1 HT")
                    await w.drain()
                if x < 0.5:
                    rst(w)
                else:
                    w.close()
            except Exception as e:
                err(f"soak churn: {e!r}")
                await asyncio.sleep(0.1)

    async def pipeliner():
        nonlocal reqs
        r, w = await connect()
        while time.time() < end:
            keys = [random.choice(POST_KEYS) for _ in range(20)]
            w.write(b"".join(READS[k] for k in keys))
            await w.drain()
            for k in keys:
                h, b = await read_resp(r)
                reqs += check(k, h, b, "soak pipeliner")
                by["pipeliner"] += 1
            await asyncio.sleep(0.05)
        w.close()

    async def writer(i):
        nonlocal reqs
        r, w = await connect()
        t = args.tokens_list[5000 + i]["token"]
        n = 0
        while time.time() < end:
            n += 1
            w.write(post("/posts", json.dumps({"body": f"soak {i} {n}"}).encode(), auth=t))
            await w.drain()
            h, _ = await read_resp(r)
            if not h.startswith(b"HTTP/1.1 201"):
                err(f"soak writer: {h[:40]!r}")
            reqs += 1
            by["writer"] += 1
            await asyncio.sleep(0.05)
        w.close()

    async def sampler():
        t0 = time.time()
        while time.time() < end:
            samples.append((round(time.time() - t0), round(rss_mb(), 1), fd_count(), reqs))
            print(f"  t={samples[-1][0]:4d}s rss={samples[-1][1]} MB fds={samples[-1][2]} requests={reqs} {by}", flush=True)
            await asyncio.sleep(10)

    async def guard(coro):
        try:
            await coro
        except Exception as e:
            err(f"soak: {e!r}")

    await asyncio.gather(sampler(), *(guard(reader()) for _ in range(200)), *(guard(churn()) for _ in range(30)),
                         *(guard(pipeliner()) for _ in range(5)), *(guard(writer(i)) for i in range(3)))
    # RSS growth: the max of the last third against the max of the middle third (after warm-up).
    third = max(1, len(samples) // 3)
    mid = max(s[1] for s in samples[third:2 * third] or samples)
    last = max(s[1] for s in samples[2 * third:] or samples)
    if len(samples) >= 6 and last > mid * 1.10 and last - mid > 10:
        err(f"soak: RSS grew from {mid} MB to {last} MB")
    return {"seconds": args.soak, "requests": reqs, "rss_mid_mb": mid, "rss_last_mb": last,
            "rss_samples": [s[1] for s in samples]}


SCENARIOS = {
    "sequential": s_sequential, "concurrent": s_concurrent, "pipeline": s_pipeline, "bytewise": s_bytewise,
    "slow": s_slow_reader, "halfclose": s_halfclose, "abort": s_abort, "burst": s_burst, "writes": s_writes,
    "soak": s_soak,
}


async def fetch_golden():
    host, port = args.ref
    r, w = await asyncio.open_connection(host, port)
    for k in KEYS:
        w.write(READS[k])
        await w.drain()
        h, b = await read_resp(r)
        golden[k] = norm(h, b)
        raw_len[k] = len(h) + len(b)
    w.close()


async def main():
    global args
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True)
    ap.add_argument("--ref", required=True)
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--tokens", required=True)
    ap.add_argument("--conns", type=int, default=10000)
    ap.add_argument("--idle", type=float, default=15)
    ap.add_argument("--depth", type=int, default=50)
    ap.add_argument("--slow-depth", type=int, default=400)
    ap.add_argument("--soak", type=int, default=300)
    ap.add_argument("--only", default=",".join(SCENARIOS))
    args = ap.parse_args()
    args.target = (args.target.rsplit(":", 1)[0], int(args.target.rsplit(":", 1)[1]))
    args.ref = (args.ref.rsplit(":", 1)[0], int(args.ref.rsplit(":", 1)[1]))
    args.tokens_list = json.load(open(args.tokens))
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
    await fetch_golden()
    print(f"golden: {len(golden)} responses from the reference server; /feed is {len(golden['feed'])} bytes", flush=True)
    # Warm up (every read once, so lazily opened files exist), then take the baseline.
    r, w = await connect()
    for k in KEYS[:50]:
        await exchange(r, w, k, "warm-up")
    w.close()
    await asyncio.sleep(1)
    base = fd_count()
    base_socks = socket_fds()
    print(f"target pid {args.pid}: baseline fds {base} (sockets {base_socks}), rss {rss_mb():.1f} MB", flush=True)
    for name in args.only.split(","):
        n0 = len(errors)
        t0 = time.time()
        print(f"== {name}", flush=True)
        try:
            res = await SCENARIOS[name]()
        except Exception as e:
            err(f"{name}: crashed {e!r}")
            res = {}
        await drain(name, base, base_socks)
        stats[name] = {"errors": len(errors) - n0, "seconds": round(time.time() - t0, 1), **res}
        print(f"  {'PASS' if len(errors) == n0 else 'FAIL'} {name}: {json.dumps(stats[name])}", flush=True)
    # Still healthy and correct at the end.
    r, w = await connect()
    ok = sum([await exchange(r, w, k, "final") for k in POST_KEYS[:200]])
    w.close()
    await drain("final", base, base_socks)
    print(f"final check: {ok}/200 correct; socket fds {socket_fds()} (baseline {base_socks}); "
          f"all fds {fd_count()} (baseline {base}); rss {rss_mb():.1f} MB")
    if ok != 200:
        err(f"final: only {ok}/200 correct")
    print(f"STRESS {'OK' if not errors else 'FAILED'}: {len(errors)} error(s)")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    asyncio.run(main())
