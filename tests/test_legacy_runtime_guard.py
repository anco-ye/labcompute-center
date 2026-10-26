import json
from pathlib import Path
import sys
import tempfile
import unittest
from legacy_runtime_guard import run


class LegacyRuntimeGuardTest(unittest.TestCase):
    def test_normal_exit_retains_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'execution.json'
            self.assertEqual(run([sys.executable,'-c','print("done")'],p,1,interval=.02),0)
            e=json.loads(p.read_text());self.assertEqual(e['status'],'succeeded');self.assertEqual(e['remaining_processes'],[])
            self.assertEqual(e['exit_code'],0);self.assertGreater(e['root_created'],0)

    def test_private_limit_stops_only_owned_process_and_retains_partial(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'execution.json';partial=Path(d)/'partial.txt'
            command=[sys.executable,'-c',f'import time;from pathlib import Path;Path({str(partial)!r}).write_text("retained");x=bytearray(64*1024*1024);time.sleep(20)']
            self.assertNotEqual(run(command,p,.04,interval=.02),0)
            e=json.loads(p.read_text());self.assertEqual(e['failure_code'],'resource_limit');self.assertEqual(e['remaining_processes'],[])
            self.assertEqual(partial.read_text(),'retained');self.assertGreater(e['peak_private_bytes'],.04*2**30)

    def test_wall_time_is_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'execution.json'
            self.assertNotEqual(run([sys.executable,'-c','import time;time.sleep(20)'],p,1,wall_minutes=.002,interval=.02),0)
            self.assertEqual(json.loads(p.read_text())['failure_code'],'time_limit')


if __name__=='__main__':unittest.main()
