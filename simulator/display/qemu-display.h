// QEMU's D-Bus display (org.qemu.Display1), peer to peer: no bus, no daemon.
// The viewer asks QEMU over QMP for a private D-Bus connection, registers
// itself as console 0's listener and receives each frame in shared memory
// the moment the guest draws it. Plain GLib, so the same code serves a macOS
// and a Linux viewer.
#pragma once
#include <stdint.h>

typedef struct {
    void *context;
    // A new surface: WIDTH x HEIGHT, STRIDE bytes per row, XRGB8888 (pixman
    // x8r8g8b8). PIXELS stays valid until the next scanout or disable.
    void (*scanout)(void *context, const void *pixels, uint32_t width, uint32_t height, uint32_t stride);
    // A rectangle of the current surface changed.
    void (*update)(void *context, int32_t x, int32_t y, int32_t width, int32_t height);
    // The display went away (guest off, connection lost).
    void (*disable)(void *context);
} qd_callbacks;

// Connects through the QMP socket at QMP_PATH (one QEMU started with
// `-display dbus,p2p=yes`). Runs its own thread; callbacks come from it.
// Returns 0, or -1 with a message in ERROR (ERROR_SIZE bytes).
int qd_start(const char *qmp_path, qd_callbacks callbacks, char *error, int error_size);
