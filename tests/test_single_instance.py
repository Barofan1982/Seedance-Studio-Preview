"""Real process contention, activation, crash recovery and startup ordering."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


PROBE = Path(__file__).parent/'helpers/single_instance_probe.py'

class SingleInstanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.processes = []
        self.reports = []

    def tearDown(self):
        for report in self.reports:
            report.with_suffix('.stop').touch()
        for process in self.processes:
            if process.poll() is None:
                try:
                    process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    self.kill_tree(process)
            process.wait(timeout=10)
        self.temp.cleanup()

    def kill_tree(self, process):
        # Windows venv executables are redirectors with a real Python child.
        # Kill only this fixture's process tree when simulating a crash.
        if os.name=='nt':
            subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,timeout=10)
        else:
            process.kill()

    def launch(self, name, *, profile=None, main=False, maximized=False, startup_delay=0, hold=15000):
        report = self.root/(name+'.json')
        command = [sys.executable,str(PROBE),'--profile',str(profile or self.root/'profile'),
                   '--report',str(report),'--hold-ms',str(hold),'--startup-delay-ms',str(startup_delay)]
        if main:
            command.append('--main')
        if maximized:
            command.append('--maximized')
        process = subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        self.processes.append(process)
        self.reports.append(report)
        return process,report

    def wait_report(self, path, predicate=lambda r:True):
        deadline = time.monotonic()+15
        while time.monotonic()<deadline:
            if path.is_file():
                data=json.loads(path.read_text(encoding='utf-8'))
                if predicate(data):
                    return data
            time.sleep(.04)
        self.fail('Subprocess did not produce the expected report: '+str(path))

    def test_repeat_launch_restores_owner_without_second_window(self):
        owner, primary = self.launch('owner')
        self.assertTrue(self.wait_report(primary)['minimized'])
        second, secondary = self.launch('second')
        self.assertEqual(second.wait(timeout=10),0)
        self.assertFalse(self.wait_report(secondary)['owned'])
        updated=self.wait_report(primary,lambda r:r['activations']==1)
        self.assertFalse(updated['minimized'])
        self.assertIsNone(owner.poll())

    def test_simultaneous_launches_have_exactly_one_owner(self):
        a, ar=self.launch('a')
        b, br=self.launch('b')
        ra,rb=self.wait_report(ar),self.wait_report(br)
        self.assertEqual(sum([ra['owned'],rb['owned']]),1)
        loser=b if ra['owned'] else a
        self.assertEqual(loser.wait(timeout=10),0)

    def test_restoring_minimized_window_keeps_maximized_state(self):
        owner,primary=self.launch('owner',maximized=True)
        self.wait_report(primary)
        second,secondary=self.launch('second')
        self.assertEqual(second.wait(timeout=10),0)
        restored=self.wait_report(primary,lambda r:r['activations']==1)
        self.assertFalse(restored['minimized'])
        self.assertTrue(restored['maximized'])

    def test_activation_is_retained_while_first_process_is_still_initializing(self):
        owner,primary=self.launch('owner',startup_delay=2000)
        self.wait_report(primary)
        second,secondary=self.launch('second')
        self.assertEqual(second.wait(timeout=10),0)
        self.assertFalse(self.wait_report(secondary)['owned'])
        restored=self.wait_report(primary,lambda r:r['activations']==1)
        self.assertFalse(restored['minimized'])

    def test_abnormal_exit_does_not_leave_a_permanent_lock(self):
        owner, first=self.launch('first')
        self.assertTrue(self.wait_report(first)['owned'])
        self.kill_tree(owner); owner.wait(timeout=10)
        recovered, second=self.launch('recovered',hold=400)
        self.assertTrue(self.wait_report(second)['owned'])
        self.assertEqual(recovered.wait(timeout=10),0)

    def test_independent_profiles_are_isolated(self):
        a,ar=self.launch('a',profile=self.root/'one')
        b,br=self.launch('b',profile=self.root/'two')
        ra,rb=self.wait_report(ar),self.wait_report(br)
        self.assertTrue(ra['owned'] and rb['owned'])
        self.assertNotEqual(ra['server'],rb['server'])

    def test_real_startup_checks_lock_before_controller_or_profile_changes(self):
        profile=self.root/'SeedanceVideoStudioPreview'
        profile.mkdir()
        (profile/'state.json').write_text('{}',encoding='utf-8')
        owner, first=self.launch('main-owner',profile=profile,main=True,hold=9000)
        self.wait_report(first,lambda r:r.get('ready'))
        time.sleep(.3)
        state=profile/'state.json'
        previous=state.read_bytes()
        history=profile/'history.sqlite3'
        stamp=history.stat().st_mtime_ns
        second, report=self.launch('main-second',profile=profile,main=True)
        self.assertEqual(second.wait(timeout=12),0)
        self.assertFalse(self.wait_report(report)['controller_created'])
        self.assertEqual(state.read_bytes(),previous)
        self.assertEqual(history.stat().st_mtime_ns,stamp)
        self.assertIsNone(owner.poll())

if __name__=='__main__':
    unittest.main()
