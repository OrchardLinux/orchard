"""Install targets from a device's partition layout, and the plan for one.

Input is the `layout` event of the device install script's `inspect` (see
install_device.py). Pure logic: nothing here touches a device. The rules
follow hardware docs/boot-contract.md:

- APFS and every partition that is not a Linux type are never touched.
- A target is either a free range or one existing Linux partition to replace.
- The disk has at most one XBOOTLDR boot partition; it is created inside the
  target when missing, and an existing one is reused.
"""
from dataclasses import dataclass
from typing import Dict, List, Optional
import uuid

from . import lwvm

XBOOTLDR = 'bc13c2ff-59e6-4262-a352-b275fd6f7172'
ROOT_ARM64 = 'b921b045-1df0-41c3-af44-4c6f280d3fae'
LINUX_DATA = '0fc63daf-8483-4772-8e79-3d69d8477de4'
REPLACEABLE = {ROOT_ARM64, LINUX_DATA}

MIB = 1 << 20
GIB = 1 << 30
BOOT_BYTES = 1 * GIB
MIN_ROOT_BYTES = 8 * GIB
PLAN_VERSION = 2


class LayoutError(ValueError):
    pass


@dataclass
class Option:
    kind: str                      # 'free' or 'replace'
    start: int                     # first block of the target range
    blocks: int
    description: str
    partition: Optional[dict] = None
    creates_boot: bool = False

    def bytes(self, block):
        return self.blocks * block


