from pathlib import Path
import unittest

class RetiredDeploymentEntrypoints(unittest.TestCase):
    def assert_refuses_before_side_effects(self,name):
        source=(Path(__file__).parents[1]/'deploy'/name).read_text()
        # Before file reads, service changes or task registration, legacy entry
        # points must refuse instead of restoring obsolete execution paths.
        statements=[line.strip()for line in source.splitlines()if line.strip()and not line.lstrip().startswith('#')]
        statements=[line for line in statements if not line.startswith('param(')and not line.startswith('$ErrorActionPreference=')]
        self.assertTrue(statements[0].startswith("throw 'Retired:"),statements[0])

    def test_legacy_upgrade_cannot_stop_busy_center(self):
        self.assert_refuses_before_side_effects('upgrade.ps1')

    def test_retired_scientific_controller_cannot_be_registered(self):
        self.assert_refuses_before_side_effects('install_science_controls.ps1')
