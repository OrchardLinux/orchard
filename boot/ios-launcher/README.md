# On-device iOS launcher

This producer prepares an iOS app and fixed-purpose privileged helper. The
helper verifies the installed assets, schedules one owned wake event and starts
kloader once. The patched iBEC reads the ARM32 staging bundle from the System
filesystem and enters it after checking its complete length and CRC-32.

The device's `boot.json` supplies the native function addresses and the exact
prepared-iBEC hash. The shared source contains no board addresses. The supported
P105 recipe requires `iPad2,5` on iOS build `13G36`, with the exact prepared
`12H321` iBEC identified in that recipe. The input is a private plaintext image
prepared from the owner's firmware; this command verifies it before compiling.
Apple firmware and its generated derivative stay in the private output.

## Prepare

On macOS with Xcode command-line tools, first build the pinned kloader source:

```sh
python3 boot/ios-launcher/kloader/build.py --output /private/tmp/ios-kloader
bin/orchard prepare-ios-launcher --device p105 \
  --ibec /private/path/ibec.raw --boot-image /private/path/staging-bundle.bin \
  --kloader /private/tmp/ios-kloader/kloader \
  --kloader-receipt /private/tmp/ios-kloader/result.json \
  --output /private/tmp/ios-launcher
```

The app and helper use Apple clang and ad-hoc signatures with SHA-1 and SHA-256
CodeDirectories for iOS 9. The freestanding iBEC shim uses Orchard's existing
LLVM 19 kernel toolchain through Podman. `--host-toolchain` selects local LLVM
executables instead. Neither producer contacts a device.

`installation.json` inventories every file under `files/`, with its hash and
intended destination, owner and mode. An installer must verify the receipt,
create protected directories and apply those modes, including root ownership
and mode 04755 for the helper. Preparing this archive leaves installation and
physical startup to a separately authorized operation. The app's Start button
initiates the attempt. Every new build reports device qualification as pending.

The P105 recipe keeps its existing app directory and bundle identifier so installation replaces that launcher. The app label defaults to Orchard; `--title Omarchy-A` changes its display text.
The supported boot paths stay in the device recipe, so a display label does
not change the installed filenames or firmware compatibility.

## Source and validation

The launcher/helper and shim derive from Shayne Legault's P105 local-storage
bring-up. Its original iOS 9.3.5 app started the installed Omarchy-A desktop
with the cable unplugged. The sources here move the board constants into its
boot recipe and add an exact iOS-build check. They require a fresh physical
qualification after their host checks pass.

The wake fixture executes the production wake lifecycle with real
CoreFoundation values and fake scheduling/process APIs. It checks cancellation
of the owned event on every returning path, and preserves foreign events.
Host tests also build the ARMv7 native executables and reject the historical
LC_MAIN instruction-mode mismatch. Synthetic iBEC tests verify exact input
checks and restrict changes to the declared hook and zero padding.

The source-built kloader is pinned separately under `kloader/`; its receipt
records the exact compiler input and derived artifact. Its hardware status
is independent of the earlier installed binary.

## Host recovery through iBEC

`recovery-autogo` supplies the cable path for a Cascadia recovery payload. An
external entry tool must first place the board in a pwned iBSS, or an owner
must enter its prepared iBEC Recovery. Orchard verifies the selected device
and never runs the exploit entry automatically through this path.

Prepare the board's private stage from its exact patched raw iBEC and the
original iBEC IMG3 member named in `boot.json`:

```sh
bin/orchard prepare-recovery-autogo --device p105 \
  --template /private/path/iBEC.p105.RELEASE.dfu \
  --ibec-raw /private/path/ibec.raw \
  --output /private/path/prepared-boot
ORCHARD_PREPARED_BOOT=/private/path/prepared-boot bin/orchard run /path/to/payload --no-build
```

Omitting `--output` uses the private cache path for that board/build. The
producer consumes an already-patched plaintext image with the exact recipe
hash. Decrypting and signature-patching the original firmware remains an
external preparation step. Its provenance and hash are explicit in the recipe;
Orchard refuses a different raw input before writing output.

The IMG3 packer derives from Project Cascadia's `scripts/img3pack.py`. It
preserves the template's tags while removing KBAG, replaces DATA with the
checked plaintext image, and verifies the complete resulting container.
Prepared stages stay on the owner's host. Public release manifests contain
the platform's Linux payload; they carry no Apple boot stage.

Before each upload, Orchard checks the selected ECID with its chip/board
identity. A plain DFU device is refused. Each attempt freezes the validated
inputs in its private directory, records the next operation as uncertain,
then uploads that snapshot once. The CB32 payload contains its populated
loader and all Linux inputs in one complete bundle, which is uploaded once.
The legacy two-upload payload format is unsupported. Completion requires the
same ECID's identified Linux recovery.
An error retains the outcome receipt and ends the command.

The host tests use synthetic IMG3 containers and a CB32 payload. They exercise
identity changes and failed transfers without connecting to USB. Current-base
recovery still needs an owner-authorized physical attempt. `orchard launch`'s
standalone m1n1 storage reader remains a separate ARM32 integration task;
the iOS app path above starts its installed file through iBEC directly.
