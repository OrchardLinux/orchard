#!/usr/bin/env python3
"""Import Project Cascadia's A5 kernel as orchard patch files.

Cascadia (https://github.com/rzxvx/Project-Cascadia) is not a kernel tree: its
build takes torvalds v6.12, copies patches/files/, applies patches/tree/*.patch
and copies patches/files-sandcastle-port/ on top (scripts/build-kernel.sh).
Since aa6c5be (2026-11-04) that A5 tree is devices/a5/ of the repository; at
earlier commits it is the repository's top. Histories follow a file across the
move.
This assembles exactly that with git plumbing, without running Cascadia's
scripts, and splits the result into one commit per subsystem. Each commit's
author and co-contributors come from Cascadia's history for exactly its files;
its date is that of the last Cascadia change to them. The commits, applied to
v6.12, reproduce Cascadia's tree; the tool checks that before writing.

Cascadia compiles its boards' device trees (dts/*.dts) with dtc outside the
kernel. One commit per board in BOARDS adds its tree to arch/arm/boot/dts/apple/,
named like the arm64 Apple trees and built by make dtbs, content unchanged.
Orchard's own commits on top of the import (--then, patch files as
format-patch writes them) are applied last with their author and message.
The patch files in OUT are replaced.

usage: import-cascadia.py --cascadia DIR --commit REV --linux DIR --base REV --out DIR
                          [--then PATCH ...]
"""
import argparse
import atexit
import collections
import functools
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path, PurePosixPath

parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
parser.add_argument('--cascadia', required=True, type=Path, help='clone of Project-Cascadia')
parser.add_argument('--commit', required=True, help='Cascadia commit to import')
parser.add_argument('--linux', required=True, type=Path, help='linux repository holding BASE')
parser.add_argument('--base', required=True, help='v6.12 in the linux repository')
parser.add_argument('--out', required=True, type=Path, help='directory for the patch files')
parser.add_argument('--then', nargs='*', default=[], type=Path,
                    help="orchard's own patches on top of the import, applied last in this order")
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
    ('wifi: brcmfmac: Support the Wi-Fi of the A5 and A5X iPads', ['drivers/net/wireless/']),
    ('Bluetooth: Support the Bluetooth of the A5 and A5X iPads', ['drivers/bluetooth/', 'net/bluetooth/']),
    ('mmc: sdhci: Add the Apple S5L SDIO host', ['drivers/mmc/']),
    ('mtd: Add the Apple H2FMI NAND controller', ['drivers/mtd/devices/']),
    # The PPN controller and FTL of drivers/mtd/nand/apple drive the same flash
    # as H2FMI and as Orchard's patches/armv7/mtd; kernel/sources.toml says
    # which of them the platform config builds.
    ('mtd: nand: Add the Apple PPN controller and Swiss FTL', ['drivers/mtd/nand/']),
    ('drm: Add the apple-sgx driver for the A5 PowerVR SGX543', ['drivers/gpu/drm/', 'include/uapi/drm/']),
    ('media: Add the Apple A5 H4 camera ISP', ['drivers/media/']),
    ('ASoC: Add the Apple S5L audio and the CS35L19 and CS42L63 codecs', ['sound/']),
    ('iio: accel: st_accel: Add the LIS3DSH', ['drivers/iio/']),
    ('hfsplus: Read decmpfs-compressed files', ['fs/hfsplus/']),
]

# Boards whose Cascadia device tree joins arch/arm/boot/dts/apple/:
# (Cascadia path below the A5 root, kernel name, subject, what the board is).
BOARDS = [
    ('dts/p105ap.dts', 's5l8942x-p105', 'ARM: dts: apple: Add the iPad mini 1 (P105)',
     'The iPad mini 1 Wi-Fi (iPad2,5, P105AP) on the A5 (S5L8942X), Cascadia\'s '
     'first board.'),
    ('dts/j1ap.dts', 's5l8945x-j1', 'ARM: dts: apple: Add the iPad 3 (J1)',
     'The iPad 3 Wi-Fi (iPad3,1, J1AP) on the A5X (S5L8945X). The tree\'s comments '
     'say where it differs from the P105 and why; Cascadia\'s '
     'devices/a5/docs/research/j1-a5x.md records the bring-up.'),
    ('dts/k93ap.dts', 's5l8940x-k93', 'ARM: dts: apple: Add the iPad 2 (K93)',
     'The iPad 2 Wi-Fi (iPad2,1, K93AP) on the original A5 (S5L8940X), with its '
     'BCM4329 Wi-Fi on SDIO, CS42L63 audio and H4 ISP cameras. The tree\'s '
     'comments say where it differs from the P105; Cascadia\'s '
     'devices/a5/docs/k93-bringup.md records what works.'),
]


