// See qemu-display.h. Interfaces: QEMU's ui/dbus-display1.xml.
#include "qemu-display.h"

#include <errno.h>
#include <gio/gio.h>
#include <gio/gunixfdlist.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#define LISTENER_PATH "/org/qemu/Display1/Listener"
#define CONSOLE_PATH "/org/qemu/Display1/Console_0"

static const char LISTENER_XML[] =
    "<node>"
    " <interface name='org.qemu.Display1.Listener'>"
    "  <method name='Scanout'><arg type='u' direction='in'/><arg type='u' direction='in'/>"
    "   <arg type='u' direction='in'/><arg type='u' direction='in'/><arg type='ay' direction='in'/></method>"
    "  <method name='Update'><arg type='i' direction='in'/><arg type='i' direction='in'/>"
    "   <arg type='i' direction='in'/><arg type='i' direction='in'/><arg type='u' direction='in'/>"
    "   <arg type='u' direction='in'/><arg type='ay' direction='in'/></method>"
    "  <method name='Disable'/>"
    "  <method name='MouseSet'><arg type='i' direction='in'/><arg type='i' direction='in'/><arg type='i' direction='in'/></method>"
    "  <method name='CursorDefine'><arg type='i' direction='in'/><arg type='i' direction='in'/>"
    "   <arg type='i' direction='in'/><arg type='i' direction='in'/><arg type='ay' direction='in'/></method>"
    "  <property name='Interfaces' type='as' access='read'/>"
    " </interface>"
    " <interface name='org.qemu.Display1.Listener.Unix.Map'>"
    "  <method name='ScanoutMap'><arg type='h' direction='in'/><arg type='u' direction='in'/>"
    "   <arg type='u' direction='in'/><arg type='u' direction='in'/><arg type='u' direction='in'/>"
    "   <arg type='u' direction='in'/></method>"
    "  <method name='UpdateMap'><arg type='i' direction='in'/><arg type='i' direction='in'/>"
    "   <arg type='i' direction='in'/><arg type='i' direction='in'/></method>"
    " </interface>"
    "</node>";

#define PIXMAN_X8R8G8B8 0x20020888u
#define PIXMAN_A8R8G8B8 0x20028888u

typedef struct {
    qd_callbacks cb;
    GMainContext *context;
    GMainLoop *loop;
    GDBusConnection *qemu;      // QEMU's objects (console, input)
    GDBusConnection *listener;  // our listener object, served to QEMU
    GDBusNodeInfo *info;
    // The current surface: a shared map from QEMU, or our own copy.
    void *map;
    size_t map_size;
    uint8_t *copy;
    uint32_t width, height, stride;
} display;

static void drop_surface(display *d)
{
    if (d->map) munmap(d->map, d->map_size);
    d->map = NULL;
    d->map_size = 0;
    g_free(d->copy);
    d->copy = NULL;
}

