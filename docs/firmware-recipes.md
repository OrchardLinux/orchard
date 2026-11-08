# Firmware recipes

Apple's firmware can't be redistributed, so each user's Orchard tooling
extracts it locally. A device's `firmware.json` (schema 2,
`schemas/v1/firmware.schema.json`) describes where every input comes from
and which declarative steps turn it into an installed file. It contains no
code: the steps come from a fixed, versioned vocabulary that the Orchard CLI
implements.

## Structure

- `device`: the Apple product (`iPad7,5`).
- `ipsw`: the pinned restore image (version, build, URL, size, sha256).
- `inputs`: named sources. Each lists where to find it:
  - `ipsw`: `{"rootfs": path}`, `{"zip": member}` or `{"kernelcache": true}`;
  - `device` (optional): the same file's path on a running iPadOS, or
    `{"kernelcache": "active"}`.
  An optional `sha256` pins the input.
- `outputs`: installed files. Each has a `destination` under
  `lib/firmware/`, a pinned `sha256`, and `steps`. The first step names an
  `input`; each later step takes the previous step's result instead. A step
  may also name a documented auxiliary input (`trx-add-nvram@1`'s `nvram`);
  it is read once and checked against its own input hash.
  An output nobody has made from the real inputs yet has `pending` (why) in
  place of `sha256`. Builds don't install it, and every input it reads must
  be pinned. Whoever has the inputs measures it and pins the hash:

  ```sh
  python -c "from orchard import firmware
  from orchard.progress import Clock, Progress
  print(firmware.measure('<profile>', Clock(), Progress(), hardware='.'))"
  ```

The host reads `ipsw` sources. The in-app installer reads `device` sources
when a recipe lists them, and falls back to the IPSW when an input is
missing there or its hash differs. Every output must match its pinned hash
either way. Per-unit data (MAC addresses, Wi-Fi, Bluetooth and touch
calibration) never goes into public firmware files. On supported 64-bit
boards, m1n1 copies it from the ADT at boot. Firmware that must carry an
address carries the placeholder `02:00:00:00:00:00` instead
(`trx-add-nvram@1` accepts no other). The platform layer replaces it at boot
on every interface that reports it, before anything brings the interface up
(`system/defaults/usr/lib/udev/rules.d/70-orchard-wifi-address.rules`): with
a stable, locally administered address derived from the device tree's
`serial-number`, or, where the boot stage passes none, a random one for that
boot.

## How the CLI reads an IPSW

The CLI never downloads the whole IPSW (about 7 GB). It reads only the byte
ranges it needs from Apple's server, about 20-40 MB per device
(`src/orchard/apple/remote_ipsw.py`, standard library only):

1. The ZIP central directory at the end of the file lists the members.
2. `{"zip": member}` inputs are read and inflated directly.
3. `{"rootfs": path}` inputs come from the largest DMG, which IPSWs store
   uncompressed. Its UDIF trailer maps the disk's chunks (zlib or LZFSE,
   `udif.py`); only the chunks a lookup touches are read. The APFS reader
   (`apfs.py`) walks the system volume's B-trees to the file and undoes
   decmpfs compression (zlib, LZVN, LZFSE).
4. `{"kernelcache": true}` takes the kernelcache that `BuildManifest.plist`
   names for the board's `DeviceClass` (`<profile>ap`), unwraps the IM4P and
   decompresses it: LZFSE, or LZSS (`complzss`) on older releases such as
   iPadOS 16 on A9. Legacy IMG3 kernelcaches use host-supplied keys before
   LZSS decompression.

P105's iOS 8 root image is different: encrypted `encrcdsa` inside a
DEFLATE-compressed ZIP member, with an HFS+ partition inside UDIF. The sparse
reader indexes stored DEFLATE block headers after a compressed prefix. It
batches small header windows using HTTP multipart ranges; predictions only
prefetch bytes, and every boundary is checked against the preceding block's
length. Unexpected compressed tail blocks are refused rather than triggering
a full-image download. A cold index still needs hundreds of range requests.
HFS catalog, attribute and overflow trees are sought by numeric key prefix,
not scanned from beginning to end. Firmware files may use zlib decmpfs.

The whole IPSW's sha256 can't be checked this way; its size is checked
against the recipe, and every pinned input and every output against its hash.

## Finding firmware during bring-up

The same reader explores an IPSW by hand, by URL or by the IPSW of a device
whose recipe already exists:

```sh
R="python -m orchard.apple.remote_ipsw"
$R https://updates.cdn-apple.com/…/iPad_…_Restore.ipsw members   # ZIP members
$R j71b ls usr/share/firmware/multitouch                           # a system directory
$R j71b get usr/share/firmware/multitouch/J71b.mtprops J71b.mtprops
$R j71b kernelcache j71bap kernelcache.macho                       # decompressed Mach-O
$R j81 dt j81ap wlan bluetooth multi-touch                         # device-tree nodes
```

