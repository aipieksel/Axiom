/* Execute the actual Windows ResourceCount module with hostile-width
 * WinAPI results. No Windows API is called by this host-only probe. */
#include <stdint.h>

typedef uint64_t U;
static U mode, bad;
static const U self = UINT64_C(0x123456789abcdef0);

U resourceCountMode(U next) {
  mode = next;
  bad = 0;
  return 0;
}
U resourceCountErrors(void) { return bad; }
U GetCurrentProcess(void) { return self; }
U GetProcessHandleCount(U process, U address) {
  U *cell = (U *)(uintptr_t)address;
  if (process != self || *cell != 0)
    ++bad;
  uint32_t *word = (uint32_t *)cell;
  *word = mode == 1 ? 0 : mode == 2 ? UINT32_MAX : mode == 3 ? 7 : mode == 4 ? 17 : 43;
  if (mode == 3)
    word[1] = UINT32_C(0xfedcba98);
  if (mode == 5)
    return UINT64_C(0xabcdef1200000000); /* A false BOOL with garbage high bits. */
  if (mode == 6)
    return 0;
  if (mode == 4)
    return UINT64_C(0xabcdef1200000001);
  return 1;
}
U GetLastError(void) {
  return UINT64_C(0xfeedface00000000) | (mode == 5 ? 5 : 6);
}
