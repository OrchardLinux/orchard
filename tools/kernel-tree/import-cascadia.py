#!/usr/bin/env python3
"""Import Project Cascadia's A5 kernel as orchard patch files.

Cascadia (https://github.com/rzxvx/Project-Cascadia) is not a kernel tree: its
build takes torvalds v6.12, copies patches/files/, applies patches/tree/*.patch
and copies patches/files-sandcastle-port/ on top (scripts/build-kernel.sh).
This assembles exactly that with git plumbing, without running Cascadia's
scripts, and splits the result into one commit per subsystem. Each commit's
author and co-contributors come from Cascadia's history for exactly its files;
its date is that of the last Cascadia change to them. The commits, applied to
v6.12, reproduce Cascadia's tree; the tool checks that before writing.

usage: import-cascadia.py --cascadia DIR --commit REV --linux DIR --base REV --out DIR
"""
import argparse
import collections
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
parser.add_argument('--cascadia', required=True, type=Path, help='clone of Project-Cascadia')
parser.add_argument('--commit', required=True, help='Cascadia commit to import')
parser.add_argument('--linux', required=True, type=Path, help='linux repository holding BASE')
parser.add_argument('--base', required=True, help='v6.12 in the linux repository')
parser.add_argument('--out', required=True, type=Path, help='directory for the patch files')
args = parser.parse_args()
URL = 'https://github.com/rzxvx/Project-Cascadia'
SANDCASTLE = 'https://github.com/corellium/linux-sandcastle'

# (subject, path prefixes). Order is the series order: platform first.
GROUPS = [
    ('ARM: apple: Add the S5L (A5) platform',
     ['arch/arm/Kconfig', 'arch/arm/Makefile', 'arch/arm/mach-apple/', 'arch/arm/include/asm/apple_boot.h',
      'arch/arm/boot/dts/Makefile']),
    ('irqchip: Add the Apple AIC1 interrupt controller',
     ['drivers/irqchip/', 'Documentation/devicetree/bindings/interrupt-controller/apple,aic1.yaml']),
    ('ARM: apple: S5L bring-up workarounds and trace points in core code',
     ['init/main.c', 'kernel/fork.c', 'arch/arm/kernel/', 'arch/arm/mm/']),
    ('tty: serial: samsung: Support the Apple S5L UART', ['drivers/tty/serial/']),
    ('clk: Add the Apple S5L8940X power manager clocks', ['drivers/clk/']),
    ('pinctrl: Add the Apple S5L8940X GPIO controller', ['drivers/pinctrl/']),
    ('mfd: regulator: Add the Apple PMU and its power switches',
     ['drivers/mfd/', 'include/linux/apple-pmu-i2c.h', 'drivers/regulator/']),
    ('i2c: Add the Apple S5L8940X I2C controller', ['drivers/i2c/']),
    ('spi: Add the Apple S5L8940X SPI controller', ['drivers/spi/']),
    ('Input: Add the Apple Z2 touchscreen for the A5', ['drivers/input/']),
    ('usb: phy: Support the Apple S5L USB PHY, dwc2 and EHCI', ['drivers/phy/', 'drivers/usb/']),
    ('power: supply: Add the Apple PMU charger and HDQ battery', ['drivers/power/']),
    ('backlight: Add the Apple PMU WLED backlight', ['drivers/video/']),
    ('wifi: brcmfmac: Support the iPad mini 1 USB Wi-Fi', ['drivers/net/wireless/']),
    ('Bluetooth: Support the iPad mini 1 Bluetooth', ['drivers/bluetooth/', 'net/bluetooth/']),
    ('mtd: Add the Apple H2FMI NAND controller', ['drivers/mtd/']),
    ('drm: Add the apple-sgx driver for the A5 PowerVR SGX543', ['drivers/gpu/drm/', 'include/uapi/drm/']),
    ('ASoC: Add the Apple S5L audio and the CS35L19 codec', ['sound/']),
    ('iio: accel: st_accel: Add the LIS3DSH', ['drivers/iio/']),
    ('hfsplus: Read decmpfs-compressed files', ['fs/hfsplus/']),
]


