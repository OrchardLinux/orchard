"""User-local boot tools and Linux USB access. No device operations."""
import grp
import os
from dataclasses import dataclass
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, Optional

from . import tools
from .apple.linux_usb import APPLE, M1N1, MODES, SERIAL_MODES
from .progress import Progress
from .transports.pongo import GLU, library_path

USB_RULES = Path('/etc/udev/rules.d/70-orchard.rules')


def usb_rules(group):
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', group):
        raise ValueError('Unsupported USB access group name')
    lines = ['# Orchard: every stage of the device and m1n1, with their serial consoles.']
    access = f'MODE="0660", GROUP="{group}", TAG+="uaccess"'
    devices = [(APPLE, pid, mode in SERIAL_MODES) for pid, mode in MODES.items()] + [(*M1N1, True)]
    for vendor, product, serial in devices:
        lines.append(f'SUBSYSTEM=="usb", ATTR{{idVendor}}=="{vendor:04x}", ATTR{{idProduct}}=="{product:04x}", {access}')
        if serial:
            lines.append(f'SUBSYSTEM=="tty", ATTRS{{idVendor}}=="{vendor:04x}", '
                         f'ATTRS{{idProduct}}=="{product:04x}", {access}')
    # Let Orchard reset the port of a device in DFU when it does not re-enumerate after checkm8.
    lines.append('ACTION=="add", SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="05ac", '
                 'ATTR{idProduct}=="1227", RUN+="/bin/sh -c \'chgrp '
                 f'{group} /sys%p/port/disable && chmod g+w /sys%p/port/disable\'"')
    return '\n'.join(lines) + '\n'


# Host requirements: one check and one way to acquire each, shared by the
# installer's boot tools (`orchard setup-host`) and development setup
# (`orchard setup`, `orchard doctor`), so the two cannot drift apart.
BOOT, BUILD = 'boot', 'build'
MAINLINE_URL = 'https://github.com/OrchardLinux/orchard.git'
# The package that provides each tool, per package manager.
PACKAGES = {
    'libusb': {'glu': 'libusb', 'brew': 'libusb', 'apt': 'libusb-1.0-0', 'dnf': 'libusb1', 'pacman': 'libusb'},
    'podman': {'glu': 'podman', 'brew': 'podman', 'apt': 'podman', 'dnf': 'podman', 'pacman': 'podman'},
    'git': {'glu': 'git', 'brew': 'git', 'apt': 'git', 'dnf': 'git', 'pacman': 'git'},
}


@dataclass
class Step:
    """One requirement: ready, or what applying it does, or (apply None) what you must do."""
    name: str
    needed_for: str
    ready: bool
    detail: str
    apply: Optional[Callable[[], None]] = None


