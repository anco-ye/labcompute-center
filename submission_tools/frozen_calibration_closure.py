"""Restore missing declared calibration inputs from their frozen identity record."""
import hashlib
import json
from pathlib import Path


def restore_calibration_inputs(destination, reference):
    destination=Path(destination)
    record_path=Path(reference['reference_result'])
    record=json.loads(record_path.read_text())
    source=record_path.parents[2]
    pending=[]
    identities=dict(record.get('runtime_source_hashes',{}))
    # Voltage-clamp receipts also have an original calibration input with the
    # same stem. The network loader opens that input, not the _vc receipt.
    # Preserve the original frozen file; never synthesize it from a receipt.
    for name,expected in list(identities.items()):
        if not name.startswith('synapse_tests_') or not name.endswith('_vc.json'):continue
        relative=Path(name)
        if relative.is_absolute() or '..' in relative.parts:raise ValueError('Unsafe frozen input path')
        receipt=source/relative
        if not receipt.is_file():continue
        if hashlib.sha256(receipt.read_bytes()).hexdigest()!=expected:
            raise ValueError('Frozen input identity mismatch: '+name)
        companion=name[:-8]+'.json'
        original=source/companion
        if original.is_file() and companion not in identities:
            identities[companion]=hashlib.sha256(original.read_bytes()).hexdigest()
    for name,expected in identities.items():
        if not name.startswith('synapse_tests_') or not name.endswith('.json'):continue
        relative=Path(name)
        if relative.is_absolute() or '..' in relative.parts:raise ValueError('Unsafe frozen input path')
        target=destination/relative
        origin=target if target.exists() else source/relative
        if not origin.is_file():raise ValueError('Missing original frozen input: '+name)
        data=origin.read_bytes()
        if hashlib.sha256(data).hexdigest()!=expected:raise ValueError('Frozen input identity mismatch: '+name)
        if not target.exists():pending.append((target,data,name,expected))
    # Verify the complete closure before writing any missing file.
    for target,data,_,_ in pending:
        target.parent.mkdir(parents=True,exist_ok=True)
        with target.open('xb') as f:f.write(data)
    return [{'name':name,'sha256':expected,'bytes':len(data)}for _,data,name,expected in pending]
