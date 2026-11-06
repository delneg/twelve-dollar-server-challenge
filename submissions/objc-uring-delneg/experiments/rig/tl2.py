import sys,re
L=sys.argv[1]
S={}
for l in open("/root/objc3/w/%s.samp"%L):
    p=l.split()
    if p and p[0].isdigit():
        d=dict(x.split("=",1) for x in p[1:] if "=" in x); S[int(p[0])]=d
T={}
for l in open("/root/objc3/w/%s.ts"%L):
    p=l.split()
    if len(p)>=2 and p[0].isdigit(): T[int(p[0])]=p[1:]
keys=sorted(S)
step=int(sys.argv[2]) if len(sys.argv)>2 else 10
print("t  rps  rest-of-ts | free file cgm scanK dstall majf ucg%")
prev=None
for t in keys:
    if t%step: continue
    d=S[t]
    ts=T.get(t,[])
    if prev:
        def dl(k): 
            try: return int(d.get(k,0))-int(prev.get(k,0))
            except: return 0
        print(t, " ".join(ts[:6]), "| free",d.get("free"),"file",d.get("file"),"cgm",d.get("cgm"),"scan",dl("pgscan_kswapd")//1000,"ds",dl("pgscan_direct")//1000,"stall",dl("allocstall_normal"),"majf",dl("pgmajfault"),"ucg%%=%.0f"%(dl("ucg")/1e4/step))
    prev=d
