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

    def test_real_worker_and_child_pause_resume_without_touching_other_process(self):
        token = str(uuid.uuid4())
        worker = subprocess.Popen([
            sys.executable, '-u', '-c',
            'import subprocess,sys,time; p=subprocess.Popen([sys.executable,"-c",'
            '"import time; time.sleep(60)"]); print(p.pid,flush=True); time.sleep(60)', token,
        ], stdout=subprocess.PIPE, text=True)
        other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        child = int(worker.stdout.readline())
        try:
            with tempfile.TemporaryDirectory() as directory, \
                    patch.object(mode, 'STATE_DIR', Path(directory)), \
                    patch.object(mode, 'STATE_FILE', Path(directory) / 'state.json'), \
                    patch.object(mode, 'managed', side_effect=lambda cmd: cmd.endswith(token)):
                paused = mode.operate('pause')
                self.assertTrue(paused['verified'], paused)
                rows = mode.processes()
                for pid in (worker.pid, child):
                    self.assertIn('T', rows[pid]['status'])
                self.assertNotIn('T', rows[other.pid]['status'])
                # A repeated status/pause must not lose ownership of stopped workers.
                self.assertTrue(mode.operate('status')['verified'])
                self.assertTrue(mode.operate('resume')['verified'])
                rows = mode.processes()
                for pid in (worker.pid, child, other.pid):
                    self.assertNotIn('T', rows[pid]['status'])
                self.assertFalse(mode.STATE_FILE.exists())
                # Preserve a process stopped by the user before manual mode.
                os.kill(worker.pid, signal.SIGSTOP)
                time.sleep(0.05)
                self.assertTrue(mode.operate('pause')['verified'])
                self.assertTrue(mode.operate('resume')['verified'])
                self.assertIn('T', mode.processes()[worker.pid]['status'])
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


if __name__ == '__main__':
    unittest.main()
