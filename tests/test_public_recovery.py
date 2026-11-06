"""The public Cascadia producer and identity handoff operate without a device."""
import hashlib
import unittest

from orchard import recovery_identity as protocol
from orchard.recovery_policy import settings
from orchard.image import cascadia
from test_cascadia_loader import PROFILE, loader, kernel, tree

POLICY = {'schema': 1, 'source': 'host-usb', 'profile': 'p105',
          'compatibles': ['apple,p105ap'], 'chip': 0x8942, 'board': 0x0a}


def payload(args='console=tty0 rdinit=/init', *, policy=POLICY, compatible=b'apple,p105ap\0apple,s5l8942x\0'):
    root = tree()
    root['properties']['compatible'] = compatible
    return cascadia.assemble(loader(), kernel(), cascadia.write_fdt(root), b'public-initramfs',
                             PROFILE, args, identity_policy=policy)[0]


def bind(data, **changes):
    values = dict(ecid=123, chip=0x8942, board=0x0a, profile='p105',
                  handoff_id='a'*32, template_sha256=hashlib.sha256(data).hexdigest())
    return cascadia.bind_host_identity(data, **{**values, **changes})


class PublicRecovery(unittest.TestCase):
    def test_platform_policy_uses_descriptor_identity_and_manual_storage(self):
        selected = settings('p105')
        self.assertEqual(selected['identity'], POLICY)
        self.assertEqual(selected['runtime']['usb_network'], 'ecm')
        self.assertEqual(selected['runtime']['manual_modules'], ['apple_sftl'])
        self.assertEqual(selected['runtime']['storage_activation'], 'manual')
        self.assertIsNone(settings('j71b'))

    def test_host_binding_keeps_kernel_initrd_and_template(self):
        original = payload()
        self.assertEqual(cascadia.host_identity_policy(original), POLICY)
        bound = bind(original)
        before, _, _ = cascadia._payload_parts(original)
        after, _, args = cascadia._payload_parts(bound)
        self.assertEqual(original[before['kernel_offset']:before['dtb_offset']],
                         bound[after['kernel_offset']:after['dtb_offset']])
        self.assertEqual(original[before['initrd_offset']:], bound[after['initrd_offset']:])
        fields = protocol.parameters(args)
        self.assertEqual(fields[protocol.POLICY_PARAMETER], POLICY)
        self.assertEqual(fields[protocol.HANDOFF_PARAMETER]['template_sha256'], hashlib.sha256(original).hexdigest())
        self.assertEqual(fields[protocol.HANDOFF_PARAMETER]['handoff_id'], 'a'*32)
        with self.assertRaisesRegex(ValueError, 'already contains'):
            cascadia.host_identity_policy(bound)
        with self.assertRaisesRegex(ValueError, 'already contains'):
            bind(bound)

    def test_selected_board_digest_and_complete_inputs_fail_before_binding(self):
        original = payload()
        for changes in ({'profile': 'j1'}, {'chip': 1}, {'board': 1}, {'ecid': True},
                        {'handoff_id': '0'*32}, {'template_sha256': '0'*64}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                bind(original, **changes)
        with self.assertRaisesRegex(ValueError, 'First DTB compatible'):
            payload(compatible=b'apple,s5l8942x\0apple,p105ap\0')
        self.assertIsNone(cascadia.host_identity_policy(payload(policy=None)))

    def test_arm_command_line_full_bound_and_truncation(self):
        example = payload('x')
        _, _, args = cascadia._payload_parts(bind(example))
        padding = protocol.MAX_CMDLINE_BYTES - len(args)
        self.assertGreater(padding, 0)
        maximum = payload('x'*(padding+1))
        _, _, full = cascadia._payload_parts(bind(maximum))
        self.assertEqual(len(full), 1023)
        with self.assertRaisesRegex(ValueError, 'command-line bound'):
            bind(payload('x'*(padding+2)))
        with self.assertRaises(ValueError):
            protocol.parameters(full[:-1])

    def test_corrupted_configuration_and_tree_are_refused(self):
        data = payload()
        bad = bytearray(data)
        bad[cascadia.CONFIG_OFFSET+8] ^= 1
        with self.assertRaises(ValueError):
            cascadia.host_identity_policy(bytes(bad))
        values = cascadia.configuration(data)
        bad = bytearray(data)
        bad[values['dtb_offset']] ^= 1
        with self.assertRaises(ValueError):
            cascadia.host_identity_policy(bytes(bad))


class RecoveryModulePolicy(unittest.TestCase):
    def test_runtime_skips_only_recorded_builtin_modules(self):
        import importlib.util
        import sys
        import tempfile
        from pathlib import Path
        from unittest import mock
        path = Path(__file__).resolve().parents[1]/'runtime/recovery/policy.py'
        specification = importlib.util.spec_from_file_location('recovery_policy_runtime', path)
        runtime = importlib.util.module_from_spec(specification)
        with mock.patch.dict(sys.modules, {'recovery_identity': protocol}):
            specification.loader.exec_module(runtime)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory/'modules.builtin').write_text('kernel/libcomposite.ko\nkernel/usb-f-ecm.ko\n')
            value = {'usb_modules': ['libcomposite', 'usb_f_ecm', 'usb_f_acm']}
            self.assertEqual(runtime.loadable_modules(value, directory), ['usb_f_acm'])
            (directory/'modules.builtin').unlink()
            with self.assertRaises(FileNotFoundError):
                runtime.loadable_modules(value, directory)

    def test_builtin_usb_is_accepted_and_builtin_manual_storage_is_refused(self):
        import tempfile
        from pathlib import Path
        from orchard.image.prepare_runtime import check_recovery_modules
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            modules = root/'usr/lib/modules/test'
            modules.mkdir(parents=True)
            selected = settings('p105')
            overlapping = {'runtime': {**selected['runtime'], 'manual_modules': ['usb-f-ecm']}}
            with self.assertRaisesRegex(ValueError, 'manual module cannot be loaded'):
                check_recovery_modules(root, 'test', overlapping)
            builtin = modules/'modules.builtin'
            builtin.write_text('kernel/libcomposite.ko\nkernel/usb_f_ecm.ko\nkernel/usb_f_acm.ko\n')
            check_recovery_modules(root, 'test', selected)
            builtin.write_text(builtin.read_text() + 'kernel/apple-sftl.ko\n')
            with self.assertRaisesRegex(ValueError, 'Manual recovery module is built in'):
                check_recovery_modules(root, 'test', selected)
            builtin.write_text('')
            with self.assertRaisesRegex(ValueError, 'USB module is unavailable'):
                check_recovery_modules(root, 'test', selected)
            for name in selected['runtime']['usb_modules']:
                (modules/(name+'.ko')).write_bytes(b'fixture')
            check_recovery_modules(root, 'test', selected)
