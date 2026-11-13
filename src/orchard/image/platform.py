"""The platform layer: what orchard puts into every installed system of one platform.

`usr/lib/modules/RELEASE/` holds the kernel (`vmlinuz`), orchard's initrd
(`initrd`, see boot/initramfs/root), the DTB of every board of the platform
(`dtb/`) and the modules. The regulatory database and the hardware defaults
of `system/defaults` (vendor configuration, the sound cards' UCM profiles)
complete it, and
`usr/lib/orchard/platform.json` says what it is. The installer writes the
layer into a root after the distro's root image, then writes the boot entry
from it (docs/boot-contract.md). Root images carry no kernel, so one serves
every platform, and a kernel change needs a new layer, not a new root image.

Members are regular files under `usr/` only, owned by root, with fixed
times, so the same inputs give the same archive.
"""
import argparse
import gzip
import io
import json
import re
import tomllib
from pathlib import Path
import stat
import tarfile
import tempfile

from ..hardware import ROOT
from .integrity import sha
from .runtime import checksums, module_files, regulatory_files

SCHEMA = 1
# What the initrd loads before it looks for the root, in order. Everything
# else storage needs is built in; modprobe adds dependencies from modules.dep.
# nvme is PCIe storage; nvme-apple is the Apple ANS2 controller of the A11
# (d221). A platform may lack either one, but not both.
STORAGE_MODULES = ('nvme', 'nvme-apple')
# The USB gadget the initrd and the install add-on set up (configfs, NCM and
# ACM). The arm64 kernels build it in; where a kernel has it as modules, the
# initrd carries them and loads them first, and a missing one costs USB only.
USB_MODULES = ('libcomposite', 'usb_f_ncm', 'usb_f_acm')
# Where system/defaults may put files: vendor configuration under /usr/lib,
# the boards' ALSA UCM profiles where alsa-lib looks for them, and WirePlumber
# fragments where WirePlumber looks for them (it reads none under /usr/lib).
DEFAULTS_UNDER = ('usr/lib/', 'usr/share/alsa/ucm2/', 'usr/share/wireplumber/')


def boards(platform, hardware=None):
    """{board: dtb} for every board of PLATFORM that the hardware describes."""
    from ..ram import boards_by_platform
    known = boards_by_platform(hardware or ROOT)
    if platform not in known:
        raise ValueError(f'Unknown platform {platform}; known: {", ".join(sorted(known))}')
    return known[platform]


ROOT_ARCH = 'arm64'  # distributions build one arm64 root for every platform


def platforms(hardware=None, architecture=ROOT_ARCH):
    """Every platform with at least one described board whose kernel runs the
    shared arm64 root, sorted. A platform of another architecture (apple-armv7)
    has no platform layer until a root of its architecture exists."""
    from ..kernel import descriptor
    from ..ram import boards_by_platform
    hardware = hardware or ROOT
    return sorted(platform for platform, boards in boards_by_platform(hardware).items()
                  if architecture is None or descriptor(hardware, sorted(boards)[0])['arch'] == architecture)


def kernel(platform, *, hardware=None, cache=None):
    """The staged kernel bundle of PLATFORM: the nightly's when it was built from
    this checkout's kernel state (orchard.published), else built or reused from
    the cache. ORCHARD_KERNEL=local always compiles."""
    from ..build import cache_root, prepare_kernel
    from ..hardware import ROOT
    from .. import published
    found = published.kernel(Path(hardware or ROOT), Path(cache) if cache else cache_root(), platform)
    if found is not None:
        return found
    return prepare_kernel(hardware, cache, device=sorted(boards(platform, hardware))[0])


def tools(hardware=None, cache=None, platform='apple-16k'):
    """orchard's static BusyBox, built or reused from the cache."""
    from ..build import prepare_tools
    return prepare_tools(hardware, cache, device=sorted(boards(platform, hardware))[0]) / 'busybox'


