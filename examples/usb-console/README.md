# USB console

A small RAM payload for checking a new board's kernel and Orchard host link.
It starts USB serial and key-authenticated SSH through Orchard's normal
`usb-session` helper. It does not load optional drivers or mount storage.
Drivers built into the selected kernel can still probe devices described by
that board's device tree.

```sh
/path/to/orchard/bin/orchard build examples/usb-console --device BOARD
/path/to/orchard/bin/orchard run examples/usb-console --no-build
/path/to/orchard/bin/orchard shell
/path/to/orchard/bin/orchard exec -- uname -a
```

Use the checkout's launcher explicitly. Build success does not establish
that a new board boots, and this payload supplies no desktop or charging
policy. Restart into the device's normal OS when the test is finished.
