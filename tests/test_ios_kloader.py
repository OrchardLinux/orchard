import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "boot/ios-launcher/kloader"
SPEC = importlib.util.spec_from_file_location("ios_kloader_build", SOURCE / "build.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class KloaderSourceTests(unittest.TestCase):
    def copy_sources(self, directory):
        for name in ("source.json", "kloader.c", "tfp0.plist"):
            shutil.copyfile(SOURCE / name, directory / name)

    def test_source_identity_matches_launcher_contract(self):
        pin = builder.inputs()
        module_spec = importlib.util.spec_from_file_location(
            "ios_kloader_identity", ROOT / "src/orchard/apple/ios_kloader.py")
        identity = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(identity)
        self.assertEqual(pin["commit"], identity.SOURCE_COMMIT)
        self.assertEqual(pin["files"]["kloader.c"], identity.SOURCE_SHA256)
        self.assertEqual(builder.HARDENING_PATCH_SHA256, identity.HARDENING_PATCH_SHA256)
        self.assertEqual(builder.DERIVED_SOURCE_SHA256, identity.DERIVED_SOURCE_SHA256)
        self.assertEqual(builder.SLEEP_WAIT_SECONDS, identity.SLEEP_WAIT_SECONDS)
        self.assertIn("Copyright 2014, winocm.", (SOURCE / "kloader.c").read_text())

    def test_changed_source_is_rejected_before_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.copy_sources(directory)
            path = directory / "kloader.c"
            path.write_bytes(path.read_bytes() + b"\n/* altered */\n")
            with self.assertRaisesRegex(ValueError, "Pinned kloader source differs"):
                builder.inputs(directory)

    def test_repinning_altered_source_requires_contract_update(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.copy_sources(directory)
            path = directory / "kloader.c"
            path.write_bytes(path.read_bytes() + b"\n/* altered */\n")
            manifest = directory / "source.json"
            pin = json.loads(manifest.read_text())
            pin["files"]["kloader.c"] = hashlib.sha256(path.read_bytes()).hexdigest()
            manifest.write_text(json.dumps(pin))
            with self.assertRaisesRegex(ValueError, "Unsupported kloader source description"):
                builder.inputs(directory)

    def test_wrong_architecture_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "foreign"
            path.write_bytes(struct.pack("<7I", 0xFEEDFACE, 0x100000C, 0, 2, 0, 0, 0))
            with self.assertRaisesRegex(ValueError, "thin ARMv7"):
                builder.image(path)

    def test_truncated_commands_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "incomplete"
            path.write_bytes(struct.pack("<7I", 0xFEEDFACE, 12, 9, 2, 1, 56, 0))
            with self.assertRaisesRegex(ValueError, "load-command table"):
                builder.image(path)

    def test_patch_is_exact_and_rejects_changes(self):
        self.assertEqual(hashlib.sha256(builder.patched_source().encode()).hexdigest(),
                         builder.DERIVED_SOURCE_SHA256)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.copy_sources(directory)
            (directory / "hardening.patch").write_bytes((SOURCE / "hardening.patch").read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "hardening patch differs"):
                builder.patched_source(directory)

    def test_dangling_symlink_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            link = directory / "link"
            link.symlink_to(directory / "absent")
            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                builder.build(link)
            self.assertFalse((directory / "absent").exists())

    @unittest.skipUnless(shutil.which("cc"), "requires a C compiler")
    def test_actual_patched_io_paths(self):
        source = builder.patched_source()
        start = source.index("/* Orchard: bounded I/O")
        end = source.index("\nstatic void generate_ttb_entries", start)
        helpers = source[start:end]
        fixture = (ROOT / "tests/fixtures/ios-kloader-io.c").read_text()
        self.assertEqual(fixture.count("/* ACTUAL_PATCHED_HELPERS */"), 1)
        with tempfile.TemporaryDirectory(prefix="orchard-kloader-io-") as temporary:
            directory = Path(temporary)
            (directory / "fixture.c").write_text(fixture.replace("/* ACTUAL_PATCHED_HELPERS */", helpers))
            command = ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                       "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                       str(directory / "fixture.c"), "-o", str(directory / "fixture")]
            built = subprocess.run(command, capture_output=True, text=True, timeout=60)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            ran = subprocess.run([str(directory / "fixture")], capture_output=True, text=True, timeout=30)
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
            self.assertIn("region exhaustion and one sleep request passed", ran.stdout)


@unittest.skipUnless(platform.system() == "Darwin" and shutil.which("xcrun"),
                     "requires the Apple iPhoneOS SDK and codesign")
class KloaderBuildTests(unittest.TestCase):
    def test_signed_source_build_is_reproducible(self):
        with tempfile.TemporaryDirectory(prefix="orchard-kloader-test-") as temporary:
            first = builder.build(Path(temporary) / "first")
            second = builder.build(Path(temporary) / "second")
            self.assertEqual(first["artifact_sha256"], second["artifact_sha256"])
            self.assertEqual(first["source_sha256"], builder.SOURCE_SHA256)
            self.assertEqual(first["output"]["architecture"], "armv7")
            self.assertTrue(first["signed"])
            self.assertFalse(first["on_device_qualified"])
            self.assertEqual(first["device_operations"], 0)
            path = Path(temporary) / "first/kloader"
            self.assertEqual(first["artifact_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            with self.assertRaises(FileExistsError):
                builder.build(Path(temporary) / "first")


if __name__ == "__main__":
    unittest.main()
