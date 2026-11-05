"""Read-only process liveness checks used only to identify confirmed exits."""
import ctypes
from ctypes import wintypes
import os


def _windows_pid_alive(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        error = ctypes.get_last_error()
        return False if error == 87 else True if error == 5 else None
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel.CloseHandle(handle)


def pid_alive(pid):
    """Return True, False or None (unknown).

    A PID can be reused: True does not prove this is the original process.
    Only False is evidence that the recorded process has ended.
    """
    if type(pid) is not int or pid <= 0:
        raise ValueError("pid must be a positive integer")
    if os.name == "nt":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True
