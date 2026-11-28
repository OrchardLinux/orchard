# System defaults

What orchard puts into every installed system, next to the kernel, DTBs and
modules of its platform (the platform layer, `orchard.image.platform`):

- `regulatory/`: the public wireless regulatory database and its upstream
  signature, which cfg80211 reads before Wi-Fi comes up;
- `defaults/`: hardware defaults under `/usr/lib`, such as NetworkManager
  leaving orchard's USB gadget link alone, the battery gauge's poll
  interval, and the udev step that gives a Wi-Fi interface still showing
  the firmware's placeholder address this unit's own
  (`usr/lib/orchard/wifi-address`).

- `usr/lib/orchard/screens.json` (written by the platform layer, not kept
  here): the rounded corners and notch of every board's screen that has
  them, under the board's device tree compatible, so a desktop keeps its
  content clear of them. `corner_radius` is in points (`scale` physical
  pixels each); `outline` is the lit area as an SVG path in physical pixels,
  held upright, y down. Both come from Apple (the device tree's
  `display-corner-radius`, Xcode's framebuffer mask) through
  `devices/<board>/screen.json` (`tools/catalog/generate screens`). A board
  without an entry has a plain rectangular screen.

- `defaults/usr/share/wireplumber/`: a place for WirePlumber fragments, which
  WirePlumber reads from `/usr/share` but not from `/usr/lib`; there is none
  yet.

Defaults live only under `/usr/lib`, apart from the UCM profiles and
WirePlumber fragments, which their readers only look for under `/usr/share`.
A distro changes them the usual way, with a file of the same name under
`/etc`. Everything else in a root image is the distro's: packages, users,
services, power and desktop policy.
