from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch
from scripts.verify_bundle import AUTOMATION_ENTITLEMENT, EXECUTABLES, deployment_target, verify_signatures, version


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

    def test_source_declares_only_required_automation_entitlement(self):
        root = Path(__file__).resolve().parents[1]
        entitlements = plistlib.loads((root / 'menubar/Automation.entitlements').read_bytes())
        self.assertEqual(entitlements, {AUTOMATION_ENTITLEMENT: True})
        info = plistlib.loads((root / 'menubar/Info.plist').read_bytes())
        self.assertTrue(info['NSAppleEventsUsageDescription'].strip())

    def test_all_executable_signatures_are_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            info = bundle / 'Contents/Info.plist'
            info.parent.mkdir()
            info.write_bytes(plistlib.dumps({'NSAppleEventsUsageDescription': 'Control Logic Pro'}))
            with patch('scripts.verify_bundle.subprocess.run') as signature_check, \
                    patch('scripts.verify_bundle.subprocess.check_output',
                          return_value=plistlib.dumps({AUTOMATION_ENTITLEMENT: True})) as read:
                verify_signatures(bundle)
            signature_check.assert_called_once()
            self.assertEqual([call.args[0][-1] for call in read.call_args_list],
                             [str(bundle / path) for path in EXECUTABLES])

    def test_missing_signed_entitlement_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            info = bundle / 'Contents/Info.plist'
            info.parent.mkdir()
            info.write_bytes(plistlib.dumps({'NSAppleEventsUsageDescription': 'Control Logic Pro'}))
            for output in (b'', plistlib.dumps({}), plistlib.dumps({AUTOMATION_ENTITLEMENT: False})):
                with self.subTest(output=output), patch('scripts.verify_bundle.subprocess.run'), \
                        patch('scripts.verify_bundle.subprocess.check_output', return_value=output):
                    with self.assertRaisesRegex(RuntimeError, 'entitlement'):
                        verify_signatures(bundle)

    def test_missing_privacy_description_is_rejected_before_codesign(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            info = bundle / 'Contents/Info.plist'
            info.parent.mkdir()
            for value in (None, '', ' ', True):
                info.write_bytes(plistlib.dumps({} if value is None else {'NSAppleEventsUsageDescription': value}))
                with self.subTest(value=value), patch('scripts.verify_bundle.subprocess.run') as check:
                    with self.assertRaisesRegex(RuntimeError, 'privacy description'):
                        verify_signatures(bundle)
                    check.assert_not_called()
