"""Launch an installed system from its boot entry on the device.

The device carries the boot contract (hardware docs/boot-contract.md): an
XBOOTLDR FAT32 partition with Boot Loader Specification entries. The host
boots a storage-capable m1n1, which reads and checks the entries itself; the
user chooses and m1n1 loads and boots the entry. The host never reads the
disk, and nothing is written.

The host brings only Pongo and that m1n1, which ship with Orchard
(orchard.hardware.loader) or are built from the hardware source, so any
host with Orchard can launch any installed system, from any distro.
"""
from dataclasses import dataclass, field

from .devices import choose
from .progress import Clock, Progress
from .service import ask, save_report
from .state import exclusive_operation, private_directory
from .state import DeviceState
from .transports.m1n1 import M1n1Error, M1n1Proxy


@dataclass
class Entry:
    """A boot entry as m1n1 lists it: its BLS keys, its root's PARTUUID and why it can't boot."""
    id: str
    keys: dict
    problems: list = field(default_factory=list)

    def get(self, key):
        values = self.keys.get(key)
        return values[0] if values else None

    @property
    def title(self):
        return self.get('title') or self.id

    @property
    def root(self):
        return self.get('root')


def describe(number, entry, entries):
    # The title alone, unless another entry has the same one.
    same = [e for e in entries if e.title == entry.title]
    line = f'  {number}. {entry.title}' + (f' ({entry.get("version")})' if len(same) > 1 else '')
    if entry.problems:
        line += ' - not bootable: ' + '; '.join(entry.problems)
    return line


def select(entries, wanted, clock, input_fn):
    if wanted is not None:
        matches = [e for e in entries if e.id == wanted]
        if not matches:
            raise ValueError(f'No boot entry {wanted!r} on this device')
        return matches[0]
    bootable = [e for e in entries if not e.problems]
    if not bootable:
        raise ValueError('No bootable entry on this device; nothing was started')
    if len(entries) == 1:
        return entries[0]
    for number, entry in enumerate(entries, 1):
        print(describe(number, entry, entries))
    default = entries.index(bootable[0]) + 1
    answer = ask(clock, f'Boot which system? [{default}] ', input_fn) or str(default)
    if not answer.isdecimal() or not 1 <= int(answer) <= len(entries):
        raise ValueError('No boot entry selected')
    return entries[int(answer) - 1]


def loader_files(profile, progress):
    """Pongo and the storage-capable m1n1: shipped with Orchard, else built from the hardware source."""
    from . import hardware
    files = hardware.loader(profile)
    if files is None:
        from .build import prepare_loader
        progress.phase('Preparing the bootloader from the hardware source')
        files = prepare_loader(device=profile)
    return files


@exclusive_operation
def launch(*, entry=None, input_fn=input, progress=None):
    clock, progress = Clock(), progress or Progress()
    adapter, device = choose()
    if device['mode'] not in ('dfu', 'pongo'):
        raise ValueError(f'Put the {adapter.NAME} in DFU mode to launch an installed system')
    # One step for the user: no transfer progress, only what to do if the device stalls.
    port = adapter.start_loader(device, loader_files(adapter.PROFILE, progress), progress, quiet=True)
    report = {'profile': adapter.PROFILE, 'ecid': device['ecid'], 'operation': 'launch',
              'entry': None, 'started': False}
    path = private_directory(DeviceState(adapter.PROFILE, device['ecid']).root) / 'last-launch.json'
    with M1n1Proxy(port) as proxy:
        proxy.storage_init()
        try:
            entries = [Entry(i, keys, problems) for i, keys, problems in proxy.boot_entries(adapter.DEVICETREE)]
            if not entries:
                raise ValueError('No boot entries on this device; install a system first')
            chosen = select(entries, entry, clock, input_fn)
            if chosen.problems:
                raise ValueError(f'{chosen.title} is not bootable: ' + '; '.join(chosen.problems))
        except (ValueError, M1n1Error):
            proxy.storage_shutdown()
            raise
        report['entry'] = {'id': chosen.id, 'title': chosen.title, 'version': chosen.get('version'),
                           'root': chosen.root}
        # m1n1 shuts storage down itself; after a failure the device stays in its proxy.
        progress.phase('Starting ' + chosen.title)
        proxy.boot_start(chosen.id, adapter.DEVICETREE)
    report.update(started=True, timing=clock.result())
    save_report(path, report)
    progress.phase(f'{chosen.title} is starting.')
    return report
