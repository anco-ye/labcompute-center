"""Measured admission pressure; committed bytes are not resident RAM.

PDH samples span heartbeat intervals. Warm-up, hysteresis and timestamps prevent
one transient observation from changing capacity. No external process is changed.
"""
import ctypes
import math
import os
import time
from collections import deque


class WindowsCounters:
    PATHS={
        'pages_in':r'\Memory\Pages Input/sec',
        'pages_out':r'\Memory\Pages Output/sec',
        'read_latency':r'\PhysicalDisk(_Total)\Avg. Disk sec/Read',
    }
    def __init__(self):
        self.query=None;self.counters={}
        if os.name!='nt':return
        try:
            self.dll=ctypes.WinDLL('pdh')
            handle=ctypes.c_void_p
            self.dll.PdhOpenQueryW.argtypes=[ctypes.c_wchar_p,ctypes.c_size_t,ctypes.POINTER(handle)]
            self.dll.PdhAddEnglishCounterW.argtypes=[handle,ctypes.c_wchar_p,ctypes.c_size_t,ctypes.POINTER(handle)]
            self.dll.PdhCollectQueryData.argtypes=[handle]
            self.dll.PdhCloseQuery.argtypes=[handle]
            self.query=handle()
            if self.dll.PdhOpenQueryW(None,0,ctypes.byref(self.query)):raise OSError('PDH query')
            for key,path in self.PATHS.items():
                counter=handle()
                if self.dll.PdhAddEnglishCounterW(self.query,path,0,ctypes.byref(counter)):raise OSError('PDH counter')
                self.counters[key]=counter
            self.dll.PdhCollectQueryData(self.query)
        except (OSError,AttributeError):self.close()
    def close(self):
        if self.query:
            self.dll.PdhCloseQuery(self.query)
        self.query=None
    def read(self):
        if not self.query:return None
        class Value(ctypes.Union):
            _fields_=[('double',ctypes.c_double),('large',ctypes.c_int64),('long',ctypes.c_int32)]
        class Formatted(ctypes.Structure):
            _anonymous_=('value',)
            _fields_=[('status',ctypes.c_uint32),('value',Value)]
        get=self.dll.PdhGetFormattedCounterValue
        get.argtypes=[ctypes.c_void_p,ctypes.c_uint32,ctypes.c_void_p,ctypes.POINTER(Formatted)]
        if self.dll.PdhCollectQueryData(self.query):return None
        result={}
        for key,counter in self.counters.items():
            value=Formatted()
            if get(counter,0x200,None,ctypes.byref(value)) or value.status not in (0,1):return None
            if not math.isfinite(value.double) or value.double<0:return None
            result[key]=value.double
        return result


class PressureWindow:
    def __init__(self):
        self.rows=deque();self.state='warming';self.good_since=None
    def observe(self,now,*,cpu_percent,total_gb,available_gb,reserve_gb,managed_cores=0,cpu_threads=1,counters=None):
        if self.rows and (now<=self.rows[-1]['at'] or now-self.rows[-1]['at']>20):
            self.rows.clear();self.state='warming';self.good_since=None
        valid=counters is not None
        low=available_gb<reserve_gb
        paging=valid and ((counters['pages_in']>=512 and counters['pages_out']>=512)
                          or (counters['pages_in']>=256 and counters['read_latency']>=.02)
                          or (counters['pages_out']>=256 and available_gb<max(reserve_gb*2,total_gb*.15)))
        row=dict(at=now,bad=low or paging,valid=valid,cpu=cpu_percent,
                 external_cores=max(0,cpu_percent*cpu_threads/100-managed_cores),available_gb=available_gb)
        self.rows.append(row)
        while self.rows and self.rows[0]['at']<now-60:self.rows.popleft()
        recent=list(self.rows)[-5:]
        span=self.rows[-1]['at']-self.rows[0]['at']
        verified=span>=20 and len(self.rows)>=5 and all(x['valid'] for x in recent)
        if not valid:self.state='unknown';self.good_since=None
        elif available_gb<max(1,reserve_gb*.5) or (verified and sum(x['bad'] for x in recent)>=3):
            self.state='pressured';self.good_since=None
        elif self.state=='pressured':
            if row['bad']:self.good_since=None
            elif self.good_since is None:self.good_since=now
            elif now-self.good_since>=30:self.state='normal'
        elif verified:self.state='normal'
        else:self.state='warming'
        # High percentile over a minute, not a single low-CPU heartbeat.
        external=sorted(x['external_cores'] for x in self.rows)
        cpu=sorted(x['cpu'] for x in self.rows)
        p90=lambda x:x[min(len(x)-1,int(len(x)*.9))]
        return dict(at=now,window_seconds=span,verified=verified,state=self.state,
                    cpu_percent_p90=p90(cpu),external_cpu_cores=p90(external),
                    available_memory_min_gb=min(x['available_gb'] for x in self.rows),
                    **(counters or {}))
