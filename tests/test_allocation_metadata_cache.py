"""Operational resource-cache tests; no scientific model is run or compared."""
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
import resource_contracts as rc
from unittest.mock import patch


class AllocationMetadataCacheTest(unittest.TestCase):
    def test_dispatch_wrapper_does_not_discard_completed_allocation_counts(self):
        with tempfile.TemporaryDirectory() as root:
            def package(name, wrapper, data=b'allocation input', engine=b'engine'):
                p=Path(root)/name
                with zipfile.ZipFile(p,'w') as z:
                    z.writestr('main.py',wrapper)
                    z.writestr(rc.ROOT+'/scripts/full_window_network_fixed_active_v3.py',engine)
                    z.writestr(rc.ROOT+'/inputs.bin',data)
                stat=p.stat()
                return rc.allocation_group(str(p),stat.st_size,stat.st_mtime_ns,'env')
            original=package('a.zip',b'old wrapper')
            self.assertEqual(original,package('b.zip',b'computation wrapper'))
            self.assertNotEqual(original,package('c.zip',b'wrapper',data=b'other inputs'))
            self.assertNotEqual(original,package('d.zip',b'wrapper',engine=b'other engine'))

    def test_retired_result_learning_and_reference_inspection_removed(self):
        self.assertFalse(hasattr(rc,'_completed'))
        self.assertFalse(hasattr(rc,'register_completed'))
        self.assertFalse(hasattr(rc,'_pair_shapes'))


if __name__=='__main__':unittest.main()
