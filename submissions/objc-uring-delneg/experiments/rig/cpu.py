#!/usr/bin/env python3
# cpu.py <label>...: app and CPU 0 microseconds per request over the steady part of the hold (t=85..end of hold)
import sys
for L in sys.argv[1:]:
    S={}
    for l in open("w/%s.samp"%L):
        p=l.split()
        if not p or not p[0].isdigit(): continue
        S[int(p[0])]=dict(x.split("=",1) for x in p[1:] if "=" in x)
    T={}
    for l in open("w/%s.ts"%L):
        p=l.split()
        if len(p)>=2 and p[0].isdigit(): T[int(p[0])]=int(p[1])
    ts=sorted(T); end=max(ts)-40
    a=85; b=end
    while a not in S: a+=1
    while b not in S: b-=1
    reqs=sum(T[t] for t in ts if a<=t<b)
    def c0(d):
        v=[int(x) for x in d["c0"].split(",")]
        return v
    x,y=c0(S[a]),c0(S[b])
    # user,nice?,sys,idle,iow,irq,softirq order from the sampler: print raw split as jiffies
    dv=[j-i for i,j in zip(x,y)]
    app=(int(S[b]["ucg"])-int(S[a]["ucg"]))
    tot=sum(dv)
    idle=dv[2]+dv[3] if len(dv)>=7 else 0
    busy=(tot-idle)*10000
    print(L,"t=%d-%d reqs=%d rps=%.0f app_us=%.2f busy_us=%.2f raw=%s"%(a,b,reqs,reqs/(b-a),app/reqs,busy/reqs,dv))
