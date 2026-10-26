"""Run an entry once; resolve missing imports in its private environment."""
import builtins
import importlib
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys
from common import atomic_json


PACKAGES = {'sklearn': 'scikit-learn', 'PIL': 'Pillow', 'cv2': 'opencv-python',
            'yaml': 'PyYAML', 'bs4': 'beautifulsoup4', 'skimage': 'scikit-image',
            'serial': 'pyserial', 'OpenSSL': 'pyOpenSSL', 'fitz': 'PyMuPDF'}


def main():
    entry = Path(sys.argv[1]).resolve()
    sys.argv = sys.argv[1:]
    sys.path[0] = str(entry.parent)
    folder = Path(os.environ['LAB_JOB_DIR'])
    original = builtins.__import__
    original_dynamic = importlib.import_module
    attempted = set()
    installing = False
    work = Path.cwd().resolve()

    def task_caller():
        try: return Path(sys._getframe(2).f_code.co_filename).resolve().is_relative_to(work)
        except (ValueError, OSError): return False

    def resolve(exc):
        nonlocal installing
        name = exc.name or ''
        if installing or '.' in name or name in sys.stdlib_module_names or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
            raise exc
        if name in attempted or len(attempted) >= 24:
            raise RuntimeError(f'依赖 {name} 安装后仍无法导入，或已达到自动安装数量上限；请在 requirements.txt 中指定正确包名和版本') from exc
        attempted.add(name)
        package = PACKAGES.get(name, name)
        installing = True
        atomic_json(folder/'dependency-status.json',dict(status='installing',package=package))
        try:
            print(f'\n[自动安装依赖] import {name} → {package}', flush=True)
            args = [sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', '--retries', '3', '--timeout', '30']
            wheels = os.environ.get('LAB_WHEELHOUSE')
            if wheels and Path(wheels).is_dir(): args += ['--find-links', wheels]
            subprocess.run(args + [package], check=True, timeout=600)
            subprocess.run([sys.executable, '-m', 'pip', 'check'], check=True, timeout=60)
            importlib.invalidate_caches()
            atomic_json(folder/'dependency-status.json',dict(status='installed',package=package))
        except (subprocess.SubprocessError, OSError) as error:
            message = f'依赖 {name}（安装包 {package}）自动安装失败：{error}；请查看 pip 日志并修正 requirements.txt 后重试'
            atomic_json(folder/'dependency-status.json',dict(status='failed',package=package))
            atomic_json(folder/'dependency-error.json',dict(detail=message,summary=f'依赖 {name}（安装包 {package}）自动安装失败；请检查包名、网络或依赖版本，详细原因见日志。'))
            raise RuntimeError(message) from error
        finally:
            installing = False

    def importing(name, globals=None, locals=None, fromlist=(), level=0):
        while True:
            try:
                return original(name, globals, locals, fromlist, level)
            except ModuleNotFoundError as exc:
                if level or not task_caller(): raise
                resolve(exc)

    def dynamic(name, package=None):
        while True:
            try:
                return original_dynamic(name, package)
            except ModuleNotFoundError as exc:
                if name.startswith('.') or not task_caller(): raise
                resolve(exc)

    builtins.__import__ = importing
    importlib.import_module = dynamic
    try:
        runpy.run_path(str(entry), run_name='__main__')
    except FileNotFoundError as exc:
        # Only task-local missing files are author errors. Missing runtime binaries,
        # mounts and external paths remain a center investigation.
        trace=exc.__traceback__;runtime_launch=False
        while trace is not None:
            runtime_launch=runtime_launch or trace.tb_frame.f_globals.get('__name__')=='subprocess'
            trace=trace.tb_next
        if exc.filename and not runtime_launch:
            missing=Path(exc.filename).resolve()
            if missing.is_relative_to(work):
                atomic_json(folder/'task-error.json',dict(failure_code='missing_input',
                    detail='任务包内文件不存在：'+str(missing.relative_to(work)),
                    failure_evidence=dict(exception_type='FileNotFoundError',path=str(missing.relative_to(work)))))
        raise
    finally:
        builtins.__import__ = original
        importlib.import_module = original_dynamic


if __name__ == '__main__': main()
