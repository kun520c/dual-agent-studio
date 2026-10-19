"""Verify the actual opened handle, including a link replaced during open."""
import os
from pathlib import Path
if os.name=='nt':
    import ctypes,msvcrt
    from ctypes import wintypes
    _kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    _final_path=_kernel.GetFinalPathNameByHandleW
    _final_path.argtypes=[wintypes.HANDLE,wintypes.LPWSTR,wintypes.DWORD,wintypes.DWORD]
    _final_path.restype=wintypes.DWORD

def opened_under_root(stream, root):
    if os.name=='nt':
        buffer=ctypes.create_unicode_buffer(32768)
        count=_final_path(msvcrt.get_osfhandle(stream.fileno()),buffer,len(buffer),0)
        if not count or count>=len(buffer):raise ValueError('无法验证历史文件句柄路径')
        name=buffer.value
        if name.startswith('\\\\?\\UNC\\'):name='\\\\'+name[8:]
        elif name.startswith('\\\\?\\'):name=name[4:]
        actual=Path(name)
    else:
        actual=Path(os.readlink(f'/proc/self/fd/{stream.fileno()}'))
    if not actual.resolve().is_relative_to(root):raise ValueError('历史文件句柄越过只读根目录')
