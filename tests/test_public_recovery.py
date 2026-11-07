"""The public Cascadia producer and identity handoff operate without a device."""
import hashlib
import unittest
from unittest import mock

from orchard import recovery_identity as protocol
from orchard.recovery_policy import settings
from orchard import cascadia_payload as cascadia
from test_cascadia_loader import PROFILE, loader, kernel, tree

POLICY = {'schema': 1, 'source': 'host-usb', 'profile': 'p105',
          'compatibles': ['apple,p105ap'], 'chip': 0x8942, 'board': 0x0a}
DUAL_POLICY = {key: value for key, value in POLICY.items() if key != 'source'}
DUAL_POLICY.update(schema=2, sources=['host-usb', 'ios-device'])


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
        for field in ('profile', 'compatibles', 'chip', 'board'):
            self.assertEqual(selected['identity'][field], POLICY[field])
        self.assertTrue(protocol.allows_source(selected['identity'], 'host-usb'))
        self.assertEqual(selected['runtime']['usb_network'], 'ecm')
        self.assertEqual(selected['runtime']['manual_modules'], ['apple_sftl'])
        self.assertEqual(selected['runtime']['storage_activation'], 'manual')
        self.assertIsNone(settings('j71b'))

    def test_ios_origin_needs_explicit_method_and_recipe_declaration(self):
        from orchard import hardware
        described = hardware.device('p105')
        for methods, origin, expected in (
                (['recovery-autogo', 'ios-local-file'], 'ios-device', DUAL_POLICY),
                (['recovery-autogo', 'ios-local-file'], None, None),
                (['recovery-autogo', 'ios-local-file'], 'host-usb', None),
                (['recovery-autogo'], 'ios-device', POLICY)):
            selected = {**described, 'boot': {'methods': methods}}
            recipe = {'methods': {'ios-local-file': {'identity_source': origin}}}
            with self.subTest(methods=methods, origin=origin), \
                    mock.patch.object(hardware, 'device', return_value=selected), \
                    mock.patch.object(hardware, 'boot_recipe', return_value=recipe):
                if expected is None:
                    with self.assertRaisesRegex(ValueError, 'explicit ios-device'):
                        settings('p105')
                else:
                    self.assertEqual(settings('p105')['identity'], expected)

    def test_ios_and_host_binding_use_only_policy_allowed_sources(self):
        original = payload(policy=DUAL_POLICY)
        for source in ('host-usb', 'ios-device'):
            bound = cascadia.bind_recovery_identity(original, source=source, ecid=123, chip=0x8942,
                board=0x0a, profile='p105', handoff_id='a'*32, template_sha256=hashlib.sha256(original).hexdigest())
            values, _, args = cascadia._payload_parts(bound)
            original_values = cascadia.configuration(original)
            self.assertEqual(bound[values['kernel_offset']:values['dtb_offset']],
                             original[original_values['kernel_offset']:original_values['dtb_offset']])
            self.assertEqual(bound[values['initrd_offset']:], original[original_values['initrd_offset']:])
            self.assertEqual(protocol.parameters(args)[protocol.HANDOFF_PARAMETER]['source'], source)
        with self.assertRaisesRegex(ValueError, 'does not permit'):
            cascadia.bind_recovery_identity(payload(), source='ios-device', ecid=123, chip=0x8942,
                board=0x0a, profile='p105', handoff_id='a'*32, template_sha256=hashlib.sha256(payload()).hexdigest())
        ios_only = {**DUAL_POLICY, 'sources': ['ios-device']}
        with self.assertRaisesRegex(ValueError, 'does not permit'):
            bind(payload(policy=ios_only))

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
