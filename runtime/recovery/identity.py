"""Publish live ADT identity or a descriptor-bound boot identity handoff."""
import json
import os
from pathlib import Path
import re
import stat
import uuid

from adt import validate

try:
    from recovery_identity import (HANDOFF_PARAMETER, POLICY_PARAMETER, allows_source, object_json,
                                   parameters, validate_policy)
except ModuleNotFoundError as error:
    if error.name != 'recovery_identity':
        raise
    from orchard.recovery_identity import (HANDOFF_PARAMETER, POLICY_PARAMETER, allows_source, object_json,
                                           parameters, validate_policy)

RUN = Path('/run/orchard')
BOOT_ID = Path('/proc/sys/kernel/random/boot_id')
MTD = Path('/proc/mtd')
DEV = Path('/dev')
COMPATIBLE = Path('/proc/device-tree/compatible')
POLICY = Path('/etc/orchard/recovery-identity.json')
CMDLINE = Path('/proc/cmdline')
MAX_IDENTITY_BYTES = 2048


def _bounded(path, maximum, *, protected=False):
    try:
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or before.st_size > maximum or
                (protected and (before.st_uid != 0 or before.st_mode & 0o022))):
            raise ValueError('Identity input is not a protected regular file')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            after = os.fstat(stream.fileno())
            if (not stat.S_ISREG(after.st_mode) or
                    (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or
                    (protected and (after.st_uid != 0 or after.st_mode & 0o022))):
                raise ValueError('Identity input changed while opening')
            raw = stream.read(maximum + 1)
    except OSError as error:
        raise ValueError('Identity input is unavailable: ' + str(path)) from error
    if len(raw) > maximum:
        raise ValueError('Identity input exceeds its bound')
    return raw


def _policy():
    if not POLICY.exists() and not POLICY.is_symlink():
        return None
    return validate_policy(object_json(_bounded(POLICY, MAX_IDENTITY_BYTES, protected=True)))


def _compatible(policy):
    raw = _bounded(COMPATIBLE, 4096)
    if not raw.endswith(b'\0') or b'\0\0' in raw:
        raise ValueError('Live compatible is incomplete')
    try:
        first = raw.split(b'\0')[0].decode('ascii')
    except UnicodeError as error:
        raise ValueError('Live compatible is invalid') from error
    if first not in policy['compatibles']:
        raise ValueError('Live compatible differs from the recovery identity policy')
    return first


def _handoff_identity(policy):
    _compatible(policy)
    selected = parameters(_bounded(CMDLINE, MAX_IDENTITY_BYTES))
    if set(selected) != {POLICY_PARAMETER, HANDOFF_PARAMETER}:
        raise ValueError('Recovery command line requires its policy and host USB handoff')
    if selected[POLICY_PARAMETER] != policy:
        raise ValueError('Recovery command-line policy differs from the staged descriptor')
    handoff = selected[HANDOFF_PARAMETER]
    if not allows_source(policy, handoff['source']):
        raise ValueError('Recovery handoff source is absent from the staged policy')
    if any(type(handoff[name]) is not type(policy[name]) or handoff[name] != policy[name]
           for name in ('profile', 'chip', 'board')):
        raise ValueError('Host USB handoff differs from the recovery identity policy')
    handoff_id, template = handoff['handoff_id'], handoff['template_sha256']
    boot = uuid.UUID(BOOT_ID.read_text().strip())
    if not boot.int:
        raise ValueError('Invalid live boot ID')
    result = {'profile': policy['profile'], 'ecid': handoff['ecid'], 'boot_id': str(boot),
              'identity_source': handoff['source'], 'handoff_id': handoff_id,
              'template_sha256': template, 'chip': handoff['chip'], 'board': handoff['board']}
    serial(result)
    return result


def profile():
    """Use the staged descriptor for handoffs, or the existing ADT board spelling."""
    policy = _policy()
    if policy is not None:
        _compatible(policy)
        return policy['profile']
    board = COMPATIBLE.read_bytes().split(b'\0')[0].decode()
    if not re.fullmatch(r'apple,[a-z0-9]+', board):
        raise ValueError('Not an Apple board: ' + board)
    return board.split(',', 1)[1]


def cached():
    path = RUN / 'identity.json'
    if not path.exists() and not path.is_symlink():
        return None
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError('Device identity record is not protected')
    policy = _policy()
    if policy is not None:
        result = object_json(_bounded(path, MAX_IDENTITY_BYTES, protected=True))
        expected = _handoff_identity(policy)
        if (set(result) != set(expected) or
                any(type(result[name]) is not type(value) or result[name] != value
                    for name, value in expected.items())):
            raise ValueError('Device identity record differs from this boot handoff')
    else:
        result = json.loads(path.read_text())
    if policy is None and (result.get('profile') != profile() or type(result.get('ecid')) is not int or
            not 0 < result['ecid'] < 2**64 or
            result.get('boot_id') != str(uuid.UUID(BOOT_ID.read_text().strip()))):
        raise ValueError('Device identity record differs from this boot')
    return result


def initialize():
    if cached() is not None:
        raise ValueError('Device identity was already published')
    policy = _policy()
    if policy is not None:
        result = _handoff_identity(policy)
    else:
        rows = re.findall(r'^mtd([0-9]+): ([0-9a-fA-F]+) [0-9a-fA-F]+ "adt"$', MTD.read_text(), re.M)
        if len(rows) != 1:
            raise ValueError('Expected one live ADT MTD entry')
        number, size = rows[0][0], int(rows[0][1], 16)
        if not 8 <= size <= 16 * 1024**2:
            raise ValueError('Live ADT length outside bounds')
        fd = os.open(DEV / ('mtd' + number), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISCHR(os.fstat(stream.fileno()).st_mode):
                raise ValueError('Live ADT must be an MTD character device')
            blob = stream.read(size)
        if len(blob) != size:
            raise ValueError('Live ADT read was truncated')
        result = {'profile': profile(), 'ecid': validate(blob),
                  'boot_id': str(uuid.UUID(BOOT_ID.read_text().strip()))}
    RUN.mkdir(mode=0o700, parents=True, exist_ok=True)
    if RUN.is_symlink():
        raise ValueError('Linked runtime state refused')
    RUN.chmod(0o700)
    fd = os.open(RUN / 'identity.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(result, stream)
        stream.flush()
        os.fsync(stream.fileno())
    return result


def serial(record):
    result = f'ECID:{record["ecid"]:016x} BOOT:{record["boot_id"]} BOARD:{record["profile"]}'
    source = record.get('identity_source')
    if source is None and ('handoff_id' in record or 'template_sha256' in record):
        raise ValueError('Recovery serial has no handoff origin')
    if source is not None:
        origins = {'host-usb': 'h', 'ios-device': 'i'}
        nonce = record.get('handoff_id')
        if (not isinstance(source, str) or source not in origins or not isinstance(nonce, str) or
                not re.fullmatch('[0-9a-f]{32}', nonce) or not int(nonce, 16)):
            raise ValueError('Recovery serial has an invalid handoff origin')
        result += ' HND:' + record['handoff_id'] + ' ORG:' + origins[source]
        if len(result) > 126:
            raise ValueError('Recovery identity exceeds the USB serial string limit')
    return result


if __name__ == '__main__':
    print(serial(initialize()))
