# sum.py <log> <hold_s>: digest of a run: CPU 0 breakdown in the hold, top other tasks, 5 s timeline of latency and memory
import sys,re
L=sys.argv[1]; H=int(sys.argv[2])
rows=[]
for l in open(L+'.samp'):
    if ' c0=' not in l: continue
    d=dict(re.findall(r'(\w+)=(\S+)',l)); d['t']=int(l.split()[0]); rows.append(d)
if not rows: sys.exit()
def c0(r): return [int(x) for x in r['c0'].split(',')]
def win(a,b):
    A=[r for r in rows if a<=r['t']<=b]
    return (A[0],A[-1]) if len(A)>1 else (None,None)
RAMP=60
def cpuline(name,a,b):
    x,y=win(a,b)
    if not x: return
    d=[q-p for p,q in zip(c0(x),c0(y))]; tot=sum(d) or 1; dt=y['t']-x['t']
    # us+ni, sys, idle, iow, irq, si, steal
    app=(int(y['ucg'])-int(x['ucg']))/1e6/dt*100
    ksi=(int(y['ksi'])-int(x['ksi']))/dt
    print(f"  {name}: app {app:.1f}% | cpu0 user {100*d[0]/tot:.0f} sys {100*d[1]/tot:.0f} si {100*d[5]/tot:.0f} irq {100*d[4]/tot:.0f} idle {100*d[2]/tot:.0f} iow {100*d[3]/tot:.0f} steal {100*d[6]/tot:.1f} | ksoftirqd {ksi:.1f}%")
cpuline('ramp',10,RAMP)
cpuline('hold',RAMP+10,RAMP+H)
# other tasks
try:
    a={};b={}
    for f,D in ((L+'.kt0',a),(L+'.kt1',b)):
        for l in open(f):
            p=l.split(); D[p[0]]=(p[1],int(p[2]),p[3])
    top=sorted(((b[k][1]-a.get(k,(0,0,0))[1],b[k][0],b[k][2]) for k in b),reverse=True)[:12]
    print('  tasks ticks(cpu0 at end): '+' '.join(f"{n}:{t}({c})" for t,n,c in top))
except Exception as e: print(e)
ts={}
try:
    for l in open(L+'.ts'):
        p=l.split(); ts[int(p[0])]=[int(p[1]),int(p[2]),int(p[3]),int(p[4]),float(p[5])]
except Exception: pass
num=lambda r,k:int(re.sub(r'[^0-9-]','',r.get(k,'0')) or 0)
print('   t   req/s  >.5s  >1s  mean  app%  busy%  si%  cgMB sock tcp file wal dirty free rdMB wrMB scanK majf rxmit')
step=5
bytm={r['t']:r for r in rows}
offs=None
maxT=max(ts) if ts else rows[-1]['t']
tl=sorted(bytm)
for s in range(0,maxT+1,step):
    n=sum(ts.get(i,[0]*5)[0] for i in range(s,s+step))
    a5=sum(ts.get(i,[0]*5)[1] for i in range(s,s+step)); a1=sum(ts.get(i,[0]*5)[2] for i in range(s,s+step))
    m=(sum(ts.get(i,[0]*5)[4]*ts.get(i,[0]*5)[0] for i in range(s,s+step))/n) if n else 0
    x=bytm.get(s) or bytm.get(s+1); y=bytm.get(s+step) or bytm.get(s+step+1)
    extra=''
    if x and y:
        dt=(y['t']-x['t']) or 1
        d=[q-p for p,q in zip(c0(x),c0(y))]; tot=sum(d) or 1
        extra=f"{(int(y['ucg'])-int(x['ucg']))/1e4/dt:5.1f} {100*(tot-d[2]-d[3])/tot:5.0f} {100*d[5]/tot:4.0f} {num(y,'cgm'):5d} {num(y,'sock'):4d} {num(y,'tcp'):5d} {num(y,'file'):4d} {num(y,'wal'):4d} {num(y,'dirty'):4d} {num(y,'free'):4d} {num(y,'rd')-num(x,'rd'):4d} {num(y,'wr')-num(x,'wr'):4d} {num(y,'pgscan_kswapd')-num(x,'pgscan_kswapd'):6d} {num(y,'pgmajfault')-num(x,'pgmajfault'):4d} {num(y,'RetransSegs')-num(x,'RetransSegs'):5d}"
    print(f"{s:5d} {n//step:6d} {a5//step:5d} {a1//step:5d} {m:6.1f} {extra}")
