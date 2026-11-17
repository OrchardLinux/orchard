"""Launch an installed system from its boot entry on the device.

The device carries the boot contract (hardware docs/boot-contract.md): an
XBOOTLDR FAT32 partition with Boot Loader Specification entries and
systemd-boot's /loader/loader.conf. m1n1 is the device's boot loader; it
can't live on the device yet, so the host puts it there for each start:

- `orchard launch`: m1n1 with `autoboot=1` appended boots the default entry
  by itself, as a PC's boot loader does at power-on. The host hands it over
  and is done; if the default can't start, m1n1 shows why and waits.
- `orchard launch --list`: m1n1 waits (like a key held at power-on); the host
  shows its entry list as a picker and starts the chosen one.
- `orchard launch --entry ID`: exactly that entry, once.

The host never reads the disk or parses loader.conf, and nothing is written.
It brings only Pongo and m1n1, which ship with Orchard (orchard.hardware.loader)
or are built from the hardware source, so any host with Orchard can launch any
installed system, from any distro.
"""
import os
import select
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .devices import choose
from .progress import Clock, Progress
from .service import fetch_chain, save_report
from .state import exclusive_operation, private_directory
from .state import DeviceState
from .transports.m1n1 import M1n1Proxy

# m1n1's appended-variable form, as `chosen.bootargs=` is (orchard m1n1 0018); zeros end the payloads.
AUTOBOOT = b'autoboot=1\n' + bytes(4)


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

    @property
    def default(self):
        return self.get('default') == 'yes'


def describe(entry):
    """One line per entry, as `bootctl list` names them: title, version, (default), the id for --entry."""
    line = entry.title + (f' ({entry.get("version")})' if entry.get('version') else '')
    line += (' (default)' if entry.default else '') + f'  {entry.id}'
    if entry.problems:
        line += ' - not bootable: ' + '; '.join(entry.problems)
    return line


def pick(entries, keys, draw):
    """The entry chosen with KEYS ('up', 'down', 'enter', 'quit'), or None.

    Starts on the default if it can boot, else on the first entry that can; entries that can't
    boot are shown but never selected. DRAW(entries, cursor) shows the list.
    """
    bootable = [n for n, entry in enumerate(entries) if not entry.problems]
    if not bootable:
        return None
    cursor = next((n for n in bootable if entries[n].default), bootable[0])
    draw(entries, cursor)
    for key in keys:
        if key == 'enter':
            return entries[cursor]
        if key == 'quit':
            return None
        if key in ('up', 'down'):
            step = bootable.index(cursor) + (-1 if key == 'up' else 1)
            cursor = bootable[max(0, min(step, len(bootable) - 1))]
            draw(entries, cursor)
    return None


def terminal_keys(fd):
    """Key names from a terminal in cbreak mode: arrows (and j/k), Enter, Esc or q."""
    while True:
        byte = os.read(fd, 1)
        if byte in (b'\r', b'\n'):
            yield 'enter'
        elif byte in (b'q', b'Q', b'\x03', b'\x04', b''):
            yield 'quit'
        elif byte in (b'k', b'K'):
            yield 'up'
        elif byte in (b'j', b'J'):
            yield 'down'
        elif byte == b'\x1b':
            # An arrow is ESC [ A (or ESC O A); ESC alone is a key of its own.
            if not select.select([fd], [], [], 0.05)[0]:
                yield 'quit'
                continue
            sequence = os.read(fd, 2)
            yield {b'[A': 'up', b'OA': 'up', b'[B': 'down', b'OB': 'down'}.get(sequence, 'other')


class Screen:
    """Draws the picker in place on a terminal."""

    def __init__(self, output):
        self.output, self.lines = output, 0

    def __call__(self, entries, cursor):
        if self.lines:
            self.output.write(f'\033[{self.lines}A')
        for n, entry in enumerate(entries):
            text = describe(entry)
            if entry.problems:
                text = f'  \033[2m{text}\033[0m'
            elif n == cursor:
                text = f'\033[7m> {text}\033[0m'
            else:
                text = '  ' + text
            self.output.write('\r\033[2K' + text + '\n')
        self.output.write('\r\033[2K\033[2m↑/↓ choose, Enter start, Esc leave m1n1 waiting\033[0m\n')
        self.output.flush()
        self.lines = len(entries) + 1


