"""Incremental controller metadata learning, keyed by immutable file identity.

A queue status transition must not reopen every historical result archive.
Only platform resource receipts and existing input metadata adapters are used.
"""
import json
from pathlib import Path


def file_identity(value):
    if not value:return None
    try:
        s=Path(value).stat()
        return (str(value),s.st_size,s.st_mtime_ns)
    except OSError:return (str(value),None,None)


class LearningCache:
    def __init__(self):
        self.rows={}

    def build(self, rows, final_observation, current_model, reservation_model):
        observations={};families={};models={};retained={};identities={}
        def identity(path):
            if path not in identities:identities[path]=file_identity(path)
            return identities[path]
        for row in rows:
            spec=json.loads(row['spec']);mode='queued' if row['status']=='queued' else 'reservation'
            key=(row['spec'],identity(row['package']),
                 identity(row.get('result')) if row['status']=='succeeded' else None,
                 mode,row['status']=='succeeded')
            previous=self.rows.get(row['id'])
            if previous and previous[0]==key:
                model,complete=previous[1:]
            else:
                complete=final_observation(spec,row.get('result')) if row['status']=='succeeded' else None
                model=(current_model if mode=='queued' else reservation_model)(row['package'],spec)
            retained[row['id']]=(key,model,complete)
            models[(row['package'],json.dumps(spec,sort_keys=True))]=model
            revision=spec.get('runtime_revision')
            if row['status']=='succeeded' and complete:
                signature=spec.get('resource_signature')
                if signature and revision:observations.setdefault((row['owner'],signature,revision),[]).append(complete)
                if model and revision:
                    families.setdefault((row['owner'],model['family'],revision),[]).append(dict(id=row['id'],model=model,observation=complete))
        # Commit only a complete rebuild. An adapter error retains the prior cache.
        self.rows=retained
        return dict(observations=observations,families=families,models=models)
