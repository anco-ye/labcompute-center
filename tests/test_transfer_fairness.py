import unittest
from common import fair_transfer_order
class FairTransfers(unittest.TestCase):
 def test_small_first_but_continuous_small_arrivals_cannot_starve_large(self):
  waiting={};served={};large={'id':'large','size':10000}
  for now in range(40):
   small={'id':'small'+str(now),'size':1};jobs=[large,small]
   first=fair_transfer_order(jobs,waiting,served,lambda j:j['size'],now)[0]
   if now==0:self.assertEqual(first['id'],'small0')
   if now==30:self.assertEqual(first['id'],'large')
   served[first['id']]=now
 def test_oldest_overdue_pass_first_and_removed_identity_pruned(self):
  waiting={'a':0,'b':0,'removed':0};served={'a':4,'b':2,'removed':1}
  result=fair_transfer_order([{'id':'a'},{'id':'b'}],waiting,served,lambda j:0,40)
  self.assertEqual([j['id']for j in result],['b','a']);self.assertNotIn('removed',waiting);self.assertNotIn('removed',served)
 def test_identity_and_resource_fields_unchanged(self):
  jobs=[dict(id='a',lease='original',spec={'cores':8})];result=fair_transfer_order(jobs,{}, {},lambda j:0,0)
  self.assertIs(result[0],jobs[0]);self.assertEqual(result[0]['spec'],{'cores':8})
