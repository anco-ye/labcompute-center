import copy,hashlib,json,tempfile,unittest
from pathlib import Path
import numpy as np
from geometry_compat import compare_geometry
class GeometryCompatibilityTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);p=self.root/'123_bridged.npz';np.savez(p,soma_root=np.array(42),vertices=np.zeros((2,3)))
  h=hashlib.sha256(p.read_bytes()).hexdigest();self.bindings={'sources/'+p.name:h}
  self.a=[dict(soma_id='cell',c3_id='123',geometry=dict(source_sha256=h,source_model_fit_sha256='fit'),active_model=dict(nodal_candidate_audit=dict(gaps=[dict(native_cable_extent_um=1.9318551123142242)])))];self.b=copy.deepcopy(self.a)
  self.b[0]['geometry'].update(source_soma_root=42,effective_soma_root=42,soma_reference_override=None)
 def tearDown(self):self.tmp.cleanup()
 def compare(self):return compare_geometry(self.a,self.b,self.root,self.bindings,'sources')
 def test_legacy_defaults_are_proven_against_native_source(self):self.assertTrue(self.compare()['passed'])
 def test_rounding_within_one_tenth_picometer_is_tolerated(self):
  self.b[0]['active_model']['nodal_candidate_audit']['gaps'][0]['native_cable_extent_um']+=5.960464477539063e-8
  r=self.compare();self.assertTrue(r['passed']);self.assertEqual(len(r['rounded_lengths']),1)
 def test_real_changes_remain_failures(self):
  cases=[lambda x:x[0]['geometry'].update(effective_soma_root=43),lambda x:x[0]['geometry'].update(soma_reference_override={'root':42}),lambda x:x[0]['geometry'].pop('source_soma_root'),lambda x:x[0]['geometry'].update(source_sha256='wrong'),lambda x:x[0]['geometry'].update(source_model_fit_sha256='other'),lambda x:x[0].update(c3_id='124'),lambda x:x.append(copy.deepcopy(x[0])),lambda x:x[0].update(unknown_field=1),lambda x:x[0]['active_model']['nodal_candidate_audit']['gaps'][0].update(native_cable_extent_um=1.932),lambda x:x[0]['active_model']['nodal_candidate_audit']['gaps'][0].update(native_cable_extent_um=float('nan'))]
  original=copy.deepcopy(self.b)
  for mutate in cases:
   with self.subTest(mutation=mutate):
    self.b=copy.deepcopy(original);mutate(self.b);self.assertFalse(self.compare()['passed'])
 def test_native_file_tampering_fails(self):
  np.savez(self.root/'123_bridged.npz',soma_root=np.array(43));self.assertFalse(self.compare()['passed'])
 def test_other_float_fields_remain_exact(self):
  self.a[0]['other']=1.;self.b[0]['other']=1.+1e-9;self.assertFalse(self.compare()['passed'])
 def test_inputs_not_mutated(self):
  a=copy.deepcopy(self.a);b=copy.deepcopy(self.b);self.compare();self.assertEqual(a,self.a);self.assertEqual(b,self.b)
if __name__=='__main__':unittest.main()
