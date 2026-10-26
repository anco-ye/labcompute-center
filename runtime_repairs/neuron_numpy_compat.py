"""Process-local NEURON 9.0.2 Vector.as_numpy compatibility adapter, v1.

Bypass the native as_numpy callback ownership path. Keep a writable zero-copy
float64 view using NEURON's existing numpy_from_pointer helper. Opt-in only;
this module never changes installed libraries or other running processes.
"""
import hashlib
import json
from pathlib import Path
import numpy as np
import neuron
from neuron import h, numpy_from_pointer

_installed = False
_previous = None

def _as_numpy(vector):
    size = int(vector.size())
    if not size:
        return np.empty(0, dtype=np.float64)
    view = np.asarray(vector)
    return numpy_from_pointer(view.ctypes.data, size)

def install(receipt=None):
    global _installed, _previous
    if not str(neuron.__version__).startswith('9.0.2'):
        raise RuntimeError('This compatibility adapter is validated only for NEURON 9.0.2')
    if not _installed:
        _previous = h.Vector.__dict__.get("as_numpy")
        h.Vector.as_numpy = _as_numpy
        _installed = True
    if receipt:
        Path(receipt).write_text(json.dumps(dict(
            adapter='neuron902-vector-as-numpy-v1', neuron_version=neuron.__version__,
            neuron_build=h.nrnversion(), numpy_version=np.__version__,
            module_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            scope='process-local array-conversion compatibility; scientific source and parameters unchanged'),indent=2),encoding='utf-8')


def restore():
    global _installed
    if _installed:
        if _previous is None:
            del h.Vector.as_numpy
        else:
            h.Vector.as_numpy = _previous
        _installed = False
