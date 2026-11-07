import hashlib
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.notarize import notarize
from scripts.package_python_notices import collect_python_notices
from scripts.release_draft import create_draft, draft_command
from scripts.release_preflight import preflight, resolve_identity, validate_configuration
from scripts.verify_bundle import developer_team, verify_provenance
from scripts.verify_distribution import ASSETS, verify_checksums, verify_distribution


class ReleaseTests(unittest.TestCase):
    def test_python_notices_use_exact_version_and_reject_unexpected_content(self):
        from io import BytesIO
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            content = b'History and License\nPYTHON SOFTWARE FOUNDATION\nOpenSSL\n'
            def fetch(url, timeout):
                self.assertEqual(url, 'https://raw.githubusercontent.com/python/cpython/v3.14.6/Doc/license.rst')
                return BytesIO(content)
            notice = collect_python_notices(folder, '3.14.6', fetch)
            self.assertEqual((folder / notice['path']).read_bytes(), content)
            self.assertEqual(notice['sha256'], hashlib.sha256(content).hexdigest())
            with self.assertRaises(RuntimeError):
                collect_python_notices(folder, '3.14.6', lambda *args, **kwargs: BytesIO(b'<html>Error</html>'))
            with self.assertRaises(RuntimeError):
                collect_python_notices(folder, '../main')

    def test_notary_timeout_retains_submission_record_and_prevents_resubmission(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'app.zip'
            with patch('scripts.notarize.subprocess.run', side_effect=subprocess.TimeoutExpired('notarytool', 1260, output=b'{"id":"existing-submission"}')):
                with self.assertRaisesRegex(RuntimeError, 'may still be processing'):
                    notarize(archive, 'profile', 'app')
            saved = json.loads((Path(directory) / 'notary-app-result.json').read_text())
            self.assertEqual(saved['id'], 'existing-submission')
            with patch('scripts.notarize.subprocess.run') as run:
                with self.assertRaises(RuntimeError):
                    notarize(archive, 'profile', 'app')
                run.assert_not_called()

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS build script')
    def test_actual_release_build_fails_before_creating_output_without_identity(self):
        import os
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'candidate'
            env = {**os.environ, 'LOGIC_MCP_PYTHON': sys.executable, 'LOGIC_MCP_RELEASE': '1',
                   'LOGIC_MCP_SIGN_IDENTITY': '-', 'LOGIC_MCP_NOTARY_PROFILE': '', 'LOGIC_MCP_OUTPUT': str(output)}
            result = subprocess.run(['/bin/zsh', str(root / 'scripts/build_app.sh')], env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Developer ID Application', result.stderr)
            self.assertFalse(output.exists())

    def test_notary_log_failure_cannot_be_reported_as_success(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.CompletedProcess([], 0, json.dumps({'id': 'submission', 'status': 'Accepted'}), '')
            with patch('scripts.notarize.subprocess.run', side_effect=[result, subprocess.CalledProcessError(1, ['notarytool', 'log'])]):
                with self.assertRaises(subprocess.CalledProcessError):
                    notarize(Path(directory) / 'app.zip', 'profile', 'app')
            self.assertTrue((Path(directory) / 'notary-app-result.json').is_file())

    def test_final_archive_failure_detaches_read_only_image(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            def command(args, **kwargs):
                if args[0] == '/usr/bin/codesign' and '-dvvv' in args:
                    return subprocess.CompletedProcess(args, 0, '', 'signed')
                if args[0] == '/usr/bin/ditto':
                    manifest = Path(args[-1]) / 'Logic MCP.app/Contents/Resources/build-manifest.json'
                    manifest.parent.mkdir(parents=True)
                    manifest.write_text('{}')
                return subprocess.CompletedProcess(args, 0, '', '')
            with patch('scripts.verify_distribution.verify_checksums', return_value={}), \
                    patch('scripts.verify_distribution.developer_team', return_value='ABCDEFGHIJ'), \
                    patch('scripts.verify_distribution.verify_release', side_effect=['ABCDEFGHIJ', RuntimeError('Bad contained app')]), \
                    patch('scripts.verify_distribution.subprocess.run', side_effect=command) as run:
                with self.assertRaisesRegex(RuntimeError, 'Bad contained app'):
                    verify_distribution(folder)
                commands = [call.args[0] for call in run.call_args_list]
                self.assertTrue(any(args[:2] == ['hdiutil', 'attach'] and '-readonly' in args for args in commands))
                self.assertEqual(commands[-1][:2], ['hdiutil', 'detach'])

    def test_draft_is_beta_only_targets_exact_commit_and_only_final_assets(self):
        verified = {'manifest': {'version': '0.2.0-beta', 'release_channel': 'beta', 'source_commit': 'a' * 40}}
        directory = Path('/tmp/release with spaces')
        command = draft_command(directory, 'v0.2.0-beta.1', verified)
        self.assertIn('--draft', command)
        self.assertIn('--prerelease', command)
        self.assertEqual(command[command.index('--target') + 1], 'a' * 40)
        self.assertEqual(command[-3:], [str(directory / name) for name in (*ASSETS, 'SHA256SUMS')])
        for tag in ['v0.2.0', 'v0.2.0-beta.0', 'v0.3.0-beta.1', 'v0.2.0-beta.1;anything']:
            with self.subTest(tag=tag), self.assertRaises(RuntimeError):
                draft_command(directory, tag, verified)
        with self.assertRaisesRegex(RuntimeError, 'Version-matched release notes'):
            draft_command(directory, 'v0.2.0-beta.2', verified)
        with patch('scripts.release_draft.subprocess.check_output', return_value='b' * 40), \
                patch('scripts.release_draft.subprocess.run') as run:
            with self.assertRaisesRegex(RuntimeError, 'pushed'):
                create_draft(command, 'a' * 40)
            run.assert_not_called()

    def test_release_requires_identity_profile_and_valid_mode(self):
        validate_configuration('-', '', '0')
        validate_configuration('Developer ID Application: Test (ABCDEFGHIJ)', 'notary', '1')
        for args in [('-', '', '1'), ('-', 'notary', '0'), ('identity', '', '1'), ('identity', 'notary', 'yes')]:
            with self.subTest(args=args), self.assertRaises(RuntimeError):
                validate_configuration(*args)

    def test_only_valid_developer_id_application_identity_is_selected(self):
        digest = 'A' * 40
        name = 'Developer ID Application: Test (ABCDEFGHIJ)'
        output = f'1) {digest} "{name}"\n2) {"B" * 40} "Apple Development: Test"'
        self.assertEqual(resolve_identity(name, output), digest)
        self.assertEqual(resolve_identity(digest.lower(), output), digest)
        for selected in ['-', 'Apple Development: Test', 'B' * 40, 'unknown']:
            with self.subTest(selected=selected), self.assertRaises(RuntimeError):
                resolve_identity(selected, output)
        with self.assertRaises(RuntimeError):
            resolve_identity(name, output + f'\n3) {"C" * 40} "{name}"')

    def test_dirty_sources_fail_before_contacting_apple(self):
        identity = 'Developer ID Application: Test (ABCDEFGHIJ)'
        with patch('scripts.release_preflight.subprocess.check_output', side_effect=[f'{"A" * 40} "{identity}"', ' M README.md']), \
                patch('scripts.release_preflight.subprocess.run') as run:
            with self.assertRaisesRegex(RuntimeError, 'clean'):
                preflight(Path('/repo'), identity, 'profile', '1')
            run.assert_not_called()

    def test_developer_id_signature_policy(self):
        valid = 'Authority=Developer ID Application: Test (ABCDEFGHIJ)\nTeamIdentifier=ABCDEFGHIJ\nTimestamp=Nov 7, 2026\nCodeDirectory flags=0x10000(runtime)\n'
        self.assertEqual(developer_team(valid), 'ABCDEFGHIJ')
        self.assertEqual(developer_team(valid.replace('flags=0x10000(runtime)', 'flags=0x0(none)'), require_runtime=False), 'ABCDEFGHIJ')
        for invalid in [valid.replace('Developer ID Application', 'Apple Development'),
                        valid.replace('ABCDEFGHIJ\nTimestamp', 'NOTATEAM\nTimestamp'),
                        valid.replace('Timestamp=', 'Signed Time='),
                        valid.replace('Nov 7, 2026', 'none'),
                        valid.replace('(runtime)', '(adhoc)'),
                        valid.replace('flags=0x10000(runtime)', 'flags=0x0(none)')]:
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
                developer_team(invalid)
        with self.assertRaises(RuntimeError):
            developer_team(valid, 'OTHERTEAM1')

    def test_checksums_require_both_exact_assets_and_no_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for name in ASSETS:
                (folder / name).write_bytes(name.encode())
            lines = [hashlib.sha256(name.encode()).hexdigest() + '  ' + name for name in ASSETS]
            checksums = folder / 'SHA256SUMS'
            checksums.write_text('\n'.join(lines))
            self.assertEqual(set(verify_checksums(folder)), set(ASSETS))
            for data in [lines[0], '\n'.join(lines + [lines[0]]), '\n'.join(lines).replace(ASSETS[1], '../unexpected')]:
                checksums.write_text(data)
                with self.subTest(data=data), self.assertRaises(RuntimeError):
                    verify_checksums(folder)
            checksums.write_text('\n'.join(lines))
            (folder / ASSETS[0]).write_bytes(b'tampered')
            with patch('scripts.verify_distribution.subprocess.run') as run:
                with self.assertRaisesRegex(RuntimeError, 'mismatch'):
                    verify_distribution(folder)
                run.assert_not_called()

    def test_notary_only_accepts_successful_accepted_submission(self):
        responses = [(0, {'id': 'submission', 'status': 'Accepted'}),
                     (1, {'id': 'submission', 'status': 'Accepted'}),
                     (0, {'id': 'submission', 'status': 'Invalid'}),
                     (0, {'id': 'submission', 'status': 'In Progress'}),
                     (0, {'status': 'Accepted'}), (0, []), (0, {'id': 123, 'status': 'Accepted'}), (0, 'not JSON')]
        for code, data in responses:
            with self.subTest(code=code, data=data), tempfile.TemporaryDirectory() as directory:
                archive = Path(directory) / 'app.zip'
                stdout = data if data == 'not JSON' else json.dumps(data)
                response = subprocess.CompletedProcess([], code, stdout, '')
                with patch('scripts.notarize.subprocess.run', return_value=response) as run:
                    if code == 0 and isinstance(data, dict) and data.get('id') == 'submission' and data.get('status') == 'Accepted':
                        self.assertEqual(notarize(archive, 'profile', 'app'), data)
                        self.assertEqual(run.call_count, 2)
                    else:
                        with self.assertRaises(RuntimeError):
                            notarize(archive, 'profile', 'app')
                self.assertTrue((archive.parent / 'notary-app-result.json').is_file())
                with patch('scripts.notarize.subprocess.run') as retry:
                    with self.assertRaisesRegex(RuntimeError, 'already exist'):
                        notarize(archive, 'profile', 'app')
                    retry.assert_not_called()

    def test_release_provenance_binds_version_source_and_binary_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            resources = bundle / 'Contents/Resources'
            (resources / 'runtime').mkdir(parents=True)
            (resources / 'bin').mkdir()
            (resources / 'runtime/logic-mcp').write_bytes(b'python')
            (resources / 'bin/LogicProMCP').write_bytes(b'native')
            notice = resources / 'licenses/Python/THIRD-PARTY-LICENSES.rst'
            notice.parent.mkdir(parents=True)
            notice.write_bytes(b'notices')
            (bundle / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleShortVersionString': '0.2.0', 'CFBundleVersion': '9'}))
            data = {'source_commit': 'a' * 40, 'source_dirty': False, 'signing_mode': 'developer-id', 'release_channel': 'beta',
                    'backend_source_dirty': False, 'backend_commit': '7979bc7623c36d2cb911b3baa5c1c0ff98e06ff4',
                    'python_third_party_notice': {'path': 'licenses/Python/THIRD-PARTY-LICENSES.rst', 'sha256': hashlib.sha256(b'notices').hexdigest()},
                    'version': '0.2.0-beta', 'build': '9', 'runtime_sha256': hashlib.sha256(b'python').hexdigest(),
                    'backend_sha256': hashlib.sha256(b'native').hexdigest()}
            manifest = resources / 'build-manifest.json'
            manifest.write_text(json.dumps(data))
            verify_provenance(bundle)
            for field, value in [('source_dirty', True), ('source_dirty', 0), ('source_commit', 'unknown'),
                                 ('backend_source_dirty', True), ('backend_commit', 'different'),
                                 ('python_third_party_notice', {}),
                                 ('signing_mode', 'ad-hoc'), ('release_channel', 'stable'), ('build', '8'),
                                 ('version', '0.1.0'), ('runtime_sha256', 'wrong'), ('backend_sha256', 'wrong')]:
                manifest.write_text(json.dumps({**data, field: value}))
                with self.subTest(field=field, value=value), self.assertRaises(RuntimeError):
                    verify_provenance(bundle)
