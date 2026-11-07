"""Install from a block image with no recovery: the normal kernel and initrd, plus a tiny add-on.

The payload is what every later boot uses (the platform layer's kernel, initrd and device tree) with
an archive appended to the initrd: a static writer, Dropbear and SSH keys made for this boot
(boot/initramfs/install). The host computes the partition table and streams the image; once the root
is on disk, the rest runs inside it (install_device.py finish), and the boot simply continues into it.
"""
import base64
from contextlib import contextmanager
import io
import json
from pathlib import Path
import shlex
import stat
import struct
import subprocess
import tarfile
import tempfile
import time
import uuid
import zlib

from . import install_device as D
from . import install_remote as remote
from .contracts import Connection
from .hardware import ROOT
from .state import BootState, DeviceState, private_directory
from .transports.ssh import KEY, one_connection, ssh_argv, wait_authenticated

ADD_ON = ROOT / 'boot/initramfs/install'
HOST = '172.16.42.1'
DISK = '/dev/nvme0n1'
BOOT_ARGS = 'fbcon=map:9 vt.global_cursor_default=0'
INITRAMFS_MAGIC = b'm1n1_initramfs'
XBOOTLDR, ROOT_ARM64 = D.XBOOTLDR, D.ROOT_ARM64
ENTRY_SIZE = 128


def dropbear_key(openssh):
    """Dropbear's form of an unencrypted ed25519 private key in OpenSSH's."""
    data = base64.b64decode(''.join(line for line in openssh.splitlines() if not line.startswith('-----')))

    def strings(buffer, position, count):
        values = []
        for _ in range(count):
            size = struct.unpack_from('>I', buffer, position)[0]
            position += 4 + size
            values.append(buffer[position - size:position])
        return values, position
    magic = b'openssh-key-v1\0'
    if not data.startswith(magic):
        raise ValueError('Expected an OpenSSH key')
    (cipher, _, _), position = strings(data, len(magic), 3)
    if cipher != b'none':
        raise ValueError('Expected an unencrypted key')
    (_, private), _ = strings(data, position + 4, 2)
    (kind, _, secret), _ = strings(private, 8, 3)     # after the two check words
    if kind != b'ssh-ed25519' or len(secret) != 64:
        raise ValueError('Expected an ed25519 key')
    return struct.pack('>I', len(kind)) + kind + struct.pack('>I', 64) + secret


def make_keys(directory):
    """(client key path, known_hosts line's key, authorized_keys text, host key for Dropbear), new for this boot."""
    directory = Path(directory)
    for name in ('client', 'host'):
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(directory / name)],
                       check=True, capture_output=True, timeout=20)
    public = (directory / 'client.pub').read_text().split()
    host_public = (directory / 'host.pub').read_text().split()
    if not KEY.fullmatch(' '.join(public[:2])) or not KEY.fullmatch(' '.join(host_public[:2])):
        raise ValueError('Generated SSH key is invalid')
    return (directory / 'client', ' '.join(host_public[:2]), ' '.join(public[:2]) + '\n',
            dropbear_key((directory / 'host').read_text()))


def newc(files):
    """A newc archive of {path: (mode, bytes)}, in order."""
    out = bytearray()

    def add(number, name, mode, body):
        encoded = name.encode() + b'\0'
        fields = (number, mode, 0, 0, 1, 0, len(body), 0, 0, 0, 0, len(encoded), 0)
        out.extend(b'070701' + b''.join(b'%08X' % value for value in fields) + encoded)
        out.extend(bytes(-len(out) % 4))
        out.extend(body)
        out.extend(bytes(-len(out) % 4))
    for number, (name, (mode, body)) in enumerate(files.items(), 1):
        add(number, name, mode, body)
    add(0, 'TRAILER!!!', 0, b'')
    return bytes(out)


def install_tools(archive):
    """{name: program} out of the release's install-tools.tar.gz."""
    with tarfile.open(archive) as opened:
        return {name: opened.extractfile(name).read() for name in ('orchard-write', 'dropbear', 'resize2fs', 'tune2fs')}


