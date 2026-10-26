import unittest
from unittest.mock import Mock
from external.ssh_bridge import Bridge
class RpcDeadlineTest(unittest.TestCase):
 def test_status_open_and_read_use_short_deadline_and_close_channel(self):
  b=Bridge.__new__(Bridge);b.cfg={'remote_python':'python','workspace':'/center'};c=Mock();i,o,e=Mock(),Mock(),Mock();o.read.return_value=b'{"state":"archiving"}';e.read.return_value=b'';o.channel.recv_exit_status.return_value=0;c.exec_command.return_value=(i,o,e)
  self.assertEqual(b.rpc(c,{'action':'status'})['state'],'archiving');self.assertEqual(c.exec_command.call_args.kwargs['timeout'],20);o.channel.close.assert_called_once()
 def test_invalid_response_still_releases_channel(self):
  b=Bridge.__new__(Bridge);b.cfg={'remote_python':'python','workspace':'/center'};c=Mock();i,o,e=Mock(),Mock(),Mock();o.read.return_value=b'bad';e.read.return_value=b'';o.channel.recv_exit_status.return_value=0;c.exec_command.return_value=(i,o,e)
  with self.assertRaises(ValueError):b.rpc(c,{'action':'status'})
  o.channel.close.assert_called_once()
