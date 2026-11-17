#!/usr/bin/env python3
# Cache: this script's text is not part of the cached kernel's key. If a change
# alters what it produces, raise KERNEL_PRODUCER in src/orchard/build.py.
"""Install built kernel and additional modules into a local filesystem tree."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys

# Recipes use buildenv.py by path, without installing the orchard package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/orchard'))
import buildenv  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', type=Path, required=True, help='kernel build output')
    parser.add_argument('--external', type=Path, action='append', default=[], help='additional module build tree')
    parser.add_argument('--output', type=Path, required=True, help='new module staging tree')
    args = parser.parse_args()
    release = (args.build / 'include/config/kernel.release').read_text().strip()
    if not release or '/' in release or args.output.exists():
        parser.error('invalid release or output already exists')
    external = [p for root in args.external for p in root.rglob('*.ko')]
    overrides = {p.stem.replace('-', '_') for p in external}
    builtin = [args.build / n for n in ('modules.builtin', 'modules.builtin.modinfo')]
    order = args.build / 'modules.order'
    if not all(p.is_file() for p in (*builtin, order)):
        parser.error('missing kernel module metadata')
    # The modules of this build are the ones its modules.order lists. A build
    # directory is reused, so it can still hold the .ko of a module an earlier
    # config built; those must not ship. A bare kernel SDK keeps the list
    # without the modules: then there is nothing in-tree to stage.
    listed = [Path(line.strip()).with_suffix('.ko') for line in order.read_text().splitlines()
              if line.strip()]
    present = [r for r in listed if (args.build / r).is_file()]
    if present and len(present) != len(listed):
        missing = sorted(set(listed) - set(present))
        parser.error(f'modules.order lists modules that were not built: {", ".join(map(str, missing[:5]))}')
    rows = []
    for relative in sorted(present):
        if relative.stem.replace('-', '_') not in overrides:
            rows.append((args.build / relative, Path('kernel') / relative))
    for p in sorted(external):
        rows.append((p, Path('extra') / p.name))
    names = [str(dest) for _, dest in rows]
    if len(names) != len(set(names)):
        parser.error('duplicate module destination')
    dest = args.output / 'lib/modules' / release
    dest.mkdir(parents=True)
    for source, relative in rows:
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    for p in builtin:
        shutil.copyfile(p, dest / p.name)
    (dest / 'modules.order').write_text(''.join(n + '\n' for n in names))
    # Debug sections belong in the build tree, not the boot filesystem.
    modules = [str(dest / relative) for _, relative in rows]
    if buildenv.native():
        if modules:
            subprocess.run(['llvm-strip-19', '--strip-debug', *modules], check=True)
        subprocess.run(['depmod', '-b', str(args.output.resolve()), release], check=True)
    else:
        podman = shutil.which('podman')
        if podman is None:
            parser.error('Podman is required')
        if modules:
            subprocess.run([podman, 'run', '--rm', '--network', 'none', '--userns', 'keep-id',
                            '-v', f'{args.output.resolve()}:/stage:rw,z',
                            'localhost/orchard-hardware-kernel-toolchain:bookworm',
                            'llvm-strip-19', '--strip-debug',
                            *['/stage/' + str(Path(p).relative_to(args.output)) for p in modules]],
                           check=True)
        subprocess.run([podman, 'run', '--rm', '--network', 'none', '--userns', 'keep-id',
                        '-v', f'{args.output.resolve()}:/stage:rw,z',
                        'localhost/orchard-hardware-kernel-toolchain:bookworm',
                        'depmod', '-b', '/stage', release], check=True)
    print(f'Staged {len(rows)} modules for {release} in {args.output}')


if __name__ == '__main__':
    main()
