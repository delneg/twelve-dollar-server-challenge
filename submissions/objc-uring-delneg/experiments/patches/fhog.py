# fhog.py <final MiB> <hole MiB>: like hog.py, but maps final + hole, touches it all in 4 KB pages, then frees evenly spaced single pages
# until only <final> is held, so free memory is scattered and no free 2 MB block exists without compaction. Stress test for huge page faults.
import sys, time, mmap
final, hole = int(sys.argv[1]), int(sys.argv[2])
n = (final + hole) << 20
m = mmap.mmap(-1, n, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
m.madvise(mmap.MADV_NOHUGEPAGE)
for i in range(0, n, 4096): m[i] = 1
pages = n >> 12
free = hole << 8
step = pages / free
for k in range(free):
    m.madvise(mmap.MADV_DONTNEED, int(k * step) << 12, 4096)
print("hog holds", final, "MiB with", hole, "MiB of holes", flush=True)
while True: time.sleep(3600)