static void method_call(GDBusConnection *connection, const char *sender, const char *path,
                        const char *interface, const char *method, GVariant *args,
                        GDBusMethodInvocation *invocation, gpointer user)
{
    display *d = user;
    if (!strcmp(method, "ScanoutMap")) {
        gint32 handle;
        guint32 offset, width, height, stride, format;
        g_variant_get(args, "(huuuuu)", &handle, &offset, &width, &height, &stride, &format);
        GUnixFDList *fds = g_dbus_message_get_unix_fd_list(g_dbus_method_invocation_get_message(invocation));
        int fd = fds ? g_unix_fd_list_get(fds, handle, NULL) : -1;
        if (fd < 0 || (format != PIXMAN_X8R8G8B8 && format != PIXMAN_A8R8G8B8)) {
            if (fd >= 0) close(fd);
            g_dbus_method_invocation_return_dbus_error(invocation, "org.qemu.Display1.Error.Unsupported", "format or handle");
            return;
        }
        // The mapping starts at a page; OFFSET may not.
        long page = sysconf(_SC_PAGESIZE);
        size_t start = offset - offset % page;
        size_t size = (size_t)stride * height + (offset - start);
        void *map = mmap(NULL, size, PROT_READ, MAP_SHARED, fd, (off_t)start);
        close(fd);
        if (map == MAP_FAILED) {
            g_dbus_method_invocation_return_dbus_error(invocation, "org.qemu.Display1.Error.Failed", strerror(errno));
            return;
        }
        // The viewer switches to the new surface before the old one goes.
        display old = *d;
        d->map = map;
        d->map_size = size;
        d->copy = NULL;
        d->width = width; d->height = height; d->stride = stride;
        d->cb.scanout(d->cb.context, (uint8_t *)map + (offset - start), width, height, stride);
        drop_surface(&old);
    } else if (!strcmp(method, "UpdateMap")) {
        gint32 x, y, w, h;
        g_variant_get(args, "(iiii)", &x, &y, &w, &h);
        d->cb.update(d->cb.context, x, y, w, h);
    } else if (!strcmp(method, "Scanout")) {
        guint32 width, height, stride, format;
        GVariant *data;
        g_variant_get(args, "(uuuu@ay)", &width, &height, &stride, &format, &data);
        gsize length;
        const void *bytes = g_variant_get_fixed_array(data, &length, 1);
        if (length >= (gsize)stride * height) {
            display old = *d;
            d->map = NULL;
            d->map_size = 0;
            d->copy = g_memdup2(bytes, (gsize)stride * height);
            d->width = width; d->height = height; d->stride = stride;
            d->cb.scanout(d->cb.context, d->copy, width, height, stride);
            drop_surface(&old);
        }
        g_variant_unref(data);
    } else if (!strcmp(method, "Update")) {
        gint32 x, y, w, h;
        guint32 stride, format;
        GVariant *data;
        g_variant_get(args, "(iiiiuu@ay)", &x, &y, &w, &h, &stride, &format, &data);
        gsize length;
        const uint8_t *bytes = g_variant_get_fixed_array(data, &length, 1);
        if (d->copy && x >= 0 && y >= 0 && w > 0 && h > 0 && (guint32)(x + w) <= d->width
                && (guint32)(y + h) <= d->height && length >= (gsize)stride * (h - 1) + (gsize)w * 4) {
            for (int row = 0; row < h; row++)
                memcpy(d->copy + (size_t)(y + row) * d->stride + (size_t)x * 4, bytes + (size_t)row * stride, (size_t)w * 4);
            d->cb.update(d->cb.context, x, y, w, h);
        }
        g_variant_unref(data);
    } else if (!strcmp(method, "Disable")) {
        d->cb.disable(d->cb.context);
        drop_surface(d);
    }
    // MouseSet, CursorDefine: the guest's pointer is not drawn; a tablet has none.
    g_dbus_method_invocation_return_value(invocation, NULL);
}

static GVariant *get_property(GDBusConnection *connection, const char *sender, const char *path,
                              const char *interface, const char *name, GError **error, gpointer user)
{
    const char *interfaces[] = {"org.qemu.Display1.Listener.Unix.Map", NULL};
    return g_variant_new_strv(interfaces, -1);
}

static const GDBusInterfaceVTable LISTENER_VTABLE = {method_call, get_property, NULL};

static void closed(GDBusConnection *connection, gboolean vanished, GError *error, gpointer user)
{
    display *d = user;
    d->cb.disable(d->cb.context);
}

static GDBusConnection *connection_on(int fd, const char *guid, GDBusConnectionFlags flags, GError **error)
{
    GSocket *socket = g_socket_new_from_fd(fd, error);
    if (!socket) return NULL;
    GSocketConnection *stream = g_socket_connection_factory_create_connection(socket);
    g_object_unref(socket);
    GDBusConnection *connection = g_dbus_connection_new_sync(G_IO_STREAM(stream), guid, flags, NULL, NULL, error);
    g_object_unref(stream);
    return connection;
}

typedef struct {
    display *d;
    int fd;
    GError *error;
    gboolean done;
    GMutex lock;
    GCond ready;
} serve_job;

// The display thread: connects our end of the listener socket (QEMU, inside
// RegisterListener, authenticates it and then asks for its Interfaces), then
// runs the loop that receives the frames.
static gpointer run_loop(gpointer user)
{
    serve_job *job = user;
    display *d = job->d;
    g_main_context_push_thread_default(d->context);
    // QEMU is the server on this socket too; the listener object is ours.
    GDBusConnection *connection = connection_on(job->fd, NULL,
        G_DBUS_CONNECTION_FLAGS_AUTHENTICATION_CLIENT | G_DBUS_CONNECTION_FLAGS_DELAY_MESSAGE_PROCESSING,
        &job->error);
    if (connection) {
        g_dbus_connection_register_object(connection, LISTENER_PATH, d->info->interfaces[0],
                                          &LISTENER_VTABLE, d, NULL, NULL);
        g_dbus_connection_register_object(connection, LISTENER_PATH, d->info->interfaces[1],
                                          &LISTENER_VTABLE, d, NULL, NULL);
        g_signal_connect(connection, "closed", G_CALLBACK(closed), d);
        g_dbus_connection_start_message_processing(connection);
        d->listener = connection;
    }
    g_mutex_lock(&job->lock);
    job->done = TRUE;
    g_cond_signal(&job->ready);
    g_mutex_unlock(&job->lock);
    if (connection) g_main_loop_run(d->loop);
    return NULL;
}

