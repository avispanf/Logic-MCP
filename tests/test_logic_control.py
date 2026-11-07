import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import anyio
from mcp.server.mcpserver.tools.base import Tool
import logic_control as control
import logic_manual_mode as manual


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        for key, value in (("STATE_DIR", Path(self.directory.name)), ("STATE_FILE", Path(self.directory.name) / "manual.json")):
            p = patch.object(manual, key, value)
            p.start()
            self.addCleanup(p.stop)

    def child(self, owner=None):
        source = 'import logic_control as c,logic_manual_mode as m;from pathlib import Path;m.STATE_DIR=Path(' + repr(self.directory.name) + ');m.STATE_FILE=m.STATE_DIR/"manual.json"\nwith c.operation(owner=' + repr(owner) + '): print("accepted")'
        return subprocess.run([sys.executable, '-B', '-c', source], capture_output=True, text=True)

    def test_transaction_blocks_other_clients_but_allows_its_workers(self):
        with control.transaction() as token:
            self.assertNotEqual(self.child().returncode, 0)
            self.assertEqual(self.child(token).returncode, 0)
        self.assertEqual(self.child().returncode, 0)
        self.assertNotEqual(self.child(token).returncode, 0)

    def test_mode_transition_invalidates_queued_request(self):
        original = control.epoch()
        manual._write(manual.STATE_DIR / 'epoch.json', {'epoch': 'next'})
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            with control.operation(expected_epoch=original):
                self.fail('old request must not run')

    def test_manual_mode_rejects_tools_without_calling_them(self):
        manual._write(manual.STATE_FILE, {'version': 2, 'processes': []})
        calls = []
        tool = control.guard_tool(lambda: calls.append(True))
        with self.assertRaisesRegex(RuntimeError, 'manual mode'):
            asyncio.run(tool())
        self.assertEqual(calls, [])
import asyncio


class QueuedToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_thread_queue_is_invalidated_by_off_on_transition(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(manual, 'STATE_DIR', Path(directory)), \
                patch.object(manual, 'STATE_FILE', Path(directory) / 'manual.json'):
            limiter = anyio.to_thread.current_default_thread_limiter()
            limiter.total_tokens = 1
            entered, release = threading.Event(), threading.Event()
            calls = []
            def occupy():
                entered.set()
                release.wait(5)
            def work(value: int) -> str:
                calls.append(value)
                return 'done'
            busy = asyncio.create_task(anyio.to_thread.run_sync(occupy))
            queued = None
            try:
                for _ in range(100):
                    if entered.is_set():
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(entered.is_set())
                tool = Tool.from_function(control.guard_tool(work))
                self.assertTrue(tool.is_async)
                with manual.register_worker():
                    queued = asyncio.create_task(tool.run({'value': 42}, context=None))
                    for _ in range(100):
                        if limiter.statistics().tasks_waiting:
                            break
                        await asyncio.sleep(.01)
                    self.assertEqual(limiter.statistics().tasks_waiting, 1)
                    record = next((manual.STATE_DIR / 'workers').glob('*.json'))
                    self.assertTrue(json.loads(record.read_text())['active'])
                    manual._write(manual.STATE_FILE, {'version': 2, 'processes': []})
                    manual._write(manual.STATE_DIR / 'epoch.json', {'epoch': 'after-toggle'})
                    manual.STATE_FILE.unlink()
                    release.set()
                    await busy
                    with self.assertRaisesRegex(Exception, 'cancelled by mode transition'):
                        await queued
                    self.assertEqual(calls, [])
                    self.assertFalse(json.loads(record.read_text())['active'])
                    self.assertEqual(await tool.run({'value': 43}, context=None), 'done')
                    self.assertEqual(calls, [43])
            finally:
                release.set()
                await asyncio.gather(busy, *([queued] if queued else []), return_exceptions=True)
