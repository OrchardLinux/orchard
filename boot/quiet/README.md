# Quiet boot

A boot shows no loader branding: m1n1 and Pongo clear the screen to black and
draw no framebuffer text. USB and serial diagnostics stay available.

- m1n1 has this built in (OrchardLinux/m1n1, `-DQUIET_BOOT`); every m1n1
  orchard builds is compiled with it.
- Pongo gets it from `pongo.patch`, applied by `build-pongo.py` (see
  `.github/workflows/build.yml`).

A build or unit test is not visual qualification. Check the black screen on
supported hardware before announcing a release.

## The one Pongo

`build-pongo.py` builds the only Pongo Orchard ships. Distro releases, `orchard
launch` and local builds all use it; distros don't build their own. Distro
branding is drawn by m1n1, after Pongo. The hardware release carries it as
`quiet/pongo.bin` in the component bundles and as a release asset of its own, and
publishes its source as `pongo-source.tar.gz`.

Its source is palera1n's PongoOS fork, at the revision the palera1n release
Orchard boots iPadOS with builds (3.0.0-beta.4). palera1n uploads its own
kernel patcher, ramdisk and overlay into Pongo for the iPadOS boot, and they
only fit its fork: with checkra1n's PongoOS, iPadOS came up without the
overlay and half hung. The Linux boot needs only `bootm`, which both have.
Move the revision with palera1n's.

The Pongo revision must include upstream `ad8b2d1` ("Fix for Xcode 16.3: don't
depend on section order"). Older revisions built with Xcode 16.3 or later copy
only part of the image into SRAM, so Pongo never reaches USB: the iPad goes back
to iOS or palera1n fails with `softboot e0004051`. Berkay Kösebay found and
tested this on the iPad 5 (OrchardLinux/omarchy-a#22).
