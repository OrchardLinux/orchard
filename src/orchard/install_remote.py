"""Run install_device.py on the recovery over the pinned SSH connection.

The script ships with this CLI, so both always come from the same commit.
Files go to /run/orchard on the device (RAM, root-only) through ssh stdin;
secrets never appear on a command line. Device events are JSON lines.
"""
import json
from pathlib import Path
import subprocess
import threading

DIRECTORY = '/run/orchard'
SCRIPT = Path(__file__).with_name('install_device.py')
# The boot entries' one implementation (also in the platform layer); install_device.py runs it.
BOOT_ENTRIES = Path(__file__).with_name('boot-entries')


class RemoteError(RuntimeError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def put(connection, data, name, timeout=120):
    """Store bytes as /run/orchard/NAME on the device, readable by root only."""
    connection.check()
    path = f'{DIRECTORY}/{name}'
    command = f'umask 077 && mkdir -p {DIRECTORY} && cat > {path}.part && mv {path}.part {path}'
    result = subprocess.run(connection.argv + [command], input=data, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RemoteError(f'Could not copy {name} to the device: ' +
                          result.stderr.decode(errors='replace').strip()[-500:])


def read_unit_adt(connection):
    """Read the selected unit's named ADT partition over its pinned SSH session."""
    connection.check()
    command = r"""set -eu
set -- $(sed -n 's/^mtd\([0-9][0-9]*\): \([0-9a-fA-F][0-9a-fA-F]*\) [0-9a-fA-F]* "adt"$/\1 \2/p' /proc/mtd)
[ "$#" -eq 2 ]
size=$((0x$2))
[ "$size" -gt 0 ] && [ "$size" -le 16777216 ] || exit 1
device=/dev/mtd${1}ro
[ -c "$device" ]
head -c "$size" "$device"
"""
    result = subprocess.run(connection.argv + [command], capture_output=True, timeout=30)
    if result.returncode:
        raise RemoteError("Could not read the selected unit's runtime ADT")
    return result.stdout


def put_scripts(connection):
    """install_device.py and the boot-entries script it runs, from this CLI."""
    put(connection, SCRIPT.read_bytes(), SCRIPT.name)
    put(connection, BOOT_ENTRIES.read_bytes(), BOOT_ENTRIES.name)


def run(connection, args, *, source=None, size=0, progress=None, timeout=3600):
    """Run the device script with ARGS; return its events, raising on an `error` event."""
    connection.check()
    argv = connection.argv + ['python3 ' + ' '.join([f'{DIRECTORY}/{SCRIPT.name}', *args])]
    process = subprocess.Popen(argv, stdin=subprocess.PIPE if source else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    events, stderr = [], []

    def collect():
        for line in process.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            events.append(event)
            if progress is not None and event.get('event') == 'step' and event.get('state') == 'done':
                progress.step(STEPS.get(event['step'], event['step'].replace('-', ' ').capitalize()))

    readers = [threading.Thread(target=collect),
               threading.Thread(target=lambda: stderr.append(process.stderr.read()))]
    for thread in readers:
        thread.start()
    timer = threading.Timer(timeout, process.kill)
    timer.start()
    try:
        if source is not None:
            if progress is not None:
                progress.reset_transfer()
            sent = 0
            try:
                for block in iter(lambda: source.read(1 << 20), b''):
                    process.stdin.write(block)
                    sent += len(block)
                    if progress is not None:
                        progress.transfer(sent, size)
                process.stdin.close()
            except BrokenPipeError:
                pass          # the device stopped reading; its error event says why
        process.wait()
    finally:
        timer.cancel()
        for thread in readers:
            thread.join()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    errors = [e for e in events if e.get('event') == 'error']
    if errors:
        raise RemoteError(errors[-1].get('detail', 'device error'), errors[-1].get('code'))
    if process.returncode:
        text = b''.join(stderr).decode(errors='replace').strip()[-500:]
        raise RemoteError('The connection to the device ended before the step finished. '
                          f'Its log is {DIRECTORY}/install.log on the recovery. {text}'.strip())
    return events


STEPS = {
    'check': 'Disk layout matches the plan',
    'partitions': 'Partitions created',
    'format-boot': 'Boot partition formatted',
    'format-root': 'System partition formatted',
    'extract-root': 'System files written',
    'platform-layer': 'Kernel and drivers installed',
    'firmware': 'Firmware installed',
    'remove-old-entries': 'Old boot entries removed',
    'account': 'User set up',
    'boot-entry': 'Boot entry written',
    'boot-default': 'Set to start by default',
}
