"""Every described board's firmware recipe keeps the format its consumers rely on.

Distributions read these recipes (their audits list every board's outputs),
so a recipe outside the schema breaks them without failing anything else here.
"""
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
RECIPES = sorted(ROOT.glob('devices/*/firmware.json'))


class FirmwareRecipes(unittest.TestCase):
    def test_every_board_orchard_builds_for_has_a_recipe(self):
        for descriptor in sorted(ROOT.glob('devices/*/device.toml')):
            if '[kernel]' not in descriptor.read_text():
                continue
            with self.subTest(board=descriptor.parent.name):
                self.assertTrue((descriptor.parent / 'firmware.json').is_file())

    def test_every_recipe_has_the_fields_consumers_read(self):
        for path in RECIPES:
            with self.subTest(board=path.parent.name):
                recipe = json.loads(path.read_text())
                self.assertEqual(recipe.get('schema'), 2)
                self.assertIsInstance(recipe.get('inputs'), dict)
                self.assertIsInstance(recipe.get('outputs'), list)
                for output in recipe['outputs']:
                    self.assertTrue(output['destination'].startswith('lib/firmware/'))

    def test_every_recipe_matches_the_schema(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest('jsonschema is an optional development dependency')
        schema = json.loads((ROOT / 'schemas/v1/firmware.schema.json').read_text())
        for path in RECIPES:
            with self.subTest(board=path.parent.name):
                jsonschema.validate(json.loads(path.read_text()), schema, cls=jsonschema.Draft202012Validator)

    def test_every_recipe_step_is_used_by_a_board(self):
        # A step no board's output uses is dead code, or the trace of an output
        # that was lost while the step stayed.
        from orchard.recipe import STEPS
        used = {step['step'] for path in RECIPES for output in json.loads(path.read_text())['outputs']
                for step in output['steps']}
        self.assertEqual(sorted(set(STEPS) - used), [])

    def test_j71t_ships_the_g5p_firmware_loader(self):
        # The G5P driver loads it at boot; without it the GPU never starts.
        recipe = json.loads((ROOT / 'devices/j71t/firmware.json').read_text())
        loader = [output for output in recipe['outputs']
                  if output['destination'] == 'lib/firmware/apple/agx-g5p-c0.ldr']
        self.assertEqual(len(loader), 1)
        self.assertEqual(loader[0]['steps'], [{'step': 'agx-g5p-ldr-from-kernelcache@1', 'input': 'kernelcache'}])
        self.assertIn('kernelcache', recipe['inputs'])


if __name__ == '__main__':
    unittest.main()
