/* The real Windows platform module runs on the host. These mocks model
 * completed reader bytes and socket/clock boundaries; the separate
 * runtime reader probe checks the worker itself. No host input is read. */
#include <stdint.h>
#include <stdio.h>
#include <string.h>

struct row { int64_t socket; uint16_t events, revents; uint32_t padding; };
struct plan { int64_t at, ready, events; };
static struct plan plans[200];
static int64_t counters[20], clock_ms, last_error, wsa_error, poll_error;
static int64_t read_result, mode_result, close_result, buffered;
static int64_t positional_result, positional_error;
static unsigned char bytes[] = {0xc3, 0xa9, 'x'};
static int64_t byte_offset;

int64_t readinessReset(void) {
    memset(counters, 0, sizeof(counters));
    for (int i = 0; i < 200; ++i) plans[i] = (struct plan){-1, 0, 0};
    clock_ms = last_error = poll_error = 0;
    wsa_error = 10038;
    read_result = 2;
    positional_result = 3; positional_error = 0;
    mode_result = close_result = buffered = byte_offset = 0;
    return 0;
}
int64_t readinessPlan(int64_t h, int64_t at, int64_t ready, int64_t events) {
    plans[h] = (struct plan){at, ready, events};
    return 0;
}
int64_t readinessResults(int64_t read, int64_t mode, int64_t close) {
    read_result = read; mode_result = mode; close_result = close;
    return 0;
}
int64_t readinessBytes(void) { buffered = 3; byte_offset = 0; return 0; }
int64_t readinessPollError(int64_t code) { poll_error = code; return 0; }
int64_t readinessPositional(int64_t result, int64_t error) {
    positional_result = result; positional_error = error; return 0;
}
int64_t readinessStat(int64_t field) { return field ? counters[field] : clock_ms; }
int64_t readinessSay(int64_t good, int64_t label) {
    static int number;
    (void)label;
    printf("%s Windows readiness case %d\n", good ? "ok  " : "FAIL", ++number);
    return good ? 0 : 1;
}

int64_t GetStdHandle(int64_t id) { return id == -10 ? 10 : id == -11 ? 11 : 12; }
int64_t GetLastError(void) { return last_error; }
int64_t GetFileType(int64_t h) {
    int64_t kind = h >= 10 && h <= 12 ? 2 : h == 20 ? 3 : h == 30 ? 1 : 0;
    if (!kind) last_error = 6;
    return kind;
}
static int64_t positional(int64_t h, int64_t count, int64_t out, int64_t overlap) {
    counters[17] = h; counters[18] = count;
    if (!overlap) ++counters[5];
    else {
        const uint64_t *words = (void *)(uintptr_t)overlap;
        counters[16] = (int64_t)words[2];
        if (words[0] || words[1] || words[3]) ++counters[5];
    }
    *(uint32_t *)(uintptr_t)out = positional_error ? 0 : (uint32_t)positional_result;
    last_error = positional_error;
    return positional_error ? 0 : 1;
}
int64_t ReadFile(int64_t h, int64_t dst, int64_t count, int64_t out, int64_t overlap) {
    (void)dst;
    ++counters[14];
    return positional(h, count, out, overlap);
}
int64_t WriteFile(int64_t h, int64_t src, int64_t count, int64_t out, int64_t overlap) {
    (void)src;
    ++counters[15];
    return positional(h, count, out, overlap);
}
int64_t GetTickCount64(void) { return clock_ms; }
int64_t Sleep(int64_t ms) {
    if (ms < 0 || ms > 10) ++counters[5];
    ++counters[1];
    clock_ms += ms;
    return 0;
}
int64_t getsockopt(int64_t h, int64_t level, int64_t name, int64_t value, int64_t len) {
    (void)level; (void)name; (void)value; (void)len;
    wsa_error = 10038;
    return h >= 100 && h < 200 ? 0 : UINT32_MAX;
}
int64_t WSAGetLastError(void) { return wsa_error; }
int64_t WSAPoll(int64_t addr, int64_t count, int64_t wait) {
    struct row *rows = (void *)(uintptr_t)addr;
    ++counters[2]; counters[3] = wait;
    if (poll_error) { wsa_error = poll_error; return UINT32_MAX; }
    int64_t advance = wait, next = INT64_MAX;
    for (int64_t i = 0; i < count; ++i) {
        if (rows[i].revents || (rows[i].events & ~272)) ++counters[5];
        struct plan *p = &plans[rows[i].socket];
        if (p->at >= 0 && p->events && p->at < next) next = p->at;
    }
    if (next <= clock_ms) advance = 0;
    else if (next != INT64_MAX && (wait < 0 || next - clock_ms < wait)) advance = next - clock_ms;
    if (advance < 0) { ++counters[5]; return UINT32_MAX; }
    clock_ms += advance;
    int64_t ready = 0;
    for (int64_t i = 0; i < count; ++i) {
        struct plan *p = &plans[rows[i].socket];
        rows[i].revents = p->at >= 0 && clock_ms >= p->at ? (uint16_t)p->events : 0;
        ready += rows[i].revents != 0;
    }
    return ready;
}
int64_t __axiom_win_reader_ready(int64_t h) {
    ++counters[4]; counters[9] = h;
    if (buffered && h == 10) return 1;
    struct plan *p = &plans[h];
    return p->at >= 0 && clock_ms >= p->at ? p->ready : 0;
}
int64_t __axiom_win_reader_read(int64_t h, int64_t dst, int64_t count) {
    ++counters[6]; counters[9] = h;
    if (!count) return 0;
    if (buffered && h == 10) {
        int64_t n = buffered < count ? buffered : count;
        memcpy((void *)(uintptr_t)dst, bytes + byte_offset, (size_t)n);
        byte_offset += n; buffered -= n;
        return n;
    }
    if (read_result > 0) memset((void *)(uintptr_t)dst, 'r', (size_t)(count < read_result ? count : read_result));
    return read_result;
}
int64_t __axiom_win_reader_mode(int64_t h, int64_t mode) {
    ++counters[7]; counters[9] = h; counters[10] = mode;
    return mode_result;
}
int64_t __axiom_win_reader_close(int64_t h) {
    ++counters[8]; counters[9] = h;
    return close_result;
}
int64_t __axiom_console_take(int64_t mask) { ++counters[13]; return mask & 2 ? 2 : 0; }
int64_t recv(int64_t h, int64_t dst, int64_t count, int64_t flags) {
    (void)h; (void)flags;
    ++counters[11];
    if (count) *(unsigned char *)(uintptr_t)dst = 's';
    return count ? 1 : 0;
}
int64_t closesocket(int64_t h) { (void)h; ++counters[12]; return 0; }
