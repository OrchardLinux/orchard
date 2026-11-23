#!/usr/bin/env python3
"""Pack a Mesa A8 recording into a replay file for a8-gpu-drm.

Usage: gen-replay.py compute|triangle MESA_OUT OUT.rpl

  compute   k_store_lid.raw: one compute job; pass = 32 dwords 0x12345678 at the output BO 0x1_00116000
  triangle  tri-job/tri-twiddled.raw: a geometry and a fragment job on HWRT set 1; pass = the 64x64 target
            at 0x1_00116000, unflipped and twiddled

MESA_OUT holds recordings of Mesa's jobs (one directory per job: its BOs, mappings and command stream), made
with Mesa (OrchardLinux/mesa, A8 branch) and a DRM call recorder that is not part of this example. The
generator accepts only the two recordings the bring-up runs used, by their sha256 prefixes (MESA_RAW_SHA,
TRI_RAW_SHA).

File format (little endian): "A8RPLAY1", u32 version 1, then records {u32 type, u32 length, payload}:
  1 BO       u32 index, u32 kind (0: CPU-mapped, 1: PM/firmware protected), u64 size
  2 MAP      u32 index, u32 0, u64 VA, u64 size            (BO offset 0)
  3 DATA     u32 index, u32 0, u64 offset, bytes           (non-zero 4 KiB pages only)
  4 FREELIST u32 index, u32 0, u64 VA, u32 initial, max, grow, threshold
  5 HWRT     u32 free list 0, u32 free list 1, struct drm_pvr_ioctl_create_hwrt_dataset_args (0x88)
  6 CONTEXT  u32 DRM_PVR_CTX_TYPE_*
  7 JOB      u32 type, u32 flags, u32 HWRT data index, u32 0, the command stream
  8 STORE    u64 VA, u32 count, u32 value
  9 TRI      u64 VA
 10 NAME     text

BOs that hold a free list or an MList are created PM/firmware protected (powervr refuses a render job
otherwise) and get no data: on the A8 the kernel keeps its own parameter manager and never reads them.
"""
import hashlib
import os
import re
import struct
import sys

# sha256 prefixes of the recordings the bring-up runs used (Mesa 5348247d857 compute, 67ca6f6897d triangle)
MESA_RAW_SHA = '944a22aad7ba0381'
TRI_RAW_SHA = 'a82dec6e55433ab5'


def fail(msg):
    raise SystemExit('gen-replay: ' + msg)


def read_maps(raw, job):
    maps = []
    for line in open(os.path.join(raw, '%s-maps.txt' % job)):
        f = line.split()
        if not f:
            continue
        va, size, h, off = int(f[0], 16), int(f[1], 16), f[2], f[3]
        if off != 'off0':
            fail('mapping at %x is not at BO offset 0' % va)
        maps.append((va, size, h))
    return maps


class Out:
    def __init__(self):
        self.recs = []

    def rec(self, t, payload):
        self.recs.append(struct.pack('<II', t, len(payload)) + payload)

    def write(self, path):
        with open(path, 'wb') as f:
            f.write(b'A8RPLAY1' + struct.pack('<I', 1))
            for r in self.recs:
                f.write(r)


def pack_bos(out, raw, job, pm_vas):
    maps = read_maps(raw, job)
    handles = []
    for va, size, h in maps:
        if h not in handles:
            handles.append(h)
    index = {h: i for i, h in enumerate(handles)}
    first = {}
    for va, size, h in maps:
        first.setdefault(h, (va, size))
    pm = set()
    for h, (va, size) in first.items():
        if any(va <= p < va + size for p in pm_vas):
            pm.add(h)
    for h in handles:
        va, size = first[h]
        out.rec(1, struct.pack('<IIQ', index[h], 1 if h in pm else 0, size))
    for va, size, h in maps:
        if (va, size) != first[h] and size != first[h][1]:
            fail('BO %s mapped with two sizes' % h)
        out.rec(2, struct.pack('<IIQQ', index[h], 0, va, size))
    for h in handles:
        va, size = first[h]
        data = open(os.path.join(raw, '%s-va%010x.bin' % (job, va)), 'rb').read()
        if len(data) != size:
            fail('%s-va%010x.bin has %d bytes, not %d' % (job, va, len(data), size))
        if h in pm:
            if any(data):
                print('gen-replay: note: PM-protected BO %s at %x has recorded data, not replayed' % (h, va))
            continue
        for off in range(0, size, 4096):
            page = data[off:off + 4096]
            if any(page):
                out.rec(3, struct.pack('<IIQ', index[h], 0, off) + page)
    return maps, index


