/* The queue is a deterministic boundary mock. The emitted runtime's
 * real atomic callback and competing readers have a separate test. */
#include <stdint.h>

static int64_t blocked, pending, calls, failure, ordinary_ready;

int64_t resetMock(void) {
    blocked = pending = calls = failure = ordinary_ready = 0;
    return 0;
}
int64_t queueMock(int64_t bits) { pending |= bits & blocked; return 0; }
int64_t failMock(int64_t code) { failure = code; return 0; }
int64_t readyMock(int64_t ready) { ordinary_ready = ready; return 0; }
int64_t callsMock(void) { return calls; }
int64_t maskMock(void) { return blocked; }

int64_t __axiom_console_block(int64_t mask) {
    calls++;
    if (failure) { int64_t code = failure; failure = 0; return -code; }
    blocked |= mask;
    return 0;
}

int64_t __axiom_console_take(int64_t mask) {
    int64_t available = pending & mask;
    if (!available) return 0;
    int64_t bit = available & -available;
    pending &= ~bit;
    return bit == 2 ? 2 : 21;
}

int64_t pollControls(int64_t address, int64_t count, int64_t timeout) {
    int64_t *rows = (int64_t *)(uintptr_t)address;
    int64_t ready = 0;
    (void)timeout;
    for (int64_t i = 0; i < count; i++) {
        rows[3*i+2] = rows[3*i] == -2
            ? -__axiom_console_take(rows[3*i+1]) : ordinary_ready;
        ready += rows[3*i+2] != 0;
    }
    return ready;
}
