#!/usr/bin/env python3
"""The platforms whose kernel a pull request changes, so its checks compile them.

  ci/changed_kernels.py BASE PLATFORM...

BASE is a checkout of the commit the pull request merges into. A platform
counts as changed when its source key (ci/keys.py: upstream pin,
series, patches, config, toolchain recipes, producer) or one of the build
scripts the key leaves out (ci/reuse_kernel.py GUARDED) differs between BASE
and this checkout: exactly what would stop the nightly's kernel from being
reused. Each side's key comes from that side's own code, so a changed key
recipe counts too; a platform BASE cannot describe (a new one) counts as
changed.

Prints two GitHub output lines: `kernels`, the JSON list of changed
platforms, and `kernel_exclude`, the matrix entries of the unchanged ones.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('reuse_kernel', ROOT / 'ci/reuse_kernel.py')
reuse_kernel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reuse_kernel)


def source_key(root, platform):
    """PLATFORM's source key in the checkout ROOT by ROOT's own code, or None when it has none."""
    with tempfile.TemporaryDirectory(prefix='changed-kernels-') as temporary:
        output = Path(temporary) / 'keys.json'
        done = subprocess.run([sys.executable, str(Path(root) / 'ci/keys.py'), '--output', str(output), platform],
                              cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if done.returncode != 0:
            return None
        return json.loads(output.read_text())['platforms'][platform]['key']


def changed(base, platforms, *, root=ROOT):
    """The PLATFORMS whose kernel differs between the checkouts BASE and ROOT."""
    base, root = Path(base), Path(root)
    if reuse_kernel.checkout_scripts(base) != reuse_kernel.checkout_scripts(root):
        return list(platforms)
    build = []
    for platform in platforms:
        before = source_key(base, platform)
        if before is None or before != source_key(root, platform):
            build.append(platform)
    return build


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__.split('\n\n')[1])
    platforms = sys.argv[2:]
    build = changed(sys.argv[1], platforms)
    print('kernels=' + json.dumps(build))
    print('kernel_exclude=' + json.dumps([{'platform': name} for name in platforms if name not in build]))


if __name__ == '__main__':
    main()