def initrd(modules, release, busybox, *, hardware=None, storage_modules=STORAGE_MODULES):
    """orchard's initrd for installed systems: BusyBox, the storage modules and their dependencies.

    MODULES is {path: (data, mode)} as module_files gives it.
    """
    hardware = Path(hardware or ROOT)
    base = f'usr/lib/modules/{release}/'
    depends = {}
    for line in modules[base + 'modules.dep'][0].decode().splitlines():
        name, _, needs = line.partition(':')
        depends[name] = needs.split()
    by_name = {Path(name).name.split('.ko')[0].replace('_', '-'): name for name in depends}
    loaded = [name for name in storage_modules if name.replace('_', '-') in by_name]
    if storage_modules and not loaded:
        raise ValueError(f'Kernel bundle has no storage module: {", ".join(storage_modules)}')
    usb = [name for name in USB_MODULES if name.replace('_', '-') in by_name]
    wanted, pending = [], [by_name[name.replace('_', '-')] for name in loaded + usb]
    while pending:
        name = pending.pop()
        if name not in wanted:
            wanted.append(name)
            pending.extend(depends[name])
    directory = stat.S_IFDIR | 0o755
    entries = {name: (directory, b'') for name in ('bin', 'dev', 'proc', 'sys', 'etc', 'etc/orchard', 'newroot',
                                                    'lib', 'lib/modules', 'lib/modules/' + release)}
    entries['init'] = (stat.S_IFREG | 0o755, (hardware / 'boot/initramfs/root/init').read_bytes())
    entries['etc/orchard/usb-identity'] = (stat.S_IFREG | 0o644, (hardware / 'src/orchard/usb-identity').read_bytes())
    entries['bin/busybox'] = (stat.S_IFREG | 0o755, Path(busybox).read_bytes())
    entries['etc/orchard/modules'] = (stat.S_IFREG | 0o644, ('\n'.join(loaded) + '\n').encode())
    if usb:
        entries['etc/orchard/usb-modules'] = (stat.S_IFREG | 0o644, ('\n'.join(usb) + '\n').encode())
    dependency_lines = []
    for name in sorted(wanted):
        target = f'lib/modules/{release}/{name}'
        for parent in reversed(Path(target).parents[:-4]):
            entries.setdefault(parent.as_posix(), (directory, b''))
        entries[target] = (stat.S_IFREG | 0o644, modules[base + name][0])
        dependency_lines.append(f'{name}: {" ".join(depends[name])}'.rstrip(' '))
    entries[f'lib/modules/{release}/modules.dep'] = (stat.S_IFREG | 0o644, ('\n'.join(dependency_lines) + '\n').encode())
    from .initramfs import write as write_initramfs
    with tempfile.TemporaryDirectory() as temporary:
        output = Path(temporary) / 'initrd'
        write_initramfs(entries, output)
        return output.read_bytes()


def storage_modules(hardware, platform):
    """The platform's early-root modules; an empty list denotes built-in storage."""
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', platform):
        raise ValueError('Invalid platform name')
    settings = tomllib.loads((Path(hardware) / 'platforms' / (platform + '.toml')).read_text())
    modules = settings.get('root', {}).get('storage_modules', list(STORAGE_MODULES))
    if (not isinstance(modules, list) or
            any(not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9_+-]+', name) for name in modules) or
            len(modules) != len(set(modules))):
        raise ValueError('Platform root.storage_modules must be distinct kernel module names')
    return tuple(modules)


def files(bundle, platform, board_dtbs, *, busybox, hardware=None):
    """(kernel release, {path: (data, mode)}) of PLATFORM's layer from a staged kernel BUNDLE."""
    hardware = Path(hardware or ROOT)
    from ..kernel import descriptor
    described = descriptor(hardware, sorted(board_dtbs)[0])
    bundle = Path(bundle)
    release, result = module_files(bundle / 'lib/modules')
    base = f'usr/lib/modules/{release}/'
    result[base + 'vmlinuz'] = ((bundle / described['image']).read_bytes(), 0o644)
    modules_to_load = storage_modules(hardware, platform)
    result[base + 'initrd'] = (initrd(result, release, busybox, hardware=hardware,
                                     storage_modules=modules_to_load), 0o644)
    for dtb in sorted(set(board_dtbs.values())):
        path = bundle / 'dtbs' / (dtb + '.dtb')
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'Kernel bundle lacks {path.name}')
        result[base + 'dtb/' + path.name] = (path.read_bytes(), 0o644)
    result.update(regulatory_files())
    defaults = hardware / 'system/defaults'
    for path in sorted(defaults.rglob('*')):
        if path.is_symlink():
            raise ValueError(f'System defaults must not contain links: {path}')
        if path.is_file():
            name = path.relative_to(defaults).as_posix()
            if not name.startswith(DEFAULTS_UNDER):
                raise ValueError(f'System defaults belong under /usr/lib, /usr/share/alsa/ucm2 or '
                                 f'/usr/share/wireplumber: {name}')
            result[name] = (path.read_bytes(), 0o755 if path.stat().st_mode & 0o111 else 0o644)
    description = {'schema': SCHEMA, 'platform': platform, 'kernel_release': release,
                   'boards': dict(sorted(board_dtbs.items()))}
    if described['arch'] != ROOT_ARCH:
        # arm64 layers stay as they were; another architecture says what it is.
        description.update(architecture=described['arch'], image=described['image'])
    result['usr/lib/orchard/platform.json'] = ((json.dumps(description, indent=2) + '\n').encode(), 0o644)
    result['usr/lib/orchard/platform.SHA256SUMS'] = (checksums(result), 0o644)
    return release, result


