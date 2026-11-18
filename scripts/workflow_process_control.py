#!/usr/bin/env python3
"""Pause and resume a workflow subprocess tree without terminating its active stage."""
from __future__ import annotations

import ctypes
import os
import signal
from dataclasses import dataclass
from typing import Any


class ProcessControlError(RuntimeError):
    """The workflow process tree could not be paused or resumed safely."""


@dataclass(frozen=True)
class ProcessPause:
    platform: str
    process_ids: tuple[int, ...]


def _windows_process_tree(root_pid: int) -> list[int]:
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if snapshot in (None, invalid_handle):
        raise ProcessControlError(f"cannot enumerate Windows processes: {ctypes.get_last_error()}")
    children: dict[int, list[int]] = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            children.setdefault(int(entry.th32ParentProcessID), []).append(int(entry.th32ProcessID))
            ok = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)

    ordered: list[int] = []
    seen: set[int] = set()

    def visit(pid: int) -> None:
        if pid in seen:
            return
        seen.add(pid)
        for child in children.get(pid, []):
            visit(child)
        ordered.append(pid)

    visit(int(root_pid))
    return ordered


def _windows_suspend(pid: int, *, resume: bool) -> None:
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    ntdll = ctypes.WinDLL("ntdll")
    function = ntdll.NtResumeProcess if resume else ntdll.NtSuspendProcess
    function.argtypes = [wintypes.HANDLE]
    function.restype = ctypes.c_long

    handle = kernel.OpenProcess(0x0800, False, int(pid))  # PROCESS_SUSPEND_RESUME
    if not handle:
        raise ProcessControlError(
            f"cannot open process {pid} for {'resume' if resume else 'pause'}: {ctypes.get_last_error()}")
    try:
        status = int(function(handle))
        if status < 0:
            raise ProcessControlError(
                f"Windows process {'resume' if resume else 'pause'} failed for PID {pid} (NTSTATUS 0x{status & 0xffffffff:08x})")
    finally:
        kernel.CloseHandle(handle)


def suspend_process_tree(process: Any) -> ProcessPause:
    """Suspend the workflow CLI and its descendants; never terminate or restart them."""
    if process is None or process.poll() is not None:
        raise ProcessControlError("workflow process is no longer running")
    if os.name == "nt":
        pids = _windows_process_tree(int(process.pid))
        suspended: list[int] = []
        try:
            # Freeze leaves first so no nested tool keeps running after its parent stops.
            for pid in pids:
                _windows_suspend(pid, resume=False)
                suspended.append(pid)
        except ProcessControlError:
            for pid in reversed(suspended):
                try:
                    _windows_suspend(pid, resume=True)
                except ProcessControlError:
                    pass
            raise
        return ProcessPause("windows", tuple(pids))

    try:
        if os.getpgid(int(process.pid)) != int(process.pid):
            raise ProcessControlError("workflow is not isolated in its own process group")
        os.killpg(int(process.pid), signal.SIGSTOP)
    except OSError as exc:
        raise ProcessControlError(f"cannot pause workflow process group: {exc}") from exc
    return ProcessPause("posix", (int(process.pid),))


def resume_process_tree(token: ProcessPause) -> None:
    """Continue exactly the process tree previously suspended by this controller."""
    if token.platform == "windows":
        errors = []
        # Resume the controller before its descendants so pipe readers can drain output.
        for pid in reversed(token.process_ids):
            try:
                _windows_suspend(pid, resume=True)
            except ProcessControlError as exc:
                errors.append(str(exc))
        if errors:
            raise ProcessControlError("could not resume every suspended process: " + "; ".join(errors))
        return
    try:
        os.killpg(token.process_ids[0], signal.SIGCONT)
    except OSError as exc:
        raise ProcessControlError(f"cannot resume workflow process group: {exc}") from exc
