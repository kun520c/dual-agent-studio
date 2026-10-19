"""Read Windows working set and private usage for this app process tree."""
import ctypes
from ctypes import wintypes
import os

class PROCESSENTRY32W(ctypes.Structure):
    _fields_=[('dwSize',wintypes.DWORD),('cntUsage',wintypes.DWORD),('th32ProcessID',wintypes.DWORD),('th32DefaultHeapID',ctypes.c_size_t),('th32ModuleID',wintypes.DWORD),('cntThreads',wintypes.DWORD),('th32ParentProcessID',wintypes.DWORD),('pcPriClassBase',wintypes.LONG),('dwFlags',wintypes.DWORD),('szExeFile',wintypes.WCHAR*260)]
class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD),('PeakWorkingSetSize',ctypes.c_size_t),('WorkingSetSize',ctypes.c_size_t),('QuotaPeakPagedPoolUsage',ctypes.c_size_t),('QuotaPagedPoolUsage',ctypes.c_size_t),('QuotaPeakNonPagedPoolUsage',ctypes.c_size_t),('QuotaNonPagedPoolUsage',ctypes.c_size_t),('PagefileUsage',ctypes.c_size_t),('PeakPagefileUsage',ctypes.c_size_t),('PrivateUsage',ctypes.c_size_t)]

# Bind Windows FFI once, with explicit ABI types and persistent DLL ownership.
kernel=ctypes.WinDLL('kernel32',use_last_error=True);psapi=ctypes.WinDLL('psapi',use_last_error=True)
kernel.CreateToolhelp32Snapshot.argtypes=[wintypes.DWORD,wintypes.DWORD];kernel.CreateToolhelp32Snapshot.restype=wintypes.HANDLE
kernel.Process32FirstW.argtypes=[wintypes.HANDLE,ctypes.POINTER(PROCESSENTRY32W)];kernel.Process32FirstW.restype=wintypes.BOOL
kernel.Process32NextW.argtypes=[wintypes.HANDLE,ctypes.POINTER(PROCESSENTRY32W)];kernel.Process32NextW.restype=wintypes.BOOL
kernel.CloseHandle.argtypes=[wintypes.HANDLE];kernel.CloseHandle.restype=wintypes.BOOL
kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];kernel.OpenProcess.restype=wintypes.HANDLE
psapi.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX),wintypes.DWORD];psapi.GetProcessMemoryInfo.restype=wintypes.BOOL

def processes():
    snapshot=kernel.CreateToolhelp32Snapshot(2,0)
    entry=PROCESSENTRY32W();entry.dwSize=ctypes.sizeof(entry);result=[]
    try:
        ok=kernel.Process32FirstW(snapshot,ctypes.byref(entry))
        while ok:
            result.append({'pid':entry.th32ProcessID,'parent':entry.th32ParentProcessID,'name':entry.szExeFile})
            ok=kernel.Process32NextW(snapshot,ctypes.byref(entry))
    finally:kernel.CloseHandle(snapshot)
    return result

def process_tree(root_pid=None):
    root_pid=root_pid or os.getpid();rows=processes();selected={root_pid}
    while True:
        expanded=selected|{p['pid'] for p in rows if p['parent'] in selected}
        if expanded==selected:break
        selected=expanded
    return [p for p in rows if p['pid'] in selected]

def memory():
    rows=[]
    for p in process_tree():
        handle=kernel.OpenProcess(0x1000|0x10,False,p['pid'])
        if not handle:continue
        value=PROCESS_MEMORY_COUNTERS_EX();value.cb=ctypes.sizeof(value)
        try:
            if psapi.GetProcessMemoryInfo(handle,ctypes.byref(value),value.cb):rows.append({**p,'working_set_bytes':value.WorkingSetSize,'private_bytes':value.PrivateUsage})
        finally:kernel.CloseHandle(handle)
    return {'processes':rows,'working_set_bytes':sum(p['working_set_bytes'] for p in rows),'private_bytes':sum(p['private_bytes'] for p in rows)}
