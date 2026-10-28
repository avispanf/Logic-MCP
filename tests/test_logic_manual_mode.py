import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

import logic_manual_mode as mode


class ManualModeTests(unittest.TestCase):
    def test_only_exact_scoped_workers_match(self):
        script = mode.SCRIPTS[0]
        self.assertTrue(mode.managed('/some/Python ' + script))
        self.assertTrue(mode.managed('/some/python3.14 -u ' + script))
        self.assertTrue(mode.managed(mode.BACKENDS[0]))
        self.assertFalse(mode.managed('/bin/zsh -c /some/Python ' + script))
        self.assertFalse(mode.managed('/some/Python ' + script + '.backup'))
        self.assertFalse(mode.managed('/Applications/Logic Pro.app/Contents/MacOS/Logic Pro'))
        self.assertFalse(mode.managed('/Applications/Claude.app/Contents/Helpers/disclaimer -- ' + script))

    def test_reused_pid_is_never_signalled(self):
        record = {'pid': 123, 'birth': 'old', 'command': mode.BACKENDS[0]}
        with patch.object(mode, 'processes', return_value={123: {**record, 'birth': 'new'}}), \
                patch.object(mode.os, 'kill') as kill:
            self.assertFalse(mode.signal_verified(record, signal.SIGCONT))
            kill.assert_not_called()

    def test_old_work_is_cancelled_without_touching_other_process(self):
        token = str(uuid.uuid4())
        worker = subprocess.Popen([
            sys.executable, '-u', '-c',
            'import subprocess,sys,time; p=subprocess.Popen([sys.executable,"-c",'
            '"import time; time.sleep(60)"]); print(p.pid,flush=True); time.sleep(60)', token,
        ], stdout=subprocess.PIPE, text=True)
        other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        child = int(worker.stdout.readline())
        original_processes = mode.processes
        def isolated_processes():
            return {p:r for p,r in original_processes().items() if p in (worker.pid, child, other.pid)}
        try:
            with tempfile.TemporaryDirectory() as directory, \
                    patch.object(mode, 'STATE_DIR', Path(directory)), \
                    patch.object(mode, 'STATE_FILE', Path(directory) / 'state.json'), \
                    patch.object(mode, 'managed', side_effect=lambda cmd: cmd.endswith(token)), \
                    patch.object(mode, 'processes', side_effect=isolated_processes):
                paused = mode.operate('pause')
                self.assertTrue(paused['verified'], paused)
                rows = mode.processes()
                for pid in (worker.pid, child):
                    self.assertIn('T', rows[pid]['status'])
                self.assertNotIn('T', rows[other.pid]['status'])
                # A repeated status/pause must not lose ownership of stopped workers.
                self.assertTrue(mode.operate('status')['verified'])
                resumed = mode.operate('resume')
                self.assertTrue(resumed['verified'], resumed)
                rows = mode.processes()
                worker.wait(timeout=3)
                self.assertNotIn('T', rows[other.pid]['status'])
                self.assertFalse(mode.STATE_FILE.exists())
        finally:
            for pid in (worker.pid, child, other.pid):
                try:
                    os.kill(pid, signal.SIGCONT)
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            worker.wait(timeout=3)
            other.wait(timeout=3)
            worker.stdout.close()

    def test_real_argv_with_spaces_relative_script_and_other_checkout(self):
        for argv in (
            ['/Users/Someone/My Tools/python', '/Downloads/Other Checkout/logic_plugins_mcp.py'],
            ['python3', 'logic_plugins_mcp.py'],
            ['/some/python', '-X', 'utf8', 'logic_audio_mcp.py'],
            ['/usr/local/bin/LogicProMCP'],
        ):
            self.assertTrue(mode.managed_argv(argv), argv)
        self.assertFalse(mode.managed_argv(['/bin/zsh', '-c', 'python logic_plugins_mcp.py']))

    def test_idle_registered_worker_stays_online_while_active_worker_is_stopped(self):
        row = {'pid': 123, 'ppid': 1, 'birth': 'now', 'status': 'S',
               'command': '/some/Python logic_plugins_mcp.py',
               'argv': ['/some/Python', 'logic_plugins_mcp.py']}
        with tempfile.TemporaryDirectory() as directory, patch.object(mode, 'STATE_DIR', Path(directory)):
            record = Path(directory) / 'workers/123.json'
            mode._write(record, {**row, 'active': False})
            self.assertEqual(mode.inventory({123: row})[0], set())
            mode._write(record, {**row, 'active': True})
            self.assertEqual(mode.inventory({123: row})[0], {123})

    def test_other_installation_state_directory_is_not_touched(self):
        row = {'pid': 123, 'ppid': 1, 'birth': 'now', 'status': 'S',
               'command': '/some/Python logic_plugins_mcp.py',
               'argv': ['/some/Python', 'logic_plugins_mcp.py'], 'state_dir': '/Other/State'}
        self.assertEqual(mode.inventory({123: row})[0], set())

    def test_unidentified_candidate_prevents_false_verified(self):
        row = {'pid': 111, 'ppid': 1, 'status': 'S', 'birth': 'now',
               'command': '/My Tools/Python /Other Checkout/logic_plugins_mcp.py', 'argv': None}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(mode, 'STATE_DIR', Path(directory)), \
                patch.object(mode, 'STATE_FILE', Path(directory) / 'state.json'), \
                patch.object(mode, 'processes', return_value={111: row}), patch.object(mode.os, 'kill') as kill:
            result = mode.operate('pause')
            self.assertFalse(result['verified'])
            self.assertEqual(result['unknown'], [111])
            kill.assert_not_called()

    def test_corrupt_state_repair_keeps_gate_closed_and_backup(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(mode, 'STATE_DIR', Path(directory)), \
                patch.object(mode, 'STATE_FILE', Path(directory) / 'state.json'), \
                patch.object(mode, 'processes', return_value={}):
            mode.STATE_FILE.write_text('{}')
            with self.assertRaises(RuntimeError):
                mode.operate('status')
            self.assertTrue(mode.operate('repair')['verified'])
            self.assertTrue(mode.STATE_FILE.exists())
            self.assertEqual(len(list(Path(directory).glob('manual-mode.backup-*'))), 1)

    def test_old_journal_migrates_and_prunes_dead_processes(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(mode, 'STATE_DIR', Path(directory)), \
                patch.object(mode, 'STATE_FILE', Path(directory) / 'state.json'), patch.object(mode, 'processes', return_value={}):
            mode.save_state({'version': 1, 'processes': [{'pid': 123, 'birth': 'old', 'command': 'old worker'}]})
            self.assertTrue(mode.operate('status')['verified'])
            self.assertEqual(mode.read_state()['version'], 2)
            self.assertEqual(mode.read_state()['processes'], [])
            self.assertEqual(len(list(Path(directory).glob('manual-mode.v1-backup-*'))), 1)

    def test_external_pause_is_preserved(self):
        worker = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])
        try:
            os.kill(worker.pid, signal.SIGSTOP)
            time.sleep(.05)
            command = mode.processes()[worker.pid]['command']
            original_processes = mode.processes
            with tempfile.TemporaryDirectory() as directory, \
                    patch.object(mode, 'STATE_DIR', Path(directory)), \
                    patch.object(mode, 'STATE_FILE', Path(directory) / 'state.json'), \
                    patch.object(mode, 'managed', side_effect=lambda cmd: cmd == command), \
                    patch.object(mode, 'processes', side_effect=lambda: {p:r for p,r in original_processes().items() if p == worker.pid}):
                mode.operate('pause')
                mode.operate('resume')
                self.assertIn('T', mode.processes()[worker.pid]['status'])
        finally:
            os.kill(worker.pid, signal.SIGKILL)
            worker.wait(timeout=3)


if __name__ == '__main__':
    unittest.main()
