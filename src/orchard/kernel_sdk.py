#!/usr/bin/env python3
"""Export compact, matched prerequisites for external modules and DTB overlays.

Not a running kernel or a full compiler object cache. Copy into a private output
before use; never let make mutate the shared SDK.
"""
from pathlib import Path
import shutil


REQUIRED = ('.config', 'Makefile', 'Module.symvers', 'modules.builtin',
            'modules.builtin.modinfo', 'include/config/kernel.release',
            'include/config/auto.conf', 'include/generated/autoconf.h',
            'include/generated/utsrelease.h', 'scripts/basic/fixdep', 'scripts/mod/modpost')
def directories(arch='arm64'):
    return ('include', 'scripts', f'arch/{arch}/include', f'arch/{arch}/boot/dts')


def export(build, output, arch='arm64'):
    build, output = Path(build), Path(output)
    missing = [name for name in REQUIRED if not (build / name).is_file()]
    if missing:
        raise ValueError('Missing module build prerequisites: ' + ', '.join(missing))
    if output.exists():
        raise ValueError(f'SDK output already exists: {output}')
    output.mkdir(parents=True)
    for name in directories(arch):
        shutil.copytree(build / name, output / name)
    for name in ('.config', 'Makefile', 'Module.symvers', 'modules.builtin',
                 'modules.builtin.modinfo', 'modules.order', '.version'):
        if (build / name).is_file():
            shutil.copy2(build / name, output / name)
    # The generated /out/source -> /src link is deliberately not exported.
    # The module build recipe binds the new local source/output itself.


