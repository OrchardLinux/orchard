# SPDX-License-Identifier: GPL-2.0-only
"""Two-part ARM32 recovery upload through a prepared iBEC autogo chain.

Entry into a pwned iBSS is an explicit external step. This transport sends
ordinary RAM uploads, records each uncertain outcome before starting it, and
waits for the same ECID's identified Linux recovery. It never sends flash,
saveenv, reset or retry commands.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import struct
import subprocess
import time
import zlib

from orchard.integrity import digest
from orchard.contracts import HostHandoff
from orchard import cascadia_payload as payload_image
from orchard.state import BootState, DeviceState, private_directory
from . import iboot32, usb

CONFIG_AT = 0x7f00
CONFIG = struct.Struct('<4s16I')
AUTOGO_TRIGGER_LIMIT = 0x8000
RECOVERY_PACKET_BYTES = 512


def tool():
    executable = os.environ.get('ORCHARD_IRECOVERY') or shutil.which('irecovery')
    if not executable:
        raise ValueError('Install libirecovery on the host before using recovery-autogo')
    return str(executable)


def query(device, executable):
    """Read iRecovery identity for the exact selected ECID; no state change."""
    result = subprocess.run([executable, '-i', hex(device['ecid']), '-q'],
                            check=False, capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise ValueError('Selected recovery query failed')
    fields = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition(':')
        if sep and key in ('ECID', 'CPID', 'BDID', 'PRODUCT', 'MODE', 'PWND'):
            if key in fields:
                raise ValueError('Recovery query repeats identity fields')
            fields[key] = value.strip()
    try:
        ecid = int(fields.get('ECID', ''), 0)
        chip, board = (int(fields.get(key, ''), 16) for key in ('CPID', 'BDID'))
    except ValueError as error:
        raise ValueError('Recovery query lacks complete chip identity') from error
    if (ecid, chip, board) != (device['ecid'], device['chip'], device['board']):
        raise ValueError('Recovery identity differs from the selected device')
    mode = fields.get('MODE', '').lower()
    if mode not in ('dfu', 'recovery'):
        raise ValueError('Expected DFU or iBEC Recovery')
    return dict(fields, mode=mode)


def bundle(path, expected, selected):
    """Validate the complete CB32 bundle before device access."""
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not re.fullmatch('[0-9a-f]{64}', expected or ''):
        raise ValueError('Expected a regular recovery bundle with a SHA-256 receipt')
    if not 0x8000 < path.stat().st_size <= selected['maximum_payload_bytes']:
        raise ValueError('Recovery payload exceeds the selected iBEC upload bound')
    with path.open('rb') as stream:
        data = stream.read(selected['maximum_payload_bytes'] + 1)
    if len(data) > selected['maximum_payload_bytes']:
        raise ValueError('Recovery payload changed beyond its upload bound')
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError('Recovery payload differs from its manifest')
    validate_bundle(data, selected)
    checked_recovery_upload_size(len(data))
    return data


def validate_bundle(data, selected):
    """Check the final bytes after an attempt identity changes the DTB size."""
    if not 0x8000 < len(data) <= selected['maximum_payload_bytes']:
        raise ValueError('Recovery payload exceeds the selected iBEC upload bound')
    if len(data) < 0x8000 + 0x30 or data[:4] != bytes.fromhex('c0000cf1'):
        raise ValueError('Expected an ARM32 recovery payload')
    values = CONFIG.unpack_from(data, CONFIG_AT)
    (magic, version, size, address, kernel, dtb, dtb_size, initrd, initrd_size,
     initrd_address, total, watchdog, l2, ram_first, ram_end, initrd_end, crc) = values
    if (magic, version, size) != (b'CB32', 1, CONFIG.size) or crc != zlib.crc32(data[CONFIG_AT:CONFIG_AT + CONFIG.size - 4]):
        raise ValueError('Recovery loader configuration is incomplete or corrupt')
    if (address != selected['payload_address'] or kernel != 0x8000 or total != len(data)
            or not kernel + 0x30 <= dtb < dtb + dtb_size <= initrd < initrd + initrd_size == total
            or not ram_first <= address < address + total <= initrd_address < initrd_address + initrd_size <= initrd_end <= ram_end
            or watchdog % 4 or l2 % 4 or initrd_address % 4096
            or struct.unpack_from('<I', data, kernel + 0x24)[0] != 0x016f2818):
        raise ValueError('Recovery loader ranges differ from the selected platform')


def loader_trigger(data, candidate=None):
    """Return the exact small loader prefix that makes the patched iBEC run it."""
    size = CONFIG_AT + CONFIG.size
    trigger = data[:size] if candidate is None else candidate
    if (size >= AUTOGO_TRIGGER_LIMIT or len(trigger) != size or
            trigger != data[:size]):
        raise ValueError('Recovery loader trigger differs from the checked bundle prefix')
    checked_recovery_upload_size(len(trigger))
    return trigger


def checked_recovery_upload_size(size):
    """Reject lengths whose final Recovery ZLP cannot be proved by irecovery."""
    if type(size) is not int or size <= 0 or size % RECOVERY_PACKET_BYTES == 0:
        raise ValueError('Recovery upload length has an unobservable trailing USB packet')
    return size


def recovery_handoff(profile, endpoint):
    """Recover the exact completed host handoff before resuming a USB session."""
    identifier = endpoint.get('handoff_id')
    if identifier is None:
        return None
    if not isinstance(identifier, str) or not re.fullmatch('[0-9a-f]{32}', identifier):
        raise ValueError('Invalid recovery handoff identity')
    scope = BootState(DeviceState(profile, endpoint['ecid']), endpoint['boot_id'])
    folder = private_directory(scope.device.root / 'boot-attempts')
    path = folder / (identifier + '.json')
    if (path.is_symlink() or not path.is_file() or path.stat().st_size > 65536 or
            path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077):
        raise ValueError('The host-bound recovery has no private completed attempt receipt')
    try:
        record = json.loads(path.read_text())
        handoff = HostHandoff(record['handoff_id'], record['template_sha256'], record['chip'], record['board'])
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise ValueError('Invalid completed recovery handoff receipt') from error
    if (not scope.matches(record) or record.get('profile') != profile or
            type(record.get('schema')) is not int or record.get('schema') != 1 or
            record.get('method') != 'recovery-autogo' or
            record.get('phase') != 'complete' or record.get('payload_format') != 'cascadia-cb32' or
            record.get('loader_bytes') != CONFIG_AT + CONFIG.size or
            not isinstance(record.get('loader_sha256'), str) or
            not re.fullmatch('[0-9a-f]{64}', record['loader_sha256']) or
            record.get('identity_source') != 'host-usb' or handoff.handoff_id != identifier or
            endpoint.get('profile') != profile):
        raise ValueError('Recovery differs from its completed host handoff receipt')
    return handoff


def save(path, record):
    temporary = path.with_suffix('.pending')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(record, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def selected(device, executable, *, mode, product):
    observed = usb.selected(device['location'], device['ecid'], chip=device['chip'],
                            board=device['board'], mode=mode)
    current = query(device, executable)
    if current['mode'] != mode or current.get('PRODUCT') != product:
        raise ValueError('Selected Apple boot stage changed')
    return observed, current


def wait_recovery(device, executable, *, product, seconds=30):
    deadline = time.monotonic() + seconds
    while True:
        try:
            observations = [row for row in usb.devices() if row['mode'] in ('dfu', 'recovery', 'pongo', 'linux')]
            if len(observations) > 1:
                raise RuntimeError('More than one target appeared during the boot transition')
            if len(observations) == 1:
                current = observations[0]
                if current.get('ecid') != device['ecid']:
                    raise RuntimeError('Another device appeared during the boot transition')
                if current['mode'] == 'recovery':
                    chosen = dict(current, chip=device['chip'], board=device['board'])
                    selected(chosen, executable, mode='recovery', product=product)
                    device.update(location=current['location'], mode='recovery')
                    return
        except (OSError, subprocess.SubprocessError, ValueError):
            pass  # Only observations are polled; uploads are never repeated.
        if time.monotonic() >= deadline:
            raise RuntimeError('iBEC did not enumerate; inspect the saved attempt before another startup')
        time.sleep(0.25)


def upload(device, path, executable, *, product, expected, mode='recovery'):
    if mode == 'recovery':
        checked_recovery_upload_size(Path(path).stat().st_size)
    selected(device, executable, mode=mode, product=product)
    if digest(path) != expected:
        raise ValueError('Private boot snapshot changed before upload')
    result = subprocess.run([executable, '-i', hex(device['ecid']), '-f', str(path)],
                            capture_output=True, timeout=180, check=False)
    if (result.returncode or
            not re.search(rb'(?<![0-9])100(?:\.0+)?%', result.stdout or b'')):
        raise RuntimeError('Recovery upload outcome is unknown; inspect the device before another attempt')


def boot(device, assets, manifest, progress, *, profile, chip, board, product):
    if device['mode'] == 'linux':
        current = usb.recovery(product, profile)
        if current['ecid'] != device['ecid'] or current['boot_id'] != device.get('boot_id'):
            raise ValueError('Selected running recovery changed')
        if current.get('handoff_id') != device.get('handoff_id'):
            raise ValueError('Selected running recovery handoff changed')
        BootState(DeviceState(profile, current['ecid']), current['boot_id'])
        recovery_handoff(profile, current)
        return
    if device['mode'] not in ('dfu', 'recovery') or (device.get('chip'), device.get('board')) != (chip, board):
        raise ValueError('Enter the selected board in kDFU or iBEC Recovery first')
    prepared_root = assets.get('prepared_boot') or os.environ.get('ORCHARD_PREPARED_BOOT')
    stage, recipe, source_receipt = iboot32.verified(profile, prepared_root)
    executable = tool()
    recovery = Path(assets['recovery'])
    expected = manifest['assets']['recovery']['sha256']
    payload = bundle(recovery, expected, recipe)
    policy = payload_image.host_identity_policy(payload)
    observed, queried = selected(device, executable, mode=device['mode'], product=recipe['device'])
    if device['mode'] == 'dfu' and not observed.get('pwned'):
        raise ValueError('Plain DFU is unsupported here; enter a pwned iBSS or iBEC Recovery first')
    folder = private_directory(DeviceState(profile, device['ecid']).root / 'boot-attempts')
    identifier = secrets.token_hex(16)
    handoff = None
    if policy is not None:
        handoff = HostHandoff(identifier, expected, chip, board)
        payload = payload_image.bind_host_identity(payload, ecid=device['ecid'], chip=chip,
                                                   board=board, profile=profile,
                                                   handoff_id=identifier, template_sha256=expected)
        validate_bundle(payload, recipe)
    checked_recovery_upload_size(len(payload))
    upload_hash = hashlib.sha256(payload).hexdigest()
    trigger = loader_trigger(payload)
    trigger_hash = hashlib.sha256(trigger).hexdigest()
    attempt = folder / (identifier + '.json')
    image_copy = folder / (identifier + '-recovery.bin')
    trigger_copy = folder / (identifier + '-loader.bin')
    stage_copy = folder / (identifier + '-ibec.img3')
    record = {'schema': 1, 'method': 'recovery-autogo', 'ecid': device['ecid'], 'profile': profile,
              'build': recipe['build'], 'recovery_sha256': expected,
              'payload_format': 'cascadia-cb32',
              'ibec_sha256': source_receipt['ibec']['sha256'], 'phase': 'validated'}
    record['recovery_sha256'] = upload_hash
    record['loader_sha256'] = trigger_hash
    record['loader_bytes'] = len(trigger)
    if handoff is not None:
        record.update(handoff.fields())
    try:
        # Freeze inputs so another checkout rebuilding a shared component cannot
        # replace an upload after this operation has validated it.
        for data, path, checksum in ((payload, image_copy, upload_hash),
                                     (trigger, trigger_copy, trigger_hash),
                                     (stage.read_bytes(), stage_copy, record['ibec_sha256'])):
            if hashlib.sha256(data).hexdigest() != checksum:
                raise ValueError('Boot input changed while creating its private snapshot')
            with path.open('xb') as stream:
                stream.write(data)
            path.chmod(0o600)
        save(attempt, record)
        if device['mode'] == 'dfu':
            record['phase'] = 'ibec-upload-outcome-unknown'
            save(attempt, record)
            upload(device, stage_copy, executable, product=recipe['device'], mode='dfu',
                   expected=record['ibec_sha256'])
            wait_recovery(device, executable, product=recipe['device'])
        record['phase'] = 'recovery-upload-outcome-unknown'
        save(attempt, record)
        progress.phase('Uploading ARM32 recovery')
        upload(device, image_copy, executable, product=recipe['device'], expected=upload_hash)
        record['phase'] = 'loader-upload-outcome-unknown'
        save(attempt, record)
        progress.phase('Starting the checked ARM32 recovery loader')
        upload(device, trigger_copy, executable, product=recipe['device'], expected=trigger_hash)
        record['phase'] = 'awaiting-linux'
        save(attempt, record)
        deadline = time.monotonic() + 120
        while True:
            try:
                observations = [row for row in usb.devices()
                                if row['mode'] in ('dfu', 'recovery', 'pongo', 'linux')]
                if len(observations) > 1:
                    raise RuntimeError('Ambiguous USB identity after the payload upload')
                if observations and observations[0].get('ecid') not in (None, device['ecid']):
                    raise RuntimeError('Another device appeared after the payload upload')
                if not observations or observations[0]['mode'] != 'linux':
                    raise ValueError('Waiting for Linux recovery')
                endpoint = usb.recovery(product, profile)
                if endpoint['ecid'] != device['ecid']:
                    raise RuntimeError('Another device appeared after the payload upload')
                if endpoint.get('profile') != profile:
                    raise RuntimeError('Another board appeared after the payload upload')
                try:
                    BootState(DeviceState(profile, endpoint['ecid']), endpoint['boot_id'])
                except (KeyError, ValueError) as error:
                    raise RuntimeError('Linux recovery has no valid device boot identity') from error
                if handoff is not None and endpoint.get('handoff_id') != handoff.handoff_id:
                    raise RuntimeError('Linux recovery differs from the uploaded host handoff')
                if handoff is None and endpoint.get('handoff_id') is not None:
                    raise RuntimeError('Linux recovery advertised an unexpected host handoff')
                record.update(phase='complete', boot_id=endpoint['boot_id'])
                save(attempt, record)
                device.update(endpoint, resumed=True)
                return
            except (OSError, subprocess.SubprocessError, ValueError):
                if time.monotonic() >= deadline:
                    raise RuntimeError('ARM32 recovery did not enumerate; inspect the saved boot attempt')
                time.sleep(0.25)
    finally:
        # Attempt-private copies are no longer needed after this command
        # exits. The outcome receipt retains their exact hashes.
        for path in (image_copy, trigger_copy, stage_copy):
            path.unlink(missing_ok=True)


def boot_ram(output, receipt, *, yes=False):
    """Use the generic adapter to boot one checked local ARM32 RAM payload."""
    from orchard import devices, hardware
    from orchard.boot import confirm_boot
    from orchard.progress import Progress
    from orchard.ram import verify_ram
    from orchard.state import operation_lock

    output = Path(output)
    with operation_lock():
        observed = usb.sole_target()
        if observed['mode'] not in ('dfu', 'recovery'):
            raise ValueError('Enter a pwned iBSS or iBEC Recovery before an ARM32 RAM boot')
        matches = []
        for profile in receipt['boards']:
            described = hardware.device(profile)
            identity = described['match']['dfu']
            if (observed.get('chip'), observed.get('board')) == (identity['chip'], identity['board']):
                matches.append(profile)
        if len(matches) != 1:
            raise ValueError('This ARM32 payload does not name exactly one matching board')
        adapter = devices.load(matches[0])
        device = adapter.discover()
        if (device['ecid'], device['location'], device['mode']) != (observed.get('ecid'), observed['location'], observed['mode']):
            raise ValueError('Device selection changed before startup confirmation')
        print(f'{adapter.NAME}, mode {device["mode"]}.')
        if not yes:
            confirm_boot()
        current = verify_ram(output)
        if current != receipt:
            raise ValueError('RAM build receipt changed after confirmation')
        request = {'boot_method': 'recovery-autogo',
                   'assets': {'recovery': {'sha256': receipt['files']['linux.bin']}}}
        adapter.boot(device, {'recovery': output / 'linux.bin'}, request, Progress())
    print('The selected ARM32 Linux recovery is ready on USB.')
