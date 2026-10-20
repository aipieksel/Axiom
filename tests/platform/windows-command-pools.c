/* Deterministic WinAPI boundaries for the actual owned command pool.
 * These are host mocks, not Windows execution. Each process becomes
 * complete when waited or when its owning job is closed. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define PROC_BASE INT64_C(0x100000000)
#define THREAD_BASE INT64_C(0x200000000)
#define JOB_BASE INT64_C(0x300000000)
#define FILE_BASE INT64_C(0x400000000)
#define NOISE INT64_C(0x1234567800000000)

struct process {
    int64_t pid, thread, job, code;
    int open, thread_open, assigned, resumed, finished;
    int attribute;
};
struct job { int64_t handle; int open, configured; };
struct attribute {
    int64_t address, job, handles[16];
    int initialized, deleted, requested, handle_count;
};
static struct process processes[4096];
static struct job jobs[512];
static struct attribute attributes[4096];
static int64_t counts[33], wait_order[8192], wait_count;
static int process_count, job_count, file_count, attribute_count;
static int standard_mask;
static int64_t last_error, fault_codes[32], fault_counts[32];
static int64_t *watched_slot;

/* counts: live children, maximum live children, created, create attempts,
 * waits, ABI violations, terminations, argv failures, env failures,
 * opened probes, closed probes, live probes, assignments, resumes,
 * process closes, thread closes, created jobs, closed jobs, open jobs,
 * open process handles, ordering violations, publication violations,
 * path violations, rejected PATH launches, attribute queries,
 * initializations, deletes and live lists. Index 31 counts every job
 * close attempt, including one that reports a failure. */
int64_t poolStat(int64_t index) { return counts[index]; }
int64_t poolRecord(int64_t value) { counts[28] = value; return value; }
int64_t poolScratchDrop(int64_t address) {
    if (address) ++counts[32];
    return 0;
}
int64_t poolWaitOrder(int64_t index) { return wait_order[index]; }
int64_t poolReset(void) {
    memset(counts, 0, sizeof counts);
    memset(processes, 0, sizeof processes);
    memset(jobs, 0, sizeof jobs);
    memset(attributes, 0, sizeof attributes);
    memset(wait_order, 0, sizeof wait_order);
    wait_count = process_count = job_count = file_count = attribute_count = 0;
    standard_mask = 0;
    last_error = 0;
    memset(fault_codes, 0, sizeof fault_codes);
    memset(fault_counts, 0, sizeof fault_counts);
    watched_slot = NULL;
    return 0;
}
int64_t poolFault(int64_t stage, int64_t code, int64_t count) {
    if (stage < 0 || stage >= 32) return -22;
    fault_codes[stage] = code; fault_counts[stage] = count; return 0;
}
int64_t poolWatchSlot(int64_t address) {
    watched_slot = (int64_t *)(intptr_t)address; return 0;
}
int64_t poolSay(int64_t good, int64_t label) {
    static int number;
    (void)label;
    printf("%s Windows command pool case %d\n", good ? "ok  " : "FAIL", ++number);
    if (!good) {
        fprintf(stderr, "case %d counters:", number);
        for (int i = 0; i < 29; ++i) fprintf(stderr, " %d=%lld", i, (long long)counts[i]);
        fputc('\n', stderr);
    }
    return good ? 0 : 1;
}

