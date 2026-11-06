#!/usr/bin/env python3
# crash.py <server binary> <reps>: writers on many connections, kill -9 mid-run, reopen the file: every acked write must be there, integrity ok.
import asyncio, json, os, random, shutil, signal, sqlite3, subprocess, sys, tempfile, time
BIN=sys.argv[1]; REPS=int(sys.argv[2]); SEED="/root/watlab/seed"; PORT=3998
tokens=json.load(open(SEED+"/tokens.json"))
async def writer(i, acked_posts, acked_likes, stop):
    r,w=await asyncio.open_connection("127.0.0.1",PORT)
    t=tokens[i%len(tokens)]["token"]
    try:
        while not stop.is_set():
            if random.random()<0.5:
                b=json.dumps({"body":"crash test %d %f"%(i,time.time())}).encode()
                w.write(b"POST /posts HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer "+t.encode()+b"\r\nContent-Type: application/json\r\nContent-Length: "+str(len(b)).encode()+b"\r\n\r\n"+b); kind=0
            else:
                pid=random.randint(1,500000)
                w.write(b"POST /posts/%d/like HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer "%pid+t.encode()+b"\r\nContent-Length: 0\r\n\r\n"); kind=1
            await w.drain()
            h=await r.readuntil(b"\r\n\r\n")
            n=int([l for l in h.split(b"\r\n") if l.lower().startswith(b"content-length")][0].split(b":")[1])
            body=await r.readexactly(n)
            if kind==0 and h.startswith(b"HTTP/1.1 201"): acked_posts.append(json.loads(body)["post"]["id"] if b'"post"' in body else json.loads(body)["id"])
            if kind==1 and h[9:12] in (b"200",b"201"): acked_likes.append((pid,t))
    except Exception: pass
    finally:
        try: w.close()
        except Exception: pass
async def run(p, secs, posts, likes):
    stop=asyncio.Event()
    ts=[asyncio.create_task(writer(i,posts,likes,stop)) for i in range(400)]
    await asyncio.sleep(secs)
    try: kids=open("/proc/%d/task/%d/children"%(p.pid,p.pid)).read().split()
    except Exception: kids=[]
    os.kill(p.pid, signal.SIGKILL)
    await asyncio.sleep(0.05)
    for k in kids:
        try: os.kill(int(k), signal.SIGKILL)
        except Exception: pass
    await asyncio.sleep(0.3); stop.set()
    await asyncio.gather(*ts, return_exceptions=True)
ok=True
for rep in range(REPS):
    d=tempfile.mkdtemp(prefix="crash"); db=d+"/feed.db"; shutil.copy(SEED+"/feed.db",db)
    env=dict(os.environ,JWT_SECRET="twelve-dollar-challenge",HOST="127.0.0.1",PORT=str(PORT),SQLITE_PATH=db)
    p=subprocess.Popen([BIN],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    time.sleep(6)
    posts=[]; likes=[]
    asyncio.run(run(p, 8+rep*3, posts, likes))
    p.wait(); time.sleep(0.5)
    wal=os.path.getsize(db+"-wal") if os.path.exists(db+"-wal") else 0
    c=sqlite3.connect(db)
    ic=c.execute("PRAGMA integrity_check").fetchone()[0]
    have=set(x[0] for x in c.execute("SELECT id FROM posts WHERE id IN (%s)"%",".join(map(str,posts[-30000:])))) if posts else set()
    miss=[x for x in posts[-30000:] if x not in have]
    print("rep",rep,"acked posts",len(posts),"likes",len(likes),"wal bytes",wal,"integrity",ic,"missing acked posts",len(miss),flush=True)
    ok=ok and ic=="ok" and not miss
    c.close(); shutil.rmtree(d)
print("PASS" if ok else "FAIL")
