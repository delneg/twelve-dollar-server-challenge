# hog.py <MiB>: hold that much anonymous memory, touched, until killed (no swap on Synteq, so it can't be reclaimed)
import sys, time, mmap
n = int(sys.argv[1]) << 20
m = mmap.mmap(-1, n, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
for i in range(0, n, 4096): m[i] = 1
print("hog holds", n >> 20, "MiB", flush=True)
while True: time.sleep(3600)
