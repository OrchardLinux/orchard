#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Build the pinned ARMv7 kloader as an ordinary host-side output.

The result needs a jailbreak that provides tfp0. This command never runs the
result, contacts a device, or modifies an existing output directory.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess


ROOT = Path(__file__).resolve().parent
SOURCE_COMMIT = "a1bbca8a8f2847d9c53070e76e0b5d093bda5993"
SOURCE_SHA256 = "c64f3252118d65b0ab52f10c8eaadc1dd1d7a989352a6e497075f15fb174296a"
HARDENING_PATCH_SHA256 = "efd2b63a85a448701a33c80f2e42323f8e4560d685c5342553f36225595d4a87"
DERIVED_SOURCE_SHA256 = "ddcea7cd58afb437cbe7ac65d09e14f6f7ba98b04814d5e088bf4404c23360fa"
SLEEP_WAIT_SECONDS = 90
SYSTEM_SYMBOLS = (
    "__stack_chk_fail", "__stack_chk_guard", "bcopy", "exit", "fclose",
    "fopen", "fread", "free", "mach_port_deallocate", "mach_task_self_", "malloc",
    "memmem", "memset", "printf", "signal", "sleep", "stat",
    "strcasestr", "sync", "sysctlbyname", "task_for_pid", "vm_read",
    "vm_region_recurse_64", "vm_write", "vm_deallocate",
)
POWER_SYMBOLS = ("IOPMFindPowerManagement", "IOPMSleepSystem")


def run(*arguments):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return result.stdout


