"""producers/contract.toml states what orchard's Python builds with; never access a device."""
from pathlib import Path
import tomllib
import unittest

from orchard import build

ROOT = Path(__file__).resolve().parents[1]


class Contract(unittest.TestCase):
    def test_the_contract_is_what_the_build_uses(self):
        contract = tomllib.loads((ROOT / 'producers/contract.toml').read_text())
        self.assertEqual(contract['schema'], 1)
        self.assertEqual(contract['kernel-producer'], build.KERNEL_PRODUCER)
        self.assertEqual(contract['inputs'], {
            'm1n1': build.M1N1_INPUTS, 'tools': build.TOOL_INPUTS,
            'pongo': build.PONGO_RECIPE, 'cascadia': build.CASCADIA_INPUTS})

    def test_every_input_is_a_file_of_the_recipes(self):
        contract = tomllib.loads((ROOT / 'producers/contract.toml').read_text())
        for name, paths in contract['inputs'].items():
            for path in paths:
                if path != '{profile}':
                    self.assertTrue((ROOT / path).exists(), f'{name}: {path}')


if __name__ == '__main__':
    unittest.main()
