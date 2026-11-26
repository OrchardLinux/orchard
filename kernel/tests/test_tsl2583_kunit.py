#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Run the tsl2583 KUnit suites of a supplied kernel tree on the host.

drivers/iio/light/tsl2583-test.c tests the Apple LSCI record-set parser, the
gain factors and the lux computation in tsl2583.h, which use no hardware.
This compiles that file against small stand-ins for the kernel headers and
KUnit, with AddressSanitizer and UndefinedBehaviorSanitizer, and runs every
case. No device access occurs. The test data is made up; no unit's
calibration is involved.

    kernel/tests/test_tsl2583_kunit.py KERNEL_TREE [--cc CC]
"""
from pathlib import Path
import argparse
import os
import subprocess
import sys
import tempfile

HEADERS = {
    'linux/types.h': r'''
#pragma once
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include <string.h>
typedef uint8_t u8; typedef uint16_t u16; typedef uint32_t u32; typedef uint64_t u64;
typedef int16_t s16; typedef int32_t s32; typedef int64_t s64;
#define U16_MAX 0xffff
#define U32_MAX 0xffffffffu
''',
    'linux/errno.h': '#pragma once\n#include <errno.h>\n',
    'linux/kernel.h': r'''
#pragma once
#include <linux/types.h>
#define min_t(t, a, b) ((t)(a) < (t)(b) ? (t)(a) : (t)(b))
#define max_t(t, a, b) ((t)(a) > (t)(b) ? (t)(a) : (t)(b))
#define DIV_ROUND_CLOSEST_ULL(x, d) (((u64)(x) + (u64)(d) / 2) / (u64)(d))
''',
    'linux/math64.h': '#pragma once\n#include <linux/types.h>\n'
                      'static inline u64 div_u64(u64 a, u32 b) { return a / b; }\n',
    'linux/overflow.h': '#pragma once\n#define check_mul_overflow(a, b, d) __builtin_mul_overflow(a, b, d)\n',
    'linux/time64.h': '#pragma once\n#define USEC_PER_MSEC 1000L\n',
    'linux/module.h': '#pragma once\n#define MODULE_DESCRIPTION(x)\n#define MODULE_LICENSE(x)\n',
    'linux/string.h': '#pragma once\n#include <string.h>\n',
    'kunit/test.h': r'''
#pragma once
#include <stdio.h>
#include <linux/types.h>
struct kunit { const char *name; int failed; };
struct kunit_case { void (*run)(struct kunit *); const char *name; };
struct kunit_suite { const char *name; struct kunit_case *test_cases; };
#define KUNIT_CASE(f) { f, #f }
#define KUNIT_CHECK_(t, op, a, b, fatal) do {                                  \
	long long _a = (long long)(a), _b = (long long)(b);                     \
	if (!(_a op _b)) {                                                      \
		printf("  %s:%d %s: %s (%lld) " #op " %s (%lld) failed\n",       \
		       __FILE__, __LINE__, (t)->name, #a, _a, #b, _b);         \
		(t)->failed = 1;                                                \
		if (fatal)                                                      \
			return;                                                 \
	}                                                                       \
} while (0)
#define KUNIT_EXPECT_EQ(t, a, b) KUNIT_CHECK_(t, ==, a, b, 0)
#define KUNIT_EXPECT_NE(t, a, b) KUNIT_CHECK_(t, !=, a, b, 0)
#define KUNIT_ASSERT_EQ(t, a, b) KUNIT_CHECK_(t, ==, a, b, 1)
#define kunit_test_suites(...) static struct kunit_suite *all_suites[] = { __VA_ARGS__, NULL };
''',
}

RUNNER = r'''
#include "tsl2583-test.c"
int main(void)
{
	int cases = 0, failed = 0;

	for (struct kunit_suite **s = all_suites; *s; s++)
		for (struct kunit_case *c = (*s)->test_cases; c->run; c++) {
			struct kunit t = { c->name, 0 };

			c->run(&t);
			cases++;
			failed += t.failed;
			printf("%s %s.%s\n", t.failed ? "not ok" : "ok", (*s)->name, c->name);
		}
	printf("%d cases, %d failed\n", cases, failed);
	return failed != 0;
}
'''

parser = argparse.ArgumentParser()
parser.add_argument('tree', type=Path)
parser.add_argument('--cc', default=os.environ.get('CC', 'cc'))
args = parser.parse_args()
light = args.tree / 'drivers/iio/light'
if not (light / 'tsl2583-test.c').is_file():
    sys.exit(f'{light}/tsl2583-test.c not found')
with tempfile.TemporaryDirectory(prefix='orchard-tsl2583-') as directory:
    work = Path(directory)
    for name, text in HEADERS.items():
        (work / 'include' / name).parent.mkdir(parents=True, exist_ok=True)
        (work / 'include' / name).write_text(text)
    (work / 'run.c').write_text(RUNNER)
    binary = work / 'run'
    subprocess.run([args.cc, '-std=gnu11', '-g', '-Wall', '-Werror', '-Wno-unused-parameter',
                    '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                    '-I', str(work / 'include'), '-I', str(light),
                    '-o', str(binary), str(work / 'run.c')], check=True)
    sys.exit(subprocess.run([str(binary)]).returncode)
