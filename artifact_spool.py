"""Redirect legacy bridge artifact files at an idle file boundary, preserving prefixes."""
import contextlib
import ctypes
import json
import os
from pathlib import Path
import uuid

def preserve_active_prefix(source, retention_dir):
    """Retain a growing legacy upload inode without interrupting its writer.

    This is an alias, not a separate backup or a completed result. It survives
    unlinking of the receiver's temporary name; it does not make a legacy
    sender support Range. Never replace an existing alias for another inode.
    """
    source, retention_dir = Path(source), Path(retention_dir)
    if source.is_symlink() or retention_dir.is_symlink():
        raise ValueError('Prefix retention cannot follow a file/directory symlink')
    original = source.stat()
    if not source.is_file():
        raise ValueError('Prefix source must be a regular file')
    retention_dir.mkdir(parents=True, exist_ok=True)
    alias = retention_dir / (source.name + '.keep')
    if alias.is_symlink():
        raise ValueError('Prefix alias cannot be a symlink')
    try:
        os.link(source, alias)
    except FileExistsError:
        if not os.path.samestat(original, alias.stat()):
            raise FileExistsError('Existing retained prefix belongs to another inode')
    retained = alias.stat()
    if not os.path.samestat(original, retained):
        raise BusyFile('Prefix identity changed during retention; source untouched')
    return dict(state='active_prefix_retained', alias=str(alias),
                bytes=retained.st_size, independent_backup=False)

class BusyFile(OSError):
    pass

@contextlib.contextmanager
def idle_reader(path):
    if os.name != 'nt':
        with Path(path).open('rb') as stream:
            yield stream
        return
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    # Allow readers and replacement, deny existing/new writers. No busy stream
    # is interrupted: acquisition fails while the legacy writer holds its file.
    handle = create(str(Path(path).resolve()), 0x80000000, 1 | 4, None, 3, 0x08000080, None)
    if handle == wintypes.HANDLE(-1).value:
        error = ctypes.get_last_error()
        if error in (32, 33):
            raise BusyFile('Artifact writer still active')
        raise ctypes.WinError(error)
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(fd, 'rb') as stream:
        yield stream


def _binding(path, identity):
    if path.exists():
        previous = json.loads(path.read_text(encoding='utf-8'))
        if previous != identity:
            raise ValueError('Artifact ownership/lease binding changed')
        return
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(identity, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, path)


def redirect(source, destination, identity, *, archive_size=None, reader=idle_reader):
    """Preserve the old prefix by hardlink; atomically install a file alias.

    identity is the caller-verified node/job/lease binding, never a content hash.
    For not-yet-created artifacts, an exclusive symlink creation reserves the
    output location before the existing bridge opens it. Existing bytes are
    copied only under the Windows deny-writer handle.
    """
    source, destination = Path(source), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    binding = destination.parent / 'spool-identity.json'
    _binding(binding, identity)
    if source.is_symlink():
        if source.resolve() != destination.resolve():
            raise ValueError('Artifact already points to a different location')
        return dict(state='already_routed', bytes=destination.stat().st_size)
    if source.with_name('result.zip').exists():
        return dict(state='complete_cache_retained')
    if not source.exists():
        source.parent.mkdir(parents=True, exist_ok=True)
        try:
            with destination.open('xb'):
                pass
        except FileExistsError:
            if destination.stat().st_size:
                raise ValueError('Uninstalled nonempty destination needs explicit recovery')
        try:
            os.symlink(destination, source)
        except FileExistsError:
            raise BusyFile('Artifact was created concurrently; retry at idle boundary')
        return dict(state='future_artifact_routed', bytes=0)
    if not source.is_file():
        raise ValueError('Artifact must be a regular file or our own alias')
    if archive_size is None:
        raise ValueError('Existing partial requires the immutable archive length')
    temporary = destination.with_name(destination.name + '.' + uuid.uuid4().hex + '.copy')
    link = source.with_name(source.name + '.' + uuid.uuid4().hex + '.link')
    backup = source.with_name(source.name + '.before-spool.prefix')
    try:
        with reader(source) as stream:
            info = os.fstat(stream.fileno())
            if not os.path.samestat(info, source.stat()):
                raise BusyFile('Artifact path changed during file acquisition')
            if not 0 < info.st_size < archive_size:
                raise ValueError('Only an incomplete, nonempty prefix can be moved')
            if destination.exists() and destination.stat().st_size > info.st_size:
                raise ValueError('Destination is newer; never roll back received bytes')
            if backup.exists():
                if not os.path.samestat(info, backup.stat()):
                    raise ValueError('Preserved prefix belongs to a different file')
            else:
                os.link(source, backup)
            copied = 0
            with temporary.open('xb') as output:
                while chunk := stream.read(1024 * 1024):
                    output.write(chunk)
                    copied += len(chunk)
                output.flush()
                os.fsync(output.fileno())
            if copied != info.st_size or source.stat().st_size != info.st_size:
                raise BusyFile('Prefix changed; source retained')
            os.replace(temporary, destination)
            os.symlink(destination, link)
            if not os.path.samestat(info, source.stat()):
                raise BusyFile('Artifact replaced during copy; source retained')
            if os.name == 'nt':
                from ctypes import wintypes
                # Rename the reparse point itself, keeping the deny-writer
                # handle on the old prefix. ReplaceFileW follows a symlink and
                # therefore cannot implement this storage redirection.
                kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                create = kernel.CreateFileW
                create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
                create.restype = wintypes.HANDLE
                alias = create(str(link), 0x10000, 7, None, 3, 0x02200000, None)
                if alias == wintypes.HANDLE(-1).value:
                    raise ctypes.WinError(ctypes.get_last_error())
                target = str(source.absolute())
                class RenameInfo(ctypes.Structure):
                    _fields_ = [('Flags', wintypes.DWORD), ('RootDirectory', wintypes.HANDLE),
                                ('FileNameLength', wintypes.DWORD), ('FileName', wintypes.WCHAR * (len(target)+1))]
                info = RenameInfo(3, None, len(target.encode('utf-16-le')), target)
                rename = kernel.SetFileInformationByHandle
                rename.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
                rename.restype = wintypes.BOOL
                try:
                    # FileRenameInfoEx; REPLACE_IF_EXISTS | POSIX_SEMANTICS.
                    # Unsupported systems fail closed without changing source.
                    if not rename(alias, 22, ctypes.byref(info), ctypes.sizeof(info)):
                        raise ctypes.WinError(ctypes.get_last_error())
                finally:
                    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                    kernel.CloseHandle(alias)
            else:
                os.replace(link, source)
            return dict(state='prefix_routed', bytes=copied, backup=str(backup))
    finally:
        # These are only our own temporary files. The original is always kept.
        if temporary.exists():
            temporary.unlink()
        if link.is_symlink():
            link.unlink()
