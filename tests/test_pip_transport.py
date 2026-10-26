import sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]/'runtime_support'))
from lab_task_support import install_pip

class PipTransportTests(unittest.TestCase):
    def scenario(self, messages, **kwargs):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'task.log';cache=Path(folder)/'cache';cache.mkdir();saved=cache/'complete-wheel';saved.write_bytes(b'retained');calls=[]
            def run(cmd,remaining):
                calls.append((cmd,remaining))
                message=messages[len(calls)-1]
                with path.open('ab')as f:f.write(message.encode())
                return int(bool(message))
            result=install_pip(['python','-m','pip','--isolated','install','package==1'],run,path,cache,**kwargs)
            self.assertEqual(saved.read_bytes(),b'retained')
            return result,calls
    def test_timeout_then_success_explicit_cache(self):
        result,calls=self.scenario(['ReadTimeoutError: Read timed out.\n',''])
        self.assertEqual(result,0);self.assertEqual(len(calls),2)
        self.assertIn('--cache-dir',calls[0][0]);self.assertIn('--isolated',calls[0][0]);self.assertGreater(calls[0][1],0)
    def test_connection_reset_retries(self):
        self.assertEqual(len(self.scenario(['Connection reset by peer\n',''])[1]),2)
    def test_permanent_conflict_not_retried(self):
        with self.assertRaisesRegex(RuntimeError,'ResolutionImpossible'):
            self.scenario(['ERROR: ResolutionImpossible\n'])
    def test_hash_mismatch_never_retried(self):
        with self.assertRaisesRegex(RuntimeError,'HASHES'):
            self.scenario(['ERROR: THESE PACKAGES DO NOT MATCH THE HASHES\n'])
    def test_max_attempts_and_raw_cause(self):
        with self.assertRaisesRegex(RuntimeError,'ReadTimeoutError'):
            self.scenario(['ReadTimeoutError: Read timed out.\n']*3)
    def test_old_timeout_does_not_retry_new_failure(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'task.log';p.write_text('ReadTimeoutError: old failure\n');calls=[]
            def run(cmd,t):
                calls.append(cmd)
                with p.open('a')as f:f.write('ERROR: No matching distribution found\n')
                return 1
            with self.assertRaisesRegex(RuntimeError,'No matching'):
                install_pip(['pip','install','x'],run,p,Path(d)/'cache')
            self.assertEqual(len(calls),1)
    def test_absolute_deadline(self):
        ticks=iter([0,0,901])
        with self.assertRaisesRegex(TimeoutError,'deadline'):
            self.scenario(['ReadTimeoutError: Read timed out.\n'],clock=lambda:next(ticks))

if __name__=='__main__':unittest.main()
