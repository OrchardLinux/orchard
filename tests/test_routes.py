"""routes.toml: every route names chips the catalog has and well-formed iOS ranges."""
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def version(text):
    return tuple(int(part) for part in text.split('.'))


class Routes(unittest.TestCase):
    def setUp(self):
        self.routes = tomllib.loads((ROOT / 'routes.toml').read_text())
        self.chips = {tomllib.loads(path.read_text())['soc'] for path in (ROOT / 'chips').glob('*.toml')}

    def test_kinds(self):
        self.assertEqual(self.routes['schema'], 1)
        self.assertEqual(set(self.routes) - {'schema'}, {'boot', 'access'})

    def test_every_route_fits_known_chips_and_cites_a_source(self):
        for kind in ('boot', 'access'):
            for name, route in self.routes[kind].items():
                with self.subTest(kind=kind, route=name):
                    self.assertTrue(route['source'])
                    self.assertTrue(route['fits'])
                    for fit in route['fits']:
                        self.assertTrue(fit['chips'])
                        self.assertLessEqual(set(fit['chips']), self.chips)
                        for first, last in fit.get('ios', []):
                            self.assertTrue(first)
                            if last:
                                self.assertLessEqual(version(first), version(last))

    def test_boot_routes_say_where_they_end(self):
        for name, route in self.routes['boot'].items():
            self.assertIn(route['ends'], ('m1n1', 'cascadia'), name)

    def test_access_routes_need_an_ios_version(self):
        for name, route in self.routes['access'].items():
            self.assertTrue(all(fit.get('ios') for fit in route['fits']), name)


if __name__ == '__main__':
    unittest.main()
