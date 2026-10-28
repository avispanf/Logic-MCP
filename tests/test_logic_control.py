import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
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
            tool()
        self.assertEqual(calls, [])