def compute(src, path):
    raw = os.path.join(src, 'k_store_lid.raw')
    # MESA_RAW_SHA: job0-* of k_store_lid.raw, then of k_store_const.raw
    h = hashlib.sha256()
    for k in ('k_store_lid', 'k_store_const'):
        d = os.path.join(src, k + '.raw')
        for f in sorted(os.listdir(d)):
            if f.startswith('job0-'):
                h.update(open(os.path.join(d, f), 'rb').read())
    if h.hexdigest()[:16] != MESA_RAW_SHA:
        fail('k_store recordings %s differ from the pinned ones (%s)' % (h.hexdigest()[:16], MESA_RAW_SHA))
    out = Out()
    out.rec(10, ('k_store_lid, raw sha256 %s (the bring-up compute replay)' % h.hexdigest()[:16]).encode())
    pack_bos(out, raw, 'job0', [])
    job = open(os.path.join(raw, 'job0-job.bin'), 'rb').read()
    jtype, _, flags, slen = struct.unpack_from('<IIII', job, 0)
    stream = open(os.path.join(raw, 'job0-compute.stream'), 'rb').read()
    if jtype != 2 or slen != len(stream):
        fail('job0 is not the compute job of its stream')
    out.rec(6, struct.pack('<I', 1))                       # DRM_PVR_CTX_TYPE_COMPUTE
    out.rec(7, struct.pack('<IIII', 2, flags, 0, 0) + stream)
    out.rec(8, struct.pack('<QII', 0x100116000, 32, 0x12345678))
    out.write(path)


def triangle(src, path):
    raw = os.path.join(src, 'tri-job', 'tri-twiddled.raw')
    h = hashlib.sha256()
    for f in sorted(os.listdir(raw)):
        h.update(open(os.path.join(raw, f), 'rb').read())
    if h.hexdigest()[:16] != TRI_RAW_SHA:
        fail('tri-twiddled recording %s differs from the pinned one (%s)' % (h.hexdigest()[:16], TRI_RAW_SHA))
    for line in open(os.path.join(raw, 'job1-maps.txt')):
        va = int(line.split()[0], 16)
        if open(os.path.join(raw, 'job1-va%010x.bin' % va), 'rb').read() != \
           open(os.path.join(raw, 'job0-va%010x.bin' % va), 'rb').read():
            fail('job1 memory differs from job0 at %x' % va)
    fls = {}
    for n in (0, 1):
        va, initial, mx, grow, thr, vmh, handle = struct.unpack_from(
            '<QIIIIII', open(os.path.join(raw, 'freelist%d.bin' % n), 'rb').read(), 0)
        fls[handle] = (n, va, initial, mx, grow, thr)
    jobs = [open(os.path.join(raw, 'job%d-job.bin' % n), 'rb').read() for n in (0, 1)]
    set_handle = struct.unpack_from('<I', jobs[0], 0x28)[0]
    hw = None
    for f in sorted(os.listdir(raw)):
        if re.match(r'hwrt\d+\.bin$', f):
            b = open(os.path.join(raw, f), 'rb').read()
            if struct.unpack_from('<I', b, 0x84)[0] == set_handle:
                hw = b
    if hw is None or len(hw) != 0x88:
        fail('no HWRT dataset with handle %d' % set_handle)
    fl_handles = struct.unpack_from('<II', hw, 0x50)
    mlists = [struct.unpack_from('<Q', hw, 0x20 + 0x18 * i)[0] for i in (0, 1)]
    out = Out()
    out.rec(10, ('tri-twiddled, raw sha256 %s (the bring-up triangle replay)' % h.hexdigest()[:16]).encode())
    pack_bos(out, raw, 'job0', [fls[x][1] for x in fl_handles] + mlists)
    for x in fl_handles:
        n, va, initial, mx, grow, thr = fls[x]
        out.rec(4, struct.pack('<IIQIIII', n, 0, va, initial, mx, grow, thr))
    out.rec(5, struct.pack('<II', fls[fl_handles[0]][0], fls[fl_handles[1]][0]) + hw)
    out.rec(6, struct.pack('<I', 0))                       # DRM_PVR_CTX_TYPE_RENDER
    for n, (name, jtype) in enumerate((('job0-geometry.stream', 0), ('job1-fragment.stream', 1))):
        stream = open(os.path.join(raw, name), 'rb').read()
        t, _, flags, slen = struct.unpack_from('<IIII', jobs[n], 0)
        sh, data_index = struct.unpack_from('<II', jobs[n], 0x28)
        if t != jtype or slen != len(stream) or sh != set_handle:
            fail('job%d does not match its stream' % n)
        out.rec(7, struct.pack('<IIII', jtype, flags, data_index, 0) + stream)
    out.rec(9, struct.pack('<Q', 0x100116000))
    out.write(path)


if len(sys.argv) != 4 or sys.argv[1] not in ('compute', 'triangle'):
    fail(__doc__.split('\n')[2])
(compute if sys.argv[1] == 'compute' else triangle)(sys.argv[2], sys.argv[3])
print('gen-replay: %s -> %s (%d bytes, sha256 %s)' % (sys.argv[1], sys.argv[3], os.path.getsize(sys.argv[3]),
      hashlib.sha256(open(sys.argv[3], 'rb').read()).hexdigest()))
