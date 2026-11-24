#!/usr/bin/env python3
"""Extract SmartIO __DATA.__firmware from a legacy prelinked ARM64 kernelcache.

No hardcoded offsets; select the exact kext via its prelink metadata, then map
its section virtual address through the outer kernel's file-backed segments.
For MH_FILESET kernels use the upstream ipsw kext extractor instead. The kext
bundle identifier is the caller's board policy (T8010 by default; S8000 for A9;
T8011 for A10X; T7000 for A8 and A8X; S5L8960X for A7).

The A7's iOS 12 kernelcache merges its kexts' segments into the kernel's own,
so the kext has no Mach-O of its own: its firmware is then the kernel's one
__DATA.__firmware section.
"""
import argparse
import hashlib
import json
from pathlib import Path

from orchard.apple.prelink import Kernelcache, MergedKext

T8010 = 'com.apple.driver.AppleT8010SmartIO'
S8000 = 'com.apple.driver.AppleS8000SmartIO'
T8011 = 'com.apple.driver.AppleT8011SmartIO'
T7000 = 'com.apple.driver.AppleT7000SmartIO'
S5L8960X = 'com.apple.driver.AppleS5L8960XSmartIO'
BUNDLES = (T8010, S8000, T7000, S5L8960X, T8011)


def extract(blob, bundle=T8010):
    kernel = Kernelcache(blob)
    try:
        ksections = kernel.kext_sections(bundle, 'SmartIO')
    except MergedKext:
        ksections = kernel.sections
    firmware = [s for s in ksections if (s['segment'], s['name']) == ('__DATA', '__firmware')]
    if len(firmware) != 1 or not 4096 <= firmware[0]['size'] <= 4*1024*1024:
        raise ValueError('No unique bounded firmware section')
    f = firmware[0]; offset = kernel.offset(f['addr'], f['size'])
    data = blob[offset:offset+f['size']]
    report = dict(kext=bundle, kernel_file_offset=offset,
                  firmware_size=len(data), sha256=hashlib.sha256(data).hexdigest())
    return data, report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kernel', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--bundle', choices=BUNDLES, default=T8010)
    args = parser.parse_args()
    data, report = extract(args.kernel.read_bytes(), args.bundle)
    # Never silently replace a firmware artifact.
    with args.output.open('xb') as out:
        out.write(data)
    print(json.dumps(report, indent=2))
