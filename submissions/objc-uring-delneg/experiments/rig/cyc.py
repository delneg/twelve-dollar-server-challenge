#!/usr/bin/env python3
# cyc.py <label> <start> <secs>...: cycles and instructions per request from perf stat -p over [start, start+secs]
import sys
L=sys.argv[1]; a=int(sys.argv[2]); n=int(sys.argv[3])
T={}
for l in open("w/%s.ts"%L):
    p=l.split()
    if len(p)>=2 and p[0].isdigit(): T[int(p[0])]=int(p[1])
reqs=sum(v for t,v in T.items() if a<=t<a+n)
out=[]
for l in open("w/%s.pstat"%L):
    if l.startswith("#") or not l.strip(): continue
    p=l.split(",")
    if not p[0].strip().isdigit(): continue
    out.append("%s=%.0f"%(p[2].replace("syscalls:sys_enter_",""),int(p[0])/reqs))
print(L,"reqs",reqs," per req:"," ".join(out))
