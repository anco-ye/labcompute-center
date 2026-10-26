import tempfile
import unittest
from pathlib import Path
from artifact_spool import preserve_active_prefix


class PrefixRetentionTests(unittest.TestCase):
    def test_open_writer_growth_and_unlink_keep_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'result.upload'
            with source.open('wb') as writer:
                writer.write(b'prefix'); writer.flush()
                receipt = preserve_active_prefix(source, root / 'retained')
                alias = Path(receipt['alias'])
                writer.write(b'-tail'); writer.flush()
                self.assertEqual(alias.read_bytes(), b'prefix-tail')
                self.assertEqual(preserve_active_prefix(source, root / 'retained')['alias'], str(alias))
            source.unlink()
            self.assertEqual(alias.read_bytes(), b'prefix-tail')
            self.assertFalse(receipt['independent_backup'])

    def test_conflicting_alias_preserves_both_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / 'result.upload'
            source.write_bytes(b'current')
            retained = root / 'retained'; retained.mkdir()
            alias = retained / 'result.upload.keep'; alias.write_bytes(b'older')
            with self.assertRaises(FileExistsError):
                preserve_active_prefix(source, retained)
            self.assertEqual(source.read_bytes(), b'current')
            self.assertEqual(alias.read_bytes(), b'older')

    def test_source_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); actual = root / 'actual'; actual.write_bytes(b'data')
            source = root / 'result.upload'
            try:
                source.symlink_to(actual)
            except OSError:
                self.skipTest('symlinks unavailable for this account')
            with self.assertRaises(ValueError):
                preserve_active_prefix(source, root / 'retained')
            self.assertEqual(actual.read_bytes(), b'data')
