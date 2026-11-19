// VM-only substitute for a tablet's accelerometer, not part of the product image.
// The simulator window turns the device; the VM bridge sets the gravity it
// would feel (in mg) through this module's parameters. iio-sensor-proxy polls
// it like any IIO accelerometer, so rotation runs the tablet's own path.
#include <linux/iio/iio.h>
#include <linux/module.h>
#include <linux/platform_device.h>

static int gravity_x;
static int gravity_y = -1000; // upright: gravity towards the bottom edge
static int gravity_z;
module_param_named(x, gravity_x, int, 0644);
module_param_named(y, gravity_y, int, 0644);
module_param_named(z, gravity_z, int, 0644);

static struct platform_device *device;
static struct iio_dev *accel;

#define VM_ACCEL_CHANNEL(axis, index) {                         \
    .type = IIO_ACCEL,                                          \
    .modified = 1,                                              \
    .channel2 = IIO_MOD_##axis,                                 \
    .info_mask_separate = BIT(IIO_CHAN_INFO_RAW),               \
    .info_mask_shared_by_type = BIT(IIO_CHAN_INFO_SCALE),       \
    .scan_index = index,                                        \
}

static const struct iio_chan_spec vm_accel_channels[] = {
    VM_ACCEL_CHANNEL(X, 0),
    VM_ACCEL_CHANNEL(Y, 1),
    VM_ACCEL_CHANNEL(Z, 2),
};

static int vm_accel_read_raw(struct iio_dev *dev, struct iio_chan_spec const *chan,
                             int *val, int *val2, long mask)
{
    switch (mask) {
    case IIO_CHAN_INFO_RAW:
        switch (chan->channel2) {
        case IIO_MOD_X: *val = READ_ONCE(gravity_x); break;
        case IIO_MOD_Y: *val = READ_ONCE(gravity_y); break;
        default: *val = READ_ONCE(gravity_z); break;
        }
        return IIO_VAL_INT;
    case IIO_CHAN_INFO_SCALE:
        // One mg in m/s².
        *val = 0;
        *val2 = 9807;
        return IIO_VAL_INT_PLUS_MICRO;
    }
    return -EINVAL;
}

static const struct iio_info vm_accel_info = {
    .read_raw = vm_accel_read_raw,
};

static int __init vm_accel_init(void)
{
    int error;

    device = platform_device_register_simple("orchard-vm-accel", -1, NULL, 0);
    if (IS_ERR(device)) return PTR_ERR(device);
    accel = iio_device_alloc(&device->dev, 0);
    if (!accel) {
        platform_device_unregister(device);
        return -ENOMEM;
    }
    accel->name = "orchard-vm-accel";
    accel->info = &vm_accel_info;
    accel->channels = vm_accel_channels;
    accel->num_channels = ARRAY_SIZE(vm_accel_channels);
    accel->modes = INDIO_DIRECT_MODE;
    error = iio_device_register(accel);
    if (error) {
        iio_device_free(accel);
        platform_device_unregister(device);
        return error;
    }
    return 0;
}

static void __exit vm_accel_exit(void)
{
    iio_device_unregister(accel);
    iio_device_free(accel);
    platform_device_unregister(device);
}
module_init(vm_accel_init);
module_exit(vm_accel_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Virtual accelerometer for the Orchard simulator");