def run(*cmd, cwd=None, env=None, input=None):
    r = subprocess.run(cmd, cwd=cwd, env=env, input=input, capture_output=True)
    if r.returncode:
        sys.exit(f'{" ".join(map(str, cmd[:6]))} failed:\n{r.stderr.decode()}')
    return r.stdout.decode().strip()


def cgit(*cmd):
    return run('git', '-C', args.cascadia, *cmd)


commit = cgit('rev-parse', '--short', args.commit)
# Where the A5 tree is at this commit: devices/a5/ since aa6c5be, the top before.
ROOT = 'devices/a5/' if cgit('ls-tree', '--name-only', commit, 'devices/a5/patches') else ''
work = Path(tempfile.mkdtemp(prefix='cascadia-'))
atexit.register(shutil.rmtree, work, ignore_errors=True)
src = work / 'src'
src.mkdir()
run('tar', '-x', '-C', src, input=subprocess.run(['git', '-C', args.cascadia, 'archive', commit, ROOT + 'patches'],
                                                capture_output=True, check=True).stdout)
src = src / ROOT
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
            return kind, ROOT + prefix + path
    for patch in tree_patches:  # changed by a patches/tree patch
        if path in sections(patch.read_text(errors='replace')):
            return 'tree', f'{ROOT}patches/tree/{patch.name}'
    sys.exit(f'{path}: not found in Cascadia\'s sources')


def origins(path):
    """Every Cascadia source of path: its file, or each patches/tree patch that changes it."""
    kind, source = origin(path)
    if kind != 'tree':
        return [source]
    return [f'{ROOT}patches/tree/{p.name}' for p in tree_patches
            if path in sections(p.read_text(errors='replace'))]


def sections(text):
    """Per-file sections of a patch: {path: section text}. A section starts at
    a git header (diff --git) or, in a plain unified diff, at its ---/+++ pair."""
    result, current, in_header = {}, None, False
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith('diff --git a/'):
            current, in_header = line.split(' b/', 1)[1].strip(), True
            result[current] = ''
        elif line.startswith('+++ '):
            in_header = False
        elif (not in_header and line.startswith(('--- a/', '--- /dev/null')) and i + 1 < len(lines)
              and lines[i + 1].startswith('+++ b/')):
            current = lines[i + 1][len('+++ b/'):].split('\t')[0].strip()
            result[current] = ''
        if current:
            result[current] += line
    return result


def show(rev, path):
    r = subprocess.run(['git', '-C', args.cascadia, 'show', f'{rev}:{path}'], capture_output=True)
    return r.stdout.decode(errors='replace') if r.returncode == 0 else ''


TREE_CHANGES = collections.defaultdict(list)  # kernel path -> [(sha, name, email, date)]
# Both places of patches/tree: commits before the move changed it at the top.
TREE_DIRS = sorted({'patches/tree', ROOT + 'patches/tree'})


@functools.lru_cache(maxsize=None)
def tree_sections(rev):
    """Sections of the patches/tree patches at rev, wherever the tree was then:
    {(patch file name, kernel path): section text}. Keyed by the file name, not
    its directory, so a patch reads the same on both sides of the move, and two
    patches that touch one kernel path keep a section each."""
    r = subprocess.run(['git', '-C', args.cascadia, 'ls-tree', '-r', '-z', '--name-only', rev, '--', *TREE_DIRS],
                       capture_output=True)
    result = {}
    for patch in filter(None, r.stdout.decode().split('\0')):
        for path, text in sections(show(rev, patch)).items():
            result[(PurePosixPath(patch).name, path)] = text
    return result


# A commit changed a kernel path when one of its sections differs before and
# after, so the move, which changes no section, is nobody's change.
for line in cgit('log', '--format=%H\t%P\t%an\t%ae\t%aD', commit, '--', *TREE_DIRS).splitlines():
    sha, parents, name, email, date = line.split('\t')
    # The first parent's sha, not sha^, so a revision is listed once (cached).
    before = tree_sections(parents.split()[0]) if parents else {}
    after = tree_sections(sha)
    for path in sorted({key[1] for key in before.keys() ^ after.keys()} |
                       {key[1] for key in before.keys() & after.keys() if before[key] != after[key]}):
        TREE_CHANGES[path].append((sha, name, email, date))


