"""Read host-private firmware keys; never obtain, log, or persist keys here.

The launcher writes firmware-keys.json from keys it gets from the device;
orchard only reads it (contract: docs/firmware-recipes.md, "Legacy image
keys"). ORCHARD_FIRMWARE_KEYS names another file, for local testing.
"""
import json
import os
from pathlib import Path
import stat

from orchard.cache import cache_root


class KeyStore:
    def __init__(self, ipsw_sha256, path=None):
        self.ipsw_sha256 = ipsw_sha256
        self.path = Path(path or os.environ.get('ORCHARD_FIRMWARE_KEYS') or
                         cache_root() / 'firmware-keys.json')
        self.document = None

    def get(self, member, kind):
        if self.document is None:
            try:
                fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except OSError:
                raise ValueError('Firmware keys unavailable: use the launcher key cache or set '
                                 'ORCHARD_FIRMWARE_KEYS to an owner-private key file') from None
            with os.fdopen(fd, 'r') as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                        info.st_mode & 0o077 or info.st_size > 1 << 20):
                    raise ValueError('Firmware key file must be an owner-private regular file (0600)')
                try:
                    document = json.load(stream)
                except (ValueError, UnicodeError):
                    raise ValueError('Invalid firmware key file') from None
            if not isinstance(document, dict) or document.get('schema') != 1 or not isinstance(document.get('ipsws'), dict):
                raise ValueError('Unsupported firmware key cache format')
            self.document = document
        try:
            entry = self.document['ipsws'][self.ipsw_sha256][member]
            if entry['kind'] != kind:
                raise ValueError()
            key = bytes.fromhex(entry['key'])
            if kind == 'encrcdsa' and len(key) == 36:
                return key
            if kind == 'img3' and len(key) in (16, 24, 32):
                iv = bytes.fromhex(entry['iv'])
                if len(iv) == 16:
                    return key, iv
        except (KeyError, TypeError, ValueError):
            pass
        raise ValueError('Missing or invalid firmware key for ' + member + ' (' + kind + ')')
