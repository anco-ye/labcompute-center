import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('toolchain_rpc',Path(os.environ.get('LINUX_RPC_PATH',str(Path(__file__).parents[1]/'external/linux_rpc.py'))))
rpc=importlib.util.module_from_spec(spec);spec.loader.exec_module(rpc)

class ToolchainPathTests(unittest.TestCase):
    def test_default_keeps_selected_python_first(self):
        with patch.object(rpc,'HOST_CFG',{}):
            self.assertEqual(rpc.execution_path(Path('/runtime/bin/python')),'/runtime/bin:/usr/bin:/bin')

    def test_private_toolchain_is_exposed_without_global_path_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);directory=root/'runtimes/compiler/bin';directory.mkdir(parents=True)
            with patch.object(rpc,'ROOT',root),patch.object(rpc,'HOST_CFG',{'native_toolchain_bin':str(directory)}):
                self.assertEqual(rpc.execution_path(Path('/runtime/bin/python')),f'/runtime/bin:{directory.resolve()}:/usr/bin:/bin')

    def test_missing_or_outside_toolchain_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            for directory in ['/usr/bin',str(Path(tmp)/'runtimes/missing')]:
                with self.subTest(directory=directory),patch.object(rpc,'ROOT',Path(tmp)),patch.object(rpc,'HOST_CFG',{'native_toolchain_bin':directory}):
                    with self.assertRaises(ValueError):rpc.execution_path(Path('/runtime/bin/python'))

if __name__=='__main__':unittest.main()
