"""Files from a remote IPSW, read over HTTP byte ranges.

A restore IPSW is ~7 GB; a device's firmware inputs are ~20-40 MB of it. The ZIP
central directory locates members; the stored system DMG (UDIF) is read chunk
by chunk and files come out of its APFS volume. Standard library only: no IPSW
download, no extraction tool and no FUSE on Linux. Callers verify every result
against the recipe's hashes.
"""
import plistlib
import re
import struct
import urllib.request
import zlib

from orchard.http import open_url
from . import apfs, hfs, legacy_images, lzfse, lzss, sparse_deflate, udif

ATTEMPTS = 3


class RemoteFile:
    """Byte ranges of one HTTPS file whose size the caller pinned."""

    def __init__(self, url, size, on_bytes=None):
        """size None takes the server's: for exploring an IPSW no recipe pins yet."""
        self.url, self.size, self.on_bytes = url, size, on_bytes
        self.downloaded = 0
        request = urllib.request.Request(url, method='HEAD')
        with open_url(request) as response:
            actual = int(response.headers.get('Content-Length', -1))
        if self.size is None:
            self.size = actual
        elif actual != size:
            raise ValueError('Remote IPSW size differs from the recipe')

    def read(self, offset, length):
        if length <= 0:
            return b''
        end = offset + length - 1
        if not 0 <= offset <= end < self.size:
            raise ValueError('Range outside the IPSW')
        for attempt in range(ATTEMPTS):
            request = urllib.request.Request(self.url, headers={
                'Range': f'bytes={offset}-{end}', 'Accept-Encoding': 'identity'})
            try:
                with open_url(request) as response:
                    if (response.status != 206 or
                            response.headers.get('Content-Range') != f'bytes {offset}-{end}/{self.size}'):
                        raise ValueError('Server did not honour the byte range')
                    data = response.read(length + 1)
            except OSError:
                if attempt == ATTEMPTS - 1:
                    raise
                continue
            if len(data) != length:
                raise ValueError('Short byte range from server')
            self.downloaded += length
            if self.on_bytes:
                self.on_bytes(self.downloaded)
            return data

    def read_ranges(self, ranges):
        """One bounded multipart request for sparse DEFLATE header windows.

        Every part's range and exact size must match the request. Never accept
        a 200/full-file response or allow MIME overhead to hide a large body.
        """
        if not ranges or len(ranges) > 128:
            raise ValueError('Invalid range batch size')
        expected = {}
        for offset, length in ranges:
            if length <= 0 or not 0 <= offset < offset + length <= self.size:
                raise ValueError('Range outside the IPSW')
            expected[offset, offset + length - 1] = length
        if len(expected) != len(ranges):
            raise ValueError('Duplicate byte ranges')
        request = urllib.request.Request(self.url, headers={
            'Range': 'bytes=' + ','.join(f'{a}-{b}' for a, b in expected),
            'Accept-Encoding': 'identity'})
        limit = sum(expected.values()) + 1024 * (len(ranges) + 1)
        for attempt in range(ATTEMPTS):
            try:
                with open_url(request) as response:
                    boundary = response.headers.get_param('boundary') if hasattr(response.headers, 'get_param') else None
                    if response.status != 206 or not boundary or response.headers.get_content_type() != 'multipart/byteranges':
                        raise ValueError('Server did not honour multipart byte ranges')
                    if not re.fullmatch(r'[A-Za-z0-9\x27()+_,./:=? -]{1,70}', boundary):
                        raise ValueError('Invalid multipart boundary')
                    raw = response.read(limit + 1)
                break
            except OSError:
                if attempt == ATTEMPTS - 1:
                    raise
        if len(raw) > limit:
            raise ValueError('Multipart range response exceeds bound')
        marker = b'--' + boundary.encode('ascii')
        pos = 2 if raw.startswith(b'\r\n') else 0
        parts, closed = {}, False
        while raw[pos:pos + len(marker)] == marker:
            pos += len(marker)
            if raw[pos:pos + 2] == b'--':
                pos += 2
                closed = True
                break
            if raw[pos:pos + 2] != b'\r\n':
                raise ValueError('Invalid multipart separator')
            pos += 2
            end = raw.find(b'\r\n\r\n', pos, pos + 1024)
            if end < 0:
                raise ValueError('Invalid multipart headers')
            matches = re.findall(rb'(?im)^Content-Range: bytes (\d+)-(\d+)/(\d+)\r?$', raw[pos:end])
            if len(matches) != 1:
                raise ValueError('Missing or ambiguous multipart Content-Range')
            first, last, total = map(int, matches[0])
            pair = first, last
            if total != self.size or pair not in expected or pair in parts:
                raise ValueError('Unexpected multipart byte range')
            pos = end + 4
            length = expected[pair]
            parts[pair] = raw[pos:pos + length]
            pos += length
            if raw[pos:pos + 2] != b'\r\n':
                raise ValueError('Truncated multipart payload')
            pos += 2
        if not closed or len(parts) != len(expected) or raw[pos:].strip(b'\r\n'):
            raise ValueError('Incomplete multipart byte ranges')
        self.downloaded += sum(map(len, parts.values()))
        if self.on_bytes:
            self.on_bytes(self.downloaded)
        return [parts[offset, offset + length - 1] for offset, length in ranges]


