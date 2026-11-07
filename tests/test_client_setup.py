import json
from pathlib import Path
import tempfile
import subprocess
import tomllib
import unittest
from unittest.mock import patch

import logic_client_setup as clients
import logic_mcp_app as app


class ClientSetupTests(unittest.TestCase):
    runtime = '/Users/Test/Музика "Work"/Logic MCP.app/Contents/Resources/runtime/logic-mcp'

    def test_codex_preserves_comments_other_servers_and_tool_policy(self):
        before = b'# personal settings\nmodel="example"\n[mcp_servers.other]\ncommand="other"\n[mcp_servers.logic-pro]\ncommand="old"\nargs=["old"]\nenabled=false\ndisabled_tools=["logic_edit"]\n'
        after = clients.merge_config(before, self.runtime, 'codex')
        data = tomllib.loads(after.decode())
        self.assertIn(b'# personal settings', after)
        self.assertEqual(data['model'], 'example')
        self.assertEqual(data['mcp_servers']['other'], {'command': 'other'})
        self.assertFalse(data['mcp_servers']['logic-pro']['enabled'])
        self.assertEqual(data['mcp_servers']['logic-pro']['disabled_tools'], ['logic_edit'])
        self.assertNotIn('command', data['mcp_servers']['logic-pro'])
        self.assertEqual(data['mcp_servers']['logic-plugins']['command'], self.runtime)
        self.assertEqual(after, clients.merge_config(after, self.runtime, 'codex'))

    def test_claude_uses_relay_not_second_backend_and_preserves_other_settings(self):
        before = json.dumps({'preferences': {'theme': 'dark'}, 'mcpServers': {'other': {'command': 'other'},
            'logic-pro': {'url': 'old', 'env': {'PRIVATE_KEY': 'not-logged', 'LOGIC_MCP_STATE_DIR': '/old'}}}}).encode()
        after = clients.merge_config(before, self.runtime, 'claude')
        data = json.loads(after)
        self.assertEqual(data['preferences'], {'theme': 'dark'})
        self.assertEqual(data['mcpServers']['other'], {'command': 'other'})
        core = data['mcpServers']['logic-pro']
        self.assertEqual(core['args'], ['--core'])
        self.assertEqual(core['command'], self.runtime)
        self.assertNotIn('url', core)
        self.assertEqual(core['env']['PRIVATE_KEY'], 'not-logged')
        self.assertEqual(core['env']['LOGIC_MCP_STATE_DIR'], str(clients.mode.STATE_DIR))
        self.assertEqual(after, clients.merge_config(after, self.runtime, 'claude'))

    def test_invalid_configuration_is_not_replaced(self):
        for client, before in [('claude', b'[]'), ('claude', b'{"mcpServers":[]}'),
                               ('claude', b'{"mcpServers":{"logic-pro":1}}'), ('claude', b'{bad'),
                               ('codex', b'mcp_servers="bad"'), ('codex', b'model=')]:
            with self.subTest(client=client, before=before), self.assertRaises(Exception):
                clients.merge_config(before, self.runtime, client)

    def test_both_files_are_validated_before_any_write(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            codex = home / '.codex/config.toml'
            claude = home / 'Library/Application Support/Claude/claude_desktop_config.json'
            codex.parent.mkdir(parents=True)
            claude.parent.mkdir(parents=True)
            codex.write_bytes(b'model="keep"\n')
            claude.write_bytes(b'not json')
            with self.assertRaises(Exception):
                clients.configure_clients(self.runtime, home=home)
            self.assertEqual(codex.read_bytes(), b'model="keep"\n')
            self.assertEqual(claude.read_bytes(), b'not json')
            self.assertFalse(list(home.rglob('*backup*')))

    def test_backups_permissions_custom_codex_directory_and_readback(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            codex = home / 'custom codex/config.toml'
            codex.parent.mkdir()
            original = b'# original\nmodel="keep"\n'
            codex.write_bytes(original)
            result = clients.configure_clients(self.runtime, home=home, codex_directory=codex.parent)
            self.assertEqual(Path(result['backups'][0]).read_bytes(), original)
            self.assertEqual(codex.stat().st_mode & 0o777, 0o600)
            self.assertEqual(set(tomllib.loads(codex.read_text())['mcp_servers']), set(clients.NAMES))
            again = clients.configure_clients(self.runtime, home=home, codex_directory=codex.parent)
            self.assertEqual(again['backups'], [])

    def test_mid_write_failure_rolls_back_only_unchanged_written_data(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            original_write = clients.atomic_write
            def fail_claude(path, data):
                if path.name == 'claude_desktop_config.json':
                    raise OSError('disk full')
                original_write(path, data)
            with patch.object(clients, 'atomic_write', side_effect=fail_claude):
                with self.assertRaisesRegex(OSError, 'disk full'):
                    clients.configure_clients(self.runtime, home=home)
            self.assertFalse((home / '.codex/config.toml').exists())

    def test_symlink_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            target = home / 'untouched'
            target.write_text('private')
            path = home / '.codex/config.toml'
            path.parent.mkdir()
            path.symlink_to(target)
            with self.assertRaisesRegex(RuntimeError, 'non-regular'):
                clients.configure_clients(self.runtime, home=home)
            self.assertEqual(target.read_text(), 'private')

    def test_legacy_second_backend_is_reported_not_deleted(self):
        before = b'{"mcpServers":{"old-logic":{"command":"/opt/homebrew/bin/LogicProMCP"}}}'
        with self.assertRaisesRegex(RuntimeError, 'duplicate MIDI ports'):
            clients.merge_config(before, self.runtime, 'claude')

    def test_quick_setup_does_not_enable_automation_or_restart_menu(self):
        with patch.object(app, 'app_bundle', return_value=Path('/Applications/Logic MCP.app')), \
                patch.object(clients, 'configure_clients', return_value={'clients': ['codex', 'claude']}), \
                patch('logic_manual_mode.operate', return_value={'verified': True, 'mode': 'manual'}) as pause, \
                patch.object(app.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)), \
                patch.object(app, 'setup', return_value={'verified': True}) as setup:
            result = app.quick_setup()
            self.assertTrue(result['verified'])
            setup.assert_called_once_with(start_menu=False)
            self.assertEqual([call.args[0] for call in pause.call_args_list], ['pause', 'status'])
            self.assertIn('MCP stays OFF', result['message'])