def tree_history(path):
    """Cascadia commits that changed path's section of a patches/tree patch."""
    return TREE_CHANGES[path]


def history(path):
    kind, source = origin(path)
    if kind == 'tree':
        return kind, tree_history(path)
    return kind, file_history(source)


def file_history(source):
    """Cascadia commits that changed the file source, followed across renames."""
    # A commit that only renamed the file (the move to devices/a5/) changed none of it.
    log = cgit('log', '--follow', '--name-status', '--format=%x00%H\t%an\t%ae\t%aD', commit, '--', source)
    entries = []
    for record in log.split('\0')[1:]:
        header, *status = record.strip().split('\n')
        if not any(l.startswith('R100\t') for l in status):
            entries.append(tuple(header.split('\t')))
    return entries


grouped = collections.OrderedDict((subject, []) for subject, _ in GROUPS)
for path in changed:
    for subject, prefixes in GROUPS:
        if any(path == p or (p.endswith('/') and path.startswith(p)) for p in prefixes):
            grouped[subject].append(path)
            break
    else:
        sys.exit(f'No group for {path}; add it to GROUPS')

# --then patches are read before OUT's patch files are replaced: they may be among them.
then = [(p.name, p.read_bytes()) for p in args.then]
args.out.mkdir(parents=True, exist_ok=True)
for old in args.out.glob('*.patch'):
    old.unlink()
parent = lgit('rev-parse', args.base + '^{commit}')
lgit('read-tree', args.base)
written = []


def credits(commits):
    """Author, email, date and the body lines naming everyone, from {sha: (name, email, date)}."""
    count = collections.Counter((n, e) for n, e, _ in commits.values())
    (author, email), _ = count.most_common(1)[0]
    newest = max(commits, key=lambda s: int(cgit('log', '-1', '--format=%at', s)))
    others = []
    for (name, mail), n in count.most_common():
        if (name, mail) == (author, email):
            continue
        ids = ', '.join(cgit('rev-parse', '--short', s) for s, v in commits.items() if v[:2] == (name, mail))
        others.append(f'{name} <{mail}> ({ids})')
    n = count[(author, email)]
    return author, email, commits[newest][2], n, others


def commit_index(message, author, email, date, committer=None):
    """Commit the index on parent and write it as the next patch file."""
    global parent
    cname, cmail, cdate = committer or (author, email, date)
    tree = lgit('write-tree')
    parent = lgit('commit-tree', tree, '-p', parent, input=message.encode(),
                  extra={'GIT_AUTHOR_NAME': author, 'GIT_AUTHOR_EMAIL': email, 'GIT_AUTHOR_DATE': date,
                         'GIT_COMMITTER_NAME': cname, 'GIT_COMMITTER_EMAIL': cmail,
                         'GIT_COMMITTER_DATE': cdate})
    number = len(written) + 1
    # A fixed abbreviation, so the files do not depend on the repository's size.
    name = lgit('format-patch', '--zero-commit', '--no-signature', '--abbrev=12', '--start-number', str(number),
                '-o', str(args.out), '-1', parent)
    written.append(Path(name).name)
    return number


for subject, paths in ((s, p) for s, p in grouped.items() if p):
    commits = {}
    kinds = set()
    for path in paths:
        kind, entries = history(path)
        kinds.add(kind)
        for sha, name, email, date in entries:
            commits[sha] = (name, email, date)
    if not commits:
        sys.exit(f'No Cascadia history for {subject}')
    author, email, date, n, others = credits(commits)
    sources = sorted({s for p in paths for s in origins(p)})
    body = [f'Imported unchanged from Project Cascadia ({URL}) at {commit}:'] + \
           [f'  {s}' for s in sources] + ['']
    body.append(f'Cascadia history of these files: {author} <{email}> '
                f'({n} commit{"s" if n != 1 else ""})' + (';' if others else '.'))
    body += [f'  {o}' for o in others]
    if 'sandcastle' in kinds:
        body += [''] + textwrap.wrap(
            f'The S5L8940X drivers here are Cascadia ports of Corellium\'s Project Sandcastle '
            f'({SANDCASTLE}, sandcastle-5.4, GPL-2.0); their copyright headers name Corellium '
            'and the drivers they were based on.', 72, break_long_words=False, break_on_hyphens=False)
    body += ['', '[Orchard: imported for the apple-armv7 platform; see kernel/sources.toml]']
    for path in paths:
        mode, kind_, sha, _ = (lgit('ls-tree', assembled, '--', path) + '\t').split(None, 3)
        lgit('update-index', '--add', '--cacheinfo', f'{mode},{sha},{path}')
    number = commit_index('\n'.join([subject, ''] + body) + '\n', author, email, date)
    print(f'{number:04d} {author:<16} {len(paths):3} files  {subject}')

