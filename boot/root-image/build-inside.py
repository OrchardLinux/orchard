#!/usr/bin/env python3
"""A root archive -> ext4 -> the allocated blocks as one zstd stream, and their map; run as root.

    build-inside.py ROOTFS OUTPUT_DIRECTORY

The tree is unpacked under ORCHARD_WORK. The map's ranges name which 4 KiB blocks of the image the
stream holds; blocks it leaves out are free in the filesystem and aren't written, so one image fits
every partition size (src/orchard/image/root_image.py).
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

BLOCK = 4096
# Written as the image's own settings, so no build host's mke2fs.conf decides the features of a root.
CONFIG = '''[defaults]
\tbase_features = sparse_super,large_file,filetype,resize_inode,dir_index,ext_attr
\tdefault_mntopts = acl,user_xattr
\tenable_periodic_fsck = 0
\tblocksize = 4096
\tinode_size = 256
\tinode_ratio = 16384
[fs_types]
\text4 = {
\t\tfeatures = has_journal,extent,huge_file,flex_bg,metadata_csum,metadata_csum_seed,64bit,dir_nlink,extra_isize,orphan_file
\t}
'''


def output_of(*argv, **options):
    return subprocess.run(argv, check=True, capture_output=True, **options).stdout.decode()


def allocated(image):
    """(all blocks, used [start, count] ranges) from dumpe2fs, checked against the superblock's free count."""
    text = output_of('dumpe2fs', str(image))
    total = int(re.search(r'^Block count:\s+(\d+)', text, re.M)[1])
    free_count = int(re.search(r'^Free blocks:\s+(\d+)', text, re.M)[1])
    free = []
    for line in re.findall(r'^\s+Free blocks: (.*)$', text, re.M):
        for part in filter(None, (word.strip() for word in line.split(','))):
            low, _, high = part.partition('-')
            free.append((int(low), int(high or low)))
    free.sort()
    if sum(high - low + 1 for low, high in free) != free_count:
        raise SystemExit('The group listing differs from the superblock')
    ranges, position = [], 0
    for low, high in free:
        if low > position:
            ranges.append([position, low - position])
        position = high + 1
    if position < total:
        ranges.append([position, total - position])
    return total, ranges


def compress(image, ranges, output):
    with output.open('wb') as stream:
        process = subprocess.Popen(['zstd', '-q', '-T0', '-19', '-c'], stdin=subprocess.PIPE, stdout=stream)
        with image.open('rb') as source:
            for start, count in ranges:
                source.seek(start * BLOCK)
                remaining = count * BLOCK
                while remaining:
                    data = source.read(min(4 << 20, remaining))
                    process.stdin.write(data)
                    remaining -= len(data)
        process.stdin.close()
        if process.wait():
            raise SystemExit('zstd failed')


def receipt(path):
    with path.open('rb') as stream:
        return {'file': path.name, 'bytes': path.stat().st_size, 'sha256': hashlib.file_digest(stream, 'sha256').hexdigest()}


def main():
    source, output = Path(sys.argv[1]), Path(sys.argv[2])
    work = Path(os.environ['ORCHARD_WORK'])
    stage, image, config = work / 'stage', work / 'root.img', work / 'mke2fs.conf'
    try:
        stage.mkdir()
        os.chmod(stage, 0o755)
        subprocess.run(['tar', '--numeric-owner', '--same-owner', '-xpf', str(source), '-C', str(stage)], check=True)
        members = sum(1 + len(names) + len(files) for _, names, files in os.walk(stage)) - 1
        used = int(output_of('du', '-s', '--block-size=1', str(stage)).split()[0])
        # Room for the filesystem's own structures; the device grows it to the partition.
        blocks = (used + used // 4 + (512 << 20)) // BLOCK
        config.write_text(CONFIG)
        output_of('mke2fs', '-q', '-F', '-t', 'ext4', '-b', str(BLOCK), '-m', '0', '-L', 'root',
                  '-E', 'nodiscard,lazy_itable_init=0,lazy_journal_init=0', '-d', str(stage), str(image), str(blocks),
                  env={**os.environ, 'MKE2FS_CONFIG': str(config)})
        shutil.rmtree(stage)
        output_of('e2fsck', '-fn', str(image))
        total, ranges = allocated(image)
        restored = sum(count for _, count in ranges) * BLOCK
        map_file = output / 'root.map.json'
        map_file.write_text(json.dumps({'format': 1, 'block_size': BLOCK, 'blocks': total,
                                        'restored_bytes': restored, 'ranges': ranges}))
        stream = output / 'root.blocks.zst'
        compress(image, ranges, stream)
        document = {**receipt(stream), 'compression': 'zstd', 'format': 'blocks', 'expanded_bytes': restored,
                    'members': members, 'map': receipt(map_file)}
        record = output / 'root-image.json'
        record.write_text(json.dumps(document, indent=2) + '\n')
        owner = output.stat()
        for path in (map_file, stream, record):
            os.chown(path, owner.st_uid, owner.st_gid)
    finally:
        # Under sudo on CI this tree is root's: the unprivileged caller couldn't remove it.
        shutil.rmtree(stage, ignore_errors=True)
        image.unlink(missing_ok=True)
        config.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
