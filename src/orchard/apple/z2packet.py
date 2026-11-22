"""Z2FW for multitouch profiles that carry one preconstructed Z2 boot packet.

Some profiles (iPad 5: C1F15,1) carry one complete, prefixed boot packet as
"Constructed Firmware" with PreconstructedBootloadPacketType "Z2" instead of
HBPP load items. The packet is checked and wrapped unmodified into a Z2FW
container: SPI mode, the unit's calibration (sent before the firmware, as
the iPad 5 bootloader expects), then the packet.
"""
import plistlib
import struct

from orchard.apple import z2fw

# SPI mode bits in the Z2FW config record's valid mask (apple_z2.c).
CONFIG_CPHA, CONFIG_CPOL = 1 << 3, 1 << 4
CONFIG_HBPP_VERSION = 1 << 11

PREFIX = b'\x18\xe1\x30\x01'


def halfword32(data: bytes) -> int:
    return int.from_bytes(bytes((data[1], data[0], data[3], data[2])), 'little')


def validate(packet: bytes) -> None:
    if len(packet) < 20 or packet[:4] != PREFIX:
        raise ValueError('Not a prefixed Z2 boot packet')
    if int.from_bytes(packet[4:6], 'big') * 4 + 16 != len(packet):
        raise ValueError('Z2 boot packet length mismatch')
    if halfword32(packet[6:10]):
        raise ValueError('Unexpected Z2 boot packet load address')
    if sum(packet[4:10]) != int.from_bytes(packet[10:12], 'big'):
        raise ValueError('Z2 boot packet header checksum mismatch')
    if sum(packet[12:-4]) & 0xffffffff != halfword32(packet[-4:]):
        raise ValueError('Z2 boot packet payload checksum mismatch')


def extract(mtprops: bytes, profile: str) -> bytes:
    entry = plistlib.loads(mtprops).get(profile)
    if not isinstance(entry, dict) or entry.get('PreconstructedBootloadPacketType') != 'Z2':
        raise ValueError('No preconstructed Z2 packet in profile ' + profile)
    packet = entry.get('Constructed Firmware')
    if not isinstance(packet, bytes):
        raise ValueError('Profile has no constructed firmware: ' + profile)
    validate(packet)
    return packet


def convert(mtprops: bytes, profile: str, calibration_address: int, calibration_size: int) -> bytes:
    packet = extract(mtprops, profile)
    config = [CONFIG_CPHA | CONFIG_CPOL] + [0] * 10  # SPI mode 0; other fields from the driver
    records = [(z2fw.CONFIG, struct.pack('<11I', *config)),
               z2fw.calibration_record('multi-touch-calibration', calibration_address,
                                       calibration_size, []),
               (z2fw.SEND, packet)]
    return z2fw.container(records)


def convert_n1(mtprops: bytes, profile: str, calibration_address: int,
               calibration_size: int, prox_address: int, prox_size: int) -> bytes:
    """N1's legacy packet, zero proximity initialization and dynamic unit data.

    The native N1 driver owns reset, readiness, register calibration and
    execute; this file contains no INIT, register-write or execute commands.
    Unlike HBPP14, the packet word count includes every payload word.
    """
    document = plistlib.loads(mtprops)
    if not isinstance(document, dict):
        raise ValueError('Expected N1 mtprops profile dictionary')
    entry = document.get(profile)
    if isinstance(entry, dict) and 'Firmware Config' in entry:
        raise ValueError('N1 packet with an unsupported firmware config')
    packet = extract(mtprops, profile)
    firmware_address, firmware = z2fw.decode_blob(packet, hbpp_version=0x103)
    providers = []
    # Validate both ranges together, including overlap and address overflow.
    z2fw.calibration_record('prox-calibration', prox_address, prox_size, providers)
    calibration = z2fw.calibration_record('multi-touch-calibration', calibration_address,
                                         calibration_size, providers)
    prox_address = providers[0]['address']
    prox_size = providers[0]['maximum']
    if any(item['address'] % 4 or item['maximum'] % 4 for item in providers):
        raise ValueError('N1 calibration ranges must be word aligned')
    if any(item['address'] < firmware_address + len(firmware) and
           firmware_address < item['address'] + ((item['maximum'] + 3) & ~3)
           for item in providers):
        raise ValueError('N1 calibration overlaps the firmware load range')
    # The legacy length field is u16, in words. Do not wrap it.
    if prox_size // 4 > 0xffff:
        raise ValueError('N1 proximity initialization is too large')
    header = struct.pack('>H', prox_size // 4) + z2fw.xe32(prox_address)
    prox = PREFIX + header + struct.pack('>H', sum(header)) + bytes(prox_size + 4)
    validate_prox_address, validate_prox = z2fw.decode_blob(prox, hbpp_version=0x103)
    if validate_prox_address != prox_address or validate_prox != bytes(prox_size):
        raise ValueError('N1 proximity initialization roundtrip mismatch')
    config = [CONFIG_CPHA | CONFIG_CPOL | CONFIG_HBPP_VERSION] + [0] * 9 + [0x103]
    return z2fw.container([(z2fw.CONFIG, struct.pack('<11I', *config)),
                           (z2fw.SEND, prox), calibration, (z2fw.SEND, packet)])