def _align_up(value, step):
    return -(-value // step) * step


def _align_down(value, step):
    return value // step * step


def boot_partition(layout) -> Optional[dict]:
    boots = [p for p in layout['partitions'] if p['type'] == XBOOTLDR]
    if len(boots) > 1:
        raise LayoutError('The disk has more than one XBOOTLDR boot partition; '
                          'refusing to choose one (see the boot contract)')
    return boots[0] if boots else None


def options(layout) -> List[Option]:
    """Every allowed target, in disk order."""
    if layout['disk'].get('table') == 'lwvm':
        return lwvm_options(layout)
    block = layout['disk']['sector']
    boot = boot_partition(layout)
    needed = MIN_ROOT_BYTES + (0 if boot else BOOT_BYTES)
    align = MIB // block
    result = []
    for free in layout['free']:
        start = _align_up(free['start'], align)
        end = _align_down(free['start'] + free['blocks'], align)
        if (end - start) * block >= needed:
            result.append(Option('free', start, end - start,
                                 f'free space, {(end - start) * block / GIB:.1f} GiB',
                                 creates_boot=boot is None))
    for part in layout['partitions']:
        if part['type'] not in REPLACEABLE:
            continue
        if part['blocks'] * block < needed:
            continue
        label = part.get('label') or part.get('name') or ''
        fs = part.get('fs') or 'no filesystem'
        result.append(Option('replace', part['start'], part['blocks'],
                             f'replace partition {part["number"]} ({fs}{", " + label if label else ""}, '
                             f'{part["blocks"] * block / GIB:.1f} GiB) - erases it',
                             partition=part, creates_boot=boot is None))
    return sorted(result, key=lambda o: o.start)


def plan(layout, option: Option, image: Dict, entry: Dict, layer: Dict, firmware: Optional[Dict] = None,
         account: Optional[Dict] = None) -> Dict:
    """The plan `install_device.py run` executes for one chosen option.

    IMAGE is the distro's root image, LAYER orchard's platform layer (kernel,
    DTBs, modules) that goes into the root after it.
    """
    if layout['disk'].get('table') == 'lwvm':
        return lwvm_plan(layout, option, image, entry, layer, firmware, account)
    block = layout['disk']['sector']
    align = MIB // block
    boot = boot_partition(layout)
    start, end = option.start, option.start + option.blocks
    if option.kind == 'replace':
        # A replaced partition's range is reused; new partitions start aligned.
        start = _align_up(start, align)
    new_boot = None
    if boot is None:
        boot_blocks = BOOT_BYTES // block
        new_boot = {'start': start, 'blocks': boot_blocks, 'partuuid': str(uuid.uuid4()),
                    'fs_uuid': uuid.uuid4().hex[:8].upper()}
        start += boot_blocks
    root_blocks = _align_down(end, align) - start
    if root_blocks * block < MIN_ROOT_BYTES:
        raise LayoutError('Target is too small for a root partition')
    if root_blocks * block < (image['expanded_bytes'] + layer['expanded_bytes']) * 1.25:
        raise LayoutError('Target is too small for this system image')
    remove = None
    if option.partition is not None:
        remove = {key: option.partition[key] for key in ('number', 'partuuid', 'start', 'blocks')}
    if new_boot is not None:
        boot_plan = {**new_boot, 'create': True}
    else:
        boot_plan = {'create': False, 'partuuid': boot['partuuid'], 'number': boot['number']}
    return {
        'version': PLAN_VERSION,
        'disk': {'path': layout['disk']['path'], 'sector': block,
                 'table_sha256': layout['disk']['table_sha256']},
        'remove': remove,
        'boot': boot_plan,
        'root': {'start': start, 'blocks': root_blocks, 'partuuid': str(uuid.uuid4()),
                 'fs_uuid': str(uuid.uuid4()), 'fs': 'ext4', 'label': entry['id'][:16]},
        'image': image,
        'layer': layer,
        'entry': entry,
        'firmware': firmware,
        'account': account,     # the release's install.account; secrets travel separately
    }


def lwvm_options(layout):
    """An already checked Data tail, or an explicitly selected Linux replacement."""
    primary, backup = (lwvm.unb64(layout['lwvm'][name]) for name in ('primary', 'backup'))
    snapshot = lwvm.parse(primary, backup, layout['disk']['bytes'])
    result = []
    linux = [p for p in snapshot['partitions'] if p['type'] == lwvm.LINUX_TYPE]
    for part in linux:
        if not part['attributes'] and part['bytes'] >= MIN_ROOT_BYTES:
            result.append(Option('replace', part['begin'] // 512, part['bytes'] // 512,
                                 f'replace Linux partition {part["number"]}, {part["bytes"] / GIB:.1f} GiB - erases it',
                                 partition=lwvm.public_partition(part)))
    if not linux and layout.get('shrink_proof'):
        data, retained = lwvm.check_proof(snapshot, layout['shrink_proof'])
        available = data['bytes'] - retained
        if available >= MIN_ROOT_BYTES:
            result.append(Option('allocate', (data['begin'] + retained) // 512, available // 512,
                                 f'allocate the checked Data tail, {available / GIB:.1f} GiB'))
    return result


def lwvm_plan(layout, option, image, entry, layer, firmware, account):
    if option not in lwvm_options(layout):
        raise LayoutError('LwVM target differs from the checked layout')
    primary, backup = (lwvm.unb64(layout['lwvm'][name]) for name in ('primary', 'backup'))
    snapshot = lwvm.parse(primary, backup, layout['disk']['bytes'])
    if option.kind == 'allocate':
        allocation = lwvm.allocation_plan(primary, backup, layout['shrink_proof'], str(uuid.uuid4()))
    else:
        root = lwvm.partition(snapshot, option.partition['partuuid'])
        allocation = {'action': 'replace', 'before': lwvm.b64(primary), 'after': lwvm.b64(primary),
                      'before_sha256': snapshot['header_sha256'], 'after_sha256': snapshot['header_sha256'],
                      'root': lwvm.public_partition(root)}
    root = allocation['root']
    if root['bytes'] < (image['expanded_bytes'] + layer['expanded_bytes']) * 1.25:
        raise LayoutError('Target is too small for this system image')
    return {'version': 3, 'kind': 'lwvm', 'disk': layout['disk'], 'lwvm': allocation,
            'io_policy': lwvm.install_io_policy(root['bytes']),
            'root': {'partuuid': root['partuuid'], 'blocks': root['bytes'] // 512, 'fs_uuid': str(uuid.uuid4()),
                     'fs': 'ext4', 'label': entry['id'][:16]},
            'boot': {'method': 'ios-local-file'}, 'image': image, 'layer': layer, 'entry': entry,
            'firmware': firmware, 'account': account}
