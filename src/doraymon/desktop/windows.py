"""Windows lifecycle helpers, with no robot or chat process management."""

import ctypes
import hashlib
from ctypes import wintypes


class DesktopInstance:
    def __init__(self, identity: str):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        name = "Local\\DoraYmon.Desktop." + hashlib.sha256(identity.casefold().encode()).hexdigest()
        self.handle = self.kernel.CreateMutexW(None, False, name)
        if not self.handle:
            raise OSError("Cannot create desktop instance mutex")
        self.existing = ctypes.get_last_error() == 183

    def show_existing(self) -> None:
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user.FindWindowW.restype = wintypes.HWND
        user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user.SetForegroundWindow.argtypes = [wintypes.HWND]
        window = user.FindWindowW(None, "DoraYmon")
        if window:
            user.ShowWindow(window, 9)
            user.SetForegroundWindow(window)

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
