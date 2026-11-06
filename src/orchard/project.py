"""orchard.toml: what a project builds, one file for RAM payloads and distributions.

    name = "minimal-boot"           # a RAM payload: init and rootfs/ beside this file
    [payload]
    transport = false               # optional: leave the USB session helper out
    modules = true                  # optional: stage the kernel's modules
    firmware = true                 # optional: add the booted board's firmware at run

    name = "minimal-linux"          # a distribution: its own script builds the release
    [release]
    build = "tools/build.py"        # relative to this file
    selects-platform = true         # optional: the script takes --platform
    selects-device = true           # optional: preserve the chosen board as --device

The file says what the project builds, never which board it is for; a
developer's choices (platform, device, kernel tree) are arguments or
orchard.local.toml. The files before it, payload.toml and distro.toml, are
still read, with a notice to rename them.
"""
from pathlib import Path
import sys
import tomllib

NAME = 'orchard.toml'
LEGACY_PAYLOAD = 'payload.toml'
LEGACY_RELEASE = 'distro.toml'


def _load(path):
    try:
        with Path(path).open('rb') as stream:
            return tomllib.load(stream)
    except (OSError, ValueError) as error:
        raise ValueError(f'{path}: {error}') from error


def _notice(path, section):
    print(f'orchard: {path} is the old name; rename it to {NAME}, with its settings under [{section}] '
          f'and a name = "..." line', file=sys.stderr)


def read(directory):
    """(kind, name, settings, path) of DIRECTORY's project file; kind is 'payload', 'release' or None.

    settings are the [payload] or [release] table, checked by the caller.
    """
    directory = Path(directory)
    path = directory / NAME
    if path.is_file():
        value = _load(path)
        unknown = set(value) - {'name', 'payload', 'release'}
        if unknown:
            raise ValueError(f'{path}: unknown settings {", ".join(sorted(unknown))}; expected name and '
                             '[payload] or [release]')
        kinds = [kind for kind in ('payload', 'release') if kind in value]
        if len(kinds) > 1:
            raise ValueError(f'{path}: a project is a payload or a release, not both')
        kind = kinds[0] if kinds else 'payload'
        table = value.get(kind, {})
        if not isinstance(table, dict):
            raise ValueError(f'{path}: [{kind}] must be a table')
        name = value.get('name')
        if not isinstance(name, str) or not name:
            raise ValueError(f'{path}: name = "..." names the project')
        return kind, name, table, path
    for legacy, kind in ((directory / LEGACY_RELEASE, 'release'), (directory / LEGACY_PAYLOAD, 'payload')):
        if legacy.is_file():
            _notice(legacy, kind)
            table = _load(legacy)
            name = table.pop('name', None) if kind == 'release' else directory.name
            return kind, name, table, legacy
    return None, None, {}, None


def find_release(directory):
    """The distribution checkout DIRECTORY is in: the nearest project file of kind release, up to its
    repository's root; or None."""
    directory = Path(directory).expanduser().resolve()
    for parent in (directory, *directory.parents):
        if (parent / NAME).is_file() or (parent / LEGACY_RELEASE).is_file():
            kind = read(parent)[0]
            if kind == 'release':
                return parent
            if (parent / NAME).is_file():
                return None  # the nearest project is a payload
        if (parent / '.git').exists():
            break
    return None
