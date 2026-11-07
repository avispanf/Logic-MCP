import asyncio
import json
import os
from pathlib import Path
import signal
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
from mcp.shared.exceptions import MCPError
import logic_manual_mode as mode
import logic_control as control


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
            launcher = ('import faulthandler,runpy,signal; '
                        'faulthandler.register(signal.SIGUSR1); '
                        'runpy.run_path("logic_pro_http_bridge.py",run_name="__main__")')
            cmd = [sys.executable, '-B', '-c', launcher, '--port', str(port), '--backend', str(backend)]
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
            async def client(blocked=False, owner=None):
                async with streamable_http_client(f'http://127.0.0.1:{port}/mcp') as streams:
                    async with ClientSession(streams[0],streams[1]) as session:
                        await session.initialize()
                        if owner is None:
                            self.assertEqual(len((await session.list_tools()).tools), 1)
                        if blocked:
                            with self.assertRaisesRegex(MCPError, 'manual mode'):
                                await session.call_tool('echo', {'test': 1})
                        else:
                            meta = {'logic-mcp-owner': owner} if owner else None
                            self.assertFalse((await session.call_tool('echo', {'test': 1}, meta=meta)).is_error)
            try:
                await wait_backend()
                await asyncio.gather(client(),client(),client())
                with patch.object(mode, 'STATE_DIR', root), patch.object(mode, 'STATE_FILE', root / 'manual-mode.json'):
                    with control.transaction() as owner:
                        await client(owner=owner)
                for _ in range(3):
                    with patch.object(mode, 'STATE_DIR', root), patch.object(mode, 'STATE_FILE', root / 'manual-mode.json'), \
                            patch.object(mode, 'processes', side_effect=test_processes_only):
                        self.assertEqual(len([r for r in test_processes_only().values() if str(backend) in (r.get('argv') or [])]), 1)
                        paused = mode.operate('pause')
                        self.assertTrue(paused['verified'], paused)
                        snapshot = test_processes_only()
                        if 'T' in snapshot[process.pid]['status']:
                            log.seek(0)
                            self.fail('Supervisor was suspended: ' + json.dumps({
                                'rows': snapshot, 'registration': (root / 'supervisor.json').read_text(),
                                'journal': mode.read_state(), 'log': log.read()}))
                        try:
                            paused_health = await asyncio.to_thread(health)
                        except OSError:
                            process.send_signal(signal.SIGUSR1)
                            await asyncio.sleep(.1)
                            log.seek(0)
                            self.fail('Manual-mode health did not respond: ' + log.read())
                        self.assertEqual(paused_health['state'], 'manual')
                        self.assertFalse(paused_health['backend_ready'])
                        self.assertIsNone(process.poll())
                        await client(blocked=True)
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
