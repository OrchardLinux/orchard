"""Hand a boot bundle to orchard-launcher, which takes the device from DFU to the bundle running.

Boot methods orchard's Python does not implement itself (a patched iBSS and iBEC on 32-bit
boards: recovery-autogo) are the launcher's. This module is the whole seam, so its shape can
change in one place:

    orchard-launcher load BUNDLE --chain CHAIN --catalog DIR

prints one JSON object per line: {"event": "step", ...} while it works,
{"event": "needs_user", "message": ...} when the person has to act (DFU, replug), and last
{"result": "started"} or {"result": "error", "reason": ..., "message": ...}.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess

NAME = 'orchard-launcher'
# Boot methods (methods.toml) that the launcher delivers.
METHODS = frozenset({'recovery-autogo'})


class LauncherError(RuntimeError):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def find():
    """The launcher binary: ORCHARD_LAUNCHER, else orchard-launcher on PATH; None if neither."""
    configured = os.environ.get('ORCHARD_LAUNCHER')
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_file() and os.access(path, os.X_OK) else None
    found = shutil.which(NAME)
    return Path(found) if found else None


def delivers(method):
    """Whether METHOD is started by the launcher rather than by orchard's own code."""
    return method in METHODS


def load(bundle, *, chain, catalog, progress):
    """Start BUNDLE on the attached device through CHAIN; returns once the launcher says it started.

    Never retried: a failed start raises with the launcher's reason, and the
    device is left as the launcher left it.
    """
    executable = find()
    if executable is None:
        raise LauncherError('missing', f'Starting this device needs {NAME}, which is not installed. '
                                       f'Install it, or point ORCHARD_LAUNCHER at it, then run the command again.')
    argv = [str(executable), 'load', str(bundle), '--chain', chain, '--catalog', str(catalog)]
    result, other = None, []
    with subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, bufsize=1) as process:
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    other.append(line.rstrip())
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get('event') == 'step':
                    progress.step(str(message.get('message') or message.get('step') or 'Starting'))
                elif message.get('event') == 'needs_user':
                    progress.say(str(message.get('message', 'The device needs you')))
                elif 'result' in message:
                    result = message
        except BaseException:
            process.kill()
            raise
        errors = process.stderr.read()
    if result is None:
        detail = (errors.strip() or '\n'.join(other)).splitlines()[-5:]
        raise LauncherError('no-result', f'{NAME} stopped without a result (exit {process.returncode})'
                                         + (': ' + ' '.join(detail) if detail else ''))
    if result['result'] != 'started':
        raise LauncherError(str(result.get('reason', 'error')), str(result.get('message') or f'{NAME} could not start the device'))
    return result