def add_on(tools, ecid, authorized_keys, host_key):
    tools = install_tools(tools)
    directory, program, data = stat.S_IFDIR | 0o755, stat.S_IFREG | 0o755, stat.S_IFREG | 0o644
    private = stat.S_IFREG | 0o600
    return newc({'install': (directory, b''),
                 'install/run': (data, (ADD_ON / 'run').read_bytes()),
                 'install/udhcpd.conf': (data, (ADD_ON / 'udhcpd.conf').read_bytes()),
                 'install/orchard-write': (program, tools['orchard-write']),
                 'install/dropbear': (program, tools['dropbear']),
                 'install/resize2fs': (program, tools['resize2fs']),
                 'install/tune2fs': (program, tools['tune2fs']),
                 'install/ecid': (data, f'{ecid:x}\n'.encode()),
                 'install/authorized_keys': (data, authorized_keys.encode()),
                 'install/host_key': (private, host_key),
                 'root': (stat.S_IFDIR | 0o700, b''),
                 # Dropbear looks its user up.
                 'etc': (directory, b''),
                 'etc/passwd': (data, b'root:x:0:0:root:/root:/bin/sh\n')})


def layer_boot_files(layer, devicetree):
    """kernel, initrd and the board's device tree out of the platform layer."""
    found = {}
    with tarfile.open(layer) as archive:
        for member in archive:
            name = member.name.removeprefix('./')
            parts = name.split('/')
            if len(parts) >= 5 and parts[:3] == ['usr', 'lib', 'modules'] and member.isfile():
                if name.endswith('/vmlinuz'):
                    found['kernel'] = archive.extractfile(member).read()
                elif name.endswith('/initrd'):
                    found['initrd'] = archive.extractfile(member).read()
                elif name.endswith('/dtb/' + devicetree):
                    found['tree'] = archive.extractfile(member).read()
    missing = {'kernel', 'initrd', 'tree'} - set(found)
    if missing:
        raise ValueError(f'The platform layer lacks {", ".join(sorted(missing))}')
    return found


def payload(m1n1, logo, layer, devicetree, extra):
    """m1n1's own payload format (image/assemble.py): one file for Pongo to start."""
    files = layer_boot_files(layer, devicetree)
    initramfs = files['initrd'] + bytes(-len(files['initrd']) % 4) + extra
    return b''.join((Path(m1n1).read_bytes(), Path(logo).read_bytes() if logo else b'',
                     f'chosen.bootargs={BOOT_ARGS}\n'.encode(), files['tree'], files['kernel'],
                     INITRAMFS_MAGIC, struct.pack('<I', len(initramfs)), initramfs))


def cascadia_payload(loader, profile, layer, devicetree, extra):
    """The Cascadia loader's payload (image/cascadia.py): one file for a patched iBEC to start, on 32-bit boards.

    The same kernel, initrd and add-on as m1n1's payload. The loader copies the initrd and the
    add-on, one archive, to the board's initrd address before it turns off the MMU (the form
    Project Cascadia booted P105 with); the DTB's own boot arguments stay as they are. A kernel
    that also has orchard's initrd built in unpacks this copy over it.
    """
    from .image import cascadia
    files = layer_boot_files(layer, devicetree)
    initramfs = files['initrd'] + bytes(-len(files['initrd']) % 4) + extra
    image, _ = cascadia.assemble(Path(loader).read_bytes(), files['kernel'], files['tree'], initramfs,
                                 cascadia.profile(profile))
    return image


def boot_image(adapter, assets, extra):
    """The one file the board's loader is given: m1n1's payload, or the Cascadia loader's on 32-bit boards.

    The Cascadia loader is built for its board's addresses, which come from orchard's device data
    (hardware.payload_profile): the host assembles it, so a release needs to know no board.
    """
    if 'cascadia' in assets:
        from .hardware import payload_profile
        return cascadia_payload(assets['cascadia'], payload_profile(adapter.PROFILE, 'cascadia'),
                                assets['layer'], adapter.DEVICETREE, extra)
    return payload(assets['m1n1'], assets.get('logo'), assets['layer'], adapter.DEVICETREE, extra)


