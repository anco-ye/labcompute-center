"""OS-owned execution lifetime. Windows job ownership precedes child creation."""
import os,signal,time
import psutil

class ProcessScope:
    def __init__(self):
        self.handle=None;self.pgid=None;self.port=None;self.limit_hit=False;self.memory_limit_bytes=0
        if os.name=='nt':
            import ctypes
            from ctypes import wintypes as w
            self.ct=ctypes;self.k=ctypes.WinDLL('kernel32',use_last_error=True)
            class BASIC(ctypes.Structure):
                _fields_=[('per_process',ctypes.c_longlong),('per_job',ctypes.c_longlong),('flags',w.DWORD),('min_ws',ctypes.c_size_t),('max_ws',ctypes.c_size_t),('active',w.DWORD),('affinity',ctypes.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
            class IO(ctypes.Structure):_fields_=[(n,ctypes.c_ulonglong) for n in ('ro','wo','oo','rb','wb','ob')]
            class EXT(ctypes.Structure):_fields_=[('basic',BASIC),('io',IO),('process_memory',ctypes.c_size_t),('job_memory',ctypes.c_size_t),('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
            self.k.CreateJobObjectW.argtypes=[ctypes.c_void_p,w.LPCWSTR];self.k.CreateJobObjectW.restype=w.HANDLE
            self.k.GetCurrentProcess.restype=w.HANDLE
            self.k.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD]
            self.k.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]
            self.k.QueryInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD,ctypes.c_void_p]
            self.k.CloseHandle.argtypes=[w.HANDLE]
            self.ext_type=EXT
            self.k.CreateIoCompletionPort.argtypes=[w.HANDLE,w.HANDLE,ctypes.c_size_t,w.DWORD];self.k.CreateIoCompletionPort.restype=w.HANDLE
            self.k.GetQueuedCompletionStatus.argtypes=[w.HANDLE,ctypes.POINTER(w.DWORD),ctypes.POINTER(ctypes.c_size_t),ctypes.POINTER(ctypes.c_void_p),w.DWORD];self.k.GetQueuedCompletionStatus.restype=w.BOOL
            self.handle=self.k.CreateJobObjectW(None,None)
            if not self.handle:raise ctypes.WinError(ctypes.get_last_error())
            limits=EXT();limits.basic.flags=0x2000
            if not self.k.SetInformationJobObject(self.handle,9,ctypes.byref(limits),ctypes.sizeof(limits)):raise ctypes.WinError(ctypes.get_last_error())
            if not self.k.AssignProcessToJobObject(self.handle,self.k.GetCurrentProcess()):raise ctypes.WinError(ctypes.get_last_error())
            self.port=self.k.CreateIoCompletionPort(w.HANDLE(-1),None,0,1)
            if not self.port:raise ctypes.WinError(ctypes.get_last_error())
            class PORT(ctypes.Structure):_fields_=[('key',ctypes.c_void_p),('port',w.HANDLE)]
            port=PORT(None,self.port)
            if not self.k.SetInformationJobObject(self.handle,7,ctypes.byref(port),ctypes.sizeof(port)):raise ctypes.WinError(ctypes.get_last_error())

    def set_memory_limit(self, gib):
        if os.name!='nt':return False
        ct=self.ct;limits=self.ext_type()
        limits.basic.flags=0x2000|0x200
        limits.job_memory=max(1,int(gib*1024**3))
        if not self.k.SetInformationJobObject(self.handle,9,ct.byref(limits),ct.sizeof(limits)):raise ct.WinError(ct.get_last_error())
        self.memory_limit_bytes=limits.job_memory
        state=self.memory_state()
        if state.get('limit_bytes')!=self.memory_limit_bytes:raise RuntimeError('Kernel memory limit was not applied')
        return True
    def memory_state(self):
        if os.name!='nt':return dict(verified=False)
        ct=self.ct;limits=self.ext_type()
        if not self.k.QueryInformationJobObject(self.handle,9,ct.byref(limits),ct.sizeof(limits),None):raise ct.WinError(ct.get_last_error())
        return dict(verified=bool(limits.basic.flags&0x200),limit_bytes=limits.job_memory,peak_bytes=limits.peak_job)
    def memory_limit_exceeded(self):
        if os.name!='nt':return False
        ct=self.ct
        from ctypes import wintypes as w
        for _ in range(256):
            message=w.DWORD();key=ct.c_size_t();data=ct.c_void_p()
            if not self.k.GetQueuedCompletionStatus(self.port,ct.byref(message),ct.byref(key),ct.byref(data),0):break
            if message.value in (9,10):self.limit_hit=True
        return self.limit_hit
    def launch_options(self):return {} if os.name=='nt' else {'start_new_session':True}
    def started(self,p):
        if os.name!='nt':self.pgid=p.pid
    def members(self):
        if os.name=='nt':
            ct=self.ct;buf=ct.create_string_buffer(8+ct.sizeof(ct.c_size_t)*65536)
            if not self.k.QueryInformationJobObject(self.handle,3,buf,len(buf),None):raise ct.WinError(ct.get_last_error())
            count=ct.c_ulong.from_buffer(buf,4).value
            ids=(ct.c_size_t*count).from_buffer(buf,8)
            rows=[]
            for pid in ids:
                if pid==os.getpid():continue
                try:rows.append(psutil.Process(pid))
                except psutil.NoSuchProcess:pass
            return rows
        rows=[]
        if self.pgid:
            for p in psutil.process_iter():
                try:
                    if os.getpgid(p.pid)==self.pgid and p.status()!=psutil.STATUS_ZOMBIE:rows.append(p)
                except (ProcessLookupError,PermissionError,psutil.Error):pass
        return rows
    def clean(self):
        for p in self.members():
            try:p.kill()
            except psutil.NoSuchProcess:pass
        if os.name!='nt' and self.pgid:
            try:os.killpg(self.pgid,signal.SIGKILL)
            except (ProcessLookupError,PermissionError):pass
        end=time.monotonic()+5
        while time.monotonic()<end:
            live=[]
            for p in self.members():
                try:
                    if p.is_running() and p.status()!=psutil.STATUS_ZOMBIE:live.append(p)
                except psutil.NoSuchProcess:pass
            if not live:return True
            time.sleep(.05)
        return False