def write(layer, output):
    """Write LAYER's files to OUTPUT (a new .tar.gz); returns (expanded bytes, members)."""
    expanded = 0
    with Path(output).open('xb') as raw, \
            gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=0) as compressed, \
            tarfile.open(fileobj=compressed, mode='w|', format=tarfile.PAX_FORMAT) as archive:
        for name, (data, mode) in sorted(layer.items()):
            member = tarfile.TarInfo(name)
            member.size, member.mode, member.mtime = len(data), mode, 0
            member.uname = member.gname = 'root'
            archive.addfile(member, io.BytesIO(data))
            expanded += len(data)
    return expanded, len(layer)


def build(platform, output_directory, *, hardware=None, cache=None, bundle=None, busybox=None,
          root_architecture=None):
    """PLATFORM's layer as OUTPUT_DIRECTORY/platform-PLATFORM.tar.gz, plus its receipt.

    BUNDLE is a staged kernel bundle and BUSYBOX orchard's static BusyBox;
    without them they are built or reused from the cache.
    """
    board_dtbs = boards(platform, hardware)
    if root_architecture is not None:
        from ..kernel import descriptor
        architecture = descriptor(hardware or ROOT, sorted(board_dtbs)[0])['arch']
        if architecture != root_architecture:
            raise ValueError(f'Platform {platform} architecture {architecture} differs from root {root_architecture}')
    bundle = Path(bundle) if bundle is not None else kernel(platform, hardware=hardware, cache=cache)
    busybox = Path(busybox) if busybox is not None else tools(hardware, cache, platform)
    if root_architecture is not None:
        from .elf import architecture as elf_architecture
        if elf_architecture(busybox.read_bytes()) != root_architecture:
            raise ValueError('Platform initrd BusyBox differs from the root architecture')
    release, layer = files(bundle, platform, board_dtbs, busybox=busybox, hardware=hardware)
    output = Path(output_directory) / f'platform-{platform}.tar.gz'
    expanded, members = write(layer, output)
    receipt = {'file': output.name, 'bytes': output.stat().st_size, 'sha256': sha(output),
               'compression': 'gzip', 'expanded_bytes': expanded, 'members': members,
               'kernel_release': release, 'boards': sorted(board_dtbs)}
    architecture = json.loads(layer['usr/lib/orchard/platform.json'][0]).get('architecture')
    if architecture:
        receipt['architecture'] = architecture
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('platform', help='e.g. apple-16k')
    parser.add_argument('--output', type=Path, required=True, help='directory for the layer')
    parser.add_argument('--kernel-dir', type=Path, help='a staged kernel bundle (default: build or reuse it)')
    parser.add_argument('--busybox', type=Path, help="orchard's static BusyBox (default: build or reuse it)")
    parser.add_argument('--receipt', type=Path, help="also write the layer's receipt (JSON) here")
    parser.add_argument('--root-architecture', choices=('arm', 'arm64'),
                        help='require the platform to match this root architecture')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    receipt = build(args.platform, args.output, bundle=args.kernel_dir, busybox=args.busybox,
                    root_architecture=args.root_architecture)
    if args.receipt:
        args.receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')
    print(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    main()
