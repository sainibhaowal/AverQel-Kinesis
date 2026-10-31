"""Trace package imports used by PyInstaller's Windows DLL discovery."""

from __future__ import annotations

import time

from PyInstaller.isolated import Python as IsolatedPython

_original_call = IsolatedPython.call


def _trace_isolated_call(self, function, *args, **kwargs):
    if function.__name__ != "import_library" or not args:
        return _original_call(self, function, *args, **kwargs)

    package = str(args[0])
    started = time.perf_counter()
    print(f"PYINSTALLER_DLL_IMPORT_START {package}", flush=True)
    result = _original_call(self, function, *args, **kwargs)
    elapsed = time.perf_counter() - started
    print(f"PYINSTALLER_DLL_IMPORT_DONE {package} {elapsed:.2f}s", flush=True)
    return result


IsolatedPython.call = _trace_isolated_call

from PyInstaller.__main__ import run

run()
