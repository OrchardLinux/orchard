import ctypes
import errno
import importlib.util
from pathlib import Path
import struct
import unittest
from unittest.mock import patch


source = Path(__file__).resolve().parents[1] / "tools/p105/check_raw_exclusion.py"
spec = importlib.util.spec_from_file_location("p105_raw_exclusion", source)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class RawExclusion(unittest.TestCase):
    def test_request_is_one_complete_page_in_the_reserved_tail(self):
        data = ctypes.create_string_buffer(16384)
        oob = ctypes.create_string_buffer(64)
        request = probe.reserved_request(data, oob)
        self.assertEqual(len(request), 48)
        self.assertEqual(probe.MEMWRITE, 0xC0304D18)
        self.assertEqual(struct.unpack("<5Q8B", request), (
            4376756224, 16384, 64, ctypes.addressof(data), ctypes.addressof(oob),
            0, 0, 0, 0, 0, 0, 0, 0))

    def test_only_driver_busy_proves_exclusion(self):
        for result in (0, errno.EINVAL, errno.EPERM, errno.EROFS, errno.EBUSY):
            with self.subTest(result=result), patch.object(probe.fcntl, "ioctl") as ioctl:
                if result:
                    ioctl.side_effect = OSError(result, "test result")
                if result == errno.EBUSY:
                    self.assertEqual(probe.check_handle("mtd0", 12, b"test"), "EBUSY")
                else:
                    with self.assertRaises(RuntimeError):
                        probe.check_handle("mtd0", 12, b"test")
                ioctl.assert_called_once_with(12, 0xC0304D18, b"test")

    def test_kernel_mismatch_stops_before_opening_any_device(self):
        with patch.object(Path, "read_bytes", return_value=b"different kernel"), \
                patch.object(probe.os, "open") as open_node:
            with self.assertRaisesRegex(RuntimeError, "Kernel differs"):
                probe.check()
            open_node.assert_not_called()