def start(adapter, device, image, assets, progress):
    """Hand IMAGE to the device: Pongo starts m1n1's payload. Starting the Cascadia loader's
    (a patched iBEC) is not part of orchard yet."""
    if 'cascadia' in assets:
        raise ValueError(f'Starting Linux on the {adapter.NAME} needs a patched iBEC, which orchard does not deliver yet')
    return adapter.boot_payload(device, image, assets['pongo'], progress)


def start_for_inspection(adapter, device, assets, ecid, progress):
    """The installer's system from RAM, with nothing written: reachable over SSH with this computer's keys."""
    from . import computer_key
    with tempfile.TemporaryDirectory(prefix='install-') as directory:
        _, _, _, host_key = make_keys(directory)
        extra = add_on(assets['install_tools'], ecid, '\n'.join(computer_key.public_keys()) + '\n', host_key)
        image = Path(directory) / 'payload.bin'
        image.write_bytes(boot_image(adapter, assets, extra))
        return start(adapter, device, image, assets, progress)


def shell(connection, command, *, input=b'', timeout=60):
    result = subprocess.run(connection.argv + [command], input=input, capture_output=True, timeout=timeout)
    if result.returncode:
        raise remote.RemoteError(f'{command.split()[0]} failed on the device: ' +
                                 result.stderr.decode(errors='replace').strip()[-400:])
    return result.stdout


def read_disk(connection, disk, offset, size, block):
    return shell(connection, f'dd if={disk} bs={block} skip={offset // block} count={size // block} 2>/dev/null')


