/* Deterministic handle reuse and retirement at the native poll boundary.
 * The actual IO owners, callbacks, marker wrappers and table run above
 * these mocks; no host descriptor is opened or closed. */
#include <stdint.h>
#include <stdio.h>

static int64_t counters[20], next_fd = 42, retire_on_poll;
static uint64_t *last_alive, *last_retired;
static int64_t fail_dup, fail_mode_fd, fail_mode_value, fail_mode_error;
static int64_t last_error;
static int64_t mode_fds[32], mode_values[32];

int64_t livenessReset(void) {
    for (int i = 0; i < 20; ++i) counters[i] = 0;
    next_fd = 42; retire_on_poll = 0;
    last_alive = last_retired = 0;
    fail_dup = fail_mode_error = 0;
    last_error = 0;
    return 0;
}
int64_t livenessFd(int64_t fd) { next_fd = fd; return 0; }
int64_t livenessRetireDuringPoll(void) { retire_on_poll = 1; return 0; }
int64_t livenessStat(int64_t field) { return counters[field]; }
int64_t livenessFailDup(int64_t call) { fail_dup = call; return 0; }
int64_t livenessModeFault(int64_t fd, int64_t mode, int64_t error) {
    fail_mode_fd = fd; fail_mode_value = mode; fail_mode_error = error; return 0;
}
int64_t livenessModeSeen(int64_t fd, int64_t mode) {
    for (int64_t i = 0; i < counters[7]; ++i)
        if (mode_fds[i] == fd && mode_values[i] == mode) return 1;
    return 0;
}
int64_t livenessMode(int64_t fd) { return fd == 1 ? 3 : 7; }
int64_t livenessSetMode(int64_t fd, int64_t mode) {
    int64_t i = counters[7]++;
    if (i >= 32) { ++counters[6]; return -5; }
    mode_fds[i] = fd; mode_values[i] = mode;
    return fail_mode_error && fd == fail_mode_fd && mode == fail_mode_value ? -fail_mode_error : 0;
}
int64_t GetStdHandle(int64_t which) { return which == -10 ? 0 : which == -11 ? 1 : 2; }
int64_t GetLastError(void) { return last_error; }
int64_t DuplicateHandle(int64_t from, int64_t fd, int64_t to, int64_t out,
                        int64_t access, int64_t inherit, int64_t options) {
    ++counters[8];
    if (from != -1 || to != -1 || access || inherit || options != 2) ++counters[6];
    if (counters[8] == fail_dup) {
        last_error = fail_dup == 1 ? 5 : 6;
        return INT64_C(0x100000000);
    }
    *(int64_t *)(uintptr_t)out = fd + 100;
    return INT64_C(0x100000001);
}
int64_t livenessSay(int64_t good, int64_t label) {
    static int number;
    (void)label;
    printf("%s Windows file liveness case %d\n", good ? "ok  " : "FAIL", ++number);
    return good ? 0 : 1;
}
int64_t livenessOpen(int64_t path, int64_t flags, int64_t mode) {
    (void)path; (void)flags; (void)mode;
    ++counters[0]; return next_fd;
}
int64_t livenessClose(int64_t fd) {
    (void)fd;
    if (fd >= 100) { ++counters[9]; return 0; }
    ++counters[1];
    if (!last_retired || __atomic_load_n(last_retired, __ATOMIC_ACQUIRE)) ++counters[5];
    return 0;
}
int64_t __axiom_win_poller_alive(int64_t cell) {
    ++counters[3]; last_alive = (void *)(uintptr_t)cell;
    return __atomic_load_n(last_alive, __ATOMIC_ACQUIRE);
}
int64_t __axiom_win_poller_retire(int64_t cell) {
    ++counters[2]; last_retired = (void *)(uintptr_t)cell;
    __atomic_store_n(last_retired, 0, __ATOMIC_RELEASE);
    return 0;
}
int64_t livenessPoll(int64_t addr, int64_t n, int64_t timeout) {
    (void)timeout;
    ++counters[4];
    if (retire_on_poll) {
        retire_on_poll = 0;
        if (!last_alive) ++counters[6];
        else __axiom_win_poller_retire((int64_t)(uintptr_t)last_alive);
    }
    int64_t *rows = (void *)(uintptr_t)addr;
    for (int64_t i = 0; i < n; ++i) rows[3*i+2] = 1;
    return n;
}
