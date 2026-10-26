import copy
import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import resource_metadata
import resource_policy
from test_active_reservations import ActiveReservationsTest


class BoundReservationsTest(unittest.TestCase):
    def test_one_complete_exact_bound_run_with_larger_margin_and_no_spec_change(self):
        now, model, spec, usage, samples = ActiveReservationsTest().fixture()
        model.update(input_binding_sha256='a'*64, growth_bound='verified_retained_allowance_v1')
        original = copy.deepcopy(spec)
        hold = resource_policy.running_reservation(spec, model, samples[:1], usage, now)
        self.assertEqual(hold['samples'], 1)
        self.assertEqual(hold['evidence_scope'], 'bounded_same_inputs')
        self.assertEqual(hold['safety_factor'], 1.5)
        self.assertGreaterEqual(hold['memory_gb'],max(usage['peak_gb'],samples[0]['observation']['peak_gb'])*1.5)
        self.assertEqual(spec, original)

    def test_missing_or_different_bound_input_scale_proof_or_cleanup_cannot_supply_sample(self):
        for change in ('binding', 'features', 'proof', 'cleanup', 'family'):
            now, model, spec, usage, samples = ActiveReservationsTest().fixture()
            model.update(input_binding_sha256='a'*64, growth_bound='verified_retained_allowance_v1')
            samples = copy.deepcopy(samples[:1])
            if change == 'binding': samples[0]['model']['input_binding_sha256'] = 'b'*64
            if change == 'features': samples[0]['model']['features']['cells'] = 29
            if change == 'proof': samples[0]['model'].pop('growth_bound')
            if change == 'cleanup': samples[0]['observation']['cleanup_verified'] = False
            if change == 'family': samples[0]['model']['family'] = 'different'
            self.assertIsNone(resource_policy.running_reservation(spec, model, samples, usage, now), change)

    def test_unknown_program_or_incomplete_current_run_is_not_a_completed_reference(self):
        now, model, spec, usage, samples = ActiveReservationsTest().fixture()
        self.assertIsNone(resource_policy.running_reservation(spec, model, samples[:1], usage, now))
        model.update(input_binding_sha256='a'*64, growth_bound='verified_retained_allowance_v1')
        self.assertIsNone(resource_policy.running_reservation(spec, model, [], usage, now))
        hold=resource_policy.running_reservation(spec, model, samples[:1], dict(usage, peak_gb=15), now)
        self.assertEqual(hold['memory_gb'],spec['memory_gb'])  # high current use prevents a reduction

    def test_missing_dimension_bound_to_real_package_and_environment_not_a_guessed_count(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'package.zip'
            table = {key: dict(id=key, environment=env, stage='long_network', duration_ms=5000,
                              condition='weak', input_scale=dict(modeled_cells=100, recorded_segments=None))
                     for key, env in [('one', 'env-a'), ('two', 'env-b')]}
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('main.py', '"""Self-contained H01 batch entry. All writes are confined to this task workdir."""\n' +
                                 "flags=['--no-savecurrents','--buffer-ms','1']")
                archive.writestr('task_table.json', json.dumps(table))
                archive.writestr('source_manifests.json', '{}')
                archive.writestr('scripts/full_window_network_fixed_active_v3.py',
                                 "for vector in im+[t]+vv:vector.resize(0)\nnp.arange(args.buffer_ms,duration,args.buffer_ms)\nraw.append(ima@coeff.T)\nvolts[n:]=va\nmax(100,int(duration*4))")
            spec = dict(kind='python', runtime='python-neuron', entry='main.py', cores=1, task_key='one')
            one = resource_metadata.current_model(path, spec)
            two = resource_metadata.current_model(path, dict(spec, task_key='two'))
            self.assertNotIn('segments', one['features'])
            self.assertNotEqual(one['family'], two['family'])
            expected = hashlib.sha256(json.dumps([hashlib.sha256(path.read_bytes()).hexdigest(), 'env-a']).encode()).hexdigest()
            self.assertEqual(one['input_binding_sha256'], expected)


if __name__ == '__main__':
    unittest.main()
