// ul.c: user-model load generator for the $12 challenge (bench/load.js loop, think times scaled).
// Each user: GET /feed -> think 3-7 -> GET /posts/:id (from the feed) -> think 3-8 -> 15% like,
// 2% create -> think 5-15. Think times are multiplied by SCALE. Keep-alive; reconnects when the
// server closes or answers "Connection: close" (like Go's net/http in k6).
// usage: ul <port> <users> <scale> <ramp_s> <measure_s> <tokens.json> <server_pid> [threads]
// prints: req/s, latency p50/p95/p99/max (ms), errors, server CPU us/req, server RSS.
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define WHEEL 65536 /* ms slots */
#define SECS 1200
#define NB 4000     /* histogram buckets of 50 us up to 200 ms, then 5 ms buckets to ~5.3 s */
enum { S_IDLE, S_CONN, S_SEND, S_RECV };
typedef struct {
  int fd, st, step, next; /* step: 0 feed,1 post,2 like?,3 create?,4 final sleep */
  int post_id, tok, sent, outn, inn, alive;
  int64_t t0;
  char out[700];
  char in[16384];
} user_t;

static uint32_t DST = 0x7f000001, SRC = 0x7f000002; /* env UL_DST, UL_SRC (dotted) */
static int PORT, NUSERS, NTH = 1, RAMP, MEAS, SPID;
static double SCALE;
static char **toks, **names;
static int ntok;
static volatile int phase; /* 0 ramp, 1 measure, 2 stop */
static int64_t t_start;
static unsigned long E[6]; /* error kinds: 0 send,1 recv0,2 recverr,3 status,4 connect,5 recv0-reused */
#define EK(k) __atomic_add_fetch(&E[k], 1, __ATOMIC_RELAXED)

typedef struct {
  user_t *u;
  int n, ep, base;
  int *head; /* wheel heads, -1 terminated via u.next */
  int64_t now_ms, cur_slot;
  uint64_t hist[NB + 1024], reqs, errs, cerr, maxus, reconn;
  uint64_t ahist[NB + 1024], areqs, aerrs, amaxus; /* k6 view: every request of the run */
  uint32_t tsn[SECS], ts05[SECS], ts1[SECS], ts2[SECS]; uint64_t tsus[SECS];
  unsigned seed;
} th_t;

static int64_t now_us(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return (int64_t)t.tv_sec * 1000000 + t.tv_nsec / 1000; }
static double rnd(th_t *T) { return (rand_r(&T->seed) & 0xffffff) / (double)0x1000000; }
static void sched(th_t *T, int i, double sec) {
  int64_t ms = (int64_t)(sec * SCALE * 1000); if (ms < 1) ms = 1; if (ms >= WHEEL) ms = WHEEL - 1;
  int64_t slot = (T->now_ms + ms) % WHEEL;
  T->u[i].next = T->head[slot]; T->head[slot] = i; T->u[i].st = S_IDLE;
}
static void rec(th_t *T, int64_t us, int ok) {
  { int64_t b0 = us / 50; if (b0 >= NB) { b0 = NB + (us - 200000) / 5000; if (b0 >= NB + 1024) b0 = NB + 1023; }
    T->ahist[b0]++; T->areqs++; if (!ok) T->aerrs++; if ((uint64_t)us > T->amaxus) T->amaxus = us;
    int sec = (int)((now_us() - t_start) / 1000000); if (sec >= 0 && sec < SECS) { T->tsn[sec]++; T->tsus[sec] += us; if (us > 500000) T->ts05[sec]++; if (us > 1000000) T->ts1[sec]++; if (us > 2000000) T->ts2[sec]++; } }
  if (phase != 1) return;
  T->reqs++; if (!ok) T->errs++;
  if ((uint64_t)us > T->maxus) T->maxus = us;
  int64_t b = us / 50; if (b >= NB) { b = NB + (us - 200000) / 5000; if (b >= NB + 1024) b = NB + 1023; }
  T->hist[b]++;
}
static int dial(th_t *T, user_t *u, int i) {
  int fd = socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK, 0); if (fd < 0) return -1;
  int one = 1; setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
  struct sockaddr_in a = { .sin_family = AF_INET, .sin_port = htons(PORT) }; a.sin_addr.s_addr = htonl(DST);
  { /* spread over 127.0.0.2-33 so >64k connections get distinct source ports, with few per address */
    struct sockaddr_in b = { .sin_family = AF_INET }; b.sin_addr.s_addr = htonl(SRC + ((T->base + i) % 32));
    setsockopt(fd, IPPROTO_IP, IP_BIND_ADDRESS_NO_PORT, &one, sizeof one); bind(fd, (void *)&b, sizeof b); }
  int r = connect(fd, (void *)&a, sizeof a);
  if (r < 0 && errno != EINPROGRESS) { close(fd); return -1; }
  struct epoll_event ev = { .events = EPOLLOUT | EPOLLIN | EPOLLET, .data.u32 = i };
  epoll_ctl(T->ep, EPOLL_CTL_ADD, fd, &ev);
  u->fd = fd; u->alive = 1; T->reconn++;
  return 0;
}
static void drop(th_t *T, user_t *u) { if (u->fd >= 0) { epoll_ctl(T->ep, EPOLL_CTL_DEL, u->fd, NULL); close(u->fd); } u->fd = -1; u->alive = 0; }
static void fail(th_t *T, int i) { user_t *u = &T->u[i]; rec(T, now_us() - u->t0, 0); drop(T, u); u->step = 0; sched(T, i, 3 + 4 * rnd(T)); }

