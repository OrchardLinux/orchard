"""Minimal hardware checkout in the platform layout, for kernel cache tests."""
from pathlib import Path
import shutil

SERIES = '''#!/usr/bin/env python3
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent


def resolve(name):
    descriptor = ROOT.parent / 'devices' / name / 'device.toml'
    if descriptor.is_file():
        values = dict(re.findall(r'^(platform|dtb)\\s*=\\s*"([^"]+)"', descriptor.read_text(), re.M))
        return values['platform'], values['dtb']
    return name, None


if sys.argv[1] == '--resolve':
    platform, dtb = resolve(sys.argv[2])
    print(platform, dtb or '-')
elif sys.argv[1] == '--platform':
    platform = resolve(sys.argv[2])[0]
    print(json.dumps({'platform': platform, 'arch': 'arm64', 'image': 'Image.gz', 'loader': 'm1n1',
                      'dtbs': 'arch/arm64/boot/dts/apple', 'upstream': 'kernel/upstream.lock',
                      'config': 'kernel/configs/' + platform + '/config'}))
'''


def write(hardware, device='j71b', platform='apple', dtb='t8010-j71b'):
    """The platform's descriptor script, config and device descriptor."""
    hardware = Path(hardware)
    files = {
        'kernel/series.py': SERIES,
        'kernel/configs/' + platform + '/config': 'config',
        'platforms/' + platform + '.toml': f'schema = 1\nkind = "hardware.platform"\nname = "{platform}"\n',
        'devices/' + device + '/device.toml': f'[kernel]\nplatform = "{platform}"\ndtb = "{dtb}"\n',
    }
    for name, text in files.items():
        path = hardware / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
