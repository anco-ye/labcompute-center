import sys,time,tempfile,unittest,os
from pathlib import Path
import psutil
from execution_broker import bounded_command

class BrokerCommandDeadlineTests(unittest.TestCase):
    def test_inherited_output_handle_does_not_hold_completed_parent(self):
        with tempfile.TemporaryDirectory()as d:
            code="import subprocess,sys;p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(1)']);print('done',p.pid)"
            began=time.monotonic();out=bounded_command([sys.executable,'-c',code],timeout=2,log_dir=d)
            self.assertIn('done',out);self.assertLess(time.monotonic()-began,.9)
            try:psutil.Process(int(out.split()[-1])).wait(timeout=3)
            except psutil.NoSuchProcess:pass

    def test_timeout_cleans_only_its_command_tree(self):
        with tempfile.TemporaryDirectory()as d:
            marker=Path(d)/'pid'
            code="import subprocess,sys,time;from pathlib import Path;p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);Path(sys.argv[1]).write_text(str(p.pid));time.sleep(60)"
            with self.assertRaises(TimeoutError):bounded_command([sys.executable,'-c',code,str(marker)],timeout=.5,log_dir=d)
            self.assertTrue(marker.exists())
            if psutil.pid_exists(int(marker.read_text())):
                self.assertEqual(psutil.Process(int(marker.read_text())).status(),psutil.STATUS_ZOMBIE)
