"""A distribution checkout as an orchard project: its orchard.toml.

`orchard.toml` with a [release] table at the root of a distribution's
checkout makes it one (orchard.project; the older distro.toml is still read):

    name = "omarchy-a"          # the distro of its index.toml
    [release]
    build = "tools/build"       # its build entry, relative to the checkout
    selects-platform = true     # optional: the entry takes --platform

`orchard build` (the orchard command, OrchardLinux/tools) runs the entry in
that checkout; this module reads and checks the file, for the distributions'
own tests.
"""
import os
from pathlib import Path
import re

_NAME = re.compile(r'[a-z0-9][a-z0-9._-]*')


def find(directory):
    """The distribution checkout DIRECTORY is in (the nearest orchard.toml with [release], or the older
    distro.toml, up to its repository's root), or None."""
    from . import project
    return project.find_release(directory)


def settings(root):
    """ROOT's orchard.toml [release] (or the older distro.toml), checked: {'name': str, 'build': Path}."""
    from . import project
    kind, name, value, path = project.read(Path(root))
    if kind != 'release':
        raise ValueError(f'{root} has no orchard.toml with [release]')
    value = {**value, 'name': name}
    unknown = set(value) - {'name', 'build', 'selects-platform'}
    if unknown:
        raise ValueError(f'{path}: unknown settings {", ".join(sorted(unknown))}; '
                         'expected name, build, selects-platform')
    selects = value.get('selects-platform', False)
    if not isinstance(selects, bool):
        raise ValueError(f'{path}: selects-platform must be true or false')
    name, build = value.get('name'), value.get('build')
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError(f'{path}: name must be the distro name of its index, e.g. "omarchy-a"')
    if (not isinstance(build, str) or not build or build.startswith('/') or '\\' in build
            or any(part in ('', '.', '..') for part in build.split('/'))):
        raise ValueError(f'{path}: build must be a path inside the checkout, e.g. "tools/build"')
    entry = Path(root) / build
    if entry.is_symlink() or not entry.is_file():
        raise ValueError(f'{path}: the build entry {build} is not a file')
    if entry.suffix != '.py' and not os.access(entry, os.X_OK):
        raise ValueError(f'{path}: the build entry {build} is not executable')
    return {'name': name, 'build': entry, 'selects_platform': selects}
