"""Recovery-side LwVM installation above the kernel's logical block provider.

The installer never programs physical NAND. A failed metadata write leaves an
uncertain outcome, with saved bytes and the last intent in RAM for recovery.
"""
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess

from . import lwvm

BLKGETSIZE64_BASE = 0x80001272
BLKFLSBUF = 0x1261
BLKROGET = 0x125e
MAPPER = '/usr/sbin/orchard-lwvm'
JOURNAL = Path('/run/orchard/lwvm-write.json')
IDENTITY = Path('/run/orchard/identity.json')
WRITE_POLICY = Path('/sys/module/apple_sftl/parameters/write_enable')


class Provider:
    def __init__(self, path, *, writable=False):
        self.path = path
        self.fd = os.open(path, (os.O_RDWR if writable else os.O_RDONLY) | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(self.fd)
            if not stat.S_ISBLK(info.st_mode):
                raise lwvm.Error('LwVM provider must be a block device')
            self.device = {'major': os.major(info.st_rdev), 'minor': os.minor(info.st_rdev)}
            # Linux defines this request with sizeof(size_t), while the
            # returned value is always u64. ARMv7 and arm64 use different
            # request numbers with the same eight-byte output buffer.
            request = BLKGETSIZE64_BASE | (struct.calcsize('P') << 16)
            self.size = struct.unpack('Q', fcntl.ioctl(self.fd, request, bytes(8)))[0]
        except BaseException:
            os.close(self.fd)
            raise

    def close(self):
        os.close(self.fd)

    def read_only(self):
        return bool(struct.unpack('I', fcntl.ioctl(self.fd, BLKROGET, bytes(4)))[0])

    def read(self, offset, length):
        if offset < 0 or length < 0 or offset + length > self.size:
            raise lwvm.Error('Provider read is outside the medium')
        raw = os.pread(self.fd, length, offset)
        if len(raw) != length:
            raise lwvm.Error('Provider read was incomplete')
        return raw

    def write_header(self, offset, header):
        if len(header) != lwvm.HEADER_BYTES or offset not in (0, lwvm.chunk_size(self.size) - lwvm.HEADER_BYTES):
            raise lwvm.Error('Only the two planned LwVM headers can be written')
        if os.pwrite(self.fd, header, offset) != len(header):
            raise lwvm.Error('LwVM header write was incomplete')

    def flush(self):
        os.fsync(self.fd)
        # Invalidate block buffers before readback, after the driver's flush.
        fcntl.ioctl(self.fd, BLKFLSBUF)

    def pair(self):
        offset = lwvm.chunk_size(self.size) - lwvm.HEADER_BYTES
        primary = self.read(0, lwvm.HEADER_BYTES)
        backup = self.read(offset, lwvm.HEADER_BYTES)
        return primary, backup


def save_intent(record, path=JOURNAL):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix('.part')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(record, stream, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def commit_headers(provider, allocation, *, record=save_intent):
    primary, backup = provider.pair()
    snapshot, proposed, _ = lwvm.validate_plan(allocation, primary, backup, provider.size)
    if proposed == primary:
        return
    state = {'schema': 1, 'kind': 'orchard.lwvm-write-intent', 'provider': provider.path,
             'device': provider.device, 'allocation': allocation, 'outcome': 'prepared'}
    record(state)
    try:
        for stage, offset in (('backup', snapshot['backup_offset']), ('primary', 0)):
            # Recheck both expected copies immediately before each write.
            live_primary, live_backup = provider.pair()
            expected_backup = primary if stage == 'backup' else proposed
            if live_primary != primary or live_backup != expected_backup:
                raise lwvm.Error('LwVM pair changed during metadata commit')
            state = {**state, 'stage': stage, 'outcome': 'write-intent'}
            record(state)
            provider.write_header(offset, proposed)
            provider.flush()
            if provider.read(offset, lwvm.HEADER_BYTES) != proposed:
                raise lwvm.Error('LwVM header readback differs')
            state = {**state, 'outcome': 'verified'}
            record(state)
        lwvm.parse(*provider.pair(), provider.size)
        state = {**state, 'outcome': 'complete'}
        record(state)
    except BaseException as error:
        try:
            record({**state, 'outcome': 'uncertain', 'error': str(error)})
        except Exception:
            pass
        raise lwvm.Error('LwVM metadata outcome is uncertain; stop and inspect the saved intent. ' + str(error)) from error


def current_identity():
    info = IDENTITY.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise lwvm.Error('Recovery identity record is not protected')
    identity = json.loads(IDENTITY.read_text())
    if identity.get('boot_id') != Path('/proc/sys/kernel/random/boot_id').read_text().strip():
        raise lwvm.Error('Recovery identity belongs to a different boot')
    return identity


def check_previous_intent(path=JOURNAL):
    if path.with_suffix('.part').exists() or path.with_suffix('.part').is_symlink():
        raise lwvm.Error('An incomplete LwVM intent record requires inspection')
    if path.exists() or path.is_symlink():
        previous = json.loads(lwvm.saved_file(path, limit=1 << 20))
        if previous.get('kind') != 'orchard.lwvm-write-intent' or previous.get('outcome') != 'complete':
            raise lwvm.Error('A previous LwVM metadata outcome requires inspection')


def inspect(disk):
    provider = Provider(disk)
    try:
        primary, backup = provider.pair()
        snapshot = lwvm.parse(primary, backup, provider.size)
        return {'disk': {'path': disk, 'sector': lwvm.SECTOR, 'table': 'lwvm',
                         'table_sha256': snapshot['header_sha256'], 'bytes': provider.size,
                         **provider.device},
                'partitions': [lwvm.public_partition(p) for p in snapshot['partitions']], 'free': [],
                'lwvm': {'primary': lwvm.b64(primary), 'backup': lwvm.b64(backup)}}
    finally:
        provider.close()


def check_identity(plan, identity):
    if plan.get('identity') != {key: identity[key] for key in ('profile', 'ecid', 'boot_id')}:
        raise lwvm.Error('Installation plan belongs to a different recovery boot')
    proof = plan['lwvm'].get('shrink_proof')
    if proof and proof['device'] != {key: identity[key] for key in ('profile', 'ecid')}:
        raise lwvm.Error('Native HFS preparation belongs to a different device')


def enable_writes(api, policy, *, changed):
    """Reload a quiet modular provider with its explicit logical-write policy.

    The policy is fixed at probe. A built-in read-only driver fails this step
    before metadata or filesystem writes. Hardware write support has its own
    default-off kernel configuration prerequisites.
    """
    if not WRITE_POLICY.is_file():
        raise lwvm.Error('Logical write policy is unavailable in this kernel')
    api.run('modprobe', '-r', 'apple-sftl', timeout=30)
    # Record the transition before loading or reading sysfs can fail. The
    # caller's cleanup owns the unload and default-policy restoration.
    changed()
    api.run('modprobe', 'apple-sftl', 'write_enable=1',
            'max_page_reads=' + str(policy['max_page_reads']),
            'max_io_time_ms=' + str(policy['max_io_time_ms']), timeout=30)
    if WRITE_POLICY.read_text().strip().lower() not in ('y', '1'):
        raise lwvm.Error('Logical write policy did not become available')
    return True


def mapped_root(api, disk, allocation, snapshot, root, device):
    raw = api.run(MAPPER, 'map', '--disk', disk, '--root', root['partuuid'], '--mode', 'rw', '--json', timeout=30)
    receipt = json.loads(raw)
    expected = [{'start_sector': p['logical'] // lwvm.SECTOR, 'sectors': p['bytes'] // lwvm.SECTOR,
                 'physical_sector': p['physical'] // lwvm.SECTOR} for p in root['extents']]
    mapping = receipt.get('mapping', {})
    canonical = root['partuuid']
    if (receipt.get('schema') != 1 or receipt.get('kind') != 'orchard-lwvm-root' or
            receipt.get('root_uuid') != canonical or receipt.get('partition_index') != root['number'] - 1 or
            receipt.get('header_crc32') != f'{lwvm.checksum(lwvm.unb64(allocation["after"])):08x}' or
            receipt.get('media_bytes') != snapshot['media_bytes'] or receipt.get('chunk_bytes') != snapshot['chunk_bytes'] or
            receipt.get('bytes') != root['bytes'] or receipt.get('extents') != expected or receipt.get('disk') != device or
            mapping.get('name') != 'orchard-lwvm-' + canonical or mapping.get('uuid') != 'ORCHARD-LWVM-' + canonical or
            mapping.get('mode') != 'rw' or not re.fullmatch(r'/dev/dm-[0-9]+', mapping.get('device', ''))):
        raise lwvm.Error('LwVM mapper receipt differs from the planned Linux extents')
    path = mapping['device']
    node = Provider(path)
    try:
        if node.size != root['bytes'] or node.read_only():
            raise lwvm.Error('Mapped root size or access mode differs from the plan')
        sysfs = Path('/sys/dev/block', f'{node.device["major"]}:{node.device["minor"]}', 'dm')
        if sysfs.joinpath('uuid').read_text().strip() != mapping['uuid'] or sysfs.joinpath('name').read_text().strip() != mapping['name']:
            raise lwvm.Error('Mapped root identity changed')
    finally:
        node.close()
    return path, {**mapping, 'root_uuid': canonical, 'extents': expected}


def cleanup_command(argv, step, failed, *, timeout):
    try:
        result = subprocess.run(argv, capture_output=True, timeout=timeout)
        if result.returncode:
            failed.append(step)
            return False
    except Exception as error:
        failed.append(step + ': ' + str(error))
        return False
    return True


def cleanup(api, disk, mounts, mapping, provider, *, disk_verified, changed_policy):
    """Attempt the raw fence even when an earlier cleanup operation fails."""
    failed, pending = [], None
    try:
        for point in reversed(mounts):
            cleanup_command(['umount', str(point)], str(point), failed, timeout=60)
        if provider is not None:
            try:
                provider.close()
            except OSError as error:
                failed.append('provider close: ' + str(error))
            provider = None
        if mapping is not None and not failed:
            argv = [MAPPER, 'unmap', '--disk', disk, '--root', mapping['root_uuid'], '--mode', mapping['mode']]
            for extent in mapping['extents']:
                argv += ['--extent', f'{extent["start_sector"]}:{extent["sectors"]}:{extent["physical_sector"]}']
            cleanup_command(argv, mapping['name'], failed, timeout=30)
        if changed_policy and not failed:
            if disk_verified:
                cleanup_command(['blockdev', '--setro', disk], 'raw provider read-only fence', failed, timeout=15)
            if cleanup_command(['modprobe', '-r', 'apple-sftl'], 'logical write policy', failed, timeout=30):
                cleanup_command(['modprobe', 'apple-sftl'], 'default read-only provider reload', failed, timeout=30)
    except BaseException as error:
        pending = error
    finally:
        # Cancellation can interrupt umount or the mapper. Release this
        # process's raw FD, then attempt the fence before re-raising it.
        if provider is not None:
            try:
                provider.close()
            except BaseException as error:
                if pending is None:
                    pending = error
        try:
            if disk_verified:
                cleanup_command(['blockdev', '--setro', disk], 'raw provider read-only fence', failed, timeout=15)
        except BaseException as error:
            if pending is None:
                pending = error
    if pending is not None:
        raise pending
    if failed:
        raise api.InstallError('storage-cleanup-stopped', 'Installation cleanup requires inspection: ' + ', '.join(failed))


def install(api, plan, stream):
    if os.geteuid() != 0:
        raise api.InstallError('not-root', 'The installer must run as root')
    disk = plan['disk']['path']
    mounts, mapping, changed_policy, disk_verified = [], None, False, False
    account = plan.get('account')
    secrets = api.read_secrets(api.SECRETS, account) if account and (account['password'] or account['pin']) else None
    provider = None
    def policy_changed():
        nonlocal changed_policy
        changed_policy = True
    try:
        check_identity(plan, current_identity())
        check_previous_intent()
        provider = Provider(disk)
        expected_device = {key: plan['disk'][key] for key in ('major', 'minor')}
        if provider.device != expected_device or provider.size != plan['disk']['bytes']:
            raise lwvm.Error('LwVM provider identity changed')
        snapshot, proposed, root = lwvm.validate_plan(plan['lwvm'], *provider.pair(), provider.size)
        if plan['root']['partuuid'] != root['partuuid'] or plan['root']['blocks'] * lwvm.SECTOR != root['bytes']:
            raise lwvm.Error('Root format range differs from the allocation plan')
        if plan.get('io_policy') != lwvm.install_io_policy(root['bytes']):
            raise lwvm.Error('Logical I/O policy differs from the bounded install plan')
        api.check_quiet(disk, [])
        disk_verified = True
        api.emit('step', step='check', state='done')
        provider.close()
        provider = None
        changed_policy = enable_writes(api, plan['io_policy'], changed=policy_changed)
        provider = Provider(disk)
        if provider.device != expected_device or provider.size != plan['disk']['bytes']:
            raise lwvm.Error('Reprobed LwVM provider identity changed')
        # A fresh gendisk's initial RO flag reports the kernel's capability,
        # including replay-floor checks. Inspect it before clearing any fence.
        if provider.read_only():
            raise lwvm.Error('The recovered logical provider has no qualified write path')
        provider.close()
        provider = None
        api.run('blockdev', '--setrw', disk)
        provider = Provider(disk, writable=True)
        if provider.device != expected_device or provider.size != plan['disk']['bytes']:
            raise lwvm.Error('Writable LwVM provider identity changed')
        commit_headers(provider, plan['lwvm'])
        api.emit('step', step='partitions', state='done')
        snapshot = lwvm.parse(*provider.pair(), provider.size)
        root_node, mapping = mapped_root(api, disk, plan['lwvm'], snapshot, root, provider.device)
        # All filesystem writes are confined by this checked dm-linear table.
        api.run('mke2fs', '-t', 'ext4', '-b', '4096', '-m', '0', '-U', plan['root']['fs_uuid'],
                '-L', plan['root']['label'], '-E', 'nodiscard,lazy_itable_init=0,lazy_journal_init=0', root_node, timeout=300)
        api.emit('step', step='format-root', state='done')
        api.TARGET.mkdir(parents=True, exist_ok=True)
        api.run('mount', '-t', 'ext4', root_node, str(api.TARGET))
        mounts.append(api.TARGET)
        result = api.extract(stream, api.TARGET, size=plan['image']['bytes'], sha256=plan['image']['sha256'],
                             expanded_limit=root['bytes'], compression=plan['image'].get('compression', 'zstd'))
        api.emit('step', step='extract-root', state='done', **result)
        layer = plan['layer']
        files = api.install_layer(layer['path'], api.TARGET, size=layer['bytes'], sha256=layer['sha256'], expanded_limit=layer['expanded_bytes'])
        Path(layer['path']).unlink()
        api.emit('step', step='platform-layer', state='done', files=files)
        firmware = plan.get('firmware')
        if firmware:
            with open(firmware['path'], 'rb') as bundle:
                files = api.install_firmware(bundle, api.TARGET, size=firmware['bytes'], sha256=firmware['sha256'])
            Path(firmware['path']).unlink()
            api.emit('step', step='firmware', state='done', files=files)
        api.write_text(api.TARGET / 'etc/fstab', f'UUID={plan["root"]["fs_uuid"]} / ext4 rw,relatime 0 1\n')
        for name, fs in (('proc', 'proc'), ('sys', 'sysfs'), ('dev', 'devtmpfs')):
            point = api.TARGET / name
            point.mkdir(exist_ok=True)
            api.run('mount', '-t', fs, name, str(point))
            mounts.append(point)
        if account:
            api.emit('step', step='account', state='done', user=api.set_account(api.TARGET, account, *(secrets or (None, None))))
            secrets = None
        version = api.kernel_version(api.TARGET)
        # Native iOS owns the System partition and prepares its launcher/assets.
        handoff = {'schema': 1, 'kind': 'orchard.ios-local-file-root', 'root_uuid': root['partuuid'],
                   'fs_uuid': plan['root']['fs_uuid'], 'kernel_version': version,
                   'options': 'root=PARTUUID=' + root['partuuid'] + ' rw ' + plan['entry'].get('options', ''),
                   'devicetree': plan['entry']['devicetree'], 'boot_assets_prepared': False}
        api.write_text(api.TARGET / 'etc/orchard/installed-root.json', json.dumps(handoff, indent=2) + '\n')
        api.run('sync', '-f', str(api.TARGET), timeout=120)
    except lwvm.Error as error:
        raise api.InstallError('lwvm-install-stopped', str(error)) from error
    finally:
        cleanup(api, disk, mounts, mapping, provider, disk_verified=disk_verified, changed_policy=changed_policy)
    api.emit('finished', root=root['partuuid'], boot=handoff, boot_preparation_required=True)
