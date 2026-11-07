import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import urlopen

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import MCPError
import logic_control as control
import logic_manual_mode as mode


class CoreStdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_relay_shares_http_backend_owner_and_manual_barrier(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='Logic MCP Relay ') as directory:
            folder = Path(directory)
            backend = folder / 'logic_plugins_mcp.py'
            backend.write_text('#!' + sys.executable + '\n' + (root / 'tests/fixtures/fake_backend.py').read_text())
            backend.chmod(0o755)
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
            environment = {**os.environ, 'LOGIC_MCP_STATE_DIR': directory}
            service = subprocess.Popen([sys.executable, str(root / 'logic_pro_http_bridge.py'), '--backend', str(backend), '--port', str(port)],
                                       env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                def health():
                    with urlopen(f'http://127.0.0.1:{port}/health', timeout=2) as response:
                        return json.load(response)
                for _ in range(100):
                    try:
                        if (await asyncio.to_thread(health))['backend_ready']:
                            break
                    except OSError:
                        pass
                    await asyncio.sleep(.1)
                else:
                    self.fail('Protocol-only HTTP fixture did not start')
                params = StdioServerParameters(command=sys.executable,
                    args=[str(root / 'logic_core_stdio.py'), '--url', f'http://127.0.0.1:{port}/mcp'], env=environment, cwd=str(root))
                with patch.object(mode, 'STATE_DIR', folder), patch.object(mode, 'STATE_FILE', folder / 'manual-mode.json'):
                    async with stdio_client(params) as streams:
                        async with ClientSession(*streams) as session:
                            await session.initialize()
                            self.assertEqual([tool.name for tool in (await session.list_tools()).tools], ['echo'])
                            with control.transaction() as owner:
                                result = await session.call_tool('echo', {'value': 'shared'}, meta={'logic-mcp-owner': owner})
                                self.assertFalse(result.is_error)
                            mode._write(mode.STATE_FILE, {'version': 2, 'processes': []})
                            with self.assertRaisesRegex(MCPError, 'manual mode'):
                                await session.call_tool('echo', {'value': 'blocked'})
                            self.assertEqual([tool.name for tool in (await session.list_tools()).tools], ['echo'])
            finally:
                service.terminate()
                try:
                    service.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    service.kill()
                    service.wait(timeout=3)
