"""Adapt only NEURON Vector arguments; leave all other NumPy semantics intact."""
import importlib.util
from functools import wraps
import threading
_state=threading.local()
RELEASE='2026-10-24-neuron-vector-compat-v1'


def install():
    if importlib.util.find_spec('numpy') is None:
        return False
    import numpy as np
    if getattr(np,'_lab_vector_compat',None)==RELEASE:
        return True
    def adapt(original):
        def convert(value):
            # Do not import NEURON: loading it could select a task-local DLL.
            if not getattr(_state,'converting',False) and type(value).__module__=='hoc':
                try:is_vector=value.hname().startswith('Vector[')
                except (AttributeError,RuntimeError,TypeError):is_vector=False
                if is_vector:
                    _state.converting=True
                    try:return value.as_numpy()
                    finally:_state.converting=False
            return value
        @wraps(original)
        def call(*args,**kwargs):
            if args:
                args=(convert(args[0]),*args[1:])
            else:
                key='object' if original.__name__=='array' else 'a'
                if key in kwargs:kwargs=dict(kwargs,**{key:convert(kwargs[key])})
            return original(*args,**kwargs)
        return call
    np.array=adapt(np.array)
    np.asarray=adapt(np.asarray)
    np._lab_vector_compat=RELEASE
    return True


def enable_on_neuron_import():
    """Do not load NumPy/NEURON, native mechanisms or alter non-NEURON tasks."""
    import sys
    import importlib.abc
    from importlib.machinery import PathFinder
    if 'neuron' in sys.modules:
        install()
        return
    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self,fullname,path=None,target=None):
            if fullname!='neuron':return None
            spec=PathFinder.find_spec(fullname,path)
            if spec is None or spec.loader is None:return None
            original=spec.loader;finder=self
            class Loader:
                def create_module(self,spec):
                    return original.create_module(spec)if hasattr(original,'create_module')else None
                def exec_module(self,module):
                    original.exec_module(module)
                    install()
                    if finder in sys.meta_path:sys.meta_path.remove(finder)
                def __getattr__(self,name):return getattr(original,name)
            spec.loader=Loader()
            return spec
    sys.meta_path.insert(0,Finder())