static int fault(int stage, int64_t code, int64_t error) {
    if (!fault_counts[stage] || (fault_codes[stage] && fault_codes[stage] != code)) return 0;
    --fault_counts[stage]; last_error = error; return 1;
}
static struct process *process_of(int64_t h) {
    if (h <= PROC_BASE || h > PROC_BASE + process_count) return NULL;
    return &processes[h - PROC_BASE - 1];
}
static struct process *thread_of(int64_t h) {
    if (h <= THREAD_BASE || h > THREAD_BASE + process_count) return NULL;
    return &processes[h - THREAD_BASE - 1];
}
static struct job *job_of(int64_t h) {
    if (h <= JOB_BASE || h > JOB_BASE + job_count) return NULL;
    return &jobs[h - JOB_BASE - 1];
}
static void finish(struct process *p) {
    if (!p->finished) { p->finished = 1; --counts[0]; }
}
static int utf8(char *out, unsigned value) {
    if (value < 128) { out[0] = (char)value; return 1; }
    if (value < 2048) { out[0] = (char)(192 | value >> 6); out[1] = (char)(128 | (value & 63)); return 2; }
    if (value < 65536) {
        out[0] = (char)(224 | value >> 12); out[1] = (char)(128 | ((value >> 6) & 63));
        out[2] = (char)(128 | (value & 63)); return 3;
    }
    out[0] = (char)(240 | value >> 18); out[1] = (char)(128 | ((value >> 12) & 63));
    out[2] = (char)(128 | ((value >> 6) & 63)); out[3] = (char)(128 | (value & 63)); return 4;
}
static void wide_text(const uint16_t *in, char *out, size_t cap) {
    size_t n = 0;
    while (*in && n + 5 < cap) {
        unsigned value = *in++;
        if (value >= 0xd800 && value <= 0xdbff && *in >= 0xdc00 && *in <= 0xdfff)
            value = 0x10000 + ((value - 0xd800) << 10) + (*in++ - 0xdc00);
        n += (size_t)utf8(out + n, value);
    }
    out[n] = 0;
}
/* Decode the Windows argv quoting emitted by WindowsProcessEncoding. */
static int command_words(const uint16_t *wide, char words[16][1024]) {
    char line[8192]; wide_text(wide, line, sizeof line);
    const char *p = line;
    int argc = 0;
    while (*p && argc < 16) {
        while (*p == ' ' || *p == '\t') ++p;
        if (!*p) break;
        int quoted = 0, n = 0;
        while (*p && (quoted || (*p != ' ' && *p != '\t'))) {
            int slashes = 0;
            while (*p == '\\') { ++slashes; ++p; }
            if (*p == '"') {
                for (int i = 0; i < slashes / 2; ++i) words[argc][n++] = '\\';
                if (slashes & 1) words[argc][n++] = '"';
                else quoted = !quoted;
                ++p;
            } else {
                for (int i = 0; i < slashes; ++i) words[argc][n++] = '\\';
                if (*p && (quoted || (*p != ' ' && *p != '\t'))) words[argc][n++] = *p++;
            }
            if (n >= 1000) { ++counts[7]; return 0; }
        }
        if (quoted) ++counts[7];
        words[argc++][n] = 0;
    }
    return argc;
}
static int path_exists(const char *path) {
    if (strstr(path, "missing")) return 0;
    if (!strncmp(path, "C:/bad/", 7)) return strstr(path, "/retry.exe") != NULL;
    if (!strncmp(path, "C:/Program Files/Pool/", 22)) return 1;
    return 0;
}
int64_t poolOpen(int64_t address, int64_t flags, int64_t mode) {
    (void)flags; (void)mode;
    const char *path = (const char *)(intptr_t)address;
    if (!path_exists(path)) return -2;
    ++counts[9]; ++counts[11]; return FILE_BASE + ++file_count;
}
int64_t poolClose(int64_t handle) {
    if (handle > FILE_BASE && handle <= FILE_BASE + file_count) {
        ++counts[10]; --counts[11]; return 0;
    }
    ++counts[5]; return -9;
}