`get`, `member` and `kernelcache` print the file's size and sha256 for the
recipe. Where a new device's firmware usually is:

| Firmware | Location |
|---|---|
| Wi-Fi (Broadcom, iOS 16 and later) | `System/Library/DriverExtensions/com.apple.DriverKit-AppleBCMWLAN.dext/Firmware/<chip>/` |
| Wi-Fi (iOS 15 and earlier) | `usr/share/firmware/wifi/<chip>/` |
| Bluetooth | patchram embedded in `usr/sbin/BlueTool` (`bluetool-hcd@1`) |
| Touch | `usr/share/firmware/multitouch/<Board>.mtprops` |
| SmartIO, video decoder | the kernelcache (`smartio-from-kernelcache@1`, `im4p-payload@1`) |

Then:

1. Pick the IPSW. api.ipsw.me gives its URL and size; pin the newest
   release the reader can open. iPadOS 18 and later encrypt the IPSW (AEA),
   so those can't be read.
2. Read the board's device tree with `dt`. It is the ADT without what iBoot
   fills in (serials, MAC addresses, calibration), so board facts come from
   the IPSW and need no device: the Wi-Fi `module-instance` is
   brcmfmac's board type (D10 `kristoff`, J81 `rieslinga`), and the chip is
   in `compatible`. BlueTool's embedded patchrams name their board and
   module too ("BCM4355C0 Kristoff MUR MCC").
3. Find the files with `ls` and save them with `get`.
4. Write the inputs and outputs. Check the whole recipe against Apple's
   server, into an empty cache:

   ```sh
   ORCHARD_HOME=$(mktemp -d) python -c "from orchard import firmware
   from orchard.progress import Clock, Progress
   print(firmware.ensure('<profile>', Clock(), Progress(), hardware='.'))"
   ```

If a step is missing from the CLI, add it there first (Steps below).

