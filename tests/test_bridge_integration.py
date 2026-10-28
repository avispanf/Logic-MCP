import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.request import urlopen
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import logic_manual_mode as mode


class HTTPIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_survives_pause_restarts_backend_and_shares_one_owner(self):
        with tempfile.TemporaryDirectory(prefix='Logic MCP Test ') as directory:
            root = Path(directory)
            backend = root / 'logic_plugins_mcp.py'
            fixture = Path(__file__).parent / 'fixtures/fake_backend.py'
            backend.write_text('#!' + sys.executable + '\n' + fixture.read_text())
            backend.chmod(0o755)
            with socket.socket() as port_socket:
                port_socket.bind(('127.0.0.1', 0))
                port = port_socket.getsockname()[1]
            env = {**os.environ, 'LOGIC_MCP_STATE_DIR': directory, 'PYTHONDONTWRITEBYTECODE': '1'}
            cmd = [sys.executable, '-B', 'logic_pro_http_bridge.py', '--port', str(port), '--backend', str(backend)]
            log = (root / 'service.log').open('w+')
            process = subprocess.Popen(cmd, env=env, stdout=log, stderr=log)
            def health():
                with urlopen(f'http://127.0.0.1:{port}/health', timeout=5) as response:
                    return json.load(response)
            async def wait_backend():
                for _ in range(100):
                    if process.poll() is not None:
                        log.seek(0)
                        self.fail(log.read())
                    try:
                        if (await asyncio.to_thread(health))['backend_ready']:
                            return
                    except OSError:
                        pass
                    await asyncio.sleep(.1)
                log.seek(0)
                self.fail('Backend not ready: ' + log.read())
            original_processes = mode.processes
            def test_processes_only():
                rows = original_processes()
                selected = {process.pid}
                while True:
                    children = {p for p,r in rows.items() if r['ppid'] in selected}
                    if children.issubset(selected):
                        break
                    selected.update(children)
                return {p: r for p,r in rows.items() if p in selected}
            async def client():
                async with streamable_http_client(f'http://127.0.0.1:{port}/mcp') as streams:
                    async with ClientSession(streams[0],streams[1]) as session:
                        await session.initialize()
                        self.assertEqual(len((await session.list_tools()).tools), 1)
                        self.assertFalse((await session.call_tool('echo', {'test': 1})).is_error)
            try:
                await wait_backend()
                await asyncio.gather(client(),client(),client())
                with patch.object(mode, 'STATE_DIR', root), patch.object(mode, 'STATE_FILE', root / 'manual-mode.json'), \
                        patch.object(mode, 'processes', side_effect=test_processes_only):
                    self.assertEqual(len([r for r in test_processes_only().values() if str(backend) in (r.get('argv') or [])]), 1)
                    self.assertTrue(mode.operate('pause')['verified'])
                    self.assertEqual(health()['state'], 'manual')
                    self.assertIsNone(process.poll())
                    self.assertTrue(mode.operate('resume')['verified'])
                await wait_backend()
                await client()
                # Singleton protection must fail before a second backend is spawned.
                other = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=5)
                self.assertNotEqual(other.returncode, 0)
                self.assertIn('already owns', other.stderr)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                log.close()
