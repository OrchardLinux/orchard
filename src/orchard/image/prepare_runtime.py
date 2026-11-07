#!/usr/bin/env python3
"""Stage finished public recovery, including Python, modules and device runtime."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import tarfile

from .runtime import files as runtime_files, recovery_files
from .integrity import sha
from .elf import needed, architecture as elf_architecture

from ..hardware import ROOT as PROJECT
HERE = PROJECT / 'boot/initramfs/recovery'


def copy(source, target, mode):
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    target.chmod(mode)


def stage_runtime(base, receipt, root, architecture='arm64'):
    spec = json.loads(receipt.read_text())
    root_architecture = spec.get('architecture', architecture)
    if not isinstance(root_architecture, str):
        raise ValueError('Python source root architecture is invalid')
    root_architecture = {'aarch64': 'arm64', 'arm64': 'arm64',
                         'armv7h': 'arm', 'armhf': 'arm', 'arm': 'arm'}.get(root_architecture)
    if root_architecture != architecture:
        raise ValueError('Python source root architecture differs from recovery')
    if base.stat().st_size != spec['bytes'] or sha(base) != spec['sha256']:
        raise ValueError('Python source root differs from receipt')
    libraries = re.compile(r'usr/lib/(?:ld-linux-(?:aarch64|armhf)|lib(?:python[0-9.]+|c|m|z|zstd|bz2|lzma|ffi|ssl|crypto|readline|ncursesw|tinfo|uuid|expat|gcc_s|mpdec))[-.].*')
    with tarfile.open(base, 'r|gz') as archive:
        for member in archive:
            name = member.name.removeprefix('./')
            tool = re.fullmatch(r'usr/(?:sbin|bin)/(blkid|sfdisk|mke2fs)', name)
            selected = (tool or name == 'etc/mke2fs.conf' or
                        re.fullmatch(r'usr/bin/python3(?:\.[0-9]+)?', name) or
                        re.match(r'usr/lib/python3\.[0-9]+(?:/|$)', name) or libraries.fullmatch(name))
            if (not selected or any('/' + component + '/' in '/' + name + '/' for component in
                                    ('site-packages', 'test', 'tests', '__pycache__', 'idlelib', 'tkinter')) or
                    ('/lib-dynload/' in name and Path(name).name.startswith(
                        ('_tkinter', '_curses', '_test', '_ctypes_test', '_dbm', '_gdbm', '_sqlite3', 'readline')))):
                continue
            if tool:
                if not member.isfile():
                    raise ValueError('Recovery storage tools must be native executables')
                name = 'usr/sbin/' + tool[1]
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if member.isdir():
                path.mkdir(exist_ok=True)
            elif member.issym():
                if not path.exists() and not path.is_symlink():
                    path.symlink_to(member.linkname)
            elif member.isfile():
                with path.open('wb') as output:
                    shutil.copyfileobj(archive.extractfile(member), output)
                path.chmod(member.mode)
            elif member.islnk():
                source = root / member.linkname.removeprefix('./')
                if not source.is_file():
                    raise ValueError('Python runtime hardlink target is missing')
                shutil.copyfile(source, path)
                path.chmod(member.mode)
    if not (root / 'usr/bin/python3').exists():
        raise ValueError('Python runtime is incomplete')
    for name in ('blkid', 'sfdisk', 'mke2fs'):
        tool = root / 'usr/sbin' / name
        if not tool.is_file() or tool.read_bytes()[:4] != b'\x7fELF' or not tool.stat().st_mode & 0o111:
            raise ValueError('Recovery storage tool is missing or not native: ' + name)
    if not (root / 'etc/mke2fs.conf').is_file():
        raise ValueError('Recovery requires the base mke2fs configuration')
    # Resolve the complete ELF closure, including dependencies of extension
    # modules. A filename whitelist alone silently misses distro changes.
    for attempt in range(12):
        required = set()
        for path in (root / 'usr').rglob('*'):
            if path.is_file() and not path.is_symlink():
                data = path.read_bytes()
                if data.startswith(b'\x7fELF') and elf_architecture(data) != architecture:
                    raise ValueError('Recovery contains an executable for another architecture: ' + str(path))
                required.update(needed(data))
        missing = set()
        for name in required:
            path = root / 'usr/lib' / name
            for _ in range(12):
                if not path.is_symlink():
                    break
                target = os.readlink(path)
                path = root / target.lstrip('/') if target.startswith('/') else path.parent / target
            if not path.is_file():
                missing.add(path.name)
                missing.add(name)
        if not missing:
            return
        copied = False
        with tarfile.open(base, 'r|gz') as archive:
            for member in archive:
                name = member.name.removeprefix('./')
                if not name.startswith('usr/lib/') or '/' in name[len('usr/lib/'):] or Path(name).name not in missing:
                    continue
                path = root / name
                if member.issym():
                    target = member.linkname
                    missing.add(Path(target).name)
                    if target.startswith('/'):
                        target = os.path.relpath(target, '/' + str(Path(name).parent))
                    if not path.is_symlink() and not path.exists():
                        path.symlink_to(target)
                        copied = True
                elif member.isfile():
                    path.write_bytes(archive.extractfile(member).read())
                    path.chmod(member.mode)
                    copied = True
        if not copied:
            raise ValueError('Unresolved Python runtime libraries: ' + ', '.join(sorted(missing)))
    raise ValueError('Python runtime dependency closure did not converge')


def check_recovery_modules(root, release, recovery):
    """Manual storage must stay modular; declared USB dependencies must exist."""
    normalized = lambda names: {name.replace('-', '_') for name in names}
    if normalized(recovery['runtime']['usb_modules']) & normalized(recovery['runtime']['manual_modules']):
        raise ValueError('A manual module cannot be loaded during USB setup')
    directory = root / 'usr/lib/modules' / release
    builtins = {Path(name).stem.replace('-', '_') for name in
                (directory / 'modules.builtin').read_text().splitlines() if name}
    available = {path.stem.replace('-', '_') for path in directory.rglob('*.ko')}
    for name in recovery['runtime']['manual_modules']:
        if name.replace('-', '_') in builtins:
            raise ValueError('Manual recovery module is built in: ' + name)
    for name in recovery['runtime']['usb_modules']:
        if name.replace('-', '_') not in builtins | available:
            raise ValueError('Recovery USB module is unavailable: ' + name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('busybox', 'dropbear', 'dropbearkey', 'wpa-supplicant', 'wpa-cli', 'iw',
                 'base', 'base-receipt', 'modules-dir', 'root-receipt', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--architecture', choices=('arm', 'arm64'), default='arm64')
    parser.add_argument('--device', help='stage the selected board recovery policy')
    parser.add_argument('--lwvm', type=Path, help='static orchard-lwvm mapper for LwVM installation')
    args = parser.parse_args()
    recovery = None
    if args.device:
        from ..recovery_policy import settings
        from ..kernel import descriptor
        if descriptor(PROJECT, args.device)['arch'] != args.architecture:
            parser.error('Recovery board architecture differs from the runtime')
        recovery = settings(args.device)
    root = args.output
    if root.exists():
        parser.error('Choose a new staging directory')
    for name in ('bin', 'sbin', 'usr/bin', 'usr/sbin', 'usr/lib', 'etc', 'root', 'proc', 'sys', 'dev', 'run', 'tmp'):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / 'lib').symlink_to('usr/lib')
    for source, name in ((args.busybox, 'bin/busybox'), (args.dropbear, 'usr/sbin/dropbear'),
                         (args.dropbearkey, 'usr/bin/dropbearkey'),
                         (args.wpa_supplicant, 'usr/sbin/wpa_supplicant'),
                         (args.wpa_cli, 'usr/bin/wpa_cli'), (args.iw, 'usr/sbin/iw')):
        copy(source, root / name, 0o755)
    for source in (args.busybox, args.dropbear, args.dropbearkey, args.wpa_supplicant, args.wpa_cli, args.iw):
        if elf_architecture(source.read_bytes()) != args.architecture:
            raise ValueError('Recovery tool architecture differs: ' + str(source))
    if args.lwvm:
        data = args.lwvm.read_bytes()
        native = args.busybox.read_bytes()
        if (args.lwvm.is_symlink() or not args.lwvm.is_file() or data[:4] != b'\x7fELF' or needed(data) or
                data[4:6] != native[4:6] or data[18:20] != native[18:20]):
            raise ValueError('LwVM mapper must be a static native executable')
        copy(args.lwvm, root / 'usr/sbin/orchard-lwvm', 0o755)
    stage_runtime(args.base, args.base_receipt, root, args.architecture)
    kernel_release, public = runtime_files(args.modules_dir)
    for name, (data, mode) in {**public, **recovery_files()}.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
    if recovery is not None:
        check_recovery_modules(root, kernel_release, recovery)
    # The release this recovery belongs to; `orchard-device status` reports it.
    # Root images carry no kernel: the release's platform layer of the same
    # kernel bundle does, so the recovery and an installed root match.
    spec = json.loads(args.root_receipt.read_text())
    (root / 'etc/orchard/release.json').write_text(json.dumps(
        {'protocol': 1, 'release': spec['release'], 'kernel_release': kernel_release}))
    for source, name in (('init', 'init'), ('usb-gadget', 'sbin/recovery-usb'),
                         ('authorize-key', 'sbin/authorize-key'), ('start-ssh', 'sbin/recovery-start-ssh'),
                         ('capture-adt', 'sbin/capture-adt')):
        copy(HERE / source, root / name, 0o755)
    copy(HERE / 'udhcpd.conf', root / 'etc/udhcpd.conf', 0o644)
    (root / 'etc/passwd').write_text('root:x:0:0:root:/root:/bin/sh\n')
    (root / 'etc/group').write_text('root:x:0:\n')
    (root / 'etc/shadow').write_text('root:!:0:0:99999:7:::\n')
    (root / 'etc/shadow').chmod(0o600)
    if recovery is not None:
        for name, value in (('recovery-identity.json', recovery['identity']), ('recovery.json', recovery['runtime'])):
            path = root / 'etc/orchard' / name
            path.write_text(json.dumps(value, sort_keys=True) + '\n')
            path.chmod(0o644)
        manual = recovery['runtime']['manual_modules']
        if manual:
            blacklist = root / 'etc/modprobe.d/orchard-recovery.conf'
            blacklist.parent.mkdir(parents=True, exist_ok=True)
            blacklist.write_text(''.join('blacklist ' + name + '\n' for name in manual))
            blacklist.chmod(0o644)
    print('Public recovery staged:', root)


if __name__ == '__main__':
    main()
