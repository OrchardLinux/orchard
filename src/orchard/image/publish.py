#!/usr/bin/env python3
"""Stage local release artifacts as a GitHub-hosted release, with immutable URLs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from .integrity import sha
import tomllib


def toml(document, prefix=()):
    lines = []
    if prefix:
        lines.append('[' + '.'.join(json.dumps(key) for key in prefix) + ']')
    for key, value in document.items():
        if not isinstance(value, dict):
            lines.append(json.dumps(key) + ' = ' + json.dumps(value))
    for key, value in document.items():
        if isinstance(value, dict):
            lines.extend(['', toml(value, prefix + (key,))])
    return '\n'.join(lines) + '\n'


def local_asset(directory, receipt):
    name = receipt.get('file', '')
    if not name or Path(name).name != name or name in ('.', '..') or '\\' in name:
        raise ValueError('Publication requires local sibling artifact files')
    path = directory / name
    if path.is_symlink() or not path.is_file():
        raise ValueError('Missing/linked artifact: ' + name)
    if path.stat().st_size != receipt['bytes'] or sha(path) != receipt['sha256']:
        raise ValueError('Artifact differs from receipt: ' + name)
    return path


def orchard_assets(directory):
    """{sha256: URL} of the files this release took finished from a published orchard build.

    Its index (platforms.toml, or platforms-armv7.toml for a 32-bit root) names its `base_url`
    when it came from one; a local build's has none, so a developer's own kernel is hosted with
    the release like anything else.
    """
    from .platforms_index import INDEX, receipts
    offered = {}
    for name in INDEX.values():
        path = directory / name
        if not path.is_file():
            continue
        document = tomllib.loads(path.read_text())
        base = document.get('base_url')
        if base:
            offered.update({entry['sha256']: base + '/' + entry['file'] for entry in receipts(document)})
    return offered


def architecture(manifest):
    """The root architecture of a release manifest (aarch64, armv7), from its boards' chips."""
    from ..hardware import device, root_architecture
    found = {root_architecture(device(board)) for row in manifest['platforms'].values() for board in row['boards']}
    if len(found) != 1:
        raise ValueError('A release has one root, of one architecture; this one covers ' + ', '.join(sorted(found)))
    return found.pop()


def hosted_name(name, architecture, hosted, sha256):
    """NAME in the release, or with ARCHITECTURE added when another root's different file has it."""
    if name in hosted and hosted[name] != sha256:
        stem, dot, suffix = name.partition('.')
        name = f'{stem}-{architecture}{dot}{suffix}'
        if name in hosted and hosted[name] != sha256:
            raise ValueError('Conflicting release artifact names')
    hosted[name] = sha256
    return name


def publish(directories, tag, output, *, distro):
    """Stage the release DIRECTORIES (one, or one per root architecture) as one hosted release in OUTPUT.

    Each directory is a built release with its own index.toml. The hosted index pins the 64-bit
    root's manifest as [release] and each other root's under [releases]; a file name two roots
    share for different files gets the architecture added for the second (release-armv7.toml).
    """
    if not re.fullmatch(r'[a-z0-9][a-z0-9._-]*', distro):
        raise ValueError('Invalid distro repository name')
    if tag in ('latest', 'main', 'master') or not re.fullmatch(r'v?[A-Za-z0-9][A-Za-z0-9._-]*', tag):
        raise ValueError('Invalid release tag')
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    directories = [Path(directories)] if isinstance(directories, (str, os.PathLike)) else list(map(Path, directories))
    roots = {}
    for directory in directories:
        index_path = directory / 'index.toml'
        if index_path.is_symlink():
            raise ValueError('Linked index refused')
        index = tomllib.loads(index_path.read_text())
        if index.get('schema') != 2 or index.get('kind') != 'orchard.index' or index.get('distro') != distro:
            raise ValueError('Expected a matching distro release index')
        if 'releases' in index:
            raise ValueError('Publish each root from its own release directory')
        source = local_asset(directory, index['release'])
        manifest = tomllib.loads(source.read_text())
        if manifest.get('kind') != 'orchard.install' or manifest.get('distro') != distro:
            raise ValueError('Manifest does not match its index')
        root = architecture(manifest)
        if root in roots:
            raise ValueError(f'Two {root} releases; one release has one root per architecture')
        roots[root] = (directory, source, manifest)
    if len(roots) > 1 and 'aarch64' not in roots:
        raise ValueError('A release of several roots has a 64-bit one')
    from ..manifests import sources
    base = 'https://github.com/OrchardLinux/' + distro + '/releases/download/' + tag
    metadata_names = {'index.toml', *(source.name for _, source, _ in roots.values())}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.publish-', dir=output.parent) as temporary:
        stage = Path(temporary) / 'release'
        stage.mkdir()
        hosted_index = {'schema': 2, 'kind': 'orchard.index', 'distro': distro}
        hosted = {}
        # The 64-bit root first: its files keep their names.
        for root in sorted(roots, key=lambda name: name != 'aarch64'):
            directory, source, manifest = roots[root]
            offered = orchard_assets(directory)
            for _, receipt in list(sources(manifest)):
                artifact = local_asset(directory, receipt)
                if receipt['sha256'] in offered:
                    # orchard's own file: the release links it and doesn't carry a copy.
                    receipt['url'] = offered[receipt['sha256']]
                    del receipt['file']
                    continue
                if artifact.name in metadata_names:
                    raise ValueError('Artifact name collides with release metadata')
                name = hosted_name(artifact.name, root, hosted, receipt['sha256'])
                target = stage / name
                if not target.exists():
                    try:
                        os.link(artifact, target)
                    except OSError:
                        shutil.copyfile(artifact, target)
                del receipt['file']
                receipt['url'] = base + '/' + name
            text = toml(manifest)
            name = hosted_name(source.name, root, hosted, hashlib.sha256(text.encode()).hexdigest())
            target = stage / name
            with target.open('x') as stream:
                stream.write(text)
            pinned = {'url': base + '/' + name, 'bytes': target.stat().st_size, 'sha256': sha(target)}
            if root == 'aarch64' or len(roots) == 1:
                hosted_index['release'] = pinned
            else:
                hosted_index.setdefault('releases', {})[root] = pinned
        (stage / 'index.toml').write_text(toml(hosted_index))
        installers = [directory / 'orchard-install.pyz' for directory, _, _ in roots.values()
                      if (directory / 'orchard-install.pyz').is_file()]
        if installers:
            # The installer carries the published index; its loaders are the local build's.
            from ..installer import loaders_from, release as add_installer
            loaders = loaders_from(installers[0], temporary)
            add_installer(stage, loaders=loaders, installer_url=base + '/orchard-install.pyz')
        stage.rename(output)
    return output / 'index.toml'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release-dir', type=Path, action='append', required=True,
                        help='a built release; repeat with one per root architecture (aarch64, armv7)')
    parser.add_argument('--tag', required=True, help='immutable GitHub release tag, not latest')
    parser.add_argument('--distro', required=True, choices=['omarchy-a', 'minimal-linux'])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(publish(args.release_dir, args.tag, args.output, distro=args.distro))


if __name__ == '__main__':
    main()