class RemoteZip:
    """The members of a ZIP archive, by name (restore IPSWs are ZIP64)."""

    def __init__(self, remote):
        self.remote = remote
        tail_size = min(remote.size, 65536 + 22)
        tail = remote.read(remote.size - tail_size, tail_size)
        locator = tail.rfind(b'PK\x06\x07')
        if locator >= 0:
            record = remote.read(struct.unpack_from('<Q', tail, locator + 8)[0], 56)
            if record[:4] != b'PK\x06\x06':
                raise ValueError('Bad ZIP64 end record')
            count, size, offset = struct.unpack_from('<QQQ', record, 32)
        else:
            end = tail.rfind(b'PK\x05\x06')
            if end < 0:
                raise ValueError('IPSW is not a ZIP archive')
            count, size, offset = struct.unpack_from('<HII', tail, end + 10)
        directory = remote.read(offset, size)
        self.members = {}
        p = 0
        for _ in range(count):
            if directory[p:p + 4] != b'PK\x01\x02':
                raise ValueError('Bad ZIP central directory entry')
            method = struct.unpack_from('<H', directory, p + 10)[0]
            csize, usize = struct.unpack_from('<II', directory, p + 20)
            nlen, elen, clen = struct.unpack_from('<HHH', directory, p + 28)
            header = struct.unpack_from('<I', directory, p + 42)[0]
            name = directory[p + 46:p + 46 + nlen].decode()
            extra = directory[p + 46 + nlen:p + 46 + nlen + elen]
            e = 0
            while e + 4 <= len(extra):
                tag, length = struct.unpack_from('<HH', extra, e)
                if tag == 1:  # ZIP64 sizes and offset, present only for saturated fields
                    values = list(struct.unpack_from('<' + 'Q' * (length // 8), extra, e + 4))
                    if usize == 0xFFFFFFFF:
                        usize = values.pop(0)
                    if csize == 0xFFFFFFFF:
                        csize = values.pop(0)
                    if header == 0xFFFFFFFF:
                        header = values.pop(0)
                e += 4 + length
            self.members[name] = (method, csize, usize, header)
            p += 46 + nlen + elen + clen

    def data_offset(self, name):
        header = self.members[name][3]
        local = self.remote.read(header, 30)
        if local[:4] != b'PK\x03\x04':
            raise ValueError('Bad ZIP local header')
        return header + 30 + sum(struct.unpack_from('<HH', local, 26))

    def read(self, name):
        method, csize, usize, _ = self.members[name]
        raw = self.remote.read(self.data_offset(name), csize)
        if method == 0:
            data = raw
        elif method == 8:
            data = zlib.decompress(raw, -15)
        else:
            raise ValueError(f'Unsupported ZIP method {method}')
        if len(data) != usize:
            raise ValueError('ZIP member size differs: ' + name)
        return data


class Member:
    """Random access to a stored (uncompressed) ZIP member."""

    def __init__(self, archive, name):
        method, size, _, _ = archive.members[name]
        if method != 0:
            raise ValueError('Compressed member has no random access: ' + name)
        self.remote, self.base, self.size = archive.remote, archive.data_offset(name), size

    def read(self, offset, length):
        if not 0 <= offset <= offset + length <= self.size:
            raise ValueError('Read outside ZIP member')
        return self.remote.read(self.base + offset, length)


class PackedMember:
    """The compressed byte range of a member, without inflating it."""
    def __init__(self, archive, name):
        self.remote = archive.remote
        self.base = archive.data_offset(name)
        self.size = archive.members[name][1]

    def read(self, offset, length):
        if not 0 <= offset <= offset + length <= self.size:
            raise ValueError('Read outside compressed ZIP member')
        return self.remote.read(self.base + offset, length)

    def read_ranges(self, ranges):
        if any(not 0 <= offset <= offset + length <= self.size for offset, length in ranges):
            raise ValueError('Range outside compressed ZIP member')
        return self.remote.read_ranges([(self.base + offset, length) for offset, length in ranges])


class Ipsw:
    def __init__(self, url, size, on_bytes=None, *, keys=None):
        self.keys = keys
        self.remote = RemoteFile(url, size, on_bytes)
        self.archive = RemoteZip(self.remote)
        self._volume = None

    def member(self, name):
        return self.archive.read(name)

    def volume(self):
        """The system volume in the largest DMG (APFS, or legacy encrypted HFS+)."""
        if self._volume is None:
            images = [n for n in self.archive.members if n.lower().endswith('.dmg')]
            if not images:
                raise ValueError('No system DMG in the IPSW')
            name = max(images, key=lambda n: self.archive.members[n][2])
            method, _, size, _ = self.archive.members[name]
            if method == 0:
                source = Member(self.archive, name)
            elif method == 8:
                source = sparse_deflate.StoredDeflate(PackedMember(self.archive, name), size)
            else:
                raise ValueError('Unsupported root image ZIP compression')
            if source.read(0, 8) == b'encrcdsa':
                source = legacy_images.Encrcdsa(source, self._key(name, 'encrcdsa'))
            image = udif.Udif(source)
            partitions = [p for p in image.partitions if 'APFS' in p[0] or 'Apple_HFS' in p[0]]
            if len(partitions) != 1:
                raise ValueError('Expected one APFS or HFS partition in the system image')
            partition = partitions[0]
            if 'Apple_HFS' in partition[0]:
                self._volume = hfs.Volume(image, partition[1] * udif.SECTOR)
            else:
                self._volume = apfs.Volume(apfs.Container(image, partition[1] * udif.SECTOR))
        return self._volume

    def system_file(self, path):
        volume = self.volume()
        result = volume.read(path)
        return result if isinstance(volume, hfs.Volume) else result[0]

    def kernelcache(self, device_class):
        """The decompressed kernelcache Mach-O of a board, e.g. 'j71bap'."""
        name = self._manifest_path(device_class, 'KernelCache')
        blob = self.member(name)
        if blob[:4] == b'3gmI':
            key, iv = self._key(name, 'img3')
            return legacy_images.img3_kernelcache(blob, key, iv)
        return im4p_macho(blob)

    def _key(self, member, kind):
        if self.keys is None:
            raise ValueError('Encrypted IPSW needs a host firmware key provider')
        return self.keys.get(member, kind)

    def device_tree(self, device_class):
        """The board's device tree as iBoot receives it: the ADT minus what iBoot fills in."""
        return im4p_payload(self.member(self._manifest_path(device_class, 'DeviceTree')))

    def _manifest_path(self, device_class, component):
        manifest = plistlib.loads(self.member('BuildManifest.plist'))
        paths = {identity['Manifest'][component]['Info']['Path']
                 for identity in manifest['BuildIdentities']
                 if identity['Info'].get('DeviceClass', '').lower() == device_class}
        if len(paths) != 1:
            raise ValueError(f'Expected one {component} for {device_class}, found {len(paths)}')
        return paths.pop()


def im4p_payload(blob):
    """The payload of an unencrypted IM4P, decompressed (LZFSE, or LZSS on older releases)."""
    def item(pos):
        tag, length = blob[pos], blob[pos + 1]
        pos += 2
        if length & 0x80:
            count = length & 0x7F
            length = int.from_bytes(blob[pos:pos + count], 'big')
            pos += count
        return tag, pos, pos + length

    tag, pos, end = item(0)
    if tag != 0x30:
        raise ValueError('Not a DER sequence')
    fields = []
    while pos < end and len(fields) < 4:
        tag, start, stop = item(pos)
        fields.append((tag, blob[start:stop]))
        pos = stop
    if len(fields) < 4 or fields[0] != (0x16, b'IM4P') or fields[3][0] != 0x04:
        raise ValueError('Not an IM4P')
    payload = fields[3][1]
    if payload[:8] == b'complzss':  # older kernelcaches, e.g. iPadOS 16 on A9
        return lzss.decompress(payload)
    if payload[:4] in (b'bvx2', b'bvx1', b'bvx-', b'bvxn'):
        return lzfse.decompress(payload)
    return payload


def im4p_macho(blob):
    """The Mach-O inside a compressed IM4P kernelcache."""
    macho = im4p_payload(blob)
    if macho[:4] == b'\xca\xfe\xba\xbe':  # one-slice fat wrapper
        if struct.unpack_from('>I', macho, 4)[0] != 1:
            raise ValueError('Fat kernelcache with several slices')
        offset, size = struct.unpack_from('>II', macho, 16)
        macho = macho[offset:offset + size]
    if macho[:4] != b'\xcf\xfa\xed\xfe':
        raise ValueError('Kernelcache is not a 64-bit Mach-O')
    return macho


def main(argv=None):
    """Explore an IPSW during bring-up, without downloading it (docs: hardware docs/firmware-recipes.md)."""
    import argparse
    import hashlib
    from pathlib import Path
    parser = argparse.ArgumentParser(prog='python -m orchard.apple.remote_ipsw', description=main.__doc__)
    parser.add_argument('ipsw', help='IPSW URL, or a device profile (j71b) to use its recipe\'s IPSW')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('members', help='the ZIP members with their sizes')
    listing = commands.add_parser('ls', help='a directory of the system volume')
    listing.add_argument('path', nargs='?', default='')
    for name, what in (('get', 'a system-volume file'), ('member', 'a ZIP member')):
        command = commands.add_parser(name, help=f'save {what}; prints its size and sha256')
        command.add_argument('path')
        command.add_argument('output', type=Path)
    kernel = commands.add_parser('kernelcache', help="save a board's decompressed kernelcache Mach-O")
    kernel.add_argument('device_class', help='BuildManifest DeviceClass, e.g. j71bap')
    kernel.add_argument('output', type=Path)
    tree = commands.add_parser('dt', help="print a board's device-tree nodes whose path contains a word")
    tree.add_argument('device_class', help='BuildManifest DeviceClass, e.g. j81ap')
    tree.add_argument('words', nargs='+', help='e.g. wlan bluetooth multi-touch')
    args = parser.parse_args(argv)
    url, size, keys = args.ipsw, None, None
    if not url.startswith('https://'):
        from orchard.firmware import recipe_for
        source = recipe_for(url)['ipsw']
        url, size = source['url'], source['bytes']
        from .firmware_keys import KeyStore
        keys = KeyStore(source['sha256'])
    ipsw = Ipsw(url, size, keys=keys)
    if args.command == 'members':
        for name, (_, packed, unpacked, _) in sorted(ipsw.archive.members.items()):
            print(f'{unpacked:>12}  {name}')
        return
    if args.command == 'ls':
        volume = ipsw.volume()
        if isinstance(volume, hfs.Volume):
            raise ValueError('HFS directory listing is not implemented; use get with an exact path')
        oid = volume.lookup(args.path) if args.path.strip('/') else 2
        for name, _, kind in sorted(volume.children(oid)):
            print(name + ('/' if kind == 4 else ''))
        return
    if args.command == 'dt':
        from orchard.apple.adt_tree import parse
        for path, node in parse(ipsw.device_tree(args.device_class)).walk():
            if any(word in path for word in args.words):
                print(path)
                for name, value in node.properties.items():
                    text = value.rstrip(b'\0')
                    if text and all(32 <= c < 127 or c == 0 for c in text):
                        shown = text.replace(b'\0', b'|').decode()
                    else:
                        shown = value[:32].hex() + ('...' if len(value) > 32 else '')
                    print(f'  {name:32} {shown}')
        return
    if args.command == 'kernelcache':
        data = ipsw.kernelcache(args.device_class)
    else:
        data = ipsw.system_file(args.path) if args.command == 'get' else ipsw.member(args.path)
    args.output.write_bytes(data)
    print(f'{len(data)} bytes  sha256 {hashlib.sha256(data).hexdigest()}  -> {args.output}')
    print(f'read {ipsw.remote.downloaded / 1e6:.1f} MB of the IPSW')


if __name__ == '__main__':
    main()
