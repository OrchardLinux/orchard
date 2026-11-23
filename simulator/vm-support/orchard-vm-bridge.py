#!/usr/bin/python3
"""VM-only hardware bridge: report backlight state; accept shutdown, the desktop switch and the device's orientation only."""
import errno
import os
import select
import subprocess
import threading
import time

BASE = '/dev/virtio-ports/'
BRIGHTNESS = '/sys/class/backlight/20e200080.backlight/brightness'
ACCEL = '/sys/module/orchard_vm_accel/parameters/'
# The gravity the VM accelerometer reports (mg) when the window holds the
# device this way up; iio-sensor-proxy names its orientations the same way.
GRAVITY = {
    b'normal': (0, -1000, 0),
    b'bottom-up': (0, 1000, 0),
    b'left-up': (1000, 0, 0),
    b'right-up': (-1000, 0, 0),
}
MAXIMUM = '/sys/class/backlight/20e200080.backlight/max_brightness'

def open_port(name):
    while True:
        try:
            return os.open(BASE + name, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError:
            time.sleep(0.5)

def report_brightness():
    fd = open_port('org.orchard.display')
    while True:
        try:
            with open(BRIGHTNESS) as f:
                value = int(f.read())
            with open(MAXIMUM) as f:
                maximum = int(f.read())
            os.write(fd, f'{value} {maximum}\n'.encode('ascii'))
        except (OSError, ValueError):
            pass
        time.sleep(0.25)

def commands():
    fd = open_port('org.orchard.control')
    pending = b''
    while True:
        ready, _, _ = select.select([fd], [], [], 1)
        if not ready:
            continue
        try:
            chunk = os.read(fd, 256)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EINTR):
                continue
            time.sleep(0.2)
            continue
        if not chunk:
            pending = b''
            time.sleep(0.2)
            continue
        pending += chunk
        if len(pending) > 256:
            pending = b''
        while b'\n' in pending:
            line, pending = pending.split(b'\n', 1)
            if line == b'shutdown':
                subprocess.run(['/usr/bin/systemctl', 'poweroff'], check=False)
            elif line.startswith(b'orientation ') and line[12:] in GRAVITY:
                for axis, value in zip('xyz', GRAVITY[line[12:]]):
                    try:
                        with open(ACCEL + axis, 'w') as f:
                            f.write(str(value))
                    except OSError:
                        pass
            elif line in (b'desktop gpu', b'desktop software'):
                # The GPU desktop on software OpenGL, or the software desktop;
                # the session starts again with it (back to the lock screen).
                flag = '/etc/omarchy-a/simulator-gpu'
                if line == b'desktop gpu':
                    os.makedirs(os.path.dirname(flag), exist_ok=True)
                    open(flag, 'a').close()
                elif os.path.exists(flag):
                    os.unlink(flag)
                restart_desktop()


def restart_desktop():
    """A new desktop session, as at boot.

    greetd starts the desktop (initial_session) once per boot and notes that
    in its runfile; restarted alone it would show its fallback instead. So
    the old session ends, the note goes, and greetd starts the desktop
    again, locked as at boot.
    """
    subprocess.run(['/usr/bin/systemctl', 'stop', 'greetd'], check=False)
    sessions = subprocess.run(['/usr/bin/loginctl', 'list-sessions', '--no-legend'],
                              capture_output=True, text=True, check=False).stdout
    seat = [row.split()[0] for row in sessions.splitlines() if row.split()[3:4] == ['seat0']]
    # With greetd stopped its sessions are abandoned: terminate-session only
    # marks them closing (user processes outlive a session here), so their
    # processes are signalled, and killed if they are not gone in 5 s.
    for signal in ('SIGTERM', 'SIGKILL'):
        for session in seat:
            subprocess.run(['/usr/bin/loginctl', 'kill-session', session, f'--signal={signal}'],
                           capture_output=True, check=False)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and subprocess.run(['/usr/bin/pgrep', '-x', 'labwc'],
                                                                capture_output=True).returncode == 0:
            time.sleep(0.1)
    for session in seat:
        subprocess.run(['/usr/bin/loginctl', 'terminate-session', session], capture_output=True, check=False)
    try:
        os.unlink('/run/greetd.run')
    except FileNotFoundError:
        pass
    subprocess.run(['/usr/bin/systemctl', 'start', 'greetd'], check=False)


threading.Thread(target=report_brightness, daemon=True).start()
commands()