def read_table(connection, disk):
    """(block, header, entries) of the disk's GPT; waits for the disk to appear."""
    deadline = time.monotonic() + 30
    name = Path(disk).name
    while True:
        result = subprocess.run(connection.argv + [f'cat /sys/class/block/{name}/queue/logical_block_size'],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
        if not result.returncode:
            break
        if time.monotonic() > deadline:
            raise ValueError('Storage did not come up within 30 s')
        time.sleep(0.1)
    block = int(result.stdout)
    header = read_disk(connection, disk, block, block, block)
    if header[:8] != b'EFI PART':
        raise D.InstallError('no-gpt', f'{disk} has no GPT')
    entries_lba, count, entry_size = struct.unpack_from('<QII', header, 72)
    size = -(-count * entry_size // block) * block
    return block, header, read_disk(connection, disk, entries_lba * block, size, block)[:count * entry_size]


def probe(start):
    """Filesystem, UUID and label from a partition's first bytes: BusyBox has no blkid here."""
    if start[1080:1082] == b'\x53\xef':
        return {'fs': 'ext4', 'fs_uuid': str(uuid.UUID(bytes=start[1128:1144])),
                'label': start[1144:1160].split(b'\0')[0].decode(errors='replace') or None}
    if start[82:90] == b'FAT32   ':
        return {'fs': 'vfat', 'fs_uuid': start[67:71][::-1].hex().upper(),
                'label': start[71:82].decode(errors='replace').strip() or None}
    if start[32:36] == b'NXSB':
        return {'fs': 'apfs', 'fs_uuid': None, 'label': None}
    return {'fs': None, 'fs_uuid': None, 'label': None}


def inspect(connection, disk):
    """The layout install_layout plans from, as install_device.py inspect reports it."""
    block, header, entries = read_table(connection, disk)
    info, partitions, free = D.parse_gpt(disk, block, header, entries)
    for part in partitions:
        part.update(probe(shell(connection, f'dd if={D.node(disk, part["number"])} bs=4096 count=1 2>/dev/null || true')))
    return {'disk': info, 'partitions': partitions, 'free': free}


def entry_bytes(kind, partuuid, start, blocks, name):
    return (uuid.UUID(kind).bytes_le + uuid.UUID(partuuid).bytes_le +
            struct.pack('<QQQ', start, start + blocks - 1, 0) + name.encode('utf-16-le')[:72].ljust(72, b'\0'))


def new_tables(plan, block, header, entries):
    """The plan's partitions in this GPT: [(lba, bytes)] of both copies, the backup first, and their numbers."""
    size = struct.unpack_from('<I', header, 12)[0]
    current, alternate = struct.unpack_from('<QQ', header, 24)
    entries_lba, count, entry_size = struct.unpack_from('<QII', header, 72)
    if entry_size != ENTRY_SIZE:
        raise D.InstallError('gpt', f'Unsupported GPT entry size {entry_size}')
    table = bytearray(entries)
    _, partitions, _ = D.parse_gpt('disk', block, header, entries)
    removed = plan.get('remove')
    if removed:
        table[(removed['number'] - 1) * entry_size:removed['number'] * entry_size] = bytes(entry_size)
    keep = [p for p in partitions if not removed or p['number'] != removed['number']]
    numbers = D.free_numbers(keep, 2)
    if max(numbers) > count:
        raise D.InstallError('gpt', 'The partition table is full')
    created = {}
    for spec, kind, name in ((plan['boot'], XBOOTLDR, 'boot'), (plan['root'], ROOT_ARM64, plan['root']['label'])):
        if spec is plan['boot'] and not spec['create']:
            continue
        number = numbers.pop(0)
        table[(number - 1) * entry_size:number * entry_size] = entry_bytes(kind, spec['partuuid'], spec['start'],
                                                                           spec['blocks'], name)
        created[spec['partuuid']] = number
    table = bytes(table)

    def sealed(this, other, where):
        raw = bytearray(header[:size])
        struct.pack_into('<QQ', raw, 24, this, other)
        struct.pack_into('<Q', raw, 72, where)
        struct.pack_into('<I', raw, 88, zlib.crc32(table))
        struct.pack_into('<I', raw, 16, 0)
        struct.pack_into('<I', raw, 16, zlib.crc32(raw))
        return bytes(raw).ljust(block, b'\0')
    last_usable = struct.unpack_from('<Q', header, 48)[0]
    return [(last_usable + 1, table), (alternate, sealed(alternate, current, last_usable + 1)),
            (entries_lba, table), (current, sealed(current, alternate, entries_lba))], created


def write_at(connection, disk, block, lba, data):
    """Write whole blocks at LBA. conv=fsync: this is what the partition table depends on."""
    padded = data + bytes(-len(data) % block)
    shell(connection, f'dd of={disk} bs={block} seek={lba} conv=notrunc,fsync 2>/dev/null', input=padded, timeout=120)


def write_partitions(connection, plan, disk):
    """Check the plan against the live table, write it and return {partuuid: node}."""
    block, header, entries = read_table(connection, disk)
    info, partitions, free = D.parse_gpt(disk, block, header, entries)
    D.check_plan(plan, info, partitions, free)
    writes, numbers = new_tables(plan, block, header, entries)
    for lba, data in writes:
        write_at(connection, disk, block, lba, data)
    # A re-read right after the write can be refused while the disk settles, so keep asking.
    wanted = ' '.join(D.node(disk, number) for number in numbers.values())
    shell(connection, f'for n in $(seq 150); do blockdev --rereadpt {disk} 2>/dev/null; ok=1; '
                      f'for p in {wanted}; do [ -e $p ] || ok=; done; [ -n "$ok" ] && exit 0; sleep 0.1; done; '
                      'echo "The new partitions did not appear" >&2; exit 1', timeout=30)
    block, header, entries = read_table(connection, disk)
    _, current, _ = D.parse_gpt(disk, block, header, entries)
    for spec in (plan['root'], plan['boot']):
        if spec.get('create', True):
            match = [p for p in current if p['partuuid'] == spec['partuuid']]
            if not match or (match[0]['start'], match[0]['blocks']) != (spec['start'], spec['blocks']):
                raise D.InstallError('table-verify', 'A new partition is not as planned')
    return {partuuid: D.node(disk, number) for partuuid, number in numbers.items()}, current


def stream_image(connection, node, image, source, map_text, progress):
    """The image onto NODE through the writer; returns its timings."""
    shell(connection, 'cat > /run/orchard/root.map', input=map_text.encode())
    process = subprocess.Popen(connection.argv + [f'/install/orchard-write {node} /run/orchard/root.map '
                                                  f'{image["bytes"]} {image["sha256"]}'],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    progress.reset_transfer()
    sent = 0
    try:
        for block in iter(lambda: source.read(1 << 20), b''):
            process.stdin.write(block)
            sent += len(block)
            progress.transfer(sent, image['bytes'])
        process.stdin.close()
    except BrokenPipeError:
        pass        # the writer stopped; its error says why
    output = process.stdout.read()
    errors = process.stderr.read()
    process.wait(timeout=600)
    if process.returncode:
        raise remote.RemoteError('Writing the system failed: ' + errors.decode(errors='replace').strip()[-400:])
    return json.loads(output)


LINK_LOCAL = 'fe80::1'
ALIAS = 'orchard-install'


def link_local_interfaces():
    """Network interfaces with an IPv6 link-local address on this computer; none where ifconfig differs."""
    try:
        text = subprocess.run(['/sbin/ifconfig'], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    found, current = set(), None
    for line in text.splitlines():
        if line and not line[0].isspace():
            current = line.split(':')[0]
        elif 'inet6 fe80' in line and current:
            found.add(current)
    return found


def device_hosts(before):
    """Where the device may answer: its link-local address on each new interface, then the DHCP route."""
    return [f'{LINK_LOCAL}%{name}' for name in sorted(link_local_interfaces() - before)] + [HOST]


@contextmanager
def session(profile, device, found, directory, client, host_key, before):
    """SSH to this boot of the add-on, with the keys made for it. BEFORE: interfaces before the boot."""
    hosts = Path(directory) / 'known_hosts'
    hosts.write_text(f'{ALIAS} {host_key}\n')
    hosts.chmod(0o600)

    def check():
        pass
    scope = BootState(DeviceState(profile, device['ecid']), found['boot_id'])
    deadline = time.monotonic() + 30
    host = None
    while host is None:
        for candidate in device_hosts(before):
            try:
                result = subprocess.run([*ssh_argv(client, hosts, candidate, 22, timeout=1, alias=ALIAS)[:-1], f'root@{candidate}', 'true'],
                                        stdin=subprocess.DEVNULL, capture_output=True, timeout=3)
            except subprocess.TimeoutExpired:
                continue
            if result.returncode == 0:
                host = candidate
                break
        if host is None:
            if time.monotonic() > deadline:
                raise RuntimeError('The device is on USB, but its SSH did not answer within 30 s')
            time.sleep(0.05)
    with one_connection(client, hosts, host, 22, alias=ALIAS) as argv:
        yield Connection(argv, scope, check)


def grow(connection, node, fs_uuid, label):
    """This install's identity and the partition's full size, while nothing has it mounted."""
    shell(connection, f'/install/tune2fs -U {fs_uuid} -L {shlex.quote(label)} {node} && /install/resize2fs {node}', timeout=300)


def mount_new_root(connection, node, boot_node):
    """The root and boot partitions mounted as the root will see them."""
    shell(connection, f'mkdir -p /newroot && mount -t ext4 {node} /newroot && '
                      'mount -t proc proc /newroot/proc && mount -t sysfs sysfs /newroot/sys && '
                      'mount -t devtmpfs dev /newroot/dev && mount -t tmpfs -o mode=0755 tmpfs /newroot/run && '
                      'mkdir -p /newroot/run/orchard /newroot/boot && mount --bind /run/orchard /newroot/run/orchard && '
                      f'mount -t vfat {boot_node} /newroot/boot && cp /newroot/etc/os-release /run/orchard/os-release',
          timeout=60)


def unmount_new_root(connection, root_partuuid):
    """Everything on storage, then the initrd carries on into the new root."""
    shell(connection, 'sync; for point in /newroot/boot /newroot/run/orchard /newroot/run /newroot/dev /newroot/sys '
                      '/newroot/proc /newroot; do umount "$point" || exit 1; done; sync', timeout=300)
    shell(connection, f"echo {root_partuuid} > /run/orchard/done.part && mv /run/orchard/done.part /run/orchard/done")
