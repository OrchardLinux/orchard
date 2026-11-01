#!/usr/bin/env python3
"""Kernel platforms: descriptor, ordered patches, and device -> platform/DTB.

  series.py PLATFORM_OR_DEVICE            print the ordered patch files
  series.py --resolve PLATFORM_OR_DEVICE  print "platform dtb" (dtb is "-" for a platform)
  series.py --platform PLATFORM_OR_DEVICE print the platform descriptor's kernel table as JSON

A platform is described by platforms/<name>.toml: its architecture, image,
DTB directory, upstream base, patch series and config.
"""
import json
from pathlib import Path
import re
import sys
import tomllib

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
DEVICES = REPO / 'devices'
PLATFORMS = REPO / 'platforms'
NAME = re.compile(r'[a-z0-9][a-z0-9_-]*')
ARCHES = {'arm64', 'arm'}


def resolve(name):
    """A device names its platform and DTB in device.toml [kernel]; a platform names itself."""
    if not NAME.fullmatch(name):
        raise ValueError('Invalid device or platform name')
    descriptor = DEVICES / name / 'device.toml'
    if descriptor.is_file():
        section = re.search(r'^\[kernel\]\n(.*?)(?=^\[|\Z)', descriptor.read_text(), re.M | re.S)
        values = dict(re.findall(r'^(platform|dtb)\s*=\s*"([a-z0-9][a-z0-9_.-]*)"', section[1], re.M)) if section else {}
        if set(values) != {'platform', 'dtb'}:
            raise ValueError(f'{descriptor}: [kernel] needs platform and dtb')
        return values['platform'], values['dtb']
    if (PLATFORMS / f'{name}.toml').is_file():
        return name, None
    raise ValueError(f'Unknown device or platform: {name}')


def _inside(relative, what, descriptor):
    """A repository path from the descriptor, refused if it leaves the repository."""
    if not isinstance(relative, str) or not relative or relative.startswith('/'):
        raise ValueError(f'{descriptor}: invalid {what}')
    path = (REPO / relative).resolve()
    if REPO.resolve() not in path.parents:
        raise ValueError(f'{descriptor}: {what} leaves the repository')
    return path


def platform(name):
    """The validated [kernel] table of a platform's descriptor, paths relative to the repository."""
    platform_name = resolve(name)[0]
    descriptor = PLATFORMS / f'{platform_name}.toml'
    if not descriptor.is_file():
        raise ValueError(f'Missing platform descriptor: {descriptor}')
    value = tomllib.loads(descriptor.read_text())
    if value.get('schema') != 1 or value.get('kind') != 'hardware.platform' or value.get('name') != platform_name:
        raise ValueError(f'{descriptor}: expected schema 1, kind "hardware.platform", name "{platform_name}"')
    kernel = value.get('kernel', {})
    if kernel.get('arch') not in ARCHES:
        raise ValueError(f'{descriptor}: kernel.arch must be one of {sorted(ARCHES)}')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', str(kernel.get('image', ''))):
        raise ValueError(f'{descriptor}: invalid kernel.image')
    for key in ('dtbs', 'upstream', 'series', 'config'):
        _inside(kernel.get(key), f'kernel.{key}', descriptor)
    for key in ('upstream', 'series', 'config'):
        if not (REPO / kernel[key]).is_file():
            raise ValueError(f'{descriptor}: kernel.{key} {kernel[key]} does not exist')
    return {'platform': platform_name, **{key: kernel[key] for key in
            ('arch', 'image', 'dtbs', 'upstream', 'series', 'config')}}


def patches(name):
    series = REPO / platform(name)['series']
    result = []
    for line in series.read_text().splitlines():
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        layer = (ROOT / line).resolve()
        if ROOT not in layer.parents or not layer.is_dir():
            raise ValueError(f'Invalid patch layer in {series}: {line}')
        result += sorted(layer.rglob('*.patch'))
    return result


if __name__ == '__main__':
    try:
        if sys.argv[1] == '--resolve':
            platform_name, dtb = resolve(sys.argv[2])
            print(platform_name, dtb or '-')
        elif sys.argv[1] == '--platform':
            print(json.dumps(platform(sys.argv[2])))
        else:
            for patch in patches(sys.argv[1]):
                print(patch)
    except ValueError as error:
        sys.exit(str(error))
