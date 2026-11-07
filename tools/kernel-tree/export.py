#!/usr/bin/env python3
"""Export a linux branch as orchard patch files, for an orchard PR.

While orchard's patch layers are authoritative, new kernel work happens on a
branch off OrchardLinux/linux main. This writes the branch's commits into
kernel/patches/<topic>/ of an orchard checkout, numbered after the files
already there, in `git format-patch --zero-commit` format: author, date,
message and trailers stay as committed.

Each commit's topic comes from the files it touches (a driver directory, a
device tree's SoC, a binding's subsystem). A commit whose files point to more
than one topic, or to none, is not guessed: name it with --topic.

A change to an Orchard defconfig (arch/*/configs/orchard_*_defconfig) is not
a patch: configs live in kernel/configs/<platform>/config. The branch's
defconfig is expanded there (`make <defconfig>` in the kernel toolchain
container), so it reaches orchard the way any change does; the rest of such
a commit is exported as usual.

usage: export.py --orchard DIR --linux DIR [--base REV] [--topic SHA=TOPIC ...]
                 [--dry-run] BRANCH
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
parser.add_argument('--orchard', required=True, type=Path, help='orchard checkout to write into')
parser.add_argument('--linux', required=True, type=Path, help='linux repository with BRANCH')
parser.add_argument('--base', default='main', help='where BRANCH started (default: main)')
parser.add_argument('--topic', action='append', default=[], metavar='SHA=TOPIC',
                    help='topic for one commit, e.g. 1a2b3c=drivers/input')
parser.add_argument('--dry-run', action='store_true', help='show the plan, write nothing')
parser.add_argument('branch')
args = parser.parse_args()
args.orchard, args.linux = args.orchard.resolve(), args.linux.resolve()
patches = args.orchard / "kernel/patches"


def git(*cmd, input=None):
    return subprocess.run(['git', '-C', args.linux, *cmd], input=input, capture_output=True,
                          check=True).stdout.decode().strip()


# Kernel paths -> orchard topic directories. Longest prefix wins.
DRIVERS = {
    'drivers/gpu/drm/': 'drivers/drm', 'drivers/net/wireless/': 'drivers/wifi',
    'drivers/bluetooth/': 'drivers/bluetooth', 'drivers/video/backlight/': 'drivers/backlight',
    'drivers/soc/apple/': 'drivers/soc-apple', 'drivers/tty/serial/': 'drivers/serial',
    'drivers/power/': 'drivers/power', 'sound/': 'drivers/sound',
}
BINDINGS = {
    'clock': 'clk', 'display': 'drm', 'gpu': 'drm', 'net/wireless': 'wifi',
    'net/bluetooth': 'bluetooth', 'leds/backlight': 'backlight', 'soc/apple': 'soc-apple',
}


def topic_of(path):
    m = re.match(r'arch/arm(64)?/boot/dts/apple/([a-z]+[0-9]+[a-z]?)[-.]', path)
    if m:
        return f'devices/{m[2]}'
    for prefix, topic in sorted(DRIVERS.items(), key=lambda kv: -len(kv[0])):
        if path.startswith(prefix):
            return topic
    m = re.match(r'drivers/([^/]+)/', path)
    if m:
        return f'drivers/{m[1]}'
    m = re.match(r'(?:Documentation/devicetree/bindings|include/dt-bindings)/(.+)/[^/]+$', path)
    if m:
        sub = m[1]
        for key, name in sorted(BINDINGS.items(), key=lambda kv: -len(kv[0])):
            if sub == key or sub.startswith(key + '/'):
                return f'drivers/{name}'
        return f'drivers/{sub.split("/")[0]}'
    return None  # include/, MAINTAINERS, ...: no say in the topic


overrides = dict(t.split('=', 1) for t in args.topic)
commits = git('rev-list', '--reverse', '--no-merges', f'{args.base}..{args.branch}').split()
if not commits:
    sys.exit(f'No commits on {args.branch} after {args.base}')
if git('rev-list', '--merges', f'{args.base}..{args.branch}'):
    sys.exit(f'{args.branch} contains merges; rebase it onto {args.base} first')

DEFCONFIG = re.compile(r'arch/(arm|arm64)/configs/orchard_[a-z0-9_]+_defconfig$')
# defconfig path -> (platform, arch, defconfig name, orchard config path), from orchard's descriptors.
platforms = {}
for descriptor in sorted((args.orchard / 'platforms').glob('*.toml')):
    described = json.loads(subprocess.run([sys.executable, str(args.orchard / 'kernel/series.py'), '--platform',
                                           descriptor.stem], capture_output=True, check=True, text=True).stdout)
    platforms[f'arch/{described["arch"]}/configs/{described["defconfig"]}'] = (
        descriptor.stem, described['arch'], described['defconfig'], described['config'])

plan, problems, configs = [], [], set()
for sha in commits:
    subject = git('log', '-1', '--format=%s', sha)
    files = [f for f in git('diff-tree', '--no-commit-id', '--name-only', '-r', sha).split('\n') if f]
    changed = [f for f in files if DEFCONFIG.match(f)]
    for path in changed:
        if path not in platforms:
            problems.append(f'{sha[:12]} {subject}: {path} is no platform\'s defconfig')
        configs.add(path)
    files = [f for f in files if f not in changed]
    if not files:
        continue  # a config change only
    explicit = next((t for k, t in overrides.items() if sha.startswith(k)), None)
    candidates = {topic_of(f) for f in files} - {None}
    if explicit:
        topic = explicit
    elif len(candidates) == 1:
        topic = candidates.pop()
    else:
        problems.append(f'{sha[:12]} {subject}: files point to '
                        f'{", ".join(sorted(candidates)) or "no topic"}; name one with '
                        f'--topic {sha[:12]}=<topic>')
        continue
    plan.append((sha, subject, topic))
if problems:
    sys.exit('Not exported:\n  ' + '\n  '.join(problems))

numbers = {}
writes = []
for sha, subject, topic in plan:
    directory = patches / topic
    if topic not in numbers:
        existing = [int(p.name[:4]) for p in directory.glob('[0-9][0-9][0-9][0-9]-*.patch')]
        numbers[topic] = max(existing, default=0)
    numbers[topic] += 1
    writes.append((sha, subject, topic, numbers[topic]))
    new = '' if directory.is_dir() else '  (new directory)'
    print(f'{topic}/{numbers[topic]:04d}  {subject}{new}')
for path in sorted(configs):
    print(f'{platforms[path][3]}  from {path} on {args.branch}')
if args.dry_run:
    sys.exit(0)

written = []
for sha, subject, topic, number in writes:
    directory = patches / topic
    directory.mkdir(parents=True, exist_ok=True)
    name = git('format-patch', '--zero-commit', '--no-signature', '--start-number', str(number),
               '-o', str(directory), '-1', sha, '--', '.', ':(exclude,glob)arch/*/configs/orchard_*_defconfig')
    written.append(Path(name))

# Defconfig changes: the branch's defconfig, expanded, becomes orchard's config.
IMAGE = 'localhost/orchard-hardware-kernel-toolchain:bookworm'
if configs:
    with tempfile.TemporaryDirectory() as t:
        out = Path(t)
        (out / 'platforms').write_text(''.join(f'{platforms[p][0]} {platforms[p][1]} {platforms[p][2]}\n'
                                               for p in sorted(configs)))
        if subprocess.run(['podman', 'image', 'exists', IMAGE]).returncode:
            subprocess.run(['podman', 'build', '-q', '-t', IMAGE, '-f', str(args.orchard / 'kernel/Containerfile'),
                            str(args.orchard / 'kernel')], check=True)
        tree = subprocess.run(['git', '-C', args.linux, 'archive', args.branch], capture_output=True, check=True).stdout
        subprocess.run(['podman', 'run', '--rm', '-i', '--userns', 'keep-id', '-v', f'{out}:/out', IMAGE, 'bash', '-euc', '''
            mkdir /tmp/src && tar -x -C /tmp/src
            while read -r name arch defconfig; do
              mkdir -p "/tmp/b/$name"
              make -s -C /tmp/src O="/tmp/b/$name" LLVM=-19 ARCH="$arch" "$defconfig"
              cp "/tmp/b/$name/.config" "/out/$name.config"
            done < /out/platforms'''], input=tree, check=True)
        for path in sorted(configs):
            platform, _, _, config = platforms[path]
            (args.orchard / config).write_bytes((out / f'{platform}.config').read_bytes())
            print(f'Wrote {config} from {path}')

# Check: the written files, applied to the base, give exactly the branch.
with tempfile.TemporaryDirectory() as t:
    env_index = {'GIT_INDEX_FILE': f'{t}/index'}
    run = lambda *c: subprocess.run(['git', '-C', args.linux, *c], capture_output=True, check=True,
                                    env={**os.environ, **env_index}).stdout.decode().strip()
    run('read-tree', args.base)
    for path in written:
        run('apply', '--cached', str(path))
    applied = run('write-tree')
    # The defconfigs are orchard's configs, not patches: they differ by design.
    if subprocess.run(['git', '-C', args.linux, 'diff', '--quiet', applied, f'{args.branch}^{{tree}}', '--', '.',
                       ':(exclude,glob)arch/*/configs/orchard_*_defconfig']).returncode:
        sys.exit('Check failed: the written patches do not reproduce the branch')
print(f'Wrote {len(written)} patch(es) and {len(configs)} config(s); applied to {args.base} they '
      f'reproduce {args.branch} exactly.\n'
      'Run tools/kernel-tree/regenerate after committing to check them in series order.')
