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

usage: export.py --orchard DIR --linux DIR [--base REV] [--topic SHA=TOPIC ...]
                 [--dry-run] BRANCH
"""
import argparse
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
patches = args.orchard / 'kernel/patches'


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

plan, problems = [], []
for sha in commits:
    subject = git('log', '-1', '--format=%s', sha)
    files = git('diff-tree', '--no-commit-id', '--name-only', '-r', sha).split('\n')
    if any(f.startswith('arch/arm64/configs/orchard_') for f in files):
        problems.append(f'{sha[:12]} {subject}: changes an Orchard defconfig. Configs live in '
                        'kernel/configs/<platform>/config: run `make orchard_<platform>_defconfig`, '
                        'change it, copy .config there, and drop the defconfig change from the branch.')
        continue
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
if args.dry_run:
    sys.exit(0)

written = []
for sha, subject, topic, number in writes:
    directory = patches / topic
    directory.mkdir(parents=True, exist_ok=True)
    name = git('format-patch', '--zero-commit', '--no-signature', '--start-number', str(number),
               '-o', str(directory), '-1', sha)
    written.append(Path(name))

# Check: the written files, applied to the base, give exactly the branch.
with tempfile.TemporaryDirectory() as t:
    env_index = {'GIT_INDEX_FILE': f'{t}/index'}
    run = lambda *c: subprocess.run(['git', '-C', args.linux, *c], capture_output=True, check=True,
                                    env={**os.environ, **env_index}).stdout.decode().strip()
    run('read-tree', args.base)
    for path in written:
        run('apply', '--cached', str(path))
    if run('write-tree') != git('rev-parse', f'{args.branch}^{{tree}}'):
        sys.exit('Check failed: the written patches do not reproduce the branch')
print(f'Wrote {len(written)} patch(es); applied to {args.base} they reproduce {args.branch} exactly.\n'
      'Run tools/kernel-tree/regenerate after committing to check them in series order.')
