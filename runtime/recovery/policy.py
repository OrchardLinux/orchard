"""Read the descriptor-selected early recovery policy, with legacy defaults."""
from pathlib import Path
import re
import stat
import os
import sys

from recovery_identity import object_json, validate_policy

CONFIG = Path('/etc/orchard/recovery.json')
IDENTITY = Path('/etc/orchard/recovery-identity.json')


def settings():
    if not CONFIG.exists() and not CONFIG.is_symlink():
        return {'usb_network': 'ncm', 'usb_modules': [], 'storage_activation': 'automatic'}
    info = CONFIG.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_size > 2048:
        raise ValueError('Recovery policy is not protected')
    value = object_json(CONFIG.read_bytes())
    fields = {'schema', 'profile', 'usb_network', 'usb_modules', 'manual_modules', 'storage_activation'}
    if (set(value) != fields or type(value['schema']) is not int or value['schema'] != 1 or
            value['usb_network'] not in ('ncm', 'ecm') or value['storage_activation'] not in ('manual', 'automatic')):
        raise ValueError('Invalid recovery runtime policy')
    identity = validate_policy(object_json(IDENTITY.read_bytes()))
    if value['profile'] != identity['profile']:
        raise ValueError('Recovery policy profiles differ')
    for key in ('usb_modules', 'manual_modules'):
        names = value[key]
        if (not isinstance(names, list) or any(not isinstance(name, str) or not re.fullmatch('[a-z0-9_-]+', name) for name in names) or
                len(set(names)) != len(names)):
            raise ValueError('Invalid recovery module policy')
    normalized = lambda names: {name.replace('-', '_') for name in names}
    if normalized(value['usb_modules']) & normalized(value['manual_modules']):
        raise ValueError('A manual module cannot be loaded during USB setup')
    return value


def loadable_modules(value, directory=None):
    names = value['usb_modules']
    if not names:
        return []
    directory = Path(directory) if directory is not None else Path('/lib/modules') / os.uname().release
    builtins = {Path(line).stem.replace('-', '_') for line in
                (directory / 'modules.builtin').read_text().splitlines() if line}
    return [name for name in names if name.replace('-', '_') not in builtins]


if __name__ == '__main__':
    value = settings()
    selected = {'network': value['usb_network'], 'modules': ' '.join(loadable_modules(value)),
                'storage': value['storage_activation']}
    if len(sys.argv) != 2 or sys.argv[1] not in selected:
        raise SystemExit('Expected network, modules or storage')
    print(selected[sys.argv[1]])