// One QMP reply line, skipping events. Returns 0 when it is a success.
static int qmp_reply(FILE *in, char *error, int size)
{
    char line[4096];
    while (fgets(line, sizeof line, in)) {
        if (strstr(line, "\"event\"")) continue;
        if (strstr(line, "\"return\"") || strstr(line, "\"QMP\"")) return 0;
        snprintf(error, size, "QMP: %s", line);
        return -1;
    }
    snprintf(error, size, "QMP: connection closed");
    return -1;
}

// A private D-Bus socket to QEMU, through QMP's getfd and add_client.
static int qemu_socket(const char *qmp_path, char *error, int size)
{
    int qmp = socket(AF_UNIX, SOCK_STREAM, 0);
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    snprintf(address.sun_path, sizeof address.sun_path, "%s", qmp_path);
    if (connect(qmp, (struct sockaddr *)&address, sizeof address) < 0) {
        snprintf(error, size, "QMP %s: %s", qmp_path, strerror(errno));
        close(qmp);
        return -1;
    }
    FILE *in = fdopen(dup(qmp), "r");
    int pair[2] = {-1, -1};
    int result = -1;
    if (qmp_reply(in, error, size)) goto out;
    const char capabilities[] = "{\"execute\":\"qmp_capabilities\"}\n";
    write(qmp, capabilities, sizeof capabilities - 1);
    if (qmp_reply(in, error, size)) goto out;
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, pair) < 0) {
        snprintf(error, size, "socketpair: %s", strerror(errno));
        goto out;
    }
    const char getfd[] = "{\"execute\":\"getfd\",\"arguments\":{\"fdname\":\"viewer\"}}\n";
    char control[CMSG_SPACE(sizeof(int))] = {0};
    struct iovec iov = {(void *)getfd, sizeof getfd - 1};
    struct msghdr message = {.msg_iov = &iov, .msg_iovlen = 1, .msg_control = control, .msg_controllen = sizeof control};
    struct cmsghdr *cmsg = CMSG_FIRSTHDR(&message);
    cmsg->cmsg_level = SOL_SOCKET;
    cmsg->cmsg_type = SCM_RIGHTS;
    cmsg->cmsg_len = CMSG_LEN(sizeof(int));
    memcpy(CMSG_DATA(cmsg), &pair[1], sizeof(int));
    if (sendmsg(qmp, &message, 0) < 0) {
        snprintf(error, size, "getfd: %s", strerror(errno));
        goto out;
    }
    if (qmp_reply(in, error, size)) goto out;
    const char add[] = "{\"execute\":\"add_client\",\"arguments\":{\"protocol\":\"@dbus-display\",\"fdname\":\"viewer\"}}\n";
    write(qmp, add, sizeof add - 1);
    if (qmp_reply(in, error, size)) goto out;
    result = pair[0];
    pair[0] = -1;
out:
    if (pair[0] >= 0) close(pair[0]);
    if (pair[1] >= 0) close(pair[1]);
    fclose(in);
    close(qmp);
    return result;
}

int qd_start(const char *qmp_path, qd_callbacks callbacks, char *error, int error_size)
{
    display *d = g_new0(display, 1);
    d->cb = callbacks;
    d->context = g_main_context_new();
    d->loop = g_main_loop_new(d->context, FALSE);
    d->info = g_dbus_node_info_new_for_xml(LISTENER_XML, NULL);
    GError *err = NULL;

    int fd = qemu_socket(qmp_path, error, error_size);
    if (fd < 0) return -1;
    d->qemu = connection_on(fd, NULL, G_DBUS_CONNECTION_FLAGS_AUTHENTICATION_CLIENT, &err);
    if (!d->qemu) goto fail;

    int pair[2];
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, pair) < 0) {
        snprintf(error, error_size, "socketpair: %s", strerror(errno));
        return -1;
    }
    serve_job *job = g_new0(serve_job, 1);
    *job = (serve_job){.d = d, .fd = pair[0]};
    g_mutex_init(&job->lock);
    g_cond_init(&job->ready);
    g_thread_unref(g_thread_new("qemu-display", run_loop, job));
    GUnixFDList *fds = g_unix_fd_list_new_from_array(&pair[1], 1);  // takes pair[1]
    GVariant *reply = g_dbus_connection_call_with_unix_fd_list_sync(
        d->qemu, NULL, CONSOLE_PATH, "org.qemu.Display1.Console", "RegisterListener",
        g_variant_new("(h)", 0), NULL, G_DBUS_CALL_FLAGS_NONE, 10000, fds, NULL, NULL, &err);
    g_object_unref(fds);
    g_mutex_lock(&job->lock);
    while (!job->done) g_cond_wait(&job->ready, &job->lock);
    g_mutex_unlock(&job->lock);
    if (!reply) goto fail;
    g_variant_unref(reply);
    if (!d->listener) {
        err = job->error;
        goto fail;
    }
    return 0;
fail:
    snprintf(error, error_size, "%s", err ? err->message : "D-Bus display failed");
    return -1;
}
