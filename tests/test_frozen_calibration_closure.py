import hashlib,importlib.util,json,tempfile,unittest
from pathlib import Path
from submission_tools import frozen_calibration_closure as m
class FrozenClosure(unittest.TestCase):
 def test_missing_input_exact_original_reused_and_idempotent(self):
  with tempfile.TemporaryDirectory()as d:
   root=Path(d);source=root/'original';name='synapse_tests_v3/env/inferred_pairs/a.json';f=source/name;f.parent.mkdir(parents=True);f.write_bytes(b'original calibration')
   record=source/'outputs/env/result.json';record.parent.mkdir(parents=True);record.write_text(json.dumps({'runtime_source_hashes':{name:hashlib.sha256(f.read_bytes()).hexdigest()}}));ref={'reference_result':str(record)};dest=root/'destination'
   self.assertEqual(len(m.restore_calibration_inputs(dest,ref)),1);self.assertEqual((dest/name).read_bytes(),f.read_bytes());self.assertEqual(m.restore_calibration_inputs(dest,ref),[])
 def test_conflict_and_unsafe_path_never_overwritten(self):
  with tempfile.TemporaryDirectory()as d:
   root=Path(d);record=root/'outputs/env/result.json';record.parent.mkdir(parents=True);name='synapse_tests_v3/a.json';f=root/name;f.parent.mkdir();f.write_bytes(b'wrong');record.write_text(json.dumps({'runtime_source_hashes':{name:'expected'}}))
   with self.assertRaisesRegex(ValueError,'identity mismatch'):m.restore_calibration_inputs(root,{'reference_result':str(record)})
   self.assertEqual(f.read_bytes(),b'wrong');record.write_text(json.dumps({'runtime_source_hashes':{'synapse_tests_v3/../../bad.json':'x'}}))
   with self.assertRaisesRegex(ValueError,'Unsafe'):m.restore_calibration_inputs(root,{'reference_result':str(record)})

 def test_vc_receipt_keeps_its_original_calibration_companion(self):
  with tempfile.TemporaryDirectory()as d:
   root=Path(d);source=root/'original';base=source/'synapse_tests_v3/env/inferred_pairs';base.mkdir(parents=True)
   receipt=base/'a_vc.json';receipt.write_bytes(b'frozen receipt');original=base/'a.json';original.write_bytes(b'original input')
   record=source/'outputs/env/result.json';record.parent.mkdir(parents=True);record.write_text(json.dumps({'runtime_source_hashes':{'synapse_tests_v3/env/inferred_pairs/a_vc.json':hashlib.sha256(receipt.read_bytes()).hexdigest()}}))
   destination=root/'destination';ref={'reference_result':str(record)}
   self.assertEqual(len(m.restore_calibration_inputs(destination,ref)),2)
   self.assertEqual((destination/'synapse_tests_v3/env/inferred_pairs/a.json').read_bytes(),original.read_bytes())
   self.assertEqual(m.restore_calibration_inputs(destination,ref),[])
   (destination/'synapse_tests_v3/env/inferred_pairs/a.json').write_bytes(b'conflicting input')
   with self.assertRaisesRegex(ValueError,'identity mismatch'):m.restore_calibration_inputs(destination,ref)
   self.assertEqual(original.read_bytes(),b'original input')