static volatile int64_t stop_base_ms; /* set at hold end; 0 until then */
static void start_req(th_t *T, int i) {
  user_t *u = &T->u[i];
  if (stop_base_ms) { int64_t st = stop_base_ms + (int64_t)(30000.0 * (T->base + i) / NUSERS);
    if (T->now_ms > st + 10000 || (u->step == 0 && T->now_ms > st)) { drop(T, u); u->st = S_IDLE; return; } }
  int tk = u->tok;
  if (u->step == 0) u->outn = sprintf(u->out, "GET /feed HTTP/1.1\r\nHost: x\r\n\r\n");
  else if (u->step == 1) u->outn = sprintf(u->out, "GET /posts/%d HTTP/1.1\r\nHost: x\r\n\r\n", u->post_id);
  else if (u->step == 2) u->outn = sprintf(u->out, "POST /posts/%d/like HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer %s\r\nContent-Length: 0\r\n\r\n", u->post_id, toks[tk]);
  else {
    char b[200]; int bl = sprintf(b, "{\"body\":\"%s says hi at %lld (VU %d)\"}", names[tk], (long long)now_us(), i);
    u->outn = sprintf(u->out, "POST /posts HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s", toks[tk], bl, b);
  }
  u->sent = 0; u->inn = 0; u->t0 = now_us();
  if (!u->alive) { if (dial(T, u, i) < 0) { T->cerr++; fail(T, i); return; } u->st = S_CONN; return; }
  u->st = S_SEND;
  ssize_t w = send(u->fd, u->out, u->outn, MSG_NOSIGNAL);
  if (w < 0 && errno != EAGAIN) { /* stale keep-alive: reconnect once */ drop(T, u); if (dial(T, u, i) < 0) { fail(T, i); return; } u->st = S_CONN; return; }
  if (w > 0) u->sent = w;
  if (u->sent == u->outn) u->st = S_RECV;
}
static void done(th_t *T, int i, int status, int closehdr, char *body, int blen) {
  user_t *u = &T->u[i];
  int ok = 0;
  if (u->step == 0) {
    ok = status == 200;
    if (ok) { /* pick a random post id from the feed */
      int ids[20], n = 0; char *p = body, *e = body + blen;
      while (n < 20 && (p = memmem(p, e - p, "{\"id\":", 6))) { p += 6; ids[n++] = atoi(p); }
      if (n == 0) ok = 0; else u->post_id = ids[rand_r(&T->seed) % n];
    }
  } else if (u->step == 1) ok = status == 200;
  else if (u->step == 2) ok = status == 200 || status == 201;
  else ok = status == 201;
  rec(T, now_us() - u->t0, ok); if (!ok) EK(3);
  if (closehdr) drop(T, u);
  if (!ok && u->step == 0) { u->step = 0; sched(T, i, 3 + 4 * rnd(T)); return; }
  switch (u->step) {
  case 0: u->step = 1; sched(T, i, 3 + 4 * rnd(T)); break;
  case 1: case 2: case 3: {
    int s = u->step; double sl = s == 1 ? 3 + 5 * rnd(T) : 0;
    int nx = 4;
    if (s < 2 && rnd(T) < 0.15) nx = 2; else if (s < 3 && rnd(T) < 0.02) nx = 3;
    if (nx == 4) { u->step = 0; sched(T, i, sl + 5 + 10 * rnd(T)); }
    else { u->step = nx; if (sl > 0) sched(T, i, sl); else start_req(T, i); }
    break;
  }
  }
}
static void on_io(th_t *T, int i) {
  user_t *u = &T->u[i];
  if (u->fd < 0) return;
  if (u->st == S_IDLE) { /* server closed an idle keep-alive */
    char c; ssize_t r = recv(u->fd, &c, 1, MSG_DONTWAIT | MSG_PEEK);
    if (r == 0 || (r < 0 && errno != EAGAIN)) drop(T, u);
    return;
  }
  if (u->st == S_CONN) {
    int err = 0; socklen_t l = sizeof err; getsockopt(u->fd, SOL_SOCKET, SO_ERROR, &err, &l);
    if (err) { EK(4); T->cerr++; fail(T, i); return; }
    u->st = S_SEND; u->t0 = now_us();
  }
  if (u->st == S_SEND) {
    ssize_t w = send(u->fd, u->out + u->sent, u->outn - u->sent, MSG_NOSIGNAL);
    if (w < 0) { if (errno == EAGAIN) return; EK(0); fail(T, i); return; }
    u->sent += w; if (u->sent < u->outn) return;
    u->st = S_RECV;
  }
  for (;;) {
    ssize_t r = recv(u->fd, u->in + u->inn, sizeof u->in - 1 - u->inn, 0);
    if (r < 0) { if (errno == EAGAIN) return; EK(2); fail(T, i); return; }
    if (r == 0) { EK(1); fail(T, i); return; }
    u->inn += r; u->in[u->inn] = 0;
    char *h = memmem(u->in, u->inn, "\r\n\r\n", 4);
    if (!h) continue;
    int hl = h + 4 - u->in, cl = 0, cls = 0;
    char *p = strcasestr(u->in, "\r\ncontent-length:"); if (p && p < h) cl = atoi(p + 17);
    p = strcasestr(u->in, "\r\nconnection: close"); if (p && p < h) cls = 1;
    if (u->inn < hl + cl) continue;
    int status = atoi(u->in + 9);
    done(T, i, status, cls, u->in + hl, cl);
    return;
  }
}
static void *run(void *arg) {
  th_t *T = arg;
  T->ep = epoll_create1(0);
  T->head = malloc(sizeof(int) * WHEEL); for (int s = 0; s < WHEEL; s++) T->head[s] = -1;
  T->now_ms = (now_us() - t_start) / 1000; T->cur_slot = T->now_ms;
  for (int i = 0; i < T->n; i++) { user_t *u = &T->u[i]; u->fd = -1; u->tok = (T->base + i) % ntok; u->step = 0;
    double d = RAMP * (double)(T->base + i) / NUSERS + 5 * rnd(T) * SCALE; /* ramp + load.js initial spread */
    int64_t ms = (int64_t)(d * 1000); if (ms >= WHEEL) ms = WHEEL - 1; int64_t sl = (T->now_ms + 1 + ms) % WHEEL;
    u->next = T->head[sl]; T->head[sl] = i; u->st = S_IDLE; }
  struct epoll_event evs[512];
  while (phase != 2) {
    int n = epoll_wait(T->ep, evs, 512, 1);
    for (int k = 0; k < n; k++) on_io(T, evs[k].data.u32);
    T->now_ms = (now_us() - t_start) / 1000;
    while (T->cur_slot <= T->now_ms) {
      int s = T->cur_slot % WHEEL, i = T->head[s]; T->head[s] = -1;
      while (i >= 0) { int nx = T->u[i].next; start_req(T, i); i = nx; }
      T->cur_slot++;
    }
  }
  return NULL;
}
static void load_tokens(const char *path) {
  FILE *f = fopen(path, "r"); if (!f) { perror(path); exit(1); }
  fseek(f, 0, SEEK_END); long sz = ftell(f); rewind(f); char *s = malloc(sz + 1); fread(s, 1, sz, f); s[sz] = 0;
  toks = malloc(sizeof(char *) * 40000); names = malloc(sizeof(char *) * 40000);
  char *p = s;
  while ((p = strstr(p, "\"username\":\"")) && ntok < 40000) {
    p += 12; char *e = strchr(p, '"'); *e = 0; names[ntok] = p; p = e + 1;
    p = strstr(p, "\"token\":\""); p += 9; e = strchr(p, '"'); *e = 0; toks[ntok++] = p; p = e + 1;
  }
}
static double cpu_s(int pid) { /* utime+stime of pid and its threads */
  char fn[64]; sprintf(fn, "/proc/%d/stat", pid); FILE *f = fopen(fn, "r"); if (!f) return 0;
  char b[1024]; size_t n = fread(b, 1, sizeof b - 1, f); fclose(f); b[n] = 0;
  char *p = strrchr(b, ')'); unsigned long ut, st; int k = 0; char *q = strtok(p + 2, " ");
  while (q && k < 12) { q = strtok(NULL, " "); k++; } ut = strtoul(q, 0, 10); q = strtok(NULL, " "); st = strtoul(q, 0, 10);
  return (ut + st) / (double)sysconf(_SC_CLK_TCK);
}
static long rss_mb(int pid) { char fn[64]; sprintf(fn, "/proc/%d/statm", pid); FILE *f = fopen(fn, "r"); if (!f) return 0; long a, b; fscanf(f, "%ld %ld", &a, &b); fclose(f); return b * 4096 / 1048576; }
int main(int argc, char **argv) {
  if (argc < 8) { fprintf(stderr, "usage: ul <port> <users> <scale> <ramp_s> <measure_s> <tokens.json> <server_pid> [threads]\n"); return 2; }
  PORT = atoi(argv[1]); NUSERS = atoi(argv[2]); SCALE = atof(argv[3]); RAMP = atoi(argv[4]); MEAS = atoi(argv[5]); SPID = atoi(argv[7]);
  if (argc > 8) NTH = atoi(argv[8]);
  if (getenv("UL_DST")) DST = ntohl(inet_addr(getenv("UL_DST")));
  if (getenv("UL_SRC")) SRC = ntohl(inet_addr(getenv("UL_SRC")));
  struct rlimit rl; getrlimit(RLIMIT_NOFILE, &rl); rl.rlim_cur = rl.rlim_max; setrlimit(RLIMIT_NOFILE, &rl);
  load_tokens(argv[6]);
  t_start = now_us();
  th_t *T = calloc(NTH, sizeof *T); pthread_t *tid = calloc(NTH, sizeof *tid);
  user_t *all = calloc(NUSERS, sizeof(user_t));
  int per = (NUSERS + NTH - 1) / NTH;
  for (int t = 0; t < NTH; t++) { T[t].base = t * per; T[t].n = (t + 1) * per > NUSERS ? NUSERS - t * per : per; T[t].u = all + T[t].base; T[t].seed = 1234 + t; pthread_create(&tid[t], 0, run, &T[t]); }
  /* ramp + 1 think cycle of settling, then measure */
  double settle = RAMP + 10 * SCALE; usleep((useconds_t)(settle * 1e6));
  double c0 = cpu_s(SPID); int64_t m0 = now_us(); phase = 1;
  usleep((useconds_t)MEAS * 1000000);
  phase = 3; double c1 = cpu_s(SPID); int64_t m1 = now_us();
  { char fn[64]; const char *cgp = getenv("UL_CG"); (void)fn; (void)cgp; }
  stop_base_ms = (now_us() - t_start) / 1000; usleep(40 * 1000000); phase = 2;
  for (int t = 0; t < NTH; t++) pthread_join(tid[t], 0);
  { uint64_t ah[NB + 1024] = {0}, ar = 0, ae = 0, amx = 0; for (int t = 0; t < NTH; t++) { for (int b = 0; b < NB + 1024; b++) ah[b] += T[t].ahist[b]; ar += T[t].areqs; ae += T[t].aerrs; if (T[t].amaxus > amx) amx = T[t].amaxus; }
    double qq[3] = {0.5, 0.95, 0.99}, qr[3]; for (int k = 0; k < 3; k++) { uint64_t need = (uint64_t)(qq[k] * ar), acc = 0; int b; for (b = 0; b < NB + 1024; b++) { acc += ah[b]; if (acc > need) break; } qr[k] = b < NB ? (b + 1) * 0.05 : 200 + (b - NB + 1) * 5.0; }
    printf("K6 reqs=%llu p50=%.2f p95=%.1f p99=%.1f max=%.0f err=%.3f%%\n", (unsigned long long)ar, qr[0], qr[1], qr[2], amx / 1000.0, ar ? 100.0 * ae / ar : 0);
    const char *tp = getenv("UL_TS"); if (tp) { FILE *f = fopen(tp, "w"); if (f) { for (int s = 0; s < SECS; s++) { uint64_t n = 0, a = 0, b = 0, c = 0, u2 = 0; for (int t = 0; t < NTH; t++) { n += T[t].tsn[s]; a += T[t].ts05[s]; b += T[t].ts1[s]; c += T[t].ts2[s]; u2 += T[t].tsus[s]; } if (n) fprintf(f, "%d %llu %llu %llu %llu %.1f\n", s, (unsigned long long)n, (unsigned long long)a, (unsigned long long)b, (unsigned long long)c, u2 / 1000.0 / n); } fclose(f); } } }
  uint64_t h[NB + 1024] = {0}, reqs = 0, errs = 0, cerr = 0, mx = 0, rc = 0;
  for (int t = 0; t < NTH; t++) { for (int b = 0; b < NB + 1024; b++) h[b] += T[t].hist[b]; reqs += T[t].reqs; errs += T[t].errs; cerr += T[t].cerr; rc += T[t].reconn; if (T[t].maxus > mx) mx = T[t].maxus; }
  double q[3] = {0.5, 0.95, 0.99}, qv[3]; for (int k = 0; k < 3; k++) { uint64_t need = (uint64_t)(q[k] * reqs), acc = 0; int b; for (b = 0; b < NB + 1024; b++) { acc += h[b]; if (acc >= need) break; } qv[k] = b < NB ? (b + 1) * 0.05 : 200.0 + (b - NB + 1) * 5.0; }
  double secs = (m1 - m0) / 1e6;
  { int64_t tn = now_us(); unsigned long st[4] = {0}, stuck = 0, s5 = 0;
    for (int i = 0; i < NUSERS; i++) { user_t *u = &all[i]; if (u->st != S_IDLE) { st[u->st]++; if (tn - u->t0 > 2000000) stuck++; if (tn - u->t0 > 5000000) s5++; } }
    fprintf(stderr, "inflight conn=%lu send=%lu recv=%lu stuck>2s=%lu stuck>5s=%lu\n", st[1], st[2], st[3], stuck, s5); }
  fprintf(stderr, "errkinds send=%lu recv0=%lu recverr=%lu status=%lu connect=%lu\n", E[0], E[1], E[2], E[3], E[4]);
  printf("users=%d scale=%.3f rps=%.0f p50=%.2f p95=%.2f p99=%.2f max=%.1f ms err=%.3f%% connerr=%llu dials=%llu cpu=%.1f%% us/req=%.2f rss=%ldMB\n",
         NUSERS, SCALE, reqs / secs, qv[0], qv[1], qv[2], mx / 1000.0, reqs ? 100.0 * errs / reqs : 0, (unsigned long long)cerr, (unsigned long long)rc,
         100 * (c1 - c0) / secs, reqs ? (c1 - c0) * 1e6 / reqs : 0, rss_mb(SPID));
  return 0;
}
