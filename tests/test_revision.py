"""The checkout's revision, as a release's build receipt names it."""
import unittest


class RevisionTests(unittest.TestCase):
    def test_a_receipt_names_the_exact_orchard_commit(self):
        from orchard import hardware
        import subprocess
        expected = subprocess.check_output(['git', '-C', str(hardware.ROOT), 'rev-parse', 'HEAD'], text=True).strip()
        revision = hardware.revision()
        self.assertEqual(revision['commit'], expected)
        self.assertIsInstance(revision['modified'], bool)
