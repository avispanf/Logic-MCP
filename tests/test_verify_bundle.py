import unittest
from scripts.verify_bundle import deployment_target, version


class BundleCompatibilityTests(unittest.TestCase):
    def test_modern_macos_target(self):
        output = 'Load command 8\n      cmd LC_BUILD_VERSION\n cmdsize 32\n platform 1\n minos 14.0\n sdk 26.0\n'
        self.assertEqual(deployment_target(output), '14.0')

    def test_legacy_macos_target(self):
        output = 'Load command 5\n      cmd LC_VERSION_MIN_MACOSX\n cmdsize 16\n version 10.13\n sdk 14.0\n'
        self.assertEqual(deployment_target(output), '10.13')

    def test_non_macos_or_missing_target_is_rejected(self):
        for output in ('', 'Load command 8\n cmd LC_BUILD_VERSION\n platform 2\n minos 14.0\n'):
            with self.assertRaises(RuntimeError):
                deployment_target(output)

    def test_numeric_versions_are_padded_before_comparison(self):
        self.assertEqual(version('14'), version('14.0.0'))
        self.assertLess(version('14.9'), version('14.10'))
