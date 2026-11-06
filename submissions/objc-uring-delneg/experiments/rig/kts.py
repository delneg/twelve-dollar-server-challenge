# kts.py: pid comm cpu_ticks of every task (threads included)
import os
for p in os.listdir('/proc'):
    if not p.isdigit(): continue
    try:
        for t in os.listdir(f'/proc/{p}/task'):
            s=open(f'/proc/{p}/task/{t}/stat').read()
            c=s[s.index('(')+1:s.rindex(')')]; f=s[s.rindex(')')+2:].split()
            print(t,c.replace(' ','_'),int(f[11])+int(f[12]),f[36])
    except Exception: pass