def run(*cmd, cwd=None, env=None, input=None):
    r = subprocess.run(cmd, cwd=cwd, env=env, input=input, capture_output=True)
    if r.returncode:
        sys.exit(f'{" ".join(map(str, cmd[:6]))} failed:\n{r.stderr.decode()}')
    return r.stdout.decode().strip()


def cgit(*cmd):
    return run('git', '-C', args.cascadia, *cmd)


commit = cgit('rev-parse', '--short', args.commit)
work = Path(tempfile.mkdtemp(prefix='cascadia-'))
src = work / 'src'
src.mkdir()
run('tar', '-x', '-C', src, input=subprocess.run(['git', '-C', args.cascadia, 'archive', commit, 'patches'],
                                                capture_output=True, check=True).stdout)
index = work / 'index'
env = {**os.environ, 'GIT_INDEX_FILE': str(index)}


def lgit(*cmd, input=None, extra=None):
    return run('git', '-C', args.linux, *cmd, env={**env, **(extra or {})}, input=input)


def add_tree(root):
    for path in sorted(p for p in root.rglob('*') if p.is_file()):
        rel = path.relative_to(root).as_posix()
        blob = lgit('hash-object', '-w', str(path))
        lgit('update-index', '--add', '--cacheinfo', f'{"100755" if os.access(path, os.X_OK) else "100644"},{blob},{rel}')


# Assemble as scripts/build-kernel.sh does.
lgit('read-tree', args.base)
add_tree(src / 'patches/files')
tree_patches = sorted((src / 'patches/tree').glob('*.patch'))
for patch in tree_patches:
    lgit('apply', '--cached', '--whitespace=nowarn', str(patch))
add_tree(src / 'patches/files-sandcastle-port')
assembled = lgit('write-tree')
changed = lgit('diff-tree', '-r', '--name-only', args.base, assembled).splitlines()

# Where each kernel path comes from in Cascadia.
SOURCES = {'files': 'patches/files/', 'sandcastle': 'patches/files-sandcastle-port/'}


def origin(path):
    for kind, prefix in SOURCES.items():
        if (src / prefix / path).is_file():
            return kind, prefix + path
    for patch in tree_patches:  # changed by a patches/tree patch
        if path in sections(patch.read_text(errors='replace')):
            return 'tree', f'patches/tree/{patch.name}'
    sys.exit(f'{path}: not found in Cascadia\'s sources')


def sections(text):
    """Per-file sections of a patch: {path: section text}."""
    result, current = {}, None
    for line in text.splitlines(keepends=True):
        if line.startswith('diff --git a/'):
            current = line.split(' b/', 1)[1].strip()
            result[current] = ''
        if current:
            result[current] += line
    return result


def show(rev, path):
    r = subprocess.run(['git', '-C', args.cascadia, 'show', f'{rev}:{path}'], capture_output=True)
    return r.stdout.decode(errors='replace') if r.returncode == 0 else ''


TREE_CHANGES = collections.defaultdict(list)  # kernel path -> [(sha, name, email, date)]
for line in cgit('log', '--format=%H\t%an\t%ae\t%aD', commit, '--', 'patches/tree').splitlines():
    sha, name, email, date = line.split('\t')
    for patch in cgit('show', '--format=', '--name-only', sha, '--', 'patches/tree').splitlines():
        before, after = sections(show(sha + '^', patch)), sections(show(sha, patch))
        for path in set(before) | set(after):
            if before.get(path) != after.get(path):
                TREE_CHANGES[path].append((sha, name, email, date))


def tree_history(path):
    """Cascadia commits that changed path's section of a patches/tree patch."""
    return TREE_CHANGES[path]


