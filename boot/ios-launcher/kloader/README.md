# ARMv7 kloader source

`kloader.c` and `tfp0.plist` are unchanged files from
[axi0mX/ios-kexec-utils at a1bbca8a8f2847d9c53070e76e0b5d093bda5993](https://github.com/axi0mX/ios-kexec-utils/tree/a1bbca8a8f2847d9c53070e76e0b5d093bda5993).
The source retains winocm's copyright and GPL-2.0-or-later license notice;
Orchard's root `LICENSE` contains the license text. `source.json` records
the complete source inventory and its SHA-256 hashes. The imported source
contains trailing spaces, also present in the exact-context patch. A scoped
`.gitattributes` rule preserves those bytes while normal whitespace checks
continue to cover the producer and tests.

This revision calls `IOPMSleepSystem` after staging the image and changing
the wakeup trampoline. The iOS launcher owns the scheduled wake event. Later
ios-kexec-utils revisions use a different sleep-notification/autowake path.

Build on a Mac with Xcode:

```sh
python3 boot/ios-launcher/kloader/build.py --output /tmp/orchard-kloader
```

The builder applies `hardening.patch` to a private output copy with exact
context and no fuzz. It verifies the patch and resulting source hashes.
The patch bounds the kernel address scan and refuses incomplete reads.
It validates the image before changing the kernel map and removes the
fallback for unrecognized chips. One sleep request keeps its caller alive
for at most 90 seconds while the scheduled wake can transfer control. An
error returns to the launcher without repeating that request or rebooting.
Host tests compile these actual patched I/O helpers against fake Mach and
power APIs under AddressSanitizer and UndefinedBehaviorSanitizer.

The builder uses the current SDK's ARMv7 compiler and describes the existing
iOS 9 imports with generated TAPI files. It supplies missing declarations in
a generated header. The upstream C path uses three storage words also
defined by its unused inline shellcode; the builder defines those words in
a small C object because current assemblers reject the unused shellcode's
relocations. The upstream source remains byte-identical to its pin.

The result is a thin ARMv7 executable, signed ad hoc with SHA-1 and SHA-256
and the pinned tfp0 entitlements. `--unsigned` omits signing. The receipt
records the compiler and output hash, with separate identities for the
upstream source, hardening patch and derived source. It supplies
`source_commit`, `source_sha256`, `hardening_patch_sha256`,
`derived_source_sha256`, `artifact_sha256`, `artifact_bytes`, `signed` and
`sleep_wait_seconds`. `on_device_qualified` remains false for each host
build. This command performs no device operation and never executes the
result.

The installed P105 binary has the same imports and diagnostic strings as
this older source, including its lack of scheduled-wake imports. Its exact
build provenance is unknown. A successful host build establishes the new
executable's structure and deterministic output. The revised handoff needs
its own on-device qualification before installation.
