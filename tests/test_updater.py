import unittest

from agape_app.updater import version_tuple


class UpdaterVersionTests(unittest.TestCase):
    def test_versions_are_compared_numerically(self):
        self.assertGreater(version_tuple("1.10.0"), version_tuple("1.9.9"))

    def test_optional_v_prefix_is_accepted(self):
        self.assertEqual(version_tuple("v2.3.4"), (2, 3, 4))

    def test_invalid_version_is_rejected(self):
        with self.assertRaises(ValueError):
            version_tuple("1.2")


if __name__ == "__main__":
    unittest.main()