When the reader can't open something, it says so: an unknown compression,
an encrypted image. For those, and for disassembling kexts, blacktop's
[`ipsw`](https://github.com/blacktop/ipsw) works on a downloaded IPSW:
`ipsw extract --files --pattern 'REGEX' IPSW`, `ipsw extract --kernel
--device iPad7,5 IPSW` and `ipsw kernel dec KERNELCACHE`. On Linux it
mounts the system image with [apfs-fuse](https://github.com/sgan81/apfs-fuse).
The CLI used to do that; it isn't needed for recipes any more.

## Steps (version 1)

| Step | Parameters | Produces |
|---|---|---|
| `copy@1` | `input` | the input unchanged |
| `bluetool-hcd@1` | `input` (BlueTool) | the Bluetooth patchram whose hash is the output's |
| `trx-add-nvram@1` | `input` (TRX), `nvram` (additional recipe input), `placeholder_mac` (`02:00:00:00:00:00`) | Broadcom USB TRX with stripped/padded NVRAM, the placeholder address, length token and recomputed CRC; existing nonzero NVRAM and embedded device MACs are refused |
| `sgx543-from-kernelcache@1` | `input` (32-bit Mach-O), `build` | Cascadia's 64-byte `SGX543FW` header plus IMGSGX543 `__data` and `__const`; the output hash pins all bytes and addresses |
| `smartio-from-kernelcache@1` | `input` (kernelcache) | SmartIO coprocessor firmware |
| `im4p-payload@1` | `input` | the payload of an IM4P container |
| `aop-image@1` | `coprocessor` | one image (`AOP`, `AOP_ext`) of an AOP firmware container, the OSSerializeBinary payload of `Firmware/AOP/aopfw-*.im4p` |
| `z2fw-from-mtprops@1` | `input`, `profile`, `packet`, `load_address`, `hbpp_version` | Z2 touch firmware with calibration records |
| `z2fw-from-mtprops@2` | `input`, `profile`, `packet` | Z2 touch firmware from an mtprops packet array (iPhone 7 Plus) |
| `z2fw-from-mtprops@3` | `input`, `profile`, `packet`, `touch_maximum`, `prox_maximum`, `fll_mval`, `ref_clk_div`, `clk32_clock_enable` | Legacy Gen1 Z2 touch firmware (iPhone 6), with full word counts, bounded calibration from the ADT and the board's kernel clock policy |
| `z2fw-from-mtfw@1` | `profile`, `otp_preflight` | Z2 touch firmware from an MTFW HBPP item list (iPad 7); each calibration loads Apple's default, then its record |
| `hidspi-from-mtfw@1` | `profile` | the op list `apple_hidspi_ts` replays, from the MTFW of a HIDSPI controller (iPhone X); calibrations are named, not loaded |

Before it extracts anything, the CLI checks that it implements every step a
recipe uses. If not, it asks for an Orchard update. New devices that reuse
known steps need only a hardware update.

## Legacy image keys

Encrypted images (P105's iOS 8 root image and IMG3 kernelcache) need keys
that only the device can unwrap. The Rust launcher gets them from the device
and caches them on the host in `firmware-keys.json`; orchard only reads that
file. The file is the contract between the two:

- **Where:** orchard's private state root, not the shared build cache:
  `$ORCHARD_HOME/firmware-keys.json` when set, otherwise
  `~/Library/Application Support/Orchard/firmware-keys.json` on macOS and
  `${XDG_DATA_HOME:-~/.local/share}/orchard/firmware-keys.json` on Linux.
  `ORCHARD_FIRMWARE_KEYS` names another file, for local testing.
- **Protection:** a regular file owned by the user, not a link, mode `0600`,
  at most 1 MiB. orchard refuses anything else.
- **Format (schema 1):** keyed by the recipe's IPSW `sha256`, then the exact
  IPSW member name. An `encrcdsa` root image has a 72-hex-digit `key` (the
  AES-128 key followed by the 20-byte HMAC key); an `img3` image has a `key`
  of 32, 48 or 64 hex digits and a 32-hex-digit `iv`. The launcher adds
  entries and keeps the ones it does not replace.

```json
{
  "schema": 1,
  "ipsws": {
    "<IPSW sha256 from the recipe>": {
      "058-24036-023.dmg": {"kind": "encrcdsa", "key": "<72 hex digits>"},
      "kernelcache.release.p105": {"kind": "img3", "key": "<hex>", "iv": "<32 hex digits>"}
    }
  }
}
```

A missing key stops the build with a message before the root image is
indexed. orchard never writes the file, asks the device or fetches keys from
the network, and no key goes into recipes, mixins, logs or build records.
The repository holds no keys, and tests use generated ones.

## Firmware variants

Some firmware comes in variants, and which one a device needs can differ from
unit to unit of the same model. Those units select it the same way everywhere:

1. The recipe installs every variant a unit could ask for, each under its own
   file name. Apple's public files are enough for this; no device is needed.
2. m1n1 reads what the unit asks for from its ADT and passes it to the kernel
   in the device tree.
3. The driver builds the file name from that and loads the file. Nothing
   chooses a variant by guessing.
4. If the file isn't there, the feature stays off and the log names the
   variant that's missing. A unit never gets a different variant's firmware.

Wi-Fi works this way (brcmfmac: `module-instance`, vendor and antenna values),
and so does the iPhone X touchscreen (`hid-merge-personality`, file
`apple/d221-touch-<variant>.bin`).

So adding a variant is one more output in the recipe, with no kernel change. A
contribution delivers what it tested and doesn't have to cover other units, but
it must leave the rest visible: what's untested is written down in the device
data, and it doesn't drop a fallback Apple provides, such as the default values
in a touch firmware.

## Legacy Gen1 clock policy

N61's iOS 12.5.1 (16H22) `N61.mtprops` selects `C1F59,1`. Its Z2 data
packet stores the full payload-word count and has no `Firmware Config`.
Step `z2fw-from-mtprops@3` follows the
[Corellium Gen1 bootstrap](https://github.com/corellium/projectsandcastle/blob/083c2d2b07f94c8114cbe7110e1d205906ab8c54/hx-touchd/mtfw/mtfw.c#L169-L219),
with calibration records at the original SysCfg load positions. Provider 9
reads `prox-calibration`; provider 0 reads `multi-touch-calibration`.
N61 bounds them to the observed 128 and 1024 bytes respectively.

The recipe takes clock values from the same IPSW's
`com.apple.driver.AppleMultitouchSPIN61` personality,
`AppleMultitouchSPI - C1,N61`: `fll-mval=6099`, `ref-clk-div-val=2`,
`clk32-clock-enable-val=1`. Resolve the source OSUnserialize XML references:
the latter fields use `IDREF="48"` and `IDREF="8"`, whose integer definitions
are `0x2` and `0x1`. A flattened plist can lose those referenced values.
The decompressed N61 kernel's SHA-256 is
`0145bc0eda01cd55e8b996d24ffc19c25712714326f893ba5085a5016d30a4ad`.
These fields are explicit recipe parameters, including zero-valued overrides.
Physical qualification of firmware loading and input events remains separate
from converting this shared firmware file.

## S8003 G5P META firmware

`agx-g5p-ldr-from-kernelcache@1` reads the unique `__TEXT,__const` section
of `com.apple.AGXFirmwareKextG5P` in the user's own legacy prelinked
kernelcache. It rejects any size other than 119112 bytes or any digest other
than `33ae20cbbe2498e4f119ba4e9a55467e4e0192d4c86d5bf1903adce63ffa5dca`.
The existing J71t kernelcache input names the active device kernelcache and
the pinned restore-image fallback. The normal recipe installer writes the
verified output to `/lib/firmware/apple/agx-g5p-c0.ldr`. No payload patch,
calibration, firmware bytes or captured job is stored in the source tree.
Other C0 revisions and other boards require separate digest qualification.

The J71t G5P driver brings the GPU up automatically once the extracted
`apple/agx-g5p-c0.ldr` is readable on the root filesystem (up to 120 seconds).
No captured render or compute job fixtures are part of this recipe.
