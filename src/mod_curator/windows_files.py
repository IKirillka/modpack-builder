"""Win32 file handles for local operations that must not follow reparse points."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import ctypes
from ctypes import wintypes
import msvcrt
import os

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
create_file = kernel32.CreateFileW
create_file.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
create_file.restype = wintypes.HANDLE
close_handle = kernel32.CloseHandle
close_handle.argtypes = (wintypes.HANDLE,)
close_handle.restype = wintypes.BOOL
file_info = kernel32.GetFileInformationByHandleEx
file_info.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
file_info.restype = wintypes.BOOL

FILE_LIST_DIRECTORY = 0x01
FILE_READ_ATTRIBUTES = 0x80
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x01
FILE_SHARE_WRITE = 0x02
OPEN_EXISTING = 3
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
INVALID_HANDLE = wintypes.HANDLE(-1).value


class FileAttributeTagInfo(ctypes.Structure):
    _fields_ = [("attributes", wintypes.DWORD), ("reparse_tag", wintypes.DWORD)]


def _open(path, *, directory):
    # No FILE_SHARE_DELETE. Ancestors stay in place until the operation finishes.
    # Use an extended path for names beyond the legacy MAX_PATH limit.
    name = str(path)
    if name.startswith("\\\\?\\"):
        pass
    elif name.startswith("\\\\"):
        name = "\\\\?\\UNC\\" + name[2:]
    else:
        name = "\\\\?\\" + name
    access = FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES if directory else GENERIC_READ
    handle = create_file(name, access,
                         FILE_SHARE_READ | (FILE_SHARE_WRITE if directory else 0), None, OPEN_EXISTING,
                         FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS, None)
    if handle == INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = FileAttributeTagInfo()
        if not file_info(handle, FILE_ATTRIBUTE_TAG_INFO_CLASS, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if info.attributes & FILE_ATTRIBUTE_REPARSE_POINT:
            raise OSError(f"Refusing reparse point: {path}")
        if bool(info.attributes & FILE_ATTRIBUTE_DIRECTORY) != directory:
            raise OSError(f"Unexpected file type: {path}")
        return handle
    except BaseException:
        close_handle(handle)
        raise


@contextmanager
def pinned_directory(path, *, parents=True):
    """Hold a directory and optionally each ancestor without sharing delete access."""
    with ExitStack() as stack:
        for component in (*reversed(path.parents), path) if parents else (path,):
            stack.callback(close_handle, _open(component, directory=True))
        yield


def open_read(path):
    handle = _open(path, directory=False)
    try:
        # Ownership of the Windows handle passes to the CRT descriptor.
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close_handle(handle)
        raise
    return os.fdopen(fd, "rb")
