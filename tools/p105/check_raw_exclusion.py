#!/usr/bin/env python3
"""Check the bound P105 raw-write gate using the driver's reserved tail block."""

from contextlib import ExitStack
import ctypes
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import struct


# Reviewed h2fmi_mtd_write_oob() and h2fmi_program_page() in the kernel built
# for this artifact. The former tests ownership; the latter rejects both tail
# blocks before the first controller transaction. Pin this test to those bytes.
NOTES = "bdad8ec70b7f63a04bd1b33002ca89767b3550ef59eb4bcc8a74530efbac1e92"
BLOCKS = 2088
ERASE_SIZE = 2097152
PAGE_SIZE = 16384
OOB_SIZE = 64
REQUEST = struct.Struct("<QQQQQB7x")  # struct mtd_write_req on ARM EABI
MEMWRITE = 0xC0004D18 | (REQUEST.size << 16)
SFTL = Path("/sys/bus/platform/devices/apple-sftl.0.auto")
DISK = Path("/sys/class/block/applesftl0")


def require(condition, detail):
    if not condition:
        raise RuntimeError(detail)


def read_number(entry):
    return int(entry.read_text().strip(), 0)


def verify_fence():
    require((SFTL / "driver").resolve(strict=True).name == "apple-sftl", "SFTL is unbound")
    require(DISK.resolve(strict=True).parent.parent == SFTL.resolve(strict=True),
            "Disk belongs to a different SFTL device")
    volumes = [DISK, *sorted(DISK.parent.glob("applesftl0p*"))]
    require([v.name for v in volumes] == ["applesftl0", "applesftl0p1", "applesftl0p2", "applesftl0p3"],
            "Expected the P105 disk and its three partitions")
    devices = set()
    for volume in volumes:
        require(read_number(volume / "ro") == 1, f"{volume.name} is writable")
        require(not any((volume / "holders").iterdir()), f"{volume.name} has holders")
        devices.add((volume / "dev").read_text().strip())
    mounts = [line.split() for line in Path("/proc/self/mountinfo").read_text().splitlines()]
    require(all(row[2] not in devices for row in mounts), "Internal storage is mounted")
    root = next(row for row in mounts if row[4] == "/")
    require(root[root.index("-") + 1] in {"nfs", "nfs4", "rootfs", "ramfs", "tmpfs"},
            "Expected a RAM or NFS root")


def reserved_request(data, oob):
    # The last block starts above 4 GiB. A caller cannot select another address.
    return REQUEST.pack((BLOCKS - 1) * ERASE_SIZE, PAGE_SIZE, OOB_SIZE,
                        ctypes.addressof(data), ctypes.addressof(oob), 0)


def check_handle(name, fd, request):
    try:
        fcntl.ioctl(fd, MEMWRITE, request)
    except OSError as error:
        if error.errno == errno.EBUSY:
            return "EBUSY"
        raise RuntimeError(f"{name} returned {error} instead of EBUSY") from error
    raise RuntimeError(f"{name} unexpectedly accepted the reserved address")


def check():
    notes = hashlib.sha256(Path("/sys/kernel/notes").read_bytes()).hexdigest()
    require(notes == NOTES, "Kernel differs from the inspected P105 artifact")
    compatible = Path("/proc/device-tree/compatible").read_bytes().split(b"\0")
    require(b"apple,p105ap" in compatible, "Expected apple,p105ap")
    require(os.uname().machine.startswith("armv7") and ctypes.sizeof(ctypes.c_void_p) == 4,
            "Expected 32-bit ARMv7")
    controller = SFTL.resolve(strict=True).parent
    require((controller / "driver").resolve(strict=True).name == "apple-h2fmi",
            "SFTL parent is not the P105 controller")
    verify_fence()

    data = ctypes.create_string_buffer(b"\xff" * PAGE_SIZE, PAGE_SIZE)
    oob = ctypes.create_string_buffer(b"\xff" * OOB_SIZE, OOB_SIZE)
    request = reserved_request(data, oob)
    results = {}
    with ExitStack() as stack:
        handles = []
        for number in range(16):
            name = f"mtd{number}"
            sysfs = Path("/sys/class/mtd") / name
            require((sysfs / "name").read_text().strip() == f"apple-nand{number}",
                    f"Unexpected MTD name for {name}")
            require((sysfs / "device").resolve(strict=True) == controller,
                    f"Wrong controller for {name}")
            geometry = tuple(read_number(sysfs / field)
                             for field in ("size", "erasesize", "writesize", "oobsize"))
            require(geometry == (BLOCKS * ERASE_SIZE, ERASE_SIZE, PAGE_SIZE, OOB_SIZE),
                    f"Unexpected geometry on {name}")
            dev = tuple(map(int, (sysfs / "dev").read_text().split(":")))
            fd = os.open("/dev/" + name, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
            stack.callback(os.close, fd)
            node = os.fstat(fd)
            require(stat.S_ISCHR(node.st_mode) and
                    (os.major(node.st_rdev), os.minor(node.st_rdev)) == dev,
                    f"Wrong device node for {name}")
            handles.append((name, fd))

        for name, fd in handles:
            results[name] = check_handle(name, fd, request)
    verify_fence()
    return {"kernel_notes_sha256": notes, "driver": "apple-h2fmi",
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "sftl_bound": True,
            "reserved_block": BLOCKS - 1, "mtd_results": results}


if __name__ == "__main__":
    print(json.dumps(check(), indent=2, sort_keys=True))
