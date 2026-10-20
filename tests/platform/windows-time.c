/* Only the API boundary is mocked. Axiom's actual Windows platform
 * implementation performs the conversions and returns its Result. */
#include <stdint.h>
#include <stdio.h>

static int64_t total_millis;
static int64_t sleep_calls;
static int invalid_sleep;
static uint64_t filetime;

int64_t resetSleep(void) {
    total_millis = 0;
    sleep_calls = 0;
    invalid_sleep = 0;
    return 0;
}

int64_t Sleep(int64_t millis) {
    /* Reject narrowing loss and the reserved infinite timeout. */
    if (millis <= 0 || millis > UINT32_MAX - 1) invalid_sleep = 1;
    if (millis > 0 && millis <= UINT32_MAX - 1) total_millis += millis;
    sleep_calls++;
    return 0;
}

int64_t checkSleep(int64_t expected_millis, int64_t expected_calls) {
    return !invalid_sleep && total_millis == expected_millis && sleep_calls == expected_calls;
}

int64_t setFileTime(int64_t ticks) {
    filetime = (uint64_t)ticks;
    return 0;
}

int64_t GetSystemTimePreciseAsFileTime(int64_t out) {
    *(uint64_t *)(uintptr_t)out = filetime;
    return 0;
}

int64_t say(int64_t good, int64_t label) {
    static int number;
    (void)label;
    printf("%s Windows time case %d\n", good ? "ok  " : "FAIL", ++number);
    return good ? 0 : 1;
}
