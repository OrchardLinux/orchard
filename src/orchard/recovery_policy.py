"""Descriptor-selected recovery identity and early runtime policy."""
from pathlib import Path
import re
import tomllib


def settings(board, hardware=None):
    from .hardware import ROOT, device
    from .kernel import descriptor
    from .recovery_identity import validate_policy
    hardware = Path(hardware or ROOT)
    selected = device(board, hardware)
    platform = descriptor(hardware, board)['platform']
    table = tomllib.loads((hardware / 'platforms' / (platform + '.toml')).read_text()).get('recovery')
    if table is None:
        return None
    expected = {'identity_source', 'usb_network', 'usb_modules', 'manual_modules', 'storage_activation'}
    if (not isinstance(table, dict) or set(table) != expected or table['identity_source'] != 'host-usb' or
            table['usb_network'] not in ('ecm', 'ncm') or table['storage_activation'] not in ('manual', 'automatic')):
        raise ValueError('Invalid platform recovery policy')
    modules = table['usb_modules']
    for key in ('usb_modules', 'manual_modules'):
        names = table[key]
        if (not isinstance(names, list) or any(not isinstance(name, str) or not re.fullmatch('[a-z0-9_-]+', name) for name in names) or
                len(set(names)) != len(names)):
            raise ValueError('Recovery modules must be distinct module names')
    normalized = lambda names: {name.replace('-', '_') for name in names}
    if normalized(table['usb_modules']) & normalized(table['manual_modules']):
        raise ValueError('A manual module cannot be loaded during USB setup')
    identity = validate_policy({'schema': 1, 'source': 'host-usb', 'profile': selected['device']['profile'],
        'compatibles': selected['device']['dt_compatible'], 'chip': selected['match']['dfu']['chip'],
        'board': selected['match']['dfu']['board']})
    runtime = {'schema': 1, 'profile': identity['profile'], 'usb_network': table['usb_network'],
               'usb_modules': modules, 'manual_modules': table['manual_modules'], 'storage_activation': table['storage_activation']}
    return {'identity': identity, 'runtime': runtime}
