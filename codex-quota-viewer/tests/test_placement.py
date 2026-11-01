import unittest

from codexquota.appenv import Settings
from codexquota.placement import startup_position


class PlacementTests(unittest.TestCase):
    def test_hud_and_legacy_geometry_keep_coordinates(self):
        for geometry in ('120,90,390,170', '120,90,470,760'):
            saved = Settings(geometry=geometry).geometry_tuple()
            self.assertIsNotNone(saved)
            self.assertEqual(startup_position(saved, (390, 170), [(0, 0, 1920, 1040)]), (120, 90))

    def test_saved_position_zero_and_negative_monitor_are_preserved(self):
        areas = [(0, 0, 1920, 1040), (-1280, -120, 1280, 1024)]
        self.assertEqual(startup_position((0, 0, 390, 170), (390, 170), areas), (0, 0))
        self.assertEqual(startup_position((-800, -60, 390, 170), (390, 460), areas), (-800, -60))

    def test_missing_monitor_recovers_nearest_visible_position(self):
        areas = [(0, 0, 1920, 1040), (1920, 0, 1280, 1024)]
        self.assertEqual(startup_position((6000, 5000, 390, 170), (390, 170), areas), (2810, 854))
        self.assertEqual(startup_position((-1200, 300, 390, 170), (390, 170), [areas[0]]), (0, 300))

    def test_first_run_centers_and_broken_data_is_ignored(self):
        for geometry in ('', 'broken', '1,2,390,0', 'NaN,5,390,170', '1,2,3', '1e30,4,390,170'):
            saved = Settings(geometry=geometry).geometry_tuple()
            self.assertIsNone(saved)
            self.assertEqual(startup_position(saved, (390, 170), [(20, 30, 1000, 700)]), (325, 295))


if __name__ == '__main__':
    unittest.main()