def host():
    system = platform.system()
    arch = {'x86_64': 'x86_64', 'amd64': 'x86_64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine())
    if system not in ('Linux', 'Darwin') or not arch:
        raise ValueError('Supported hosts: Linux/macOS on x86_64 or ARM64')
    if os.geteuid() == 0:
        raise ValueError('Run Orchard setup as your normal user, not root')
    return system, arch


def package_manager():
    """(name, argv prefix) of the package manager to use, or None.

    macOS: glu, then Homebrew; neither is installed automatically. Linux: apt,
    dnf or pacman, with sudo.
    """
    if platform.system() == 'Darwin':
        glu = shutil.which('glu') or (GLU if Path(GLU).is_file() else None)
        if glu:
            return 'glu', [glu, 'install', '--yes']
        brew = shutil.which('brew')
        return ('brew', [brew, 'install']) if brew else None
    for name, argv in (('apt', ['apt-get', 'install', '-y']), ('dnf', ['dnf', 'install', '-y']),
                       ('pacman', ['pacman', '-S', '--needed', '--noconfirm'])):
        if shutil.which(argv[0]):
            return name, ['sudo', *argv]
    return None


def _install(tool):
    """A Step's apply and description for installing TOOL's package, or (None, why not)."""
    manager = package_manager()
    if manager is None:
        if platform.system() == 'Darwin':
            return None, f'install glu or Homebrew (https://brew.sh) first, then rerun; it installs {tool}'
        return None, f'install {tool} with your package manager, then rerun'
    name, argv = manager
    package = PACKAGES[tool][name]
    return (lambda: subprocess.run([*argv, package], check=True)), f'install with {name}: {package}'


def _palera1n_step():
    existing = tools.find('palera1n')
    if existing:
        return Step('palera1n', BOOT, True, existing)
    version = tools.TOOLS['palera1n']['version']
    return Step('palera1n', BOOT, False, f'download palera1n {version} into the Orchard cache',
                lambda: tools.ensure_tool('palera1n'))


def _libusb_step():
    try:
        library_path()
        return Step('libusb', BOOT, True, 'found')
    except Exception:  # library_path raises PongoError when no candidate loads
        apply, detail = _install('libusb')
        return Step('libusb', BOOT, False, detail, apply)


def _usb_rules_step():
    rules = usb_rules(grp.getgrgid(os.getgid()).gr_name)
    if USB_RULES.exists() and USB_RULES.read_text() == rules:
        return Step('USB permissions', BOOT, True, str(USB_RULES))
    if not shutil.which('udevadm') or not shutil.which('sudo'):
        return Step('USB permissions', BOOT, False, 'Linux USB access needs udevadm and sudo')
    def apply():
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '70-orchard.rules'
            path.write_text(rules)
            subprocess.run(['sudo', 'install', '-m', '644', str(path), str(USB_RULES)], check=True)
        subprocess.run(['sudo', 'udevadm', 'control', '--reload-rules'], check=True)
    return Step('USB permissions', BOOT, False, f'install {USB_RULES} with sudo; reconnect the device afterwards', apply)


def _tool_step(tool, needed_for):
    existing = shutil.which(tool)
    if existing:
        return Step(tool, needed_for, True, existing)
    apply, detail = _install(tool)
    return Step(tool, needed_for, False, detail, apply)


def _podman_machine_step():
    """macOS builds run in the Podman machine; it must exist and run."""
    inspect = subprocess.run(['podman', 'machine', 'inspect', '--format', '{{.State}}'],
                             capture_output=True, text=True)
    if inspect.returncode == 0 and inspect.stdout.strip() == 'running':
        return Step('Podman machine', BUILD, True, 'running')
    def apply():
        if inspect.returncode != 0:
            subprocess.run(['podman', 'machine', 'init'], check=True)
        subprocess.run(['podman', 'machine', 'start'], check=True)
    action = 'create and start' if inspect.returncode != 0 else 'start'
    return Step('Podman machine', BUILD, False, f'{action} the default Podman machine', apply)


def _launcher_step():
    """`orchard` on PATH: a copy of this checkout's tools/launcher, which runs the newest nightly.

    `orchard update` replaces it with the newest nightly's. A checkout's own
    bin/orchard runs that checkout; it is called by its path, never put on PATH.
    """
    from .hardware import ROOT
    source = ROOT / 'tools/launcher'
    launcher = Path.home() / '.local/bin/orchard'
    if launcher.is_file() and not launcher.is_symlink() and launcher.read_bytes() == source.read_bytes():
        return Step('orchard command', BUILD, True, f'{launcher} runs the newest nightly')
    if launcher.exists() and not launcher.is_symlink() and b'tools/launcher' not in launcher.read_bytes() \
            and b'orchard on PATH' not in launcher.read_bytes():
        return Step('orchard command', BUILD, False, f'{launcher} is another program; move it away, then rerun')
    def apply():
        launcher.parent.mkdir(parents=True, exist_ok=True)
        temporary = launcher.with_name('.orchard-launcher')
        temporary.unlink(missing_ok=True)
        shutil.copyfile(source, temporary)
        temporary.chmod(0o755)
        os.replace(temporary, launcher)
    replacing = f' (replacing the link to {os.readlink(launcher)})' if launcher.is_symlink() else ''
    return Step('orchard command', BUILD, False, f'install {launcher}, which runs the newest nightly{replacing}', apply)


def _path_step():
    from . import shell_path
    if shell_path.on_path() or not any(shell_path.MARK not in (p.read_text() if p.exists() else '')
                                       for p in shell_path.startup_files()):
        return Step('PATH', BUILD, True, f'{shell_path.binary()} is on PATH')
    return Step('PATH', BUILD, False, f'put {shell_path.binary()} on PATH for new terminals (shell startup files)',
                lambda: shell_path.ensure())


def steps(needed_for=(BOOT, BUILD)):
    """The host's requirements for NEEDED_FOR, in the order setup applies them."""
    system, arch = host()
    result = [Step('OpenSSH', BOOT, all(shutil.which(c) for c in ('ssh', 'ssh-keygen')),
                   'found' if all(shutil.which(c) for c in ('ssh', 'ssh-keygen')) else 'install OpenSSH, then rerun'),
              _libusb_step(), _palera1n_step()]
    if system == 'Linux':
        result.append(_usb_rules_step())
    if BUILD in needed_for:
        result += [_tool_step('git', BUILD), _tool_step('podman', BUILD)]
        if system == 'Darwin' and shutil.which('podman'):
            result.append(_podman_machine_step())
        result += [_launcher_step(), _path_step()]
    return [step for step in result if step.needed_for in needed_for]


def describe(found):
    lines = []
    for step in found:
        mark = 'ready' if step.ready else ('todo' if step.apply else 'yours')
        lines.append(f'  [{mark:5}] {step.name}: {step.detail}')
    return '\n'.join(lines)


def doctor(needed_for=(BOOT, BUILD), output=print):
    """Show the host's state without changing anything; 0 if everything is ready."""
    found = steps(needed_for)
    output(describe(found))
    return 0 if all(step.ready for step in found) else 1


def setup_interactive(needed_for=(BOOT, BUILD), *, yes=False, output=print, ask=input, interactive=None):
    """Show what setup will do, ask, then do it. Safe to rerun: done steps are skipped."""
    found = steps(needed_for)
    output('Orchard setup:\n' + describe(found))
    todo = [step for step in found if not step.ready and step.apply]
    yours = [step for step in found if not step.ready and not step.apply]
    if not todo:
        output('Ready.' if not yours else 'Nothing Orchard can do; the steps marked "yours" remain.')
        return 0 if not yours else 1
    if not yes:
        interactive = sys.stdin.isatty() if interactive is None else interactive
        if not interactive:
            raise ValueError('Run setup in a terminal, or pass --yes to apply these steps')
        if ask(f'Do the {len(todo)} step{"s" if len(todo) > 1 else ""} marked "todo"? [Y/n] ').strip().lower() not in ('', 'y', 'yes'):
            output('Nothing changed.')
            return 1
    for step in todo:
        output(f'{step.name}: {step.detail}')
        try:
            step.apply()
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            raise ValueError(f'{step.name} failed: {error}\nFix it, then rerun setup; finished steps are skipped.') from error
    remaining = [step for step in steps(needed_for) if not step.ready]
    output(describe(remaining) if remaining else 'Ready.')
    return 0 if not remaining else 1


def setup(progress=None):
    """The installer's boot tools: palera1n, Linux USB permissions; libusb is checked.

    Non-interactive, unlike `orchard setup`: it never runs a package manager.
    """
    progress = progress or Progress()
    host()
    for command in ('ssh', 'ssh-keygen'):
        if not shutil.which(command):
            raise ValueError('Install OpenSSH first; missing ' + command)
    libusb = _libusb_step()
    if not libusb.ready:
        raise ValueError('libusb-1.0 is unavailable: ' + libusb.detail)
    if platform.system() == 'Linux' and (not shutil.which('udevadm') or not shutil.which('sudo')):
        raise ValueError('Linux host setup requires udevadm and sudo')
    found = {}
    palera = _palera1n_step()
    if not palera.ready:
        palera.apply()
    found['palera1n'] = _palera1n_step().detail
    if platform.system() == 'Linux':
        rules = _usb_rules_step()
        if not rules.ready:
            progress.phase('Installing Linux USB permissions (sudo); reconnect the device afterward')
            rules.apply()
    return {'tools': found, 'device_modified': False}
