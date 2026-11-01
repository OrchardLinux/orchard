"""Minimal m1n1 USB proxy client: the operations `orchard launch` needs.

Implements m1n1's UART proxy framing (proxyclient/m1n1/proxy.py upstream)
for memory transfer, the host heap, storage init and the boot manager ops
(docs/boot-contract.md). m1n1 reads the boot partition and boots the entry
itself; the host only lists, chooses and starts. Op numbers are fixed in
m1n1's src/proxy.h. No m1n1 Python tree or extra dependency is needed.
"""
import fcntl
import os
import select
import struct
import termios
import time

REQ_NOP, REQ_PROXY, REQ_MEMREAD, REQ_MEMWRITE = 0x00AA55FF, 0x01AA55FF, 0x02AA55FF, 0x03AA55FF
REQ_BOOT, REQ_EVENT = 0x04AA55FF, 0x05AA55FF
CMD_LEN, REPLY_LEN, EVENT_HDR_LEN = 56, 36, 8

P_HEAPBLOCK_ALLOC, P_HEAPBLOCK_SET_LIMIT = 0x600, 0x604
P_MAIN_STORAGE_INIT, P_MAIN_STORAGE_SHUTDOWN = 0xf08, 0xf09
P_BOOT_ENTRIES, P_BOOT_ENTRIES_FREE, P_BOOT_START = 0x1500, 0x1501, 0x1502   # orchard m1n1 0007, 0008; Orchard-local
BOOT_CONTRACT = 1
# An m1n1 without the boot manager ops answers them with "bad command".
NO_BOOT_MANAGER = ('This loader has no boot manager (orchard m1n1 patches 0007-0009); '
                   'update orchard, or rebuild the loader from this checkout')

M1N1_HEAP = 128 << 20   # room left to m1n1's own heap, as proxyclient does
HOST_HEAP = 1 << 30


class M1n1Error(RuntimeError):
    pass


def checksum(data):
    value = 0xDEADBEEF
    for byte in data:
        value = (value * 31337 + (byte ^ 0x5a)) & 0xFFFFFFFF
    return value ^ 0xADDEDBAD