def interactive_pick(entries, stdin=None, output=None):
    """The picker on this terminal; None if there is none (the list is printed instead) or Esc."""
    import termios
    import tty
    stdin, output = stdin or sys.stdin, output or sys.stdout
    fd = stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        output.write('\033[?25l')
        return pick(entries, terminal_keys(fd), Screen(output))
    except KeyboardInterrupt:
        return None
    finally:
        output.write('\033[?25h')
        output.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def is_terminal():
    return sys.stdin.isatty() and sys.stdout.isatty()


def loader_files(profile, progress):
    """Pongo and the storage-capable m1n1: shipped with Orchard, else built from the hardware source."""
    from . import hardware
    files = hardware.loader(profile)
    if files is None:
        from .build import prepare_loader
        progress.phase('Preparing the bootloader from the hardware source')
        files = prepare_loader(device=profile)
    return files


def waiting_port():
    """The proxy port of an m1n1 that already waits (after --list, or a default that failed), if any."""
    from .apple import usb
    ports = usb.m1n1_ports()
    if len(ports) > 1:
        raise ValueError('More than one m1n1 device is connected')
    return ports[0] if ports else None


def prepare(progress, clock):
    """The one device in DFU (or Pongo), with Pongo fetched: (adapter, device, loader files)."""
    adapter, device = choose()
    if device['mode'] not in ('dfu', 'pongo'):
        raise ValueError(f'Put the {adapter.NAME} in DFU mode to launch an installed system')
    # A computer that never booted this device has nothing yet, so show that as a download.
    fetch_chain(adapter.boot_inputs() if device['mode'] == 'dfu' else [], progress, clock)
    return adapter, device, loader_files(adapter.PROFILE, progress)


def autoboot(progress, clock):
    """m1n1 starts the default entry by itself, like pressing power; the host hands it over and is done."""
    adapter, device, loader = prepare(progress, clock)
    report = {'profile': adapter.PROFILE, 'ecid': device['ecid'], 'operation': 'launch', 'entry': 'default',
              'started': False}
    with tempfile.TemporaryDirectory(prefix='launch-') as directory:
        image = Path(directory) / 'm1n1-autoboot.bin'
        image.write_bytes(Path(loader['m1n1']).read_bytes() + AUTOBOOT)
        # One step for the user: no transfer progress, only what to do if the device stalls.
        adapter.start_loader(device, {**loader, 'm1n1': image}, progress, quiet=True, proxy=False)
    report.update(started=True, timing=clock.result())
    save_report(private_directory(DeviceState(adapter.PROFILE, device['ecid']).root) / 'last-launch.json', report)
    progress.phase('Started.')
    return report


def proxy_port(progress, clock):
    """m1n1 waiting in its proxy: the one already waiting, else freshly started without autoboot."""
    port = waiting_port()
    if port:
        return port
    adapter, device, loader = prepare(progress, clock)
    return adapter.start_loader(device, loader, progress, quiet=True)


def show(entries, progress, chooser):
    """The picker on a terminal, else the list; returns the chosen entry or None."""
    with progress.lock:
        progress.end_line()
    if chooser is None and is_terminal():
        chooser = interactive_pick
    if chooser is not None and any(not e.problems for e in entries):
        return chooser(entries)
    print('\n'.join(('* ' if e.default else '  ') + describe(e) for e in entries), file=progress.output, flush=True)
    return None


@exclusive_operation
def launch(*, entry=None, list_entries=False, progress=None, chooser=None):
    """Start the default system (no arguments), the chosen one (--list) or exactly ENTRY (--entry).

    CHOOSER(entries) replaces the terminal picker (tests).
    """
    clock, progress = Clock(), progress or Progress()
    if entry is None and not list_entries:
        return autoboot(progress, clock)
    port = proxy_port(progress, clock)
    report = {'operation': 'launch', 'entry': entry, 'started': False}
    # m1n1 keeps storage up after a failure (orchard m1n1 0017), so it stays ready for the next command.
    with M1n1Proxy(port) as proxy:
        proxy.storage_init()
        if entry is None:
            entries = [Entry(i, keys, problems) for i, keys, problems in proxy.boot_entries()]
            if not entries:
                raise ValueError('No boot entries on this device; install a system first')
            chosen = show(entries, progress, chooser)
            if chosen is None:
                if not any(not e.problems for e in entries):
                    progress.phase('No entry can start.')
                progress.phase('m1n1 waits; start an entry with: orchard launch --entry ID')
                return report
            entry = chosen.id
            report['entry'] = entry
        # m1n1 shuts storage down itself; after a failure the device stays in its proxy.
        progress.phase('Starting ' + entry)
        proxy.boot_start(entry)
    report.update(started=True, timing=clock.result())
    progress.phase(f'{entry} is starting.')
    return report
