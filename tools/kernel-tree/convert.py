#!/usr/bin/env python3
"""Turn orchard's patch layers into git history on top of their upstream base.

Each patch directory becomes a topic: its patches are applied as commits on
top of main, then merged into main with --no-ff, in series order. The topic
tips are kept as refs/topics/<directory>; only main is meant to be published.

Pure plumbing (temporary index, git apply --cached, commit-tree), so no
checkout is touched. Deterministic and stable: a commit's committer and
committer date are those of the orchard commit that last changed its patch
file, and a merge cites the orchard commit that last changed its topic, so
regenerating from a newer orchard commit keeps the SHAs of every topic
before the first changed one.

usage: convert.py --orchard REPO --commit REV --layers kernel|boot/m1n1
                  --target REPO --base REV [--map FILE]
"""
import argparse
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
parser.add_argument('--orchard', required=True, type=Path, help='orchard repository')
parser.add_argument('--commit', required=True, help='orchard commit to convert')
parser.add_argument('--layers', required=True, help='kernel or boot/m1n1')
parser.add_argument('--target', required=True, type=Path, help='linux or m1n1 repository')
parser.add_argument('--base', required=True, help='upstream base in the target repository')
parser.add_argument('--map', type=Path, help='write "patch path<TAB>commit" lines here')
args = parser.parse_args()


def run(*cmd, env=None, input=None, cwd=None):
    r = subprocess.run(cmd, env=env, input=input, cwd=cwd, capture_output=True)
    if r.returncode:
        sys.exit(f'{" ".join(map(str, cmd[:5]))} failed:\n{r.stderr.decode()}')
    return r.stdout.decode().strip()


commit = run('git', '-C', args.orchard, 'rev-parse', '--short', args.commit)
work = Path(tempfile.mkdtemp(prefix='kernel-tree-'))
index = work / 'index'
source = work / 'src'
source.mkdir()
archive = subprocess.run(['git', '-C', args.orchard, 'archive', commit, args.layers],
                         capture_output=True, check=True).stdout
run('tar', '-x', '-C', source, input=archive)
layers = source / args.layers


def git(*cmd, env=None, input=None):
    return run('git', '-C', args.target, *cmd, input=input,
               env={**os.environ, 'GIT_INDEX_FILE': str(index), **(env or {})})


def orchard_log(fmt, *rest):
    return run('git', '-C', args.orchard, 'log', f'--format={fmt}', commit, *rest)


def last_change(path):
    """Committer of the orchard commit that last changed path."""
    name, email, date = orchard_log('%cn\t%ce\t%cD', '-1', '--', path).split('\t')
    return {'GIT_COMMITTER_NAME': name, 'GIT_COMMITTER_EMAIL': email, 'GIT_COMMITTER_DATE': date}


# Patch order exactly as the build applies it: series layers in order; the
# kernel's series.py takes every patch below a layer (rglob), m1n1's build
# only the layer's own files. Both kernel platforms share one layer list.
if (layers / 'series').is_dir():
    lists = {p.name: p.read_text() for p in (layers / 'series').iterdir()}
    strip = lambda text: [l.split('#', 1)[0].strip() for l in text.splitlines()]
    if len({tuple(filter(None, strip(t))) for t in lists.values()}) != 1:
        sys.exit(f'Platforms list different layers: {sorted(lists)}; convert them separately')
    series, find = next(iter(lists.values())), 'rglob'
else:
    series, find = (layers / 'series').read_text(), 'glob'
patches = []
for line in series.splitlines():
    line = line.split('#', 1)[0].strip()
    if line:
        patches += sorted(getattr(layers / line, find)('*.patch'))

topics = []
for p in patches:
    name = str(p.parent.relative_to(layers / 'patches'))
    if not topics or topics[-1][0] != name:
        topics.append((name, []))
    topics[-1][1].append(p)

provenance = {s['patches'].removeprefix('patches/'): s
              for s in tomllib.loads((layers / 'sources.toml').read_text()).get('source', [])}


def source_note(topic):
    for prefix, s in provenance.items():
        if topic == prefix or topic.startswith(prefix + '/'):
            return f"Source: {s['url']} {s.get('branch', '')} @ {s['commit']}\n"
    return ''


git('read-tree', args.base)
main = git('rev-parse', args.base + '^{commit}')
mapping = []
for topic, files in topics:
    tip, latest = main, None
    for p in files:
        rel = str(p.relative_to(source))
        info = run('git', 'mailinfo', work / 'msg', work / 'patch', input=p.read_bytes())
        fields = dict(l.split(': ', 1) for l in info.splitlines() if ': ' in l)
        body = (work / 'msg').read_text()
        if 'Subject' not in fields:
            # Bare patch (description + diff, no mail header): the subject is
            # its first line; author and date are those of the orchard commit
            # that added the file. Reported, never guessed silently.
            text = p.read_text().split('\ndiff --git ', 1)[0].strip('\n').split('\n')
            fields['Subject'], body = text[0], '\n'.join(text[1:]).strip('\n') + '\n'
            added = orchard_log('%an\t%ae\t%aD\t%h', '--diff-filter=A', '--', rel).splitlines()[-1]
            fields['Author'], fields['Email'], fields['Date'], origin = added.split('\t')
            print(f'     header-less: {rel}: author and date from orchard {origin}')
        message = run('git', 'stripspace',
                      input=(fields['Subject'] + ('\n\n' + body if body.strip() else '\n')).encode())
        git('apply', '--cached', str(p))
        committer = last_change(rel)
        tip = git('commit-tree', git('write-tree'), '-p', tip, input=(message + '\n').encode(), env={
            'GIT_AUTHOR_NAME': fields['Author'], 'GIT_AUTHOR_EMAIL': fields['Email'],
            'GIT_AUTHOR_DATE': fields['Date'], **committer})
        mapping.append((rel, tip))
    git('update-ref', f'refs/topics/{topic}', tip)
    # The merge is authored and committed as the topic's most recent change.
    newest = orchard_log('%h\t%an\t%ae\t%cn\t%ce\t%cD', '-1', '--',
                         *(str(p.relative_to(source)) for p in files))
    changed, an, ae, cn, ce, date = newest.split('\t')
    count = len(files)
    message = (f'Merge {topic} ({count} patch{"es" if count != 1 else ""})\n\n'
               f'From {args.layers}/patches/{topic} as of orchard {changed}.\n' + source_note(topic))
    main = git('commit-tree', git('write-tree'), '-p', main, '-p', tip, input=message.encode(), env={
        'GIT_AUTHOR_NAME': an, 'GIT_AUTHOR_EMAIL': ae, 'GIT_AUTHOR_DATE': date,
        'GIT_COMMITTER_NAME': cn, 'GIT_COMMITTER_EMAIL': ce, 'GIT_COMMITTER_DATE': date})
    print(f'{count:4} {topic}')

git('update-ref', 'refs/orchard/converted', main)
if args.map:
    args.map.write_text(''.join(f'{p}\t{c}\n' for p, c in mapping))
print(f'converted {main} tree {git("rev-parse", main + "^{tree}")} '
      f'patches {len(mapping)} topics {len(topics)} from orchard {commit}')
