/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Compile the producer's actual patched helpers against fake Mach/power APIs. */
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef unsigned int task_t;
typedef unsigned int mach_port_t;
typedef unsigned int mach_msg_type_number_t;
typedef int kern_return_t;
typedef uintptr_t pointer_t;
typedef uintptr_t vm_address_t;
typedef uintptr_t vm_size_t;
typedef unsigned int *vm_region_info_t;
typedef struct { unsigned int unused; } vm_region_submap_info_data_64_t;
#define KERN_SUCCESS 0
#define KERN_FAILURE 5
#define MACH_PORT_NULL 0
#define VM_REGION_SUBMAP_INFO_COUNT_64 1

static unsigned int reads, bad_read, short_read, releases;
static unsigned int region_calls, region_mode, task_failure;
static unsigned int sleeps, waits, waited_seconds, port_releases;
static mach_port_t power_port;
static kern_return_t sleep_result;
static _Alignas(uint32_t) uint8_t page[2048];

static mach_port_t mach_task_self(void) { return 1; }

static kern_return_t task_for_pid(task_t task, int pid, task_t *result)
{
    (void)task;
    (void)pid;
    *result = 2;
    return task_failure ? KERN_FAILURE : KERN_SUCCESS;
}

static kern_return_t vm_read(task_t task, vm_address_t address, vm_size_t length,
                             pointer_t *buffer, mach_msg_type_number_t *size)
{
    (void)task;
    (void)address;
    assert(length <= sizeof(page));
    reads++;
    if (reads == bad_read)
        return KERN_FAILURE;
    *buffer = (pointer_t)page;
    *size = (mach_msg_type_number_t)length - (reads == short_read ? 1 : 0);
    return KERN_SUCCESS;
}

static kern_return_t vm_deallocate(task_t task, pointer_t buffer, vm_size_t size)
{
    (void)task;
    assert(buffer == (pointer_t)page && size <= sizeof(page));
    releases++;
    return KERN_SUCCESS;
}

static kern_return_t mach_port_deallocate(task_t task, mach_port_t port)
{
    (void)task;
    assert(port != MACH_PORT_NULL);
    port_releases++;
    return KERN_SUCCESS;
}

static kern_return_t vm_region_recurse_64(task_t task, vm_address_t *address,
                                        vm_size_t *size, unsigned int *depth,
                                        vm_region_info_t info,
                                        mach_msg_type_number_t *count)
{
    (void)task;
    (void)depth;
    (void)info;
    assert(*count == VM_REGION_SUBMAP_INFO_COUNT_64);
    region_calls++;
    switch (region_mode) {
    case 1: *size = 0; return KERN_SUCCESS;
    case 2: *size = 1; return KERN_SUCCESS;
    case 3: *address = UINT32_MAX - 4; *size = 8; return KERN_SUCCESS;
    case 4: *size = (1u << 30) + 1; return KERN_SUCCESS;
    default: return KERN_FAILURE;
    }
}

static mach_port_t IOPMFindPowerManagement(mach_port_t master)
{
    assert(master == MACH_PORT_NULL);
    return power_port;
}

static kern_return_t IOPMSleepSystem(mach_port_t port)
{
    assert(port == power_port && port != MACH_PORT_NULL);
    sleeps++;
    return sleep_result;
}

static unsigned int sleep(unsigned int seconds)
{
    waits++;
    waited_seconds += seconds;
    return 0;
}

#define bcopy(from, to, count) memcpy((to), (from), (count))

/* ACTUAL_PATCHED_HELPERS */

static void reset(void)
{
    reads = bad_read = short_read = releases = 0;
    region_calls = region_mode = task_failure = 0;
    sleeps = waits = waited_seconds = port_releases = 0;
    power_port = MACH_PORT_NULL;
    sleep_result = KERN_SUCCESS;
    memset(page, 0xab, sizeof(page));
}

int main(void)
{
    uint8_t *destination = malloc(ORCHARD_KERNEL_DUMP_BYTES);
    assert(destination);
    reset();
    assert(orchard_dump_kernel(2, UINT32_MAX, destination) == KERN_FAILURE);
    assert(reads == 0);
    for (unsigned int failed = 1; failed <= 3; failed++) {
        reset();
        bad_read = failed;
        assert(orchard_dump_kernel(2, 0x81200000, destination) == KERN_FAILURE);
        assert(reads == failed && releases == failed - 1);
    }
    reset();
    short_read = 1;
    assert(orchard_dump_kernel(2, 0x81200000, destination) == KERN_FAILURE);
    assert(reads == 1 && releases == 1);
    reset();
    assert(orchard_dump_kernel(2, 0x81200000, destination) == KERN_SUCCESS);
    assert(reads == (ORCHARD_KERNEL_DUMP_BYTES - ORCHARD_KERNEL_HEADER_BYTES) /
                    ORCHARD_KERNEL_READ_BYTES);
    assert(releases == reads);
    assert(destination[0] == 0xab);
    assert(destination[ORCHARD_KERNEL_DUMP_BYTES - ORCHARD_KERNEL_HEADER_BYTES - 1] == 0xab);
    free(destination);

    for (unsigned int mode = 0; mode <= 3; mode++) {
        reset();
        region_mode = mode;
        assert(get_kernel_base() == (vm_address_t)-1);
        assert(region_calls == (mode == 2 ? ORCHARD_MAX_KERNEL_REGIONS : 1));
        assert(reads == 0 && port_releases == 1);
    }
    reset();
    task_failure = 1;
    assert(get_kernel_base() == (vm_address_t)-1);
    assert(region_calls == 0 && port_releases == 0);
    reset();
    region_mode = 4;
    bad_read = 1;
    assert(get_kernel_base() == (vm_address_t)-1);
    assert(reads == 1 && port_releases == 1);
    reset();
    region_mode = 4;
    assert(get_kernel_base() == (vm_address_t)-1);
    assert(reads == 2 && releases == 2);
    reset();
    region_mode = 4;
    *(uint32_t *)page = 0xfeedface;
    assert(get_kernel_base() == 0x81400000);
    assert(reads == 1 && releases == 1 && port_releases == 1);

    reset();
    assert(orchard_handoff_once() == KERN_FAILURE);
    assert(sleeps == 0 && waits == 0 && port_releases == 0);
    reset();
    power_port = 3;
    sleep_result = 8;
    assert(orchard_handoff_once() == 8);
    assert(sleeps == 1 && waits == 0 && port_releases == 1);
    reset();
    power_port = 3;
    assert(orchard_handoff_once() == KERN_FAILURE);
    assert(sleeps == 1 && waits == 1 && waited_seconds == 90 && port_releases == 1);
    puts("bounded reads, region exhaustion and one sleep request passed");
    return 0;
}