class M1n1Proxy:
    def __init__(self, path, timeout=10.0):
        self.path, self.timeout = path, timeout
        self.fd = None
        self.console = bytearray()   # m1n1 log text that arrives between replies

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            settings = termios.tcgetattr(self.fd)
            settings[0] = settings[1] = settings[3] = 0
            settings[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            settings[4] = settings[5] = termios.B115200
            settings[6][termios.VMIN] = settings[6][termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSANOW, settings)
            termios.tcflush(self.fd, termios.TCIOFLUSH)
            self._command(REQ_NOP, struct.pack('<Q', 0))   # no optional features: keep data checksums
            self._reply(REQ_NOP)
            self.heap = self.call(P_HEAPBLOCK_ALLOC, 0) + M1N1_HEAP
            self.call(P_HEAPBLOCK_SET_LIMIT, self.heap)
            self.heap_top = self.heap + HOST_HEAP
            return self
        except BaseException:
            self.__exit__()
            raise

    def __exit__(self, *_):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    # framing
    def _write(self, data):
        view = memoryview(data)
        while view:
            _, ready, _ = select.select([], [self.fd], [], self.timeout)
            if not ready:
                raise M1n1Error('m1n1 proxy write timed out')
            view = view[os.write(self.fd, view[:65536]):]

    def _read(self, size):
        out = bytearray()
        deadline = time.monotonic() + self.timeout
        while len(out) < size:
            ready, _, _ = select.select([self.fd], [], [], max(0.0, deadline - time.monotonic()))
            if not ready:
                raise M1n1Error(f'm1n1 proxy timed out ({len(out)}/{size} bytes)')
            chunk = os.read(self.fd, size - len(out))
            if not chunk:
                raise M1n1Error('m1n1 proxy port closed')
            out += chunk
        return bytes(out)

    def _command(self, kind, payload=b''):
        frame = struct.pack('<I', kind) + payload.ljust(CMD_LEN, b'\0')
        self._write(frame + struct.pack('<I', checksum(frame)))

    def _reply(self, kind):
        while True:
            # Resynchronise on the ff 55 aa prefix; anything else is m1n1 console text.
            byte = self._read(1)
            if byte != b'\xff':
                self.console += byte
                continue
            head = byte + self._read(3)
            if head[1:3] != b'\x55\xaa':
                self.console += head
                continue
            command = struct.unpack('<I', head)[0]
            if command == REQ_EVENT:
                rest = self._read(EVENT_HDR_LEN - 4)
                length = struct.unpack('<H', rest[:2])[0]
                self._read(length + 4)
                continue
            body = head + self._read(REPLY_LEN - 4)
            status, data, check = struct.unpack('<i24sI', body[4:])
            if check != checksum(body[:-4]):
                raise M1n1Error('m1n1 reply checksum error')
            if command != kind:
                raise M1n1Error(f'm1n1 reply mismatch: {command:#x} for {kind:#x}')
            if status != 0:
                raise M1n1Error(f'm1n1 request failed with status {status}')
            return data

    def call(self, opcode, *args, signed=False):
        args = [a & ((1 << 64) - 1) for a in args] + [0] * (6 - len(args))
        self._command(REQ_PROXY, struct.pack('<7Q', opcode, *args))
        reply = self._reply(REQ_PROXY)
        op, status, value = struct.unpack('<Qq' + ('q' if signed else 'Q'), reply)
        if op != opcode or status != 0:
            raise M1n1Error(f'm1n1 op {opcode:#x} failed (status {status})')
        return value

    def write(self, addr, data):
        self._command(REQ_MEMWRITE, struct.pack('<QQI', addr, len(data), checksum(data)))
        self._write(data)
        self._reply(REQ_MEMWRITE)

    def read(self, addr, size):
        self._command(REQ_MEMREAD, struct.pack('<QQ', addr, size))
        expected = struct.unpack('<I', self._reply(REQ_MEMREAD)[:4])[0]
        data = self._read(size)
        if checksum(data) != expected:
            raise M1n1Error('m1n1 memory read checksum error')
        return data

    # host-managed heap above m1n1's own, as proxyclient does
    def memalign(self, align, size):
        addr = (self.heap + align - 1) & ~(align - 1)
        if addr + size > self.heap_top:
            raise M1n1Error('m1n1 host heap exhausted')
        self.heap = addr + size
        return addr

    # storage
    def storage_init(self):
        if not self.call(P_MAIN_STORAGE_INIT):
            raise M1n1Error('This loader has no storage support for the device '
                            '(m1n1 main_storage_init failed)')

    def storage_shutdown(self):
        self.call(P_MAIN_STORAGE_SHUTDOWN)

    def boot_entries(self, devicetree=None):
        """m1n1's own reading of the boot partition: (id, keys, problems) per entry, in BLS order.

        Needs storage_init first. The reply format is in docs/boot-contract.md.
        """
        out = self.memalign(8, 16)
        name = self.string(devicetree) if devicetree else 0
        try:
            failed = self.call(P_BOOT_ENTRIES, out, name, signed=True)
        except M1n1Error as error:
            raise M1n1Error(NO_BOOT_MANAGER) from error
        addr, size = struct.unpack('<QQ', self.read(out, 16))
        if failed or not addr:
            raise M1n1Error('m1n1 could not list the boot entries')
        try:
            text = self.read(addr, size).decode('utf-8', 'replace') if size else ''
        finally:
            self.call(P_BOOT_ENTRIES_FREE, out)
        return parse_boot_entries(text)

    def boot_start(self, entry_id, devicetree=None, timeout=120.0):
        """m1n1 loads entry_id from storage, shuts storage down and boots it (orchard m1n1 patch 0008).

        m1n1 replies before the jump. On failure it stays in the proxy; the reason is on its console.
        """
        name, tree = self.string(entry_id), self.string(devicetree) if devicetree else 0
        mark, saved = len(self.console), self.timeout
        self.timeout = timeout
        try:
            failed = self.call(P_BOOT_START, name, tree, 0, signed=True)
        except M1n1Error as error:
            if 'status -1' in str(error):
                raise M1n1Error(NO_BOOT_MANAGER) from error
            raise
        finally:
            self.timeout = saved
        if failed:
            lines = self.console[mark:].decode('utf-8', 'replace').splitlines()
            reasons = [line.split(': ', 1)[1] for line in lines if line.startswith(('bls: ', 'bootmgr: '))]
            storage = [line for line in lines if line.startswith(('main_storage_read', 'apcie-nvme', 'nvme'))]
            raise M1n1Error('m1n1 could not start the entry: ' + ('; '.join(reasons[-2:]) or 'see its console')
                            + (' (storage: ' + '; '.join(storage[-4:]) + ')' if storage else ''))

    def string(self, text):
        data = text.encode() + b'\0'
        addr = self.memalign(8, len(data))
        self.write(addr, data)
        return addr

def parse_boot_entries(text):
    """The text of P_BOOT_ENTRIES: [(id, {key: [values]}, [problems])]; an `error` line raises."""
    lines = text.splitlines()
    if not lines or lines[0] != f'contract {BOOT_CONTRACT}':
        raise M1n1Error('This loader speaks another boot contract: ' + (lines[0] if lines else 'no reply'))
    entries = []
    for line in lines[1:]:
        if not line:
            continue
        key, _, value = line.partition(' ')
        if key == 'error':
            raise M1n1Error(value)
        if key == 'entry':
            entries.append((value, {}, []))
        elif not entries:
            raise M1n1Error(f'Unexpected boot entry line {line!r}')
        elif key == 'problem':
            entries[-1][2].append(value)
        else:
            entries[-1][1].setdefault(key, []).append(value)
    return entries
