import sys
L,a,b=sys.argv[1],int(sys.argv[2]),int(sys.argv[3])
S={}
for l in open("w/%s.samp"%L):
    p=l.split()
    if p and p[0].isdigit(): S[int(p[0])]=dict(x.split("=",1) for x in p[1:] if "=" in x)
T={}
for l in open("w/%s.ts"%L):
    p=l.split()
    if len(p)>=2 and p[0].isdigit(): T[int(p[0])]=int(p[1])
while a not in S: a+=1
while b not in S: b-=1
r=sum(v for t,v in T.items() if a<=t<b)
print(L,a,b,"app_us=%.2f"%((int(S[b]["ucg"])-int(S[a]["ucg"]))/r))