int64_t GetLastError(void) { return NOISE | last_error; }
int64_t GetStdHandle(int64_t which) {
    int index = (int)(-10 - which);
    if (!index) standard_mask = 0;
    if (index < 0 || index > 2) { ++counts[5]; return 0; }
    if (fault(17, 0, 6)) return 0;
    standard_mask |= 1 << index;
    return 40 + index * 4;
}
int64_t SetHandleInformation(int64_t h, int64_t mask, int64_t flags) {
    if ((h != 40 && h != 44 && h != 48) || mask != 1 || flags != 1) ++counts[5];
    if (fault(15, h, 5)) return NOISE;
    return NOISE | 1;
}
int64_t CreateJobObjectW(int64_t attributes, int64_t name) {
    if (attributes || name) ++counts[5];
    if (fault(1, 0, 5)) return 0;
    struct job *j = &jobs[job_count++];
    *j = (struct job){JOB_BASE + job_count, 1, 0};
    ++counts[16]; ++counts[18]; return j->handle;
}
int64_t SetInformationJobObject(int64_t h, int64_t kind, int64_t address, int64_t bytes) {
    struct job *j = job_of(h);
    if (!j || !j->open || !address || kind != 9 || bytes != 144) {
        ++counts[5]; last_error = 87; return NOISE;
    }
    const unsigned char *info = (const unsigned char *)(intptr_t)address;
    uint32_t flags; memcpy(&flags, info + 16, sizeof flags);
    if (flags != 8192) ++counts[5];
    if (fault(2, 0, 87)) return NOISE;
    j->configured = 1; return NOISE | 1;
}
static int attribute_of(int64_t address) {
    for (int i = attribute_count; i > 0; --i)
        if (attributes[i-1].address == address && !attributes[i-1].deleted) return i-1;
    return -1;
}
int64_t InitializeProcThreadAttributeList(int64_t address, int64_t count, int64_t flags,
                                         int64_t size_address) {
    if (!size_address) { ++counts[5]; last_error = 87; return NOISE; }
    uint64_t *size = (uint64_t *)(intptr_t)size_address;
    if ((count != 1 && count != 2) || flags) ++counts[5];
    if (!address) {
        ++counts[24];
        if (fault(13, 0, 5)) return NOISE;
        *size = count == 2 ? 256 : 128; last_error = 122; return NOISE;
    }
    ++counts[25];
    if (*size < (count == 2 ? 256U : 128U)) ++counts[5];
    if (fault(4, 0, 5)) return NOISE;
    attributes[attribute_count++] = (struct attribute){.address=address, .initialized=1, .requested=(int)count};
    ++counts[27]; return NOISE | 1;
}
int64_t UpdateProcThreadAttribute(int64_t address, int64_t flags, int64_t kind,
                                  int64_t value, int64_t bytes, int64_t previous, int64_t returned) {
    int index = attribute_of(address); ++counts[12];
    if (index < 0 || flags || previous || returned || !value) {
        ++counts[20]; last_error = 87; return NOISE;
    }
    if (kind == 131085) {
        int64_t job = *(const int64_t *)(intptr_t)value;
        struct job *j = job_of(job);
        if (bytes != 8 || !j || !j->open || !j->configured) {
            ++counts[20]; last_error = 87; return NOISE;
        }
        if (fault(12, 0, 87)) return NOISE;
        attributes[index].job = job;
    } else if (kind == 131074) {
        struct attribute *a = &attributes[index];
        if (a->requested != (a->job ? 2 : 1) || bytes < 8 || bytes > 128 || bytes % 8) {
            ++counts[20]; last_error = 87; return NOISE;
        }
        if (fault(16, 0, 87)) return NOISE;
        a->handle_count = (int)(bytes / 8);
        memcpy(a->handles, (void *)(intptr_t)value, (size_t)bytes);
        counts[30] = a->handle_count;
        int standard_count = 0;
        for (int i = 0; i < 3; ++i)
            if (standard_mask & (1 << i)) {
                if (a->handles[standard_count] != 40 + i * 4) ++counts[5];
                ++standard_count;
            }
        for (int i = 0; i < a->handle_count; ++i) {
            int64_t h = a->handles[i];
            if (h != 40 && h != 44 && h != 48 && h != INT64_C(0x500000004) && h != INT64_C(0x500000008)) ++counts[5];
            for (int j = 0; j < i; ++j) if (a->handles[j] == h) ++counts[5];
        }
    } else { ++counts[20]; last_error = 87; return NOISE; }
    return NOISE | 1;
}
int64_t DeleteProcThreadAttributeList(int64_t address) {
    int index = attribute_of(address);
    if (index < 0) { ++counts[5]; return 0; }
    attributes[index].deleted = 1; ++counts[26]; --counts[27]; return 0;
}
int64_t CreateProcessW(int64_t app, int64_t command, int64_t pa, int64_t ta, int64_t inherit,
                       int64_t flags, int64_t environment, int64_t directory, int64_t startup,
                       int64_t information) {
    if (!app || !command || !startup || !information) {
        ++counts[5]; last_error = 87; return NOISE;
    }
    char path[2048], words[16][1024];
    wide_text((const uint16_t *)(intptr_t)app, path, sizeof path);
    int argc = command_words((const uint16_t *)(intptr_t)command, words);
    int64_t code = argc > 1 ? strtoll(words[1], NULL, 10) : 0;
    ++counts[3];
    if (argc != 8 || strcmp(words[2], "payload") || strcmp(words[3], "") ||
        strcmp(words[4], "space and\ttab") || strcmp(words[5], "a\"b") ||
        strcmp(words[6], "tail\\") || strcmp(words[7], "caf\xc3\xa9")) ++counts[7];
    if (!strstr(path, ".exe")) ++counts[22];
    const uint64_t *si = (const uint64_t *)(intptr_t)startup;
    int attribute = attribute_of((int64_t)si[13]);
    int contained = (flags & 4) != 0;
    int handle_count = attribute < 0 ? 0 : attributes[attribute].handle_count;
    int attributes_expected = contained + (handle_count > 0);
    int64_t expected_flags = (attributes_expected ? 525312 : 1024) | (contained ? 4 : 0);
    if (pa || ta || flags != expected_flags || directory ||
        (uint32_t)si[0] != (attributes_expected ? 112 : 104) || inherit != (handle_count > 0)) ++counts[5];
    if (attributes_expected && (attribute < 0 || !attributes[attribute].initialized)) ++counts[20];
    if ((uint32_t)(si[7] >> 32) != (standard_mask ? 256U : 0U)) ++counts[5];
    int standard_count = 0;
    for (int i = 0; i < 3; ++i) {
        int valid = (standard_mask & (1 << i)) != 0;
        if (si[10+i] != (uint64_t)(valid ? 40 + i * 4 : 0)) ++counts[5];
        standard_count += valid;
    }
    if (handle_count < standard_count) ++counts[5];
    if (attribute >= 0) {
        const struct attribute *a = &attributes[attribute];
        if (a->requested != attributes_expected || (a->job > 0) != contained) ++counts[5];
    }
    int found = 0;
    if (environment) {
        const uint16_t *e = (const uint16_t *)(intptr_t)environment;
        while (*e) {
            char item[4096]; wide_text(e, item, sizeof item);
            if (!strcmp(item, "AXIOM_POOL_VALUE=forwarded")) found = 1;
            while (*e++) {}
        }
    }
    if (!found) ++counts[8];
    if (strstr(path, "missing") || fault(3, code, 2)) { last_error = 2; return NOISE; }
    if (strstr(path, "denied") || (!strncmp(path, "C:/bad/", 7) && strstr(path, "retry.exe"))) {
        if (strstr(path, "retry.exe")) ++counts[23];
        last_error = 5; return NOISE;
    }
    struct process *p = &processes[process_count++];
    *p = (struct process){PROC_BASE + process_count, THREAD_BASE + process_count,
                         attribute < 0 ? 0 : attributes[attribute].job, code,
                         1, 1, contained, !contained, 0, attribute};
    int64_t *pi = (int64_t *)(intptr_t)information;
    pi[0] = p->pid; pi[1] = p->thread; pi[2] = process_count;
    ++counts[0]; ++counts[2]; ++counts[19];
    if (counts[0] > counts[1]) counts[1] = counts[0];
    return NOISE | 1;
}
int64_t ResumeThread(int64_t thread) {
    struct process *p = thread_of(thread); ++counts[13];
    if (!p || !p->thread_open || !p->assigned || p->attribute < 0 ||
        !attributes[p->attribute].deleted) {
        ++counts[20]; last_error = 6; return NOISE | UINT32_MAX;
    }
    if (watched_slot && *watched_slot != p->pid) ++counts[21];
    if (fault(5, p ? p->code : 0, 31)) return NOISE | UINT32_MAX;
    if (fault(6, p ? p->code : 0, 31)) return NOISE | 2;
    p->resumed = 1; return NOISE | 1;
}
int64_t WaitForSingleObject(int64_t pid, int64_t timeout) {
    struct process *p = process_of(pid); ++counts[4];
    if (!p || !p->open || (timeout && timeout != UINT32_MAX)) { ++counts[5]; last_error = 6; return NOISE | UINT32_MAX; }
    if (!timeout) {
        ++counts[29];
        if (fault(14, p->code, 6)) return NOISE | UINT32_MAX;
        return NOISE | (p->finished ? 0 : 258);
    }
    wait_order[wait_count++] = p->code;
    if (fault(7, p->code, 6)) return NOISE | UINT32_MAX;
    finish(p); return NOISE;
}
int64_t GetExitCodeProcess(int64_t pid, int64_t destination) {
    struct process *p = process_of(pid);
    if (!p || !p->open || !p->finished) { ++counts[20]; last_error = 6; return NOISE; }
    if (fault(8, p->code, 5)) return NOISE;
    uint32_t code = p->code == 101 ? 0xc0000005U : (uint32_t)p->code;
    memcpy((void *)(intptr_t)destination, &code, sizeof code); return NOISE | 1;
}
int64_t TerminateProcess(int64_t pid, int64_t code) {
    struct process *p = process_of(pid); ++counts[6];
    if (!p || !p->open) { ++counts[5]; last_error = 6; return NOISE; }
    if (code < 0 || code > UINT32_MAX) ++counts[5];
    if (fault(9, p->code, 5)) return NOISE;
    if (p->finished) { last_error = 5; return NOISE; }
    finish(p); p->code = code; return NOISE | 1;
}
int64_t CloseHandle(int64_t handle) {
    struct job *j = job_of(handle);
    if (j) {
        ++counts[31];
        if (!j->open) ++counts[5];
        if (fault(10, 0, 5)) return NOISE;
        j->open = 0; ++counts[17]; --counts[18];
        for (int i = 0; i < process_count; ++i)
            if (processes[i].assigned && processes[i].job == handle) finish(&processes[i]);
        return NOISE | 1;
    }
    struct process *p = process_of(handle);
    if (p) {
        if (!p->open) ++counts[5];
        if (fault(11, p->code, 6)) return NOISE;
        p->open = 0; ++counts[14]; --counts[19]; return NOISE | 1;
    }
    p = thread_of(handle);
    if (p) {
        if (!p->thread_open) ++counts[5];
        p->thread_open = 0; ++counts[15]; return NOISE | 1;
    }
    if (handle > FILE_BASE && handle <= FILE_BASE + file_count) {
        ++counts[10]; --counts[11]; return NOISE | 1;
    }
    ++counts[5]; last_error = 6; return NOISE;
}