def inputs(root=ROOT):
    pin = json.loads((root / "source.json").read_text())
    if (pin.get("schema") != 1 or pin.get("license") != "GPL-2.0-or-later"
            or pin.get("repository") != "https://github.com/axi0mX/ios-kexec-utils"
            or pin.get("commit") != SOURCE_COMMIT
            or pin.get("files", {}).get("kloader.c") != SOURCE_SHA256):
        raise ValueError("Unsupported kloader source description")
    if set(pin["files"]) != {"kloader.c", "tfp0.plist"}:
        raise ValueError("Unexpected kloader source inventory")
    for name, expected in pin["files"].items():
        actual = hashlib.sha256((root / name).read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError("Pinned kloader source differs: " + name)
    return pin


def patched_source(root=ROOT):
    """Apply the reviewed unified patch with exact context and no fuzz."""
    inputs(root)
    patch = (root / "hardening.patch").read_bytes()
    if hashlib.sha256(patch).hexdigest() != HARDENING_PATCH_SHA256:
        raise ValueError("kloader hardening patch differs from its pin")
    lines = patch.decode().splitlines(keepends=True)
    if lines[:2] != ["--- a/kloader.c\n", "+++ b/kloader.c\n"]:
        raise ValueError("Unexpected kloader patch paths")
    source = (root / "kloader.c").read_text().splitlines(keepends=True)
    result = []
    cursor = 0
    index = 2
    while index < len(lines):
        hunk = re.fullmatch(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@\n", lines[index])
        if not hunk:
            raise ValueError("Invalid kloader patch hunk")
        start = int(hunk[1]) - 1
        old_count = int(hunk[2] or 1)
        new_count = int(hunk[4] or 1)
        if start < cursor or start > len(source):
            raise ValueError("Overlapping kloader patch hunk")
        result.extend(source[cursor:start])
        cursor = start
        read = written = 0
        index += 1
        while index < len(lines) and not lines[index].startswith("@@ "):
            line = lines[index]
            if not line or line[0] not in " +-":
                raise ValueError("Unsupported kloader patch record")
            if line[0] in " -":
                if cursor >= len(source) or source[cursor] != line[1:]:
                    raise ValueError("kloader patch context differs")
                cursor += 1
                read += 1
            if line[0] in " +":
                result.append(line[1:])
                written += 1
            index += 1
        if (read, written) != (old_count, new_count):
            raise ValueError("kloader patch counts differ")
    result.extend(source[cursor:])
    text = "".join(result)
    if hashlib.sha256(text.encode()).hexdigest() != DERIVED_SOURCE_SHA256:
        raise ValueError("Derived kloader source differs from its pin")
    return text


def library(folder, name, install_name, symbols):
    """Describe imports available on iOS 9 to the modern ARM64-only SDK."""
    path = folder / (name + ".tbd")
    path.write_text(
        "--- !tapi-tbd\ntbd-version: 4\ntargets: [ armv7-ios ]\n"
        + "install-name: " + json.dumps(install_name) + "\n"
        + "exports:\n  - targets: [ armv7-ios ]\n    symbols: "
        + json.dumps(["_" + symbol for symbol in symbols]) + "\n...\n"
    )
    return path


def image(path):
    raw = path.read_bytes()
    if len(raw) < 28:
        raise ValueError("Incomplete kloader Mach-O")
    magic, cpu, subtype, kind, count, size, _ = struct.unpack_from("<7I", raw)
    if (magic, cpu, subtype & 0xFFFFFF, kind) != (0xFEEDFACE, 12, 9, 2):
        raise ValueError("Expected a thin ARMv7 kloader executable")
    end = 28 + size
    if end > len(raw) or count > size // 8:
        raise ValueError("Invalid kloader load-command table")
    cursor = 28
    main = None
    text = None
    for _ in range(count):
        if cursor + 8 > end:
            raise ValueError("Incomplete kloader load command")
        command, length = struct.unpack_from("<II", raw, cursor)
        if length < 8 or length % 4 or cursor + length > end:
            raise ValueError("Invalid kloader load command")
        if command == 1 and length >= 56:
            name = raw[cursor + 8:cursor + 24].rstrip(b"\0")
            if name == b"__TEXT":
                if text is not None:
                    raise ValueError("Repeated kloader text segment")
                text = struct.unpack_from("<4I", raw, cursor + 24)
        if command == 0x80000028:
            if length != 24 or main is not None:
                raise ValueError("Unexpected kloader LC_MAIN")
            main = struct.unpack_from("<Q", raw, cursor + 8)[0]
        cursor += length
    if cursor != end or main is None or text is None:
        raise ValueError("Missing kloader entry metadata")
    _, _, offset, length = text
    if offset != 0 or length > len(raw) or main % 4 or not 28 <= main < length:
        raise ValueError("kloader entry must address ARM code in its text segment")
    return {"architecture": "armv7", "entry_file_offset": main, "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest()}


def build(output, *, sign=True):
    pin = inputs()
    derived = patched_source()
    output = Path(output)
    if output.is_symlink():
        raise ValueError("kloader output must not be a symlink")
    output = output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    # Modern SDK headers omit declarations used by this older source. The
    # omitted inline shellcode is in an upstream #if 0 branch. Its three
    # shared storage words still need definitions without that assembler.
    header = output / "compat.h"
    header.write_text("#include <string.h>\n#include <unistd.h>\nextern int reboot(int);\n")
    globals_file = output / "globals.c"
    globals_file.write_text(
        "#include <stdint.h>\n"
        "uint32_t larm_init_tramp = 0xdeadbeef;\n"
        "uint32_t kern_base = 0xdeadbeef;\n"
        "uint32_t kern_tramp_phys = 0x9bf00000;\n"
    )
    compiled_source = output / "kloader.c"
    compiled_source.write_text(derived)
    compiler = ["xcrun", "--sdk", "iphoneos", "clang", "-arch", "armv7",
                "-miphoneos-version-min=9.0", "-marm", "-Os", "-std=gnu11",
                "-fno-builtin", "-D_FORTIFY_SOURCE=0", "-DINLINE_IT_ALL=0"]
    run(*compiler, "-include", str(header), "-c", str(compiled_source),
        "-o", str(output / "kloader.o"))
    run(*compiler, "-c", str(globals_file), "-o", str(output / "globals.o"))
    system = library(output, "libSystem", "/usr/lib/libSystem.B.dylib", SYSTEM_SYMBOLS)
    power = library(output, "IOKit", "/System/Library/Frameworks/IOKit.framework/IOKit",
                    POWER_SYMBOLS)
    target = output / "kloader"
    run(*compiler, "-nostdlib", str(output / "kloader.o"), str(output / "globals.o"),
        str(system), str(power), "-Wl,-bind_at_load", "-Wl,-no_uuid", "-o", str(target))
    if sign:
        run("/usr/bin/codesign", "--force", "--sign", "-", "--timestamp=none",
            "--digest-algorithm=sha1,sha256", "--entitlements", str(ROOT / "tfp0.plist"),
            str(target))
        run("/usr/bin/codesign", "--verify", "--strict", str(target))
    target.chmod(0o755)
    imported = set(run("xcrun", "nm", "-u", str(target)).splitlines())
    expected = {"_" + name for name in (*SYSTEM_SYMBOLS, *POWER_SYMBOLS)}
    if imported - {"dyld_stub_binder"} != expected:
        raise ValueError("kloader imports differ from the pinned ARMv7 path")
    artifact = image(target)
    report = {"schema": 1, "device_operations": 0, "source": pin,
              "kind": "ios-kloader-build", "source_commit": SOURCE_COMMIT,
              "source_sha256": SOURCE_SHA256,
              "hardening_patch_sha256": HARDENING_PATCH_SHA256,
              "derived_source_sha256": DERIVED_SOURCE_SHA256,
              "sleep_wait_seconds": SLEEP_WAIT_SECONDS,
              "artifact_sha256": artifact["sha256"], "artifact_bytes": artifact["bytes"],
              "signed": sign, "on_device_qualified": False,
              "compiler": run("xcrun", "--sdk", "iphoneos", "clang", "--version").splitlines()[0],
              "minimum_ios": "9.0", "signing": "ad-hoc-sha1-sha256" if sign else "unsigned",
              "output": artifact, "on_device_startup_qualified": False,
              "installed_binary_equivalence": "unverified"}
    (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--unsigned", action="store_true")
    args = parser.parse_args()
    print(json.dumps(build(args.output, sign=not args.unsigned), indent=2))


if __name__ == "__main__":
    main()
