# burn3.py <hold pct> <ramp pct> <ramp s>: takes pct % of CPU counted in CPU time, in 20 ms cycles, so it doesn't give up its share
# when the CPU is contended (burn.py and burn2.py spin on the wall clock and do). <ramp pct> for the first <ramp s>, then <hold pct>.
import sys, time
hold, ramp, rs = float(sys.argv[1]) / 100, float(sys.argv[2]) / 100, float(sys.argv[3])
W = 0.02
t0 = time.perf_counter()
while True:
    t = time.perf_counter()
    p = ramp if t - t0 < rs else hold
    c = time.process_time()
    while time.process_time() - c < W * p: pass
    r = W - (time.perf_counter() - t)
    if r > 0: time.sleep(r)