if lgit('rev-parse', parent + '^{tree}') != assembled:
    sys.exit('Check failed: the commits do not reproduce the assembled Cascadia tree')
imported = len(written)


def blob(text):
    return lgit('hash-object', '-w', '--stdin', input=text.encode())


def read(path):
    r = subprocess.run(['git', '-C', args.linux, 'show', f':{path}'], capture_output=True, env=env)
    return r.stdout.decode() if r.returncode == 0 else None


# The boards' device trees, in arch/arm/boot/dts/apple/.
for source, name, subject, about in BOARDS:
    source = ROOT + source
    text = show(commit, source)
    if not text:
        sys.exit(f'{source}: not in Cascadia {commit}')
    commits = {sha: (n, e, d) for sha, n, e, d in file_history(source)}
    author, email, date, n, others = credits(commits)
    dts = f'arch/arm/boot/dts/apple/{name}.dts'
    makefile = read('arch/arm/boot/dts/apple/Makefile')
    note = (f'Cascadia compiles this file with dtc outside the kernel. Added to '
            f'arch/arm/boot/dts/apple/ as {name}.dts, named like the arm64 Apple device trees, '
            'and built from there by make dtbs; content unchanged.')
    if makefile is None:
        # The first board: the apple/ directory, in place of the dtb that
        # Cascadia's 0001-cascadia.patch names in arch/arm/boot/dts/Makefile
        # and does not build.
        top = read('arch/arm/boot/dts/Makefile').splitlines(keepends=True)
        top = [l for l in top if 'ipad-mini1.dtb' not in l]
        subdirs = [i for i, l in enumerate(top) if l.startswith('subdir-y += ')]
        at = next((i for i in subdirs if top[i] > 'subdir-y += apple\n'), subdirs[-1] + 1)
        top.insert(at, 'subdir-y += apple\n')
        lgit('update-index', '--add', '--cacheinfo', f'100644,{blob("".join(top))},arch/arm/boot/dts/Makefile')
        makefile = '# SPDX-License-Identifier: GPL-2.0\n'
        note += (' The directory replaces the ipad-mini1.dtb that Cascadia\'s tree patch adds to '
                 'arch/arm/boot/dts/Makefile, a file no rule builds.')
    makefile += f'dtb-$(CONFIG_ARCH_APPLE_S5L) += {name}.dtb\n'
    lgit('update-index', '--add', '--cacheinfo', f'100644,{blob(makefile)},arch/arm/boot/dts/apple/Makefile')
    lgit('update-index', '--add', '--cacheinfo', f'100644,{blob(text)},{dts}')
    body = textwrap.wrap(f'Imported from Project Cascadia ({URL}) at {commit}: {source}.', 72,
                         break_long_words=False, break_on_hyphens=False) + ['']
    body.append(f'Cascadia history of this file: {author} <{email}> '
                f'({n} commit{"s" if n != 1 else ""})' + (';' if others else '.'))
    body += [f'  {o}' for o in others]
    body += [''] + textwrap.wrap(about, 72, break_long_words=False, break_on_hyphens=False)
    body += [''] + textwrap.wrap(f'[Orchard: {note}]', 72, break_long_words=False, break_on_hyphens=False)
    body += ['', 'Assisted-by: LLM']
    number = commit_index('\n'.join([subject, ''] + body) + '\n', author, email, date)
    print(f'{number:04d} {author:<16}   1 board  {subject}')

# Orchard's own commits on top, as git am would apply them.
for filename, data in then:
    with tempfile.TemporaryDirectory() as t:
        info = run('git', 'mailinfo', f'{t}/msg', f'{t}/patch', input=data)
        fields = dict(l.split(': ', 1) for l in info.splitlines() if ': ' in l)
        message = fields['Subject'] + '\n\n' + Path(f'{t}/msg').read_text().strip() + '\n'
        lgit('apply', '--cached', f'{t}/patch')
    number = commit_index(message, fields['Author'], fields['Email'], fields['Date'])
    print(f'{number:04d} {fields["Author"]:<16}          {fields["Subject"]} (from {filename})')

lgit('update-ref', 'refs/orchard/cascadia-import', parent)
print(f'{len(written)} patches in {args.out}; applied to {args.base}, the first {imported} give Cascadia '
      f'{commit} exactly (tree {assembled[:12]}).')