def history(path):
    kind, source = origin(path)
    if kind == 'tree':
        return kind, tree_history(path)
    lines = cgit('log', '--follow', '--format=%H\t%an\t%ae\t%aD', commit, '--', source).splitlines()
    return kind, [tuple(l.split('\t')) for l in lines]


grouped = collections.OrderedDict((subject, []) for subject, _ in GROUPS)
for path in changed:
    for subject, prefixes in GROUPS:
        if any(path == p or (p.endswith('/') and path.startswith(p)) for p in prefixes):
            grouped[subject].append(path)
            break
    else:
        sys.exit(f'No group for {path}; add it to GROUPS')

parent = lgit('rev-parse', args.base + '^{commit}')
lgit('read-tree', args.base)
written = []
args.out.mkdir(parents=True, exist_ok=True)
for number, (subject, paths) in enumerate(((s, p) for s, p in grouped.items() if p), 1):
    commits = {}
    kinds = set()
    for path in paths:
        kind, entries = history(path)
        kinds.add(kind)
        for sha, name, email, date in entries:
            commits[sha] = (name, email, date)
    if not commits:
        sys.exit(f'No Cascadia history for {subject}')
    count = collections.Counter((n, e) for n, e, _ in commits.values())
    (author, email), _ = count.most_common(1)[0]
    newest = max(commits, key=lambda s: int(cgit('log', '-1', '--format=%at', s)))
    date = commits[newest][2]
    others = []
    for (name, mail), n in count.most_common():
        if (name, mail) == (author, email):
            continue
        ids = ', '.join(cgit('rev-parse', '--short', s) for s, v in commits.items() if v[:2] == (name, mail))
        others.append(f'{name} <{mail}> ({ids})')
    sources = sorted({origin(p)[1] for p in paths})
    body = [f'Imported unchanged from Project Cascadia ({URL}) at {commit}:'] + \
           [f'  {s}' for s in sources] + ['']
    body.append(f'Cascadia history of these files: {author} <{email}> '
                f'({count[(author, email)]} commit{"s" if count[(author, email)] != 1 else ""})'
                + (';' if others else '.'))
    body += [f'  {o}' for o in others]
    if 'sandcastle' in kinds:
        body += [''] + textwrap.wrap(
            f'The S5L8940X drivers here are Cascadia ports of Corellium\'s Project Sandcastle '
            f'({SANDCASTLE}, sandcastle-5.4, GPL-2.0); their copyright headers name Corellium '
            'and the drivers they were based on.', 72, break_long_words=False, break_on_hyphens=False)
    body += ['', '[Orchard: imported for the armv7-a5 platform; see kernel/sources.toml]']
    for path in paths:
        mode, kind_, sha, _ = (lgit('ls-tree', assembled, '--', path) + '\t').split(None, 3)
        lgit('update-index', '--add', '--cacheinfo', f'{mode},{sha},{path}')
    tree = lgit('write-tree')
    parent = lgit('commit-tree', tree, '-p', parent, input=('\n'.join([subject, ''] + body) + '\n').encode(),
                  extra={'GIT_AUTHOR_NAME': author, 'GIT_AUTHOR_EMAIL': email, 'GIT_AUTHOR_DATE': date,
                         'GIT_COMMITTER_NAME': author, 'GIT_COMMITTER_EMAIL': email,
                         'GIT_COMMITTER_DATE': date})
    name = lgit('format-patch', '--zero-commit', '--no-signature', '--start-number', str(number),
                '-o', str(args.out), '-1', parent)
    written.append(Path(name).name)
    print(f'{number:04d} {author:<16} {len(paths):3} files  {subject}')

if lgit('rev-parse', parent + '^{tree}') != assembled:
    sys.exit('Check failed: the commits do not reproduce the assembled Cascadia tree')
lgit('update-ref', 'refs/orchard/cascadia-import', parent)
print(f'{len(written)} patches in {args.out}; applied to {args.base} they give Cascadia {commit} exactly '
      f'(tree {assembled[:12]}).')
