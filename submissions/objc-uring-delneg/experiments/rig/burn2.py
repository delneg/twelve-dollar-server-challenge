# burn2.py <hold pct> <ramp pct> <ramp s>: like burn.py, but takes <ramp pct> for the first <ramp s>, then <hold pct>.
# Stands in for a connection's extra cost on a real NIC while connections arrive (ramp pct - hold pct = conn/s x extra us).
import sys, time
hold, ramp, rs = float(sys.argv[1]) / 100, float(sys.argv[2]) / 100, float(sys.argv[3])
W = 0.02
t0 = time.perf_counter()
while True:
    t = time.perf_counter()
    p = ramp if t - t0 < rs else hold
    e = t + W * p
    while time.perf_counter() < e: pass
    time.sleep(max(0, W * (1 - p)))
