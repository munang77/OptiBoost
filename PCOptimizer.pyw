# -*- coding: utf-8 -*-
"""
OptiBoost (올인원 · 신급)
- 대시보드: CPU/RAM/디스크 게이지 + PC 건강점수 + 슈퍼 최적화 + 자동 반복
- 청소:  딥클린 19종 (임시/캐시/셰이더/썸네일/앱캐시/휴지통) + 매주 예약
- 부스터: 메모리 정리(워킹셋+대기메모리 퍼지) / 전원 / DNS / 프로세스 종료
- 게임:  게임 부스트 (고성능+게임모드+앱정리, 종료 시 복원)
- 트윅:  성능 트윅 9종 (전부 백업·되돌리기 가능)
- 복구:  SFC / DISM / CHKDSK / Winsock (실시간 출력)
- 시작앱/디스크/중복: 시작프로그램 관리, 큰 파일·폴더, 중복 파일

추가 설치 없이 파이썬 표준 라이브러리(tkinter, ctypes, winreg)만 사용.
Windows 전용.   실행: 실행.bat  |  예약청소 내부용: --silent-clean
"""

import os
import re
import sys
import glob
import json
import time
import queue
import shutil
import winreg
import ctypes
import hashlib
import tempfile
import threading
import subprocess
import urllib.request
from ctypes import wintypes

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ---------------------------------------------------------------------------
# 고해상도(DPI) 대응 - 창이 흐릿하게 나오지 않도록
# ---------------------------------------------------------------------------
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

CREATE_NO_WINDOW = 0x08000000
FROZEN = getattr(sys, "frozen", False)


def _app_launch():
    """(실행파일, 앞쪽 인자들). exe로 빌드되면 exe 자체, 아니면 pythonw + 스크립트."""
    if FROZEN:
        return sys.executable, []
    return sys.executable, [os.path.abspath(sys.argv[0])]

# ---------------------------------------------------------------------------
# Windows API (ctypes)
# ---------------------------------------------------------------------------
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("i64Size", ctypes.c_int64),
        ("i64NumItems", ctypes.c_int64),
    ]


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", wintypes.LPVOID),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


# 함수 시그니처 지정 (64비트에서 핸들이 잘리지 않게)
kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

psapi.EnumProcesses.argtypes = [
    ctypes.POINTER(wintypes.DWORD),
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
psapi.EnumProcesses.restype = wintypes.BOOL
psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]
psapi.EmptyWorkingSet.restype = wintypes.BOOL

shell32.SHQueryRecycleBinW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(SHQUERYRBINFO)]
shell32.SHQueryRecycleBinW.restype = ctypes.c_long
shell32.SHEmptyRecycleBinW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.DWORD]
shell32.SHEmptyRecycleBinW.restype = ctypes.c_long
shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
shell32.SHFileOperationW.restype = ctypes.c_int


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def relaunch_as_admin():
    try:
        exe, pre = _app_launch()
        parts = pre + list(sys.argv[1:])
        params = " ".join('"{}"'.format(p) for p in parts)
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, params if params else None, None, 1
        )
        if rc > 32:
            sys.exit(0)
    except Exception as e:
        messagebox.showerror("오류", "관리자 권한 실행 실패:\n{}".format(e))


def mem_status():
    m = MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m


def trim_working_sets():
    """열 수 있는 모든 프로세스의 워킹셋을 비워서 물리 메모리를 되돌린다."""
    trimmed = 0
    n_slots = 8192
    arr = (wintypes.DWORD * n_slots)()
    needed = wintypes.DWORD()
    if not psapi.EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(needed)):
        return 0
    count = needed.value // ctypes.sizeof(wintypes.DWORD)
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_SET_QUOTA = 0x0100
    for i in range(count):
        pid = arr[i]
        if pid == 0:
            continue
        h = kernel32.OpenProcess(
            PROCESS_QUERY_INFORMATION | PROCESS_SET_QUOTA, False, pid
        )
        if h:
            try:
                if psapi.EmptyWorkingSet(h):
                    trimmed += 1
            finally:
                kernel32.CloseHandle(h)
    return trimmed


def recycle_bin_info():
    info = SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(info)
    res = shell32.SHQueryRecycleBinW(None, ctypes.byref(info))
    if res == 0:
        return int(info.i64Size), int(info.i64NumItems)
    return 0, 0


def empty_recycle_bin():
    # SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
    flags = 0x00000001 | 0x00000002 | 0x00000004
    return shell32.SHEmptyRecycleBinW(None, None, flags) == 0


def recycle_delete(paths):
    """파일들을 휴지통으로 보낸다(복구 가능). 성공 시 True."""
    if not paths:
        return True
    buf = "\0".join(paths) + "\0\0"
    op = SHFILEOPSTRUCTW()
    op.wFunc = 0x0003  # FO_DELETE
    op.pFrom = buf
    # ALLOWUNDO | NOCONFIRMATION | SILENT | NOERRORUI | NOCONFIRMMKDIR
    op.fFlags = 0x0040 | 0x0010 | 0x0004 | 0x0400 | 0x0200
    res = shell32.SHFileOperationW(ctypes.byref(op))
    return res == 0 and not op.fAnyOperationsAborted


def run_hidden(cmd):
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            creationflags=CREATE_NO_WINDOW,
            encoding="mbcs",
            errors="replace",
        )
    except Exception as e:
        class _R:
            returncode = 1
            stdout = ""
            stderr = str(e)
        return _R()


# ---------------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------------
def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            if unit == "B":
                return "{:.0f} {}".format(n, unit)
            return "{:.1f} {}".format(n, unit)
        n /= 1024.0


def scan_size(paths):
    """경로들 안의 파일 전체 크기와 개수."""
    total = 0
    files = 0
    for base in paths:
        if not base or not os.path.isdir(base):
            continue
        for root, dirs, names in os.walk(base):
            for name in names:
                fp = os.path.join(root, name)
                try:
                    total += os.path.getsize(fp)
                    files += 1
                except Exception:
                    pass
    return total, files


def clean_paths(paths):
    """경로들 안의 내용을 삭제(폴더 자체는 유지). 회수한 바이트 반환."""
    freed = 0
    for base in paths:
        if not base or not os.path.isdir(base):
            continue
        for root, dirs, names in os.walk(base, topdown=False):
            for name in names:
                fp = os.path.join(root, name)
                try:
                    sz = os.path.getsize(fp)
                    os.chmod(fp, 0o777)
                    os.remove(fp)
                    freed += sz
                except Exception:
                    pass
            for d in dirs:
                dp = os.path.join(root, d)
                try:
                    os.rmdir(dp)
                except Exception:
                    pass
    return freed


FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def is_reparse_point(path):
    """정션/심볼릭 링크 등 재분석 지점이면 True (디스크 스캔 순환 방지용)."""
    try:
        return bool(os.lstat(path).st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT)
    except (OSError, AttributeError, ValueError):
        return False


LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")
APPDATA = os.environ.get("APPDATA", "")
WINDIR = os.environ.get("WINDIR", r"C:\Windows")


PROGRAMDATA = os.environ.get("PROGRAMDATA", r"C:\ProgramData")


def _gdirs(*patterns):
    """glob 패턴들에서 존재하는 폴더만 수집."""
    out = []
    for pat in patterns:
        for d in glob.glob(pat):
            if os.path.isdir(d):
                out.append(d)
    return out


def build_categories():
    """청소 항목 정의(딥클린). 존재하는 경로만 포함."""
    cats = []

    def add(key, label, paths, default, note="", globs=None):
        real = [p for p in paths if p and os.path.isdir(p)]
        gl = [g for g in (globs or []) if glob.glob(g)]
        if real or gl or key == "recyclebin":
            cats.append(dict(key=key, label=label, paths=real,
                             default=default, note=note, globs=gl))

    # --- 기본 임시/오류 ---
    add("temp_user", "내 임시 파일 (%TEMP%)", [os.environ.get("TEMP")], True)
    add("temp_win", "윈도우 임시 파일", [os.path.join(WINDIR, "Temp")], True, "관리자 권장")
    add("crashdumps", "오류 덤프 파일", [os.path.join(LOCALAPPDATA, "CrashDumps")], True)
    add("wer", "Windows 오류 보고", [os.path.join(LOCALAPPDATA, r"Microsoft\Windows\WER")], True)
    add("minidump", "블루스크린 덤프 (Minidump)",
        [os.path.join(WINDIR, "Minidump")], True, "관리자 필요",
        globs=[os.path.join(WINDIR, "MEMORY.DMP")])
    add("winlogs", "윈도우 로그 파일", [os.path.join(WINDIR, "Logs")], False, "관리자 필요")
    add("inetcache", "인터넷 임시 파일 (INetCache)",
        [os.path.join(LOCALAPPDATA, r"Microsoft\Windows\INetCache")], True)
    add("thumbcache", "탐색기 썸네일·아이콘 캐시", [], False, "일부는 사용 중 잠김",
        globs=[os.path.join(LOCALAPPDATA, r"Microsoft\Windows\Explorer", "thumbcache_*.db"),
               os.path.join(LOCALAPPDATA, r"Microsoft\Windows\Explorer", "iconcache_*.db")])
    add("recent", "최근 사용한 문서 기록",
        [os.path.join(APPDATA, r"Microsoft\Windows\Recent")], False)
    add("prefetch", "프리페치 (Prefetch)", [os.path.join(WINDIR, "Prefetch")], False, "관리자 필요")

    # --- GPU 셰이더 캐시 ---
    add("d3dcache", "DirectX 셰이더 캐시", [os.path.join(LOCALAPPDATA, "D3DSCache")], True)
    add("nvcache", "NVIDIA 셰이더 캐시",
        [os.path.join(LOCALAPPDATA, r"NVIDIA\DXCache"),
         os.path.join(LOCALAPPDATA, r"NVIDIA\GLCache"),
         os.path.join(LOCALAPPDATA, r"NVIDIA Corporation\NV_Cache"),
         os.path.join(PROGRAMDATA, r"NVIDIA Corporation\NV_Cache")],
        False, "삭제 후 첫 게임 로딩만 살짝 느려짐")
    add("amdcache", "AMD 셰이더 캐시",
        [os.path.join(LOCALAPPDATA, r"AMD\DxCache"),
         os.path.join(LOCALAPPDATA, r"AMD\DXCache"),
         os.path.join(LOCALAPPDATA, r"AMD\GLCache")],
        False, "삭제 후 첫 게임 로딩만 살짝 느려짐")

    # --- 앱 캐시 ---
    add("chrome", "Chrome 캐시 (전체 프로필)",
        _gdirs(os.path.join(LOCALAPPDATA, r"Google\Chrome\User Data\*\Cache"),
               os.path.join(LOCALAPPDATA, r"Google\Chrome\User Data\*\Code Cache"),
               os.path.join(LOCALAPPDATA, r"Google\Chrome\User Data\*\GPUCache")),
        False, "브라우저 종료 후")
    add("edge", "Edge 캐시 (전체 프로필)",
        _gdirs(os.path.join(LOCALAPPDATA, r"Microsoft\Edge\User Data\*\Cache"),
               os.path.join(LOCALAPPDATA, r"Microsoft\Edge\User Data\*\Code Cache"),
               os.path.join(LOCALAPPDATA, r"Microsoft\Edge\User Data\*\GPUCache")),
        False, "브라우저 종료 후")
    add("discord", "Discord 캐시",
        [os.path.join(APPDATA, r"discord\Cache"),
         os.path.join(APPDATA, r"discord\Code Cache"),
         os.path.join(APPDATA, r"discord\GPUCache")],
        True, "Discord 종료 후 권장")
    add("spotify", "Spotify 캐시",
        [os.path.join(LOCALAPPDATA, r"Spotify\Storage"),
         os.path.join(LOCALAPPDATA, r"Spotify\Data")],
        False, "오프라인 곡 다시 받게 됨")

    # --- 윈도우 업데이트 ---
    add("winupdate", "Windows 업데이트 캐시",
        [os.path.join(WINDIR, r"SoftwareDistribution\Download")], False, "관리자 필요")
    add("deliveryopt", "전달 최적화 캐시",
        [os.path.join(WINDIR, r"SoftwareDistribution\DeliveryOptimization")],
        False, "관리자 필요")

    # 휴지통은 항상 마지막
    cats.append(dict(key="recyclebin", label="휴지통 비우기", paths=[],
                     default=True, note="", globs=[]))
    return cats


def cat_size(c):
    """카테고리 총 용량."""
    if c["key"] == "recyclebin":
        return recycle_bin_info()[0]
    total, _ = scan_size(c["paths"])
    for pat in c.get("globs", []):
        for fp in glob.glob(pat):
            try:
                total += os.path.getsize(fp)
            except Exception:
                pass
    return total


def clean_cat(c):
    """카테고리 정리. 회수 바이트 반환."""
    if c["key"] == "recyclebin":
        size, _ = recycle_bin_info()
        return size if empty_recycle_bin() else 0
    freed = clean_paths(c["paths"])
    for pat in c.get("globs", []):
        for fp in glob.glob(pat):
            try:
                sz = os.path.getsize(fp)
                os.chmod(fp, 0o777)
                os.remove(fp)
                freed += sz
            except Exception:
                pass
    return freed


# 종료하면 안 되는 핵심 프로세스
PROTECTED = {
    "system", "system idle process", "registry", "memory compression",
    "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
    "lsass.exe", "svchost.exe", "dwm.exe", "fontdrvhost.exe", "explorer.exe",
    "conhost.exe", "sihost.exe", "ctfmon.exe", "taskhostw.exe",
    "pythonw.exe", "python.exe", "runtimebroker.exe", "searchhost.exe",
    "startmenuexperiencehost.exe", "shellexperiencehost.exe", "textinputhost.exe",
    "audiodg.exe", "wininit.exe", "lsaiso.exe", "securityhealthservice.exe",
}


def list_processes():
    """(name, pid, mem_bytes) 리스트. 핵심 프로세스는 제외."""
    r = run_hidden(["tasklist", "/FO", "CSV", "/NH"])
    procs = {}
    if r.returncode != 0 or not r.stdout:
        return []
    import csv
    import io

    reader = csv.reader(io.StringIO(r.stdout))
    for row in reader:
        if len(row) < 5:
            continue
        name = row[0].strip()
        try:
            pid = int(row[1])
        except ValueError:
            continue
        mem_str = row[4].replace(",", "").replace("K", "").replace("\xa0", "").strip()
        try:
            mem = int(mem_str) * 1024
        except ValueError:
            mem = 0
        if name.lower() in PROTECTED:
            continue
        if pid <= 4:
            continue
        # 같은 이름 프로세스 합산(메모리 큰 것 대표 pid 유지)
        cur = procs.get(name)
        if cur:
            cur[0] += mem
            cur[1].append(pid)
        else:
            procs[name] = [mem, [pid]]
    out = []
    for name, (mem, pids) in procs.items():
        out.append((name, pids, mem))
    out.sort(key=lambda x: x[2], reverse=True)
    return out


def kill_pids(pids):
    ok = 0
    for pid in pids:
        r = run_hidden(["taskkill", "/PID", str(pid), "/F", "/T"])
        if r.returncode == 0:
            ok += 1
    return ok


def get_active_power_scheme():
    r = run_hidden(["powercfg", "/getactivescheme"])
    if r.returncode == 0 and r.stdout:
        line = r.stdout.strip()
        if "(" in line and ")" in line:
            return line[line.rfind("(") + 1 : line.rfind(")")]
        return line
    return "알 수 없음"


def set_power_scheme(alias):
    r = run_hidden(["powercfg", "/setactive", alias])
    return r.returncode == 0


def flush_dns():
    return run_hidden(["ipconfig", "/flushdns"]).returncode == 0


def get_active_scheme_guid():
    r = run_hidden(["powercfg", "/getactivescheme"])
    if r.returncode == 0 and r.stdout:
        mm = re.search(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            r.stdout,
        )
        if mm:
            return mm.group(0)
    return None


def set_game_mode(enable):
    try:
        k = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\GameBar")
        winreg.SetValueEx(k, "AutoGameModeEnabled", 0, winreg.REG_DWORD, 1 if enable else 0)
        winreg.SetValueEx(k, "AllowAutoGameMode", 0, winreg.REG_DWORD, 1 if enable else 0)
        winreg.CloseKey(k)
        return True
    except Exception:
        return False


# 게임 중 닫아도 안전한 백그라운드 앱 (기본 체크)
GAME_SAFE = {
    "spotify.exe", "onedrive.exe", "dropbox.exe", "googledrivefs.exe",
    "adobearm.exe", "ccxprocess.exe", "creative cloud.exe", "cclibrary.exe",
    "adobeipcbroker.exe", "gamebarpresencewriter.exe", "skypeapp.exe",
    "skype.exe", "ituneshelper.exe", "icloudservices.exe", "steamwebhelper.exe",
}
# 닫을 수 있으나 작업 중일 수 있어 기본 해제
GAME_OPTIONAL = {
    "chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "brave.exe",
    "whale.exe", "discord.exe", "teams.exe", "msteams.exe", "ms-teams.exe",
    "slack.exe", "kakaotalk.exe", "telegram.exe", "zoom.exe", "whatsapp.exe",
    "notion.exe",
}


def list_closable_apps():
    """현재 실행 중인 '정리 가능' 앱 목록. [(name, pids, mem, default_checked)]"""
    out = []
    for name, pids, mem in list_processes():
        low = name.lower()
        if low in GAME_SAFE:
            out.append((name, pids, mem, True))
        elif low in GAME_OPTIONAL:
            out.append((name, pids, mem, False))
    return out


# ---------------------------------------------------------------------------
# CPU / 디스크 사용률 (ctypes)
# ---------------------------------------------------------------------------
class FILETIME(ctypes.Structure):
    _fields_ = [("dwLow", wintypes.DWORD), ("dwHigh", wintypes.DWORD)]


kernel32.GetSystemTimes.argtypes = [ctypes.POINTER(FILETIME)] * 3
kernel32.GetSystemTimes.restype = wintypes.BOOL
kernel32.GetDiskFreeSpaceExW.argtypes = [
    wintypes.LPCWSTR,
    ctypes.POINTER(ctypes.c_ulonglong),
    ctypes.POINTER(ctypes.c_ulonglong),
    ctypes.POINTER(ctypes.c_ulonglong),
]
kernel32.GetDiskFreeSpaceExW.restype = wintypes.BOOL


def _ft_val(ft):
    return (ft.dwHigh << 32) | ft.dwLow


_cpu_prev = {}


def cpu_percent():
    idle, kern, user = FILETIME(), FILETIME(), FILETIME()
    if not kernel32.GetSystemTimes(
        ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)
    ):
        return 0.0
    i, k, u = _ft_val(idle), _ft_val(kern), _ft_val(user)
    if _cpu_prev:
        di = i - _cpu_prev["i"]
        total = (k - _cpu_prev["k"]) + (u - _cpu_prev["u"])
        _cpu_prev.update(i=i, k=k, u=u)
        if total > 0:
            return max(0.0, min(100.0, 100.0 * (total - di) / total))
        return 0.0
    _cpu_prev.update(i=i, k=k, u=u)
    return 0.0


def disk_usage(path):
    free = ctypes.c_ulonglong(0)
    total = ctypes.c_ulonglong(0)
    totfree = ctypes.c_ulonglong(0)
    if kernel32.GetDiskFreeSpaceExW(
        path, ctypes.byref(free), ctypes.byref(total), ctypes.byref(totfree)
    ):
        return total.value - totfree.value, total.value
    return 0, 0


def uptime_seconds():
    kernel32.GetTickCount64.restype = ctypes.c_ulonglong
    return kernel32.GetTickCount64() / 1000.0


# ---------------------------------------------------------------------------
# 신급 메모리 정리: 대기 메모리(Standby List) 퍼지 - RAMMap 방식 (관리자 필요)
# ---------------------------------------------------------------------------
ntdll = ctypes.WinDLL("ntdll")
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", ctypes.c_long)]


class _LUID_AND_ATTRS(ctypes.Structure):
    _fields_ = [("Luid", _LUID), ("Attributes", wintypes.DWORD)]


class _TOKEN_PRIVS(ctypes.Structure):
    _fields_ = [("PrivilegeCount", wintypes.DWORD),
                ("Privileges", _LUID_AND_ATTRS * 1)]


advapi32.OpenProcessToken.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
advapi32.OpenProcessToken.restype = wintypes.BOOL
advapi32.LookupPrivilegeValueW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(_LUID)]
advapi32.LookupPrivilegeValueW.restype = wintypes.BOOL
advapi32.AdjustTokenPrivileges.argtypes = [
    wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(_TOKEN_PRIVS),
    wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
advapi32.AdjustTokenPrivileges.restype = wintypes.BOOL
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
ntdll.NtSetSystemInformation.argtypes = [
    ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
ntdll.NtSetSystemInformation.restype = ctypes.c_int32


def _enable_privilege(name):
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), 0x20 | 0x8, ctypes.byref(token)
    ):
        return False
    try:
        luid = _LUID()
        if not advapi32.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
            return False
        tp = _TOKEN_PRIVS()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = 0x2  # SE_PRIVILEGE_ENABLED
        ok = advapi32.AdjustTokenPrivileges(
            token, False, ctypes.byref(tp), 0, None, None)
        return bool(ok) and ctypes.get_last_error() == 0
    finally:
        kernel32.CloseHandle(token)


def purge_standby_list():
    """대기 메모리(스탠바이 캐시)를 통째로 비운다. 관리자 필요."""
    if not _enable_privilege("SeProfileSingleProcessPrivilege"):
        return False
    cmd = ctypes.c_int(4)  # MemoryPurgeStandbyList
    status = ntdll.NtSetSystemInformation(  # SystemMemoryListInformation=80
        80, ctypes.byref(cmd), ctypes.sizeof(cmd))
    return status == 0


def flush_system_cache():
    """시스템 파일 캐시 워킹셋 비우기. 관리자 필요."""
    try:
        _enable_privilege("SeIncreaseQuotaPrivilege")
        kernel32.SetSystemFileCacheSize.argtypes = [
            ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
        kernel32.SetSystemFileCacheSize.restype = wintypes.BOOL
        return bool(kernel32.SetSystemFileCacheSize(
            ctypes.c_size_t(-1), ctypes.c_size_t(-1), 0))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 레지스트리 / 서비스 헬퍼
# ---------------------------------------------------------------------------
HKCU = winreg.HKEY_CURRENT_USER
HKLM = winreg.HKEY_LOCAL_MACHINE
GUID_RE = (r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
           r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def reg_get(hive, path, name):
    try:
        k = winreg.OpenKey(hive, path)
        v, _ = winreg.QueryValueEx(k, name)
        winreg.CloseKey(k)
        return v
    except Exception:
        return None


def reg_set(hive, path, name, value, vtype=winreg.REG_DWORD):
    try:
        k = winreg.CreateKey(hive, path)
        winreg.SetValueEx(k, name, 0, vtype, value)
        winreg.CloseKey(k)
        return True
    except Exception:
        return False


def reg_del(hive, path, name):
    try:
        k = winreg.OpenKey(hive, path, 0, winreg.KEY_SET_VALUE)
        winreg.DeleteValue(k, name)
        winreg.CloseKey(k)
        return True
    except Exception:
        return False


def service_start_type(name):
    r = run_hidden(["sc", "qc", name])
    if r.returncode == 0 and r.stdout:
        mm = re.search(r"START_TYPE\s*:\s*(\d)", r.stdout)
        if mm:
            return int(mm.group(1))  # 2=auto 3=manual 4=disabled
    return None


def service_set(name, start):
    return run_hidden(["sc", "config", name, "start=", start]).returncode == 0


# ---------------------------------------------------------------------------
# 🔥 트윅 프레임워크 (전부 되돌리기 가능, 백업은 pcopt_config.json)
# ---------------------------------------------------------------------------
VFX_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects"
PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
GAMECONF = r"System\GameConfigStore"
GAMEDVR_KEY = r"Software\Microsoft\Windows\CurrentVersion\GameDVR"
ADS_KEY = r"Software\Microsoft\Windows\CurrentVersion\AdvertisingInfo"
MOUSE_KEY = r"Control Panel\Mouse"
TCPIF_KEY = r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces"
DATACOLL_KEY = r"SOFTWARE\Policies\Microsoft\Windows\DataCollection"
ULTIMATE_SRC = "e9a42b02-d5df-448d-aa66-3dfcae7fd0e0"


def _tw_flag(key):
    return load_config().get("tweaks", {}).get(key)


def _tw_save(key, backup):
    cfg = load_config()
    cfg.setdefault("tweaks", {})[key] = {"backup": backup}
    save_config(cfg)


def _tw_clear(key):
    cfg = load_config()
    if key in cfg.get("tweaks", {}):
        del cfg["tweaks"][key]
        save_config(cfg)


def _tw_backup(key):
    d = _tw_flag(key) or {}
    return d.get("backup") or {}


def ult_check():
    d = _tw_backup("ultimate")
    return bool(d.get("ult")) and get_active_scheme_guid() == d.get("ult")


def ult_apply():
    prev = get_active_scheme_guid()
    r = run_hidden(["powercfg", "-duplicatescheme", ULTIMATE_SRC])
    gm = None
    if r.returncode == 0 and r.stdout:
        mm = re.search(GUID_RE, r.stdout)
        gm = mm.group(0) if mm else None
    if not gm:
        return False
    if run_hidden(["powercfg", "/setactive", gm]).returncode != 0:
        return False
    _tw_save("ultimate", {"prev": prev, "ult": gm})
    return True


def ult_revert():
    d = _tw_backup("ultimate")
    ok = set_power_scheme(d.get("prev") or "scheme_balanced")
    if d.get("ult"):
        run_hidden(["powercfg", "/delete", d["ult"]])
    _tw_clear("ultimate")
    return ok


def vfx_check():
    return reg_get(HKCU, VFX_KEY, "VisualFXSetting") == 2


def vfx_apply():
    old = reg_get(HKCU, VFX_KEY, "VisualFXSetting")
    if reg_set(HKCU, VFX_KEY, "VisualFXSetting", 2):
        _tw_save("visualfx", {"old": old})
        return True
    return False


def vfx_revert():
    old = _tw_backup("visualfx").get("old")
    ok = reg_set(HKCU, VFX_KEY, "VisualFXSetting",
                 old if isinstance(old, int) else 0)
    _tw_clear("visualfx")
    return ok


def trans_check():
    return reg_get(HKCU, PERSONALIZE, "EnableTransparency") == 0


def trans_apply():
    old = reg_get(HKCU, PERSONALIZE, "EnableTransparency")
    if reg_set(HKCU, PERSONALIZE, "EnableTransparency", 0):
        _tw_save("transparency", {"old": old})
        return True
    return False


def trans_revert():
    old = _tw_backup("transparency").get("old")
    ok = reg_set(HKCU, PERSONALIZE, "EnableTransparency",
                 old if isinstance(old, int) else 1)
    _tw_clear("transparency")
    return ok


def dvr_check():
    return reg_get(HKCU, GAMECONF, "GameDVR_Enabled") == 0


def dvr_apply():
    old = {"g": reg_get(HKCU, GAMECONF, "GameDVR_Enabled"),
           "a": reg_get(HKCU, GAMEDVR_KEY, "AppCaptureEnabled")}
    ok = reg_set(HKCU, GAMECONF, "GameDVR_Enabled", 0)
    reg_set(HKCU, GAMEDVR_KEY, "AppCaptureEnabled", 0)
    if ok:
        _tw_save("gamedvr", old)
    return ok


def dvr_revert():
    d = _tw_backup("gamedvr")
    ok = reg_set(HKCU, GAMECONF, "GameDVR_Enabled",
                 d.get("g") if isinstance(d.get("g"), int) else 1)
    reg_set(HKCU, GAMEDVR_KEY, "AppCaptureEnabled",
            d.get("a") if isinstance(d.get("a"), int) else 1)
    _tw_clear("gamedvr")
    return ok


def _spi_mouse(t1, t2, accel):
    try:
        arr = (ctypes.c_int * 3)(t1, t2, accel)
        ctypes.windll.user32.SystemParametersInfoW(0x0004, 0, arr, 0x0002)
    except Exception:
        pass


def mouse_check():
    return reg_get(HKCU, MOUSE_KEY, "MouseSpeed") == "0"


def mouse_apply():
    old = {n: reg_get(HKCU, MOUSE_KEY, n)
           for n in ("MouseSpeed", "MouseThreshold1", "MouseThreshold2")}
    ok = all(reg_set(HKCU, MOUSE_KEY, n, "0", winreg.REG_SZ)
             for n in ("MouseSpeed", "MouseThreshold1", "MouseThreshold2"))
    if ok:
        _spi_mouse(0, 0, 0)
        _tw_save("mouse", old)
    return ok


def mouse_revert():
    d = _tw_backup("mouse")

    def dv(n, dft):
        v = d.get(n)
        return v if isinstance(v, str) and v else dft
    sp = dv("MouseSpeed", "1")
    t1 = dv("MouseThreshold1", "6")
    t2 = dv("MouseThreshold2", "10")
    ok = all((reg_set(HKCU, MOUSE_KEY, "MouseSpeed", sp, winreg.REG_SZ),
              reg_set(HKCU, MOUSE_KEY, "MouseThreshold1", t1, winreg.REG_SZ),
              reg_set(HKCU, MOUSE_KEY, "MouseThreshold2", t2, winreg.REG_SZ)))
    try:
        _spi_mouse(int(t1), int(t2), int(sp))
    except Exception:
        pass
    _tw_clear("mouse")
    return ok


def _tcp_ifaces():
    out = []
    try:
        k = winreg.OpenKey(HKLM, TCPIF_KEY)
    except Exception:
        return out
    i = 0
    while True:
        try:
            out.append(winreg.EnumKey(k, i))
        except OSError:
            break
        i += 1
    winreg.CloseKey(k)
    return out


def nagle_check():
    for sub in _tcp_ifaces():
        if reg_get(HKLM, TCPIF_KEY + "\\" + sub, "TcpAckFrequency") == 1:
            return True
    return False


def nagle_apply():
    touched = []
    for sub in _tcp_ifaces():
        p = TCPIF_KEY + "\\" + sub
        if reg_set(HKLM, p, "TcpAckFrequency", 1) and \
           reg_set(HKLM, p, "TCPNoDelay", 1):
            touched.append(sub)
    if touched:
        _tw_save("nagle", {"touched": touched})
        return True
    return False


def nagle_revert():
    subs = _tw_backup("nagle").get("touched") or _tcp_ifaces()
    for sub in subs:
        p = TCPIF_KEY + "\\" + sub
        reg_del(HKLM, p, "TcpAckFrequency")
        reg_del(HKLM, p, "TCPNoDelay")
    _tw_clear("nagle")
    return True


def tele_check():
    return reg_get(HKCU, ADS_KEY, "Enabled") == 0


def tele_apply():
    old = reg_get(HKCU, ADS_KEY, "Enabled")
    ok = reg_set(HKCU, ADS_KEY, "Enabled", 0)
    if is_admin():
        reg_set(HKLM, DATACOLL_KEY, "AllowTelemetry", 0)
        service_set("DiagTrack", "disabled")
        run_hidden(["sc", "stop", "DiagTrack"])
    if ok:
        _tw_save("telemetry", {"old": old})
    return ok


def tele_revert():
    ok = reg_set(HKCU, ADS_KEY, "Enabled", 1)
    if is_admin():
        reg_del(HKLM, DATACOLL_KEY, "AllowTelemetry")
        service_set("DiagTrack", "auto")
        run_hidden(["sc", "start", "DiagTrack"])
    _tw_clear("telemetry")
    return ok


def sysmain_check():
    return service_start_type("SysMain") == 4


def sysmain_apply():
    ok = service_set("SysMain", "disabled")
    run_hidden(["sc", "stop", "SysMain"])
    return ok


def sysmain_revert():
    ok = service_set("SysMain", "auto")
    run_hidden(["sc", "start", "SysMain"])
    return ok


def hib_check():
    return not os.path.exists(r"C:\hiberfil.sys")


def hib_apply():
    return run_hidden(["powercfg", "/h", "off"]).returncode == 0


def hib_revert():
    return run_hidden(["powercfg", "/h", "on"]).returncode == 0


TWEAKS = [
    dict(key="ultimate", label="🏆 궁극의 성능 전원 모드",
         desc="숨겨진 'Ultimate Performance' 전원 계획을 만들어 적용. 고성능보다 한 단계 위.",
         admin=False, check=ult_check, apply=ult_apply, revert=ult_revert),
    dict(key="gamedvr", label="🎥 게임 DVR·백그라운드 녹화 끄기",
         desc="Xbox 게임 바의 몰래 녹화 기능을 꺼서 게임 FPS를 올립니다.",
         admin=False, check=dvr_check, apply=dvr_apply, revert=dvr_revert),
    dict(key="nagle", label="🌐 네트워크 지연 감소 (Nagle 끄기)",
         desc="TcpAckFrequency/TCPNoDelay 설정으로 온라인 게임 핑을 줄입니다. 재부팅 후 완전 적용.",
         admin=True, check=nagle_check, apply=nagle_apply, revert=nagle_revert),
    dict(key="mouse", label="🖱 마우스 가속 끄기 (게이머용)",
         desc="'포인터 정밀도 향상'을 꺼서 에임을 일관되게 만듭니다.",
         admin=False, check=mouse_check, apply=mouse_apply, revert=mouse_revert),
    dict(key="visualfx", label="✨ 시각효과 최고 성능",
         desc="애니메이션·그림자 등을 꺼서 반응 속도를 올립니다. (재로그인 시 완전 적용)",
         admin=False, check=vfx_check, apply=vfx_apply, revert=vfx_revert),
    dict(key="transparency", label="🪟 투명 효과 끄기",
         desc="창 투명 효과를 꺼서 GPU/CPU 부담을 줄입니다.",
         admin=False, check=trans_check, apply=trans_apply, revert=trans_revert),
    dict(key="telemetry", label="🕵 진단 데이터·광고 ID 끄기",
         desc="Windows 텔레메트리와 맞춤 광고 추적을 차단합니다. (관리자면 더 강력)",
         admin=False, check=tele_check, apply=tele_apply, revert=tele_revert),
    dict(key="sysmain", label="⚙ SysMain(Superfetch) 끄기",
         desc="SSD 사용 시 불필요한 프리로딩 서비스를 꺼서 디스크 부하를 줄입니다.",
         admin=True, check=sysmain_check, apply=sysmain_apply, revert=sysmain_revert),
    dict(key="hibernate", label="💤 최대 절전 모드 끄기",
         desc="hiberfil.sys를 삭제해 C: 드라이브 수 GB를 확보합니다. ('빠른 시작'도 꺼짐)",
         admin=True, check=hib_check, apply=hib_apply, revert=hib_revert),
]


# ---------------------------------------------------------------------------
# 🩺 PC 건강 점수
# ---------------------------------------------------------------------------
def health_report():
    score = 100
    issues = []
    m = mem_status()
    if m.dwMemoryLoad >= 90:
        score -= 20
        issues.append("메모리 {}% 사용".format(m.dwMemoryLoad))
    elif m.dwMemoryLoad >= 75:
        score -= 10
        issues.append("메모리 {}% 사용".format(m.dwMemoryLoad))
    used, total = disk_usage("C:\\")
    if total:
        freep = 100.0 * (total - used) / total
        if freep < 8:
            score -= 25
            issues.append("C: 여유공간 {}%뿐".format(int(freep)))
        elif freep < 15:
            score -= 12
            issues.append("C: 여유공간 {}%".format(int(freep)))
    try:
        n = len([r for r in list_startup() if r.get("enabled")])
    except Exception:
        n = 0
    if n >= 20:
        score -= 15
        issues.append("시작앱 {}개".format(n))
    elif n >= 12:
        score -= 8
        issues.append("시작앱 {}개".format(n))
    up_d = uptime_seconds() / 86400.0
    if up_d >= 7:
        score -= 12
        issues.append("재부팅 안 한 지 {}일".format(int(up_d)))
    elif up_d >= 3:
        score -= 5
        issues.append("재부팅 안 한 지 {}일".format(int(up_d)))
    tsize, _ = scan_size([os.environ.get("TEMP")])
    if tsize > 2 * 1024 ** 3:
        score -= 10
        issues.append("임시파일 " + human(tsize))
    elif tsize > 500 * 1024 ** 2:
        score -= 5
        issues.append("임시파일 " + human(tsize))
    return max(0, score), issues


def health_grade(score):
    if score >= 90:
        return "S", ACCENT2
    if score >= 80:
        return "A", ACCENT2
    if score >= 65:
        return "B", WARN
    if score >= 50:
        return "C", WARN
    return "D", DANGER


# ---------------------------------------------------------------------------
# 🛡 보안 검사 (Windows Defender 연동)
# ---------------------------------------------------------------------------
def find_mpcmdrun():
    """Windows Defender 명령줄 도구(MpCmdRun.exe) 경로."""
    cands = []
    plat = os.path.join(PROGRAMDATA, r"Microsoft\Windows Defender\Platform")
    if os.path.isdir(plat):
        cands += sorted(
            glob.glob(os.path.join(plat, "*", "MpCmdRun.exe")), reverse=True)
    cands.append(os.path.join(
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        r"Windows Defender\MpCmdRun.exe"))
    for c in cands:
        if os.path.exists(c):
            return c
    return None


def _ps(cmd):
    return run_hidden(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd])


def defender_status():
    """Defender 상태 딕셔너리."""
    cmd = (
        "$s=Get-MpComputerStatus;"
        "'RTP='+$s.RealTimeProtectionEnabled;"
        "'AV='+$s.AntivirusEnabled;"
        "'MODE='+$s.AMRunningMode;"
        "'SIG='+$s.AntivirusSignatureVersion;"
        "'SIGDATE='+$s.AntivirusSignatureLastUpdated;"
        "'QSCAN='+$s.QuickScanEndTime;"
        "'FSCAN='+$s.FullScanEndTime"
    )
    r = _ps(cmd)
    d = {}
    if r.returncode == 0 and r.stdout:
        for line in r.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip()
    return d


def defender_threats():
    """탐지된 위협 이름 목록 (없으면 빈 리스트)."""
    cmd = ("$x=Get-MpThreat; if($x){ $x | ForEach-Object { $_.ThreatName } }")
    r = _ps(cmd)
    if r.returncode == 0 and r.stdout:
        return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    return []


# ---------------------------------------------------------------------------
# 통계 / 경로
# ---------------------------------------------------------------------------
APP_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
STATS_FILE = os.path.join(APP_DIR, "pcopt_stats.json")
CONFIG_FILE = os.path.join(APP_DIR, "pcopt_config.json")
DISABLED_FILE = os.path.join(APP_DIR, "pcopt_disabled_startup.json")
DISABLED_LNK_DIR = os.path.join(APP_DIR, "_disabled_startup")
TASK_NAME = "PCOptimizer_WeeklyClean"


def resource_path(name):
    """번들된 리소스 경로 (exe면 _MEIPASS, 아니면 앱 폴더)."""
    base = getattr(sys, "_MEIPASS", APP_DIR)
    p = os.path.join(base, name)
    return p if os.path.exists(p) else os.path.join(APP_DIR, name)


def hms(sec):
    sec = max(0, int(sec))
    return "{:02d}:{:02d}:{:02d}".format(sec // 3600, (sec % 3600) // 60, sec % 60)


def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_config(d):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False)
    except Exception:
        pass


def load_stats():
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"total_freed": 0, "runs": 0}


def add_freed(nbytes):
    s = load_stats()
    s["total_freed"] = s.get("total_freed", 0) + int(nbytes)
    s["runs"] = s.get("runs", 0) + 1
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as fh:
            json.dump(s, fh)
    except Exception:
        pass
    return s


# ---------------------------------------------------------------------------
# 시작 프로그램 관리 (레지스트리 Run 키 + 시작폴더)
# ---------------------------------------------------------------------------
RUN_KEYS = [
    (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", "현재 사용자"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run", "모든 사용자"),
    (winreg.HKEY_LOCAL_MACHINE,
     r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run", "모든 사용자(32)"),
]


def _startup_folders():
    out = []
    u = os.path.join(APPDATA, r"Microsoft\Windows\Start Menu\Programs\Startup")
    c = os.path.join(
        os.environ.get("PROGRAMDATA", ""),
        r"Microsoft\Windows\Start Menu\Programs\Startup",
    )
    if os.path.isdir(u):
        out.append((u, "시작폴더(사용자)"))
    if os.path.isdir(c):
        out.append((c, "시작폴더(공용)"))
    return out


def _load_disabled():
    try:
        with open(DISABLED_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return []


def _save_disabled(lst):
    try:
        with open(DISABLED_FILE, "w", encoding="utf-8") as fh:
            json.dump(lst, fh, ensure_ascii=False)
    except Exception:
        pass


def list_startup():
    """활성/비활성 시작 프로그램 목록(dict 리스트)."""
    rows = []
    for hive, subkey, label in RUN_KEYS:
        try:
            k = winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ)
        except Exception:
            continue
        try:
            i = 0
            while True:
                try:
                    name, val, _ = winreg.EnumValue(k, i)
                except OSError:
                    break
                rows.append({
                    "kind": "reg", "enabled": True, "name": name,
                    "command": str(val), "label": label,
                    "hive": hive, "subkey": subkey,
                })
                i += 1
        finally:
            winreg.CloseKey(k)
    for folder, label in _startup_folders():
        try:
            for fn in os.listdir(folder):
                if fn.lower().endswith((".lnk", ".url", ".bat", ".cmd", ".exe")):
                    p = os.path.join(folder, fn)
                    rows.append({
                        "kind": "lnk", "enabled": True,
                        "name": os.path.splitext(fn)[0], "command": p,
                        "label": label, "path": p, "folder": folder,
                    })
        except Exception:
            pass
    for d in _load_disabled():
        row = dict(d)
        row["enabled"] = False
        rows.append(row)
    return rows


def disable_startup(row):
    if row["kind"] == "reg":
        try:
            k = winreg.OpenKey(row["hive"], row["subkey"], 0, winreg.KEY_SET_VALUE)
            winreg.DeleteValue(k, row["name"])
            winreg.CloseKey(k)
        except PermissionError:
            return False, "관리자 권한이 필요합니다"
        except Exception as e:
            return False, str(e)
        lst = _load_disabled()
        lst.append({
            "kind": "reg", "name": row["name"], "command": row["command"],
            "label": row["label"], "hive": row["hive"], "subkey": row["subkey"],
        })
        _save_disabled(lst)
        return True, ""
    else:
        try:
            os.makedirs(DISABLED_LNK_DIR, exist_ok=True)
            fn = os.path.basename(row["path"])
            shutil.move(row["path"], os.path.join(DISABLED_LNK_DIR, fn))
        except Exception as e:
            return False, str(e)
        lst = _load_disabled()
        lst.append({
            "kind": "lnk", "name": row["name"], "filename": fn,
            "orig_folder": row["folder"], "label": row["label"],
            "command": os.path.join(DISABLED_LNK_DIR, fn),
        })
        _save_disabled(lst)
        return True, ""


def enable_startup(row):
    lst = _load_disabled()
    if row["kind"] == "reg":
        try:
            k = winreg.OpenKey(row["hive"], row["subkey"], 0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(k, row["name"], 0, winreg.REG_SZ, row["command"])
            winreg.CloseKey(k)
        except PermissionError:
            return False, "관리자 권한이 필요합니다"
        except Exception as e:
            return False, str(e)
        lst = [d for d in lst if not (
            d.get("kind") == "reg" and d.get("name") == row["name"]
            and d.get("subkey") == row["subkey"])]
        _save_disabled(lst)
        return True, ""
    else:
        try:
            fn = row["filename"]
            shutil.move(
                os.path.join(DISABLED_LNK_DIR, fn),
                os.path.join(row["orig_folder"], fn),
            )
        except Exception as e:
            return False, str(e)
        lst = [d for d in lst if not (
            d.get("kind") == "lnk" and d.get("filename") == row["filename"])]
        _save_disabled(lst)
        return True, ""


# ---------------------------------------------------------------------------
# 자동 예약 청소 (작업 스케줄러)
# ---------------------------------------------------------------------------
def schedule_exists():
    return run_hidden(["schtasks", "/query", "/tn", TASK_NAME]).returncode == 0


def schedule_create():
    exe, pre = _app_launch()
    cmd = '"{}"'.format(exe)
    for p in pre:
        cmd += ' "{}"'.format(p)
    cmd += " --silent-clean"
    r = run_hidden([
        "schtasks", "/create", "/tn", TASK_NAME, "/tr", cmd,
        "/sc", "weekly", "/d", "SUN", "/st", "18:00", "/f",
    ])
    return r.returncode == 0


def schedule_delete():
    return run_hidden(["schtasks", "/delete", "/tn", TASK_NAME, "/f"]).returncode == 0


def silent_clean():
    """GUI 없이 안전 항목만 정리 (예약 작업용)."""
    freed = 0
    for c in build_categories():
        if c["key"] == "recyclebin" or c["default"]:
            freed += clean_cat(c)
    add_freed(freed)


# ---------------------------------------------------------------------------
# Windows 시작 시 자동 실행 (HKCU Run)
# ---------------------------------------------------------------------------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "PCOptimizer"


def autorun_check():
    return reg_get(HKCU, RUN_KEY, RUN_NAME) is not None


def autorun_set(enable):
    if enable:
        exe, pre = _app_launch()
        cmd = '"{}"'.format(exe)
        for p in pre:
            cmd += ' "{}"'.format(p)
        cmd += " --minimized"
        return reg_set(HKCU, RUN_KEY, RUN_NAME, cmd, winreg.REG_SZ)
    return reg_del(HKCU, RUN_KEY, RUN_NAME)


# ---------------------------------------------------------------------------
# 자동 업데이트 (GitHub Releases)
# ---------------------------------------------------------------------------
APP_VERSION = "1.2"
GITHUB_REPO = "munang77/OptiBoost"


def _parse_ver(s):
    s = str(s).lstrip("vV").strip()
    out = []
    for p in s.split("."):
        digits = re.sub(r"[^0-9]", "", p)
        out.append(int(digits) if digits else 0)
    return tuple(out) if out else (0,)


def check_update(timeout=8):
    """최신 릴리스 확인. 더 새 버전이면 dict, 아니면 None. 실패 시 예외."""
    url = "https://api.github.com/repos/{}/releases/latest".format(GITHUB_REPO)
    req = urllib.request.Request(url, headers={
        "User-Agent": "OptiBoost-Updater",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    tag = data.get("tag_name", "")
    if _parse_ver(tag) <= _parse_ver(APP_VERSION):
        return None
    exe_url = None
    for a in data.get("assets", []):
        if a.get("name", "").lower() == "optiboost.exe":
            exe_url = a.get("browser_download_url")
            break
    return {"tag": tag, "url": exe_url, "notes": data.get("body", "")}


def apply_update(url):
    """새 exe를 내려받아 교체 예약(배치)하고 True. exe 모드에서만 동작."""
    if not FROZEN or not url:
        return False
    cur = sys.executable
    newp = cur + ".new"
    req = urllib.request.Request(url, headers={"User-Agent": "OptiBoost-Updater"})
    with urllib.request.urlopen(req, timeout=120) as r, open(newp, "wb") as f:
        shutil.copyfileobj(r, f)
    if os.path.getsize(newp) < 1000000:  # 1MB 미만이면 손상된 다운로드
        os.remove(newp)
        return False
    # 앱이 종료되어 exe 잠금이 풀릴 때까지 재시도하며 교체 후 재실행
    bat = os.path.join(tempfile.gettempdir(), "optiboost_update.bat")
    script = (
        "@echo off\r\n"
        "set /a n=0\r\n"
        ":loop\r\n"
        'move /y "{new}" "{cur}" >nul 2>&1\r\n'
        "if not errorlevel 1 goto done\r\n"
        "set /a n+=1\r\n"
        "if %n% geq 40 goto done\r\n"
        "ping 127.0.0.1 -n 2 >nul\r\n"
        "goto loop\r\n"
        ":done\r\n"
        'start "" "{cur}"\r\n'
        'del "%~f0"\r\n'
    ).format(new=newp, cur=cur)
    with open(bat, "w", encoding="mbcs") as f:
        f.write(script)
    subprocess.Popen(["cmd", "/c", bat], creationflags=CREATE_NO_WINDOW)
    return True


# ---------------------------------------------------------------------------
# 시스템 트레이 아이콘 (순수 ctypes Win32 Shell_NotifyIcon)
# ---------------------------------------------------------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
HICON = wintypes.HANDLE
HMENU = wintypes.HANDLE

WM_APP = 0x8000
TRAY_CALLBACK = WM_APP + 1
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_DESTROY = 0x0002
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
IMAGE_ICON = 1
LR_LOADFROMFILE, LR_DEFAULTSIZE = 0x10, 0x40
MF_STRING, MF_SEPARATOR = 0x0, 0x800
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x2, 0x100
HWND_MESSAGE = -3

WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, ctypes.c_uint, WPARAM, LPARAM)


class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", HICON),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


# 64비트 핸들 잘림 방지용 시그니처
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
user32.RegisterClassW.restype = wintypes.ATOM
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint, WPARAM, LPARAM]
user32.LoadImageW.restype = wintypes.HANDLE
user32.LoadImageW.argtypes = [
    wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
    ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.LoadIconW.restype = HICON
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.CreatePopupMenu.restype = HMENU
user32.AppendMenuW.argtypes = [HMENU, wintypes.UINT, WPARAM, wintypes.LPCWSTR]
user32.TrackPopupMenu.restype = ctypes.c_int
user32.TrackPopupMenu.argtypes = [
    HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.LPVOID]
user32.DestroyMenu.argtypes = [HMENU]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL
shell32.Shell_NotifyIconW.argtypes = [
    wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATA)]


class Tray:
    """작업표시줄 알림영역(트레이) 아이콘. 좌클릭=열기, 우클릭=메뉴."""

    def __init__(self, on_open, on_optimize, on_quit, tip="OptiBoost"):
        self.on_open = on_open
        self.on_optimize = on_optimize
        self.on_quit = on_quit
        self.tip = tip
        self._added = False
        self._proc = WNDPROC(self._wndproc)  # 참조 유지 필수
        hinst = kernel32.GetModuleHandleW(None)
        self._clsname = "PCOptTrayWnd_{}".format(id(self))
        cls = WNDCLASS()
        cls.lpfnWndProc = self._proc
        cls.hInstance = hinst
        cls.lpszClassName = self._clsname
        self._atom = user32.RegisterClassW(ctypes.byref(cls))
        self.hwnd = user32.CreateWindowExW(
            0, self._clsname, "PCOptTray", 0, 0, 0, 0, 0,
            HWND_MESSAGE, None, hinst, None)
        self.hicon = self._load_icon()

    def _load_icon(self):
        path = resource_path("icon.ico")
        if os.path.exists(path):
            h = user32.LoadImageW(None, path, IMAGE_ICON, 0, 0,
                                  LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if h:
                return h
        return user32.LoadIconW(None, 32512)  # IDI_APPLICATION

    def _nid(self, flags):
        nid = NOTIFYICONDATA()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = flags
        return nid

    def add(self):
        nid = self._nid(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        nid.uCallbackMessage = TRAY_CALLBACK
        nid.hIcon = self.hicon
        nid.szTip = self.tip
        if shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            self._added = True
        return self._added

    def remove(self):
        if self._added:
            nid = self._nid(0)
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            self._added = False

    def notify(self, title, msg):
        if not self._added:
            return
        nid = self._nid(NIF_INFO)
        nid.szInfoTitle = title[:63]
        nid.szInfo = msg[:255]
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def _show_menu(self):
        hmenu = user32.CreatePopupMenu()
        user32.AppendMenuW(hmenu, MF_STRING, 1, "열기")
        user32.AppendMenuW(hmenu, MF_STRING, 2, "지금 최적화")
        user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(hmenu, MF_STRING, 3, "종료")
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(
            hmenu, TPM_RIGHTBUTTON | TPM_RETURNCMD, pt.x, pt.y, 0,
            self.hwnd, None)
        user32.DestroyMenu(hmenu)
        if cmd == 1:
            self.on_open()
        elif cmd == 2:
            self.on_optimize()
        elif cmd == 3:
            self.on_quit()

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == TRAY_CALLBACK:
            if lparam in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self.on_open()
            elif lparam == WM_RBUTTONUP:
                self._show_menu()
            return 0
        if msg == WM_DESTROY:
            self.remove()
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


# ---------------------------------------------------------------------------
# 단일 인스턴스 (하나만 실행) — 뮤텍스 + 이벤트로 중복 실행 방지
# ---------------------------------------------------------------------------
ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x0002
MUTEX_NAME = "Local\\PCOptimizer_SingleInstance_v2"
SHOWEVENT_NAME = "Local\\PCOptimizer_ShowWindow_v2"

kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.CreateEventW.argtypes = [
    wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenEventW.restype = wintypes.HANDLE
kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.SetEvent.argtypes = [wintypes.HANDLE]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.POINTER(wintypes.LPVOID), wintypes.LPVOID]


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]


_single_mutex = None    # 참조 유지 (프로세스 살아있는 동안)
_show_event = None
_sec_attr = None


def _low_integrity_sa():
    """관리자/일반 권한 인스턴스가 서로 인식하도록 낮은 무결성 보안 속성."""
    sd = wintypes.LPVOID()
    sddl = "D:(A;;GA;;;WD)S:(ML;;NW;;;LW)"  # 모두 허용 + 낮은 무결성 라벨
    if advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(sd), None):
        sa = SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(sa)
        sa.lpSecurityDescriptor = sd
        sa.bInheritHandle = False
        return sa
    return None


def acquire_single_instance():
    """첫 인스턴스면 True. 이미 실행 중이면 그 창을 띄우라고 신호 후 False."""
    global _single_mutex, _show_event, _sec_attr
    _sec_attr = _low_integrity_sa()
    sa_ref = ctypes.byref(_sec_attr) if _sec_attr else None
    ctypes.set_last_error(0)
    _single_mutex = kernel32.CreateMutexW(sa_ref, False, MUTEX_NAME)
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS or not _single_mutex:
        h = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, SHOWEVENT_NAME)
        if h:
            kernel32.SetEvent(h)
            kernel32.CloseHandle(h)
        return False
    _show_event = kernel32.CreateEventW(sa_ref, False, False, SHOWEVENT_NAME)
    return True


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
BG = "#1e1e2e"
CARD = "#28283c"
FG = "#e6e6f0"
SUB = "#a6a6c0"
ACCENT = "#7c5cff"
ACCENT2 = "#00c896"
DANGER = "#ff5c72"
WARN = "#ffb020"


class Gauge(tk.Canvas):
    """원형 게이지 (CPU/RAM/디스크 사용률)."""

    def __init__(self, parent, label, size=140):
        super().__init__(parent, width=size, height=size, bg=CARD, highlightthickness=0)
        self.size = size
        self.label = label
        self._draw(0, ACCENT2)

    def _draw(self, pct, color):
        s = self.size
        pad = 14
        w = 13
        self.delete("all")
        self.create_arc(
            pad, pad, s - pad, s - pad, start=0, extent=359.99,
            style="arc", outline="#3a3a52", width=w,
        )
        if pct > 0:
            self.create_arc(
                pad, pad, s - pad, s - pad, start=90, extent=-359.99 * pct / 100.0,
                style="arc", outline=color, width=w,
            )
        self.create_text(
            s / 2, s / 2 - 8, text="{}%".format(int(round(pct))),
            fill=FG, font=("Malgun Gothic", 20, "bold"),
        )
        self.create_text(
            s / 2, s / 2 + 18, text=self.label, fill=SUB,
            font=("Malgun Gothic", 9),
        )

    def set(self, pct):
        pct = max(0.0, min(100.0, float(pct)))
        color = ACCENT2 if pct < 60 else (WARN if pct < 85 else DANGER)
        self._draw(pct, color)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("OptiBoost")
        self.geometry("960x760")
        self.minsize(900, 680)
        self.configure(bg=BG)

        self.status = tk.StringVar(value="준비됨")
        self._scroll_canvases = []

        # 스레드 → UI 안전 전달 큐 (Tkinter는 메인스레드에서만 만짐)
        self._alive = True
        self._ui_q = queue.Queue()
        self.after(60, self._drain_ui)

        # 트레이 아이콘 (탭 빌드 전에 준비 — 대시보드 옵션 카드가 참조함)
        self.close_to_tray = tk.BooleanVar(value=load_config().get("close_to_tray", True))
        self._tray_notified = False
        self.tray = None
        try:
            self.tray = Tray(
                on_open=lambda: self.ui(self.show_from_tray),
                on_optimize=lambda: self.ui(self.one_click),
                on_quit=lambda: self.ui(self.quit_app),
            )
            self.tray.add()
        except Exception:
            self.tray = None
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        try:
            self.iconbitmap(resource_path("icon.ico"))
        except Exception:
            pass

        # 두 번째 실행이 신호하면 이 창을 앞으로 가져오는 대기 스레드
        if _show_event:
            threading.Thread(target=self._show_waiter, daemon=True).start()

        self._setup_style()
        self._build_header()
        # 마우스 휠 스크롤: 포인터 아래 스크롤 영역을 찾아 스크롤 (전역 1회 바인딩)
        self.bind_all("<MouseWheel>", self._global_wheel)

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=14, pady=(0, 8))

        self.tab_dash = ttk.Frame(self.nb, style="Card.TFrame")
        self.tab_clean = ttk.Frame(self.nb, style="Card.TFrame")
        self.tab_boost = ttk.Frame(self.nb, style="Card.TFrame")
        self.tab_game = ttk.Frame(self.nb, style="Card.TFrame")
        self.tab_security = ttk.Frame(self.nb, style="Card.TFrame")
        self.tab_tweaks = ttk.Frame(self.nb, style="Card.TFrame")
        self.tab_repair = ttk.Frame(self.nb, style="Card.TFrame")
        self.tab_startup = ttk.Frame(self.nb, style="Card.TFrame")
        self.tab_disk = ttk.Frame(self.nb, style="Card.TFrame")
        self.tab_dup = ttk.Frame(self.nb, style="Card.TFrame")
        self.nb.add(self.tab_dash, text=" 📊 대시보드 ")
        self.nb.add(self.tab_clean, text=" 🧹 청소 ")
        self.nb.add(self.tab_boost, text=" 🚀 부스터 ")
        self.nb.add(self.tab_game, text=" 🎮 게임 ")
        self.nb.add(self.tab_security, text=" 🛡 보안 ")
        self.nb.add(self.tab_tweaks, text=" 🔥 트윅 ")
        self.nb.add(self.tab_repair, text=" 🩺 복구 ")
        self.nb.add(self.tab_startup, text=" 🧩 시작앱 ")
        self.nb.add(self.tab_disk, text=" 💽 디스크 ")
        self.nb.add(self.tab_dup, text=" 🔁 중복 ")

        self._build_dash_tab()
        self._build_clean_tab()
        self._build_boost_tab()
        self._build_game_tab()
        self._build_security_tab()
        self._build_tweaks_tab()
        self._build_repair_tab()
        self._build_startup_tab()
        self._build_disk_tab()
        self._build_dup_tab()

        bar = tk.Label(
            self, textvariable=self.status, anchor="w", bg=CARD, fg=SUB,
            padx=12, pady=5, font=("Malgun Gothic", 9),
        )
        bar.pack(fill="x", side="bottom")

        self.after(300, self.refresh_mem)
        self.after(500, self._tick)
        if "--minimized" in sys.argv or "--tray" in sys.argv:
            self.after(250, self.hide_to_tray)

    # ---------- 트레이 / 창 닫기 ----------
    def _on_close(self):
        if self.close_to_tray.get() and self.tray:
            self.hide_to_tray()
        else:
            self.quit_app()

    def hide_to_tray(self):
        if not self.tray:
            self.iconify()
            return
        self.withdraw()
        if not self._tray_notified:
            self._tray_notified = True
            self.tray.notify("OptiBoost",
                             "트레이에서 백그라운드로 계속 실행 중입니다.\n"
                             "아이콘을 클릭하면 다시 열려요.")

    def show_from_tray(self):
        self.deiconify()
        self.state("normal")
        self.lift()
        self.focus_force()

    def _show_waiter(self):
        """다른 인스턴스가 이벤트를 켜면 이 창을 앞으로 가져온다."""
        while self._alive and _show_event:
            r = kernel32.WaitForSingleObject(_show_event, 500)
            if not self._alive:
                break
            if r == 0:  # 신호 받음
                self.ui(self.show_from_tray)

    def quit_app(self):
        self._alive = False  # 큐 드레인 중단 → 스레드가 Tk를 못 건드림
        try:
            if getattr(self, "auto_on", False):
                self.stop_auto()
        except Exception:
            pass
        # 진행 중인 외부 프로세스(보안 검사/복구)가 있으면 정리 (고아 방지)
        for attr in ("sec_proc", "repair_proc"):
            try:
                proc = getattr(self, attr, None)
                if proc:
                    run_hidden(["taskkill", "/PID", str(proc.pid), "/F", "/T"])
            except Exception:
                pass
        if self.tray:
            self.tray.remove()
        # 예약된 모든 after 콜백 취소 → 종료 후 "invalid command" 오류 방지
        try:
            for aid in self.tk.call("after", "info"):
                try:
                    self.after_cancel(aid)
                except Exception:
                    pass
        except Exception:
            pass
        self.destroy()

    # ---------- 스타일 ----------
    def _setup_style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure("Card.TFrame", background=CARD)
        st.configure("TFrame", background=CARD)
        st.configure(
            "TNotebook", background=BG, borderwidth=0, tabmargins=[2, 6, 2, 0]
        )
        st.configure(
            "TNotebook.Tab",
            background=BG, foreground=SUB, padding=[10, 6],
            font=("Malgun Gothic", 10, "bold"), borderwidth=0,
        )
        st.map(
            "TNotebook.Tab",
            background=[("selected", CARD)],
            foreground=[("selected", FG)],
        )
        st.configure(
            "TCheckbutton", background=CARD, foreground=FG,
            font=("Malgun Gothic", 10),
        )
        st.map("TCheckbutton", background=[("active", CARD)])
        st.configure(
            "Treeview", background="#232336", fieldbackground="#232336",
            foreground=FG, rowheight=26, borderwidth=0,
            font=("Malgun Gothic", 9),
        )
        st.configure(
            "Treeview.Heading", background=CARD, foreground=SUB,
            font=("Malgun Gothic", 9, "bold"),
        )
        st.map("Treeview", background=[("selected", ACCENT)])
        st.configure(
            "Vertical.TScrollbar", background=CARD, troughcolor=BG,
            borderwidth=0, arrowcolor=SUB,
        )

    def _build_header(self):
        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", padx=14, pady=(12, 6))
        tk.Label(
            head, text="OptiBoost", bg=BG, fg=FG,
            font=("Malgun Gothic", 16, "bold"),
        ).pack(side="left")
        role = "관리자" if is_admin() else "일반 권한"
        col = ACCENT2 if is_admin() else SUB
        tk.Label(
            head, text="● " + role, bg=BG, fg=col,
            font=("Malgun Gothic", 9, "bold"),
        ).pack(side="right", padx=(0, 4))
        if not is_admin():
            self.btn(
                head, "관리자로 실행", relaunch_as_admin, kind="ghost"
            ).pack(side="right", padx=6)

    # ---------- 공용 버튼 ----------
    def btn(self, parent, text, cmd, kind="primary"):
        colors = {
            "primary": (ACCENT, "#ffffff"),
            "green": (ACCENT2, "#08120e"),
            "danger": (DANGER, "#ffffff"),
            "ghost": (CARD, FG),
        }
        bg, fg = colors.get(kind, colors["primary"])
        b = tk.Button(
            parent, text=text, command=cmd, bg=bg, fg=fg,
            activebackground=bg, activeforeground=fg,
            relief="flat", bd=0, padx=14, pady=7, cursor="hand2",
            font=("Malgun Gothic", 10, "bold"),
        )
        def on_enter(_):
            b.configure(bg=self._lighten(bg))
        def on_leave(_):
            b.configure(bg=bg)
        b.bind("<Enter>", on_enter)
        b.bind("<Leave>", on_leave)
        return b

    @staticmethod
    def _lighten(hexcol, amt=20):
        try:
            r = int(hexcol[1:3], 16)
            g = int(hexcol[3:5], 16)
            b = int(hexcol[5:7], 16)
            r = min(255, r + amt)
            g = min(255, g + amt)
            b = min(255, b + amt)
            return "#{:02x}{:02x}{:02x}".format(r, g, b)
        except Exception:
            return hexcol

    def set_status(self, text):
        self.status.set(text)

    def ui(self, fn, *args, **kwargs):
        """워커 스레드에서 UI 갱신을 안전하게 요청 (큐에 넣기만 함)."""
        if self._alive:
            self._ui_q.put(lambda: fn(*args, **kwargs))

    def _drain_ui(self):
        """메인스레드에서 주기적으로 큐를 비워 UI 갱신 실행."""
        try:
            while True:
                job = self._ui_q.get_nowait()
                try:
                    job()
                except Exception:
                    pass
        except queue.Empty:
            pass
        if self._alive:
            self.after(60, self._drain_ui)

    def _global_wheel(self, e):
        """포인터 아래에 스크롤 영역이 있으면 그것을 스크롤."""
        w = self.winfo_containing(e.x_root, e.y_root)
        while w is not None:
            if w in self._scroll_canvases:
                first, last = w.yview()
                if not (first <= 0.0 and last >= 1.0):  # 스크롤할 내용이 있을 때만
                    w.yview_scroll(-1 if e.delta > 0 else 1, "units")
                return
            w = getattr(w, "master", None)

    def _make_scrollable(self, parent, bg="#232336"):
        """마우스휠 지원 스크롤 영역을 만들고 내부 프레임을 반환."""
        canvas = tk.Canvas(parent, bg=bg, highlightthickness=0)
        vsb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=bg)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>", lambda e: canvas.itemconfigure(win, width=e.width)
        )
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self._scroll_canvases.append(canvas)
        return inner

    # ========================================================================
    # 탭 0: 대시보드
    # ========================================================================
    def _build_dash_tab(self):
        f = self._make_scrollable(self.tab_dash, bg=CARD)
        tk.Label(
            f, text="시스템 상태", bg=CARD, fg=FG,
            font=("Malgun Gothic", 13, "bold"),
        ).pack(anchor="w", padx=18, pady=(16, 4))

        gauges = tk.Frame(f, bg=CARD)
        gauges.pack(pady=(4, 8))
        self.g_cpu = Gauge(gauges, "CPU", size=110)
        self.g_ram = Gauge(gauges, "메모리", size=110)
        self.g_disk = Gauge(gauges, "디스크 C:", size=110)
        self.g_cpu.grid(row=0, column=0, padx=16)
        self.g_ram.grid(row=0, column=1, padx=16)
        self.g_disk.grid(row=0, column=2, padx=16)

        self.disk_free_lbl = tk.Label(
            f, text="", bg=CARD, fg=SUB, font=("Malgun Gothic", 9)
        )
        self.disk_free_lbl.pack()

        # PC 건강 점수
        hc = tk.Frame(f, bg="#232336")
        hc.pack(fill="x", padx=18, pady=(8, 4))
        self.health_lbl = tk.Label(
            hc, text="🩺 PC 건강 점수: 측정 중...", bg="#232336", fg=FG,
            font=("Malgun Gothic", 11, "bold"),
        )
        self.health_lbl.pack(side="left", padx=14, pady=9)
        self.health_issues = tk.Label(
            hc, text="", bg="#232336", fg=SUB, font=("Malgun Gothic", 9)
        )
        self.health_issues.pack(side="left", padx=4)
        self.btn(hc, "↻ 측정", self.measure_health, "ghost").pack(
            side="right", padx=10, pady=5
        )
        self.after(1200, self.measure_health)

        # 슈퍼 최적화
        oc = tk.Frame(f, bg="#232336")
        oc.pack(fill="x", padx=18, pady=(6, 6))
        tk.Label(
            oc, text="슈퍼 최적화", bg="#232336", fg=FG,
            font=("Malgun Gothic", 12, "bold"),
        ).pack(anchor="w", padx=14, pady=(10, 0))
        tk.Label(
            oc,
            text="임시·캐시 정리 + 휴지통 + 메모리 정리 + 대기메모리 퍼지(관리자) + DNS 정리를 한 번에.",
            bg="#232336", fg=SUB, font=("Malgun Gothic", 9),
        ).pack(anchor="w", padx=14, pady=(2, 6))
        ocb = tk.Frame(oc, bg="#232336")
        ocb.pack(fill="x", padx=14, pady=(0, 10))
        self.btn(ocb, "⚡ 슈퍼 최적화", self.one_click, "green").pack(side="left")
        self.oc_result = tk.Label(
            ocb, text="", bg="#232336", fg=ACCENT2,
            font=("Malgun Gothic", 10, "bold"),
        )
        self.oc_result.pack(side="left", padx=14)

        # ---- 자동 반복 최적화 ----
        self._build_auto_card(f)

        # ---- 백그라운드 / 자동 실행 ----
        self._build_bg_card(f)

        # ---- 업데이트 ----
        self._build_update_card(f)

        # 누적 통계
        self.stats_lbl = tk.Label(
            f, text="", bg=CARD, fg=SUB, font=("Malgun Gothic", 10)
        )
        self.stats_lbl.pack(anchor="w", padx=18, pady=(6, 0))
        self.refresh_stats()

    def _build_bg_card(self, parent):
        card = tk.Frame(parent, bg="#232336")
        card.pack(fill="x", padx=18, pady=(4, 8))
        tk.Label(
            card, text="🖥 백그라운드 실행", bg="#232336", fg=FG,
            font=("Malgun Gothic", 12, "bold"),
        ).pack(anchor="w", padx=14, pady=(12, 0))
        tk.Label(
            card,
            text="트레이(시계 옆)에 숨어서 계속 돌아갑니다. 자동 반복 최적화와 함께 쓰면 좋아요.",
            bg="#232336", fg=SUB, font=("Malgun Gothic", 9),
        ).pack(anchor="w", padx=14, pady=(2, 6))

        opt = tk.Frame(card, bg="#232336")
        opt.pack(fill="x", padx=14, pady=(0, 4))
        self.autorun_var = tk.BooleanVar(value=autorun_check())
        ttk.Checkbutton(
            opt, text=" Windows 시작 시 자동 실행 (트레이로)",
            variable=self.autorun_var, command=self._toggle_autorun,
            style="TCheckbutton",
        ).pack(anchor="w", pady=1)
        ttk.Checkbutton(
            opt, text=" 닫기(X) 버튼을 누르면 종료하지 않고 트레이로 숨기기",
            variable=self.close_to_tray, command=self._save_close_pref,
            style="TCheckbutton",
        ).pack(anchor="w", pady=1)

        brow = tk.Frame(card, bg="#232336")
        brow.pack(fill="x", padx=14, pady=(4, 12))
        self.btn(brow, "🡇 트레이로 숨기기", self.hide_to_tray, "ghost").pack(side="left")
        state = "사용 가능" if self.tray else "사용 불가(트레이 초기화 실패)"
        tk.Label(brow, text="트레이 " + state, bg="#232336", fg=SUB,
                 font=("Malgun Gothic", 9)).pack(side="left", padx=10)

    def _toggle_autorun(self):
        want = self.autorun_var.get()
        if autorun_set(want):
            self.set_status("시작 시 자동 실행 " + ("켜짐" if want else "꺼짐"))
        else:
            self.autorun_var.set(not want)
            self.set_status("자동 실행 설정 변경 실패")

    def _save_close_pref(self):
        cfg = load_config()
        cfg["close_to_tray"] = self.close_to_tray.get()
        save_config(cfg)

    # ---------- 자동 업데이트 ----------
    def _build_update_card(self, parent):
        card = tk.Frame(parent, bg="#232336")
        card.pack(fill="x", padx=18, pady=(4, 8))
        top = tk.Frame(card, bg="#232336")
        top.pack(fill="x", padx=14, pady=(12, 2))
        tk.Label(
            top, text="🔄 업데이트", bg="#232336", fg=FG,
            font=("Malgun Gothic", 12, "bold"),
        ).pack(side="left")
        tk.Label(
            top, text="현재 버전 v" + APP_VERSION, bg="#232336", fg=SUB,
            font=("Malgun Gothic", 9),
        ).pack(side="left", padx=10)
        self.update_btn = self.btn(top, "업데이트 확인", self.check_update_ui, "ghost")
        self.update_btn.pack(side="right")
        self.btn(top, "🌐 GitHub", self.open_github, "ghost").pack(
            side="right", padx=6)

        self.update_lbl = tk.Label(
            card, text="", bg="#232336", fg=SUB, font=("Malgun Gothic", 9),
        )
        self.update_lbl.pack(anchor="w", padx=14, pady=(0, 4))

        self.autoupdate_var = tk.BooleanVar(
            value=load_config().get("auto_update_check", True))
        ttk.Checkbutton(
            card, text=" 시작할 때 새 버전 자동 확인",
            variable=self.autoupdate_var, command=self._save_autoupdate,
            style="TCheckbutton",
        ).pack(anchor="w", padx=14, pady=(0, 12))

        # 시작 시 자동 확인 (exe 모드 + 옵션 켜짐)
        if FROZEN and self.autoupdate_var.get():
            self.after(3000, lambda: self.check_update_ui(silent=True))

    def _save_autoupdate(self):
        cfg = load_config()
        cfg["auto_update_check"] = self.autoupdate_var.get()
        save_config(cfg)

    def open_github(self):
        try:
            os.startfile("https://github.com/" + GITHUB_REPO)
        except Exception:
            self.set_status("브라우저를 열 수 없습니다")

    def check_update_ui(self, silent=False):
        self.update_lbl.configure(text="업데이트 확인 중...", fg=SUB)
        if not silent:
            self.set_status("업데이트 확인 중...")

        def work():
            try:
                info = check_update()
                err = None
            except Exception as e:
                info, err = None, e
            self.ui(self._update_result, info, err, silent)

        threading.Thread(target=work, daemon=True).start()

    def _update_result(self, info, err, silent):
        if err is not None:
            self.update_lbl.configure(text="확인 실패 (인터넷 연결 확인)", fg=WARN)
            if not silent:
                self.set_status("업데이트 확인 실패")
            return
        if info is None:
            self.update_lbl.configure(
                text="최신 버전을 사용 중입니다. ✓", fg=ACCENT2)
            if not silent:
                self.set_status("최신 버전")
            return
        # 새 버전 있음 → 라벨/버튼 강조
        tag = info.get("tag", "")
        self._pending_update = info
        self.update_lbl.configure(text="🔴 새 버전 {} 있음!".format(tag), fg=DANGER)
        try:
            self.update_btn.configure(
                text="🔴 " + tag + " 설치", command=lambda: self._prompt_update(info))
        except Exception:
            pass
        if silent:
            # 자동 확인: 알림만, 대화상자는 안 띄움
            if self.tray:
                self.tray.notify(
                    "OptiBoost 업데이트",
                    "새 버전 {} 이 나왔어요. 대시보드에서 설치하세요.".format(tag))
            self.set_status("새 버전 {} 있음 — 대시보드에서 설치".format(tag))
            return
        self._prompt_update(info)

    def _prompt_update(self, info):
        tag = info.get("tag", "")
        if not FROZEN:
            messagebox.showinfo(
                "업데이트",
                "새 버전 {} 이 있습니다.\n개발(파이썬) 모드에서는 자동 설치가 안 되니 "
                "GitHub에서 받아주세요.".format(tag))
            return
        if not messagebox.askyesno(
            "업데이트",
            "새 버전 {} 이 있습니다. 지금 업데이트할까요?\n"
            "(다운로드 후 자동으로 재시작됩니다)".format(tag)):
            return
        self._do_update(info)

    def _do_update(self, info):
        self.set_status("업데이트 다운로드 중...")
        self.update_lbl.configure(text="다운로드 중... 잠시만요", fg=WARN)

        def work():
            try:
                ok = apply_update(info.get("url"))
            except Exception:
                ok = False
            if ok:
                self.ui(lambda: messagebox.showinfo(
                    "업데이트", "다운로드 완료! 프로그램을 재시작합니다."))
                self.ui(self.quit_app)
            else:
                self.ui(lambda: messagebox.showwarning(
                    "업데이트", "업데이트에 실패했습니다.\n"
                    "GitHub 릴리스에서 직접 받아주세요."))
                self.ui(self.set_status, "업데이트 실패")

        threading.Thread(target=work, daemon=True).start()

    def _build_auto_card(self, parent):
        cfg = load_config()
        self.auto_on = False
        self._auto_after = None
        self.auto_interval = 0
        self.auto_remaining = 0

        card = tk.Frame(parent, bg="#232336")
        card.pack(fill="x", padx=18, pady=(4, 8))
        tk.Label(
            card, text="⏱ 자동 반복 최적화", bg="#232336", fg=FG,
            font=("Malgun Gothic", 12, "bold"),
        ).pack(anchor="w", padx=14, pady=(12, 0))
        tk.Label(
            card, text="설정한 간격마다 자동으로 최적화합니다. (앱이 켜져 있는 동안 작동)",
            bg="#232336", fg=SUB, font=("Malgun Gothic", 9),
        ).pack(anchor="w", padx=14, pady=(2, 6))

        row = tk.Frame(card, bg="#232336")
        row.pack(fill="x", padx=14, pady=(0, 4))
        tk.Label(row, text="간격:", bg="#232336", fg=FG,
                 font=("Malgun Gothic", 10)).pack(side="left")

        def spin(maxv, default):
            v = tk.StringVar(value=str(default))
            sp = tk.Spinbox(
                row, from_=0, to=maxv, width=4, textvariable=v,
                bg="#1a1a28", fg=FG, insertbackground=FG, relief="flat",
                justify="center", font=("Malgun Gothic", 11, "bold"),
                buttonbackground=CARD, highlightthickness=1,
                highlightbackground="#3a3a52",
            )
            return v, sp

        self.auto_h, sph = spin(99, cfg.get("auto_h", 0))
        sph.pack(side="left", padx=(8, 2))
        tk.Label(row, text="시간", bg="#232336", fg=SUB,
                 font=("Malgun Gothic", 9)).pack(side="left")
        self.auto_m, spm = spin(59, cfg.get("auto_m", 30))
        spm.pack(side="left", padx=(8, 2))
        tk.Label(row, text="분", bg="#232336", fg=SUB,
                 font=("Malgun Gothic", 9)).pack(side="left")
        self.auto_s, sps = spin(59, cfg.get("auto_s", 0))
        sps.pack(side="left", padx=(8, 2))
        tk.Label(row, text="초", bg="#232336", fg=SUB,
                 font=("Malgun Gothic", 9)).pack(side="left")

        self.auto_start_btn = self.btn(row, "▶ 시작", self.start_auto, "green")
        self.auto_start_btn.pack(side="right")
        self.auto_stop_btn = self.btn(row, "■ 정지", self.stop_auto, "danger")
        self.auto_stop_btn.pack(side="right", padx=6)

        opt = tk.Frame(card, bg="#232336")
        opt.pack(fill="x", padx=14, pady=(2, 4))
        tk.Label(opt, text="포함:", bg="#232336", fg=SUB,
                 font=("Malgun Gothic", 9)).pack(side="left")
        self.auto_mem = tk.BooleanVar(value=cfg.get("auto_mem", True))
        self.auto_temp = tk.BooleanVar(value=cfg.get("auto_temp", True))
        self.auto_recycle = tk.BooleanVar(value=cfg.get("auto_recycle", False))
        for txt, var in (("메모리 정리", self.auto_mem),
                         ("임시파일 정리", self.auto_temp),
                         ("휴지통 비우기", self.auto_recycle)):
            ttk.Checkbutton(opt, text=" " + txt, variable=var,
                            style="TCheckbutton").pack(side="left", padx=8)

        self.auto_status = tk.Label(
            card, text="자동 최적화 꺼짐", bg="#232336", fg=SUB,
            font=("Malgun Gothic", 10, "bold"),
        )
        self.auto_status.pack(anchor="w", padx=14, pady=(4, 2))
        self.auto_last = tk.Label(
            card, text="", bg="#232336", fg=SUB, font=("Malgun Gothic", 9)
        )
        self.auto_last.pack(anchor="w", padx=14, pady=(0, 12))

        # 지난 세션에 켜져 있었다면 자동 재개
        if cfg.get("auto_on"):
            self.after(800, self.start_auto)

    def _read_interval(self):
        def iv(var):
            try:
                return max(0, int(var.get()))
            except Exception:
                return 0
        return iv(self.auto_h) * 3600 + iv(self.auto_m) * 60 + iv(self.auto_s)

    def _save_auto_cfg(self):
        try:
            save_config({
                "auto_h": int(self.auto_h.get() or 0),
                "auto_m": int(self.auto_m.get() or 0),
                "auto_s": int(self.auto_s.get() or 0),
                "auto_mem": self.auto_mem.get(),
                "auto_temp": self.auto_temp.get(),
                "auto_recycle": self.auto_recycle.get(),
                "auto_on": self.auto_on,
            })
        except Exception:
            pass

    def start_auto(self):
        interval = self._read_interval()
        if interval < 5:
            messagebox.showinfo("안내", "간격은 최소 5초 이상으로 설정하세요.")
            return
        if self._auto_after:
            self.after_cancel(self._auto_after)
        self.auto_interval = interval
        self.auto_remaining = interval
        self.auto_on = True
        self._save_auto_cfg()
        self.auto_status.configure(
            text="✅ 켜짐 · {} 마다 · 다음까지 {}".format(hms(interval), hms(interval)),
            fg=ACCENT2,
        )
        self.set_status("자동 반복 최적화 시작 ({} 간격)".format(hms(interval)))
        self._auto_after = self.after(1000, self._auto_tick)

    def stop_auto(self):
        self.auto_on = False
        if self._auto_after:
            self.after_cancel(self._auto_after)
            self._auto_after = None
        self._save_auto_cfg()
        self.auto_status.configure(text="자동 최적화 꺼짐", fg=SUB)
        self.set_status("자동 반복 최적화 정지")

    def _auto_tick(self):
        if not self.auto_on or not self._alive:
            return
        self.auto_remaining -= 1
        if self.auto_remaining <= 0:
            self._run_auto()
            self.auto_remaining = self.auto_interval
        self.auto_status.configure(
            text="✅ 켜짐 · {} 마다 · 다음까지 {}".format(
                hms(self.auto_interval), hms(self.auto_remaining)
            ),
            fg=ACCENT2,
        )
        self._auto_after = self.after(1000, self._auto_tick)

    def _run_auto(self):
        self.set_status("자동 최적화 실행 중...")
        do_mem = self.auto_mem.get()
        do_temp = self.auto_temp.get()
        do_rb = self.auto_recycle.get()

        def work():
            freed = 0
            for c in build_categories():
                if c["key"] == "recyclebin":
                    if do_rb:
                        freed += clean_cat(c)
                elif c["default"] and do_temp:
                    freed += clean_cat(c)
            if do_mem:
                trim_working_sets()
                if is_admin():
                    purge_standby_list()
            if freed:
                add_freed(freed)
            now = time.strftime("%H:%M:%S")
            self.ui(self.refresh_stats)
            self.ui(self.refresh_mem)
            self.ui(lambda: self.auto_last.configure(
                text="마지막 실행: {} · {} 확보".format(now, human(freed))))
            self.ui(self.set_status,
                       "자동 최적화 완료 ({}) · {} 확보".format(now, human(freed)))

        threading.Thread(target=work, daemon=True).start()

    def refresh_stats(self):
        s = load_stats()
        self.stats_lbl.configure(
            text="🏆 지금까지 확보한 공간: {}   ·   정리 실행 {}회".format(
                human(s.get("total_freed", 0)), s.get("runs", 0)
            )
        )

    def measure_health(self):
        self.health_lbl.configure(text="🩺 PC 건강 점수: 측정 중...", fg=FG)
        self.health_issues.configure(text="")

        def work():
            try:
                score, issues = health_report()
            except Exception:
                score, issues = 0, ["측정 실패"]
            g, col = health_grade(score)
            self.ui(lambda: self.health_lbl.configure(
                text="🩺 PC 건강 점수: {}점 ({}등급)".format(score, g), fg=col))
            self.ui(lambda: self.health_issues.configure(
                text=(" · ".join(issues) if issues else "아주 깨끗해요! 👍")))

        threading.Thread(target=work, daemon=True).start()

    def _tick(self):
        if not self._alive:
            return
        try:
            self.g_cpu.set(cpu_percent())
            self.g_ram.set(mem_status().dwMemoryLoad)
            used, total = disk_usage("C:\\")
            if total:
                self.g_disk.set(100.0 * used / total)
                self.disk_free_lbl.configure(
                    text="C: 사용 {} · 여유 {} · 전체 {}".format(
                        human(used), human(total - used), human(total)
                    )
                )
        except Exception:
            pass
        if self._alive:
            self.after(1500, self._tick)

    def one_click(self):
        if not messagebox.askyesno(
            "슈퍼 최적화",
            "임시파일/캐시 정리 + 휴지통 비우기 + 메모리 정리\n"
            "+ 대기메모리 퍼지(관리자) + DNS 정리를 실행합니다.\n계속할까요?",
        ):
            return
        self.oc_result.configure(text="실행 중...")
        self.set_status("원클릭 최적화 중...")

        def work():
            freed = 0
            for c in build_categories():
                if c["key"] == "recyclebin" or c["default"]:
                    freed += clean_cat(c)
            trim_working_sets()
            extra = ""
            if is_admin():
                if purge_standby_list():
                    extra = " + 대기메모리 퍼지"
                flush_system_cache()
            flush_dns()
            add_freed(freed)
            self.ui(self.oc_result.configure,
                       {"text": "완료! {} 확보{}".format(human(freed), extra)})
            self.ui(self.refresh_stats)
            self.ui(self.refresh_mem)
            self.ui(self.set_status, "최적화 완료 · {} 확보".format(human(freed)))

        threading.Thread(target=work, daemon=True).start()

    # ========================================================================
    # 탭: 시작 프로그램 관리
    # ========================================================================
    def _build_startup_tab(self):
        f = self.tab_startup
        top = tk.Frame(f, bg=CARD)
        top.pack(fill="x", padx=16, pady=(14, 4))
        tk.Label(
            top, text="부팅 시 자동 실행되는 프로그램", bg=CARD, fg=FG,
            font=("Malgun Gothic", 12, "bold"),
        ).pack(side="left")
        self.btn(top, "↻ 새로고침", self.refresh_startup, "ghost").pack(side="right")
        tk.Label(
            f, text="필요 없는 항목을 끄면 부팅이 빨라집니다. (언제든 다시 켤 수 있어요)",
            bg=CARD, fg=SUB, font=("Malgun Gothic", 9),
        ).pack(anchor="w", padx=18)

        tvf = tk.Frame(f, bg=CARD)
        tvf.pack(fill="both", expand=True, padx=16, pady=8)
        self.su_tv = ttk.Treeview(
            tvf, columns=("state", "loc", "cmd"), show="tree headings",
            selectmode="browse",
        )
        self.su_tv.heading("#0", text="프로그램")
        self.su_tv.heading("state", text="상태")
        self.su_tv.heading("loc", text="위치")
        self.su_tv.heading("cmd", text="실행 명령")
        self.su_tv.column("#0", width=200)
        self.su_tv.column("state", width=70, anchor="center")
        self.su_tv.column("loc", width=120, anchor="center")
        self.su_tv.column("cmd", width=380)
        self.su_tv.tag_configure("on", foreground=ACCENT2)
        self.su_tv.tag_configure("off", foreground=SUB)
        sb = ttk.Scrollbar(tvf, orient="vertical", command=self.su_tv.yview)
        self.su_tv.configure(yscrollcommand=sb.set)
        self.su_tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        bottom = tk.Frame(f, bg=CARD)
        bottom.pack(fill="x", padx=16, pady=(4, 14))
        tk.Label(
            bottom, text="항목 선택 후 →", bg=CARD, fg=SUB,
            font=("Malgun Gothic", 9),
        ).pack(side="left")
        self.btn(bottom, "✔ 켜기", lambda: self.toggle_startup(True), "ghost").pack(
            side="left", padx=6
        )
        self.btn(bottom, "⛔ 끄기", lambda: self.toggle_startup(False), "danger").pack(
            side="left"
        )
        self.su_rows = {}
        self.refresh_startup()

    def refresh_startup(self):
        self.set_status("시작 프로그램 읽는 중...")
        threading.Thread(target=self._refresh_startup_worker, daemon=True).start()

    def _refresh_startup_worker(self):
        rows = list_startup()
        self.ui(self._fill_startup, rows)

    def _fill_startup(self, rows):
        self.su_tv.delete(*self.su_tv.get_children())
        self.su_rows = {}
        rows.sort(key=lambda r: (not r["enabled"], r["name"].lower()))
        for r in rows:
            tag = "on" if r["enabled"] else "off"
            state = "켜짐" if r["enabled"] else "꺼짐"
            cmd = r.get("command", "")
            if len(cmd) > 90:
                cmd = cmd[:90] + "…"
            iid = self.su_tv.insert(
                "", "end", text="  " + r["name"],
                values=(state, r.get("label", ""), cmd), tags=(tag,),
            )
            self.su_rows[iid] = r
        self.set_status("시작 프로그램 {}개".format(len(rows)))

    def toggle_startup(self, enable):
        sel = self.su_tv.selection()
        if not sel:
            messagebox.showinfo("안내", "항목을 선택하세요.")
            return
        row = self.su_rows.get(sel[0])
        if not row:
            return
        if enable and row["enabled"]:
            messagebox.showinfo("안내", "이미 켜져 있는 항목입니다.")
            return
        if not enable and not row["enabled"]:
            messagebox.showinfo("안내", "이미 꺼져 있는 항목입니다.")
            return
        ok, err = (enable_startup(row) if enable else disable_startup(row))
        if ok:
            self.set_status("'{}' {} 완료".format(row["name"], "켜기" if enable else "끄기"))
            self.refresh_startup()
        else:
            if "관리자" in err:
                messagebox.showwarning(
                    "권한 필요",
                    "이 항목은 '모든 사용자'용이라 관리자 권한이 필요합니다.\n"
                    "우측 상단 '관리자로 실행'으로 다시 열어주세요.",
                )
            else:
                messagebox.showwarning("실패", "변경하지 못했습니다:\n" + err)

    # ========================================================================
    # 탭 1: 청소
    # ========================================================================
    def _build_clean_tab(self):
        f = self.tab_clean
        top = tk.Frame(f, bg=CARD)
        top.pack(fill="x", padx=16, pady=(14, 6))
        tk.Label(
            top, text="정리할 항목을 선택하세요", bg=CARD, fg=FG,
            font=("Malgun Gothic", 12, "bold"),
        ).pack(side="left")
        self.clean_total = tk.Label(
            top, text="", bg=CARD, fg=ACCENT2,
            font=("Malgun Gothic", 12, "bold"),
        )
        self.clean_total.pack(side="right")

        bodywrap = tk.Frame(f, bg=CARD)
        bodywrap.pack(fill="both", expand=True, padx=16, pady=6)
        body = self._make_scrollable(bodywrap, bg=CARD)

        self.cats = build_categories()
        self.clean_vars = {}
        self.clean_size_lbls = {}
        for c in self.cats:
            row = tk.Frame(body, bg="#232336")
            row.pack(fill="x", pady=3)
            var = tk.BooleanVar(value=c["default"])
            self.clean_vars[c["key"]] = var
            cb = ttk.Checkbutton(row, text="  " + c["label"], variable=var)
            cb.configure(style="TCheckbutton")
            cb.pack(side="left", padx=8, pady=6)
            if c["note"]:
                tk.Label(
                    row, text="· " + c["note"], bg="#232336", fg=WARN,
                    font=("Malgun Gothic", 9),
                ).pack(side="left")
            lbl = tk.Label(
                row, text="—", bg="#232336", fg=SUB,
                font=("Malgun Gothic", 10, "bold"),
            )
            lbl.pack(side="right", padx=12)
            self.clean_size_lbls[c["key"]] = lbl

        btns = tk.Frame(f, bg=CARD)
        btns.pack(fill="x", padx=16, pady=(6, 6))
        self.btn(btns, "🔍 검사 (용량 확인)", self.scan_clean, "ghost").pack(side="left")
        self.btn(btns, "🧹 선택 항목 정리", self.do_clean, "green").pack(side="right")

        # 자동 예약 청소
        sched = tk.Frame(f, bg="#232336")
        sched.pack(fill="x", padx=16, pady=(0, 14))
        self.sched_lbl = tk.Label(
            sched, text="", bg="#232336", fg=SUB, font=("Malgun Gothic", 9)
        )
        self.sched_lbl.pack(side="left", padx=12, pady=10)
        self.btn(sched, "🗓 매주 자동청소 끄기", self.disable_schedule, "ghost").pack(
            side="right", padx=(6, 12)
        )
        self.btn(sched, "🗓 매주 자동청소 켜기", self.enable_schedule, "ghost").pack(
            side="right", pady=6
        )
        self.refresh_schedule()

    def refresh_schedule(self):
        if schedule_exists():
            self.sched_lbl.configure(
                text="✅ 자동청소 켜짐 (매주 일요일 18시, 안전 항목 자동 정리)", fg=ACCENT2
            )
        else:
            self.sched_lbl.configure(text="자동청소 꺼짐", fg=SUB)

    def enable_schedule(self):
        if schedule_create():
            self.refresh_schedule()
            self.set_status("자동청소 예약 완료")
        else:
            messagebox.showwarning("실패", "예약 등록에 실패했습니다.")

    def disable_schedule(self):
        if schedule_delete():
            self.refresh_schedule()
            self.set_status("자동청소 예약 해제")
        else:
            self.set_status("예약이 없거나 해제 실패")

    def scan_clean(self):
        self.set_status("검사 중...")
        for c in self.cats:
            self.clean_size_lbls[c["key"]].configure(text="...", fg=SUB)
        threading.Thread(target=self._scan_clean_worker, daemon=True).start()

    def _scan_clean_worker(self):
        grand = 0
        for c in self.cats:
            size = cat_size(c)
            grand += size
            txt = human(size) if size else "0 B"
            self.ui(self._set_cat_size, c["key"], txt)
        self.ui(self.clean_total.configure, {"text": "회수 가능: " + human(grand)})
        self.ui(self.set_status, "검사 완료")

    def _set_cat_size(self, key, txt):
        self.clean_size_lbls[key].configure(text=txt, fg=FG)

    def do_clean(self):
        chosen = [c for c in self.cats if self.clean_vars[c["key"]].get()]
        if not chosen:
            messagebox.showinfo("안내", "정리할 항목을 하나 이상 선택하세요.")
            return
        if not messagebox.askyesno(
            "확인",
            "선택한 {}개 항목을 정리합니다.\n(임시/캐시 파일은 안전하게 삭제됩니다)\n계속할까요?".format(
                len(chosen)
            ),
        ):
            return
        self.set_status("정리 중...")
        threading.Thread(
            target=self._clean_worker, args=(chosen,), daemon=True
        ).start()

    def _clean_worker(self, chosen):
        freed = 0
        for c in chosen:
            self.ui(self.set_status, "정리 중: " + c["label"])
            freed += clean_cat(c)
            self.ui(self._set_cat_size, c["key"], "완료")
        add_freed(freed)
        self.ui(self.clean_total.configure, {"text": "정리됨: " + human(freed)})
        self.ui(self.refresh_stats)
        self.ui(self.set_status, "정리 완료 · {} 확보".format(human(freed)))
        self.ui(lambda: messagebox.showinfo(
            "완료", "정리 완료!\n확보한 공간: " + human(freed)))

    # ========================================================================
    # 탭 2: 부스터
    # ========================================================================
    def _build_boost_tab(self):
        f = self.tab_boost
        # 메모리 카드
        memcard = tk.Frame(f, bg="#232336")
        memcard.pack(fill="x", padx=16, pady=(14, 8))
        tk.Label(
            memcard, text="메모리 (RAM)", bg="#232336", fg=SUB,
            font=("Malgun Gothic", 10, "bold"),
        ).pack(anchor="w", padx=12, pady=(10, 2))
        self.mem_lbl = tk.Label(
            memcard, text="—", bg="#232336", fg=FG,
            font=("Malgun Gothic", 14, "bold"),
        )
        self.mem_lbl.pack(anchor="w", padx=12)
        self.mem_bar = ttk.Progressbar(memcard, maximum=100, length=100)
        self.mem_bar.pack(fill="x", padx=12, pady=(6, 4))
        mrow = tk.Frame(memcard, bg="#232336")
        mrow.pack(fill="x", padx=12, pady=(4, 12))
        self.btn(mrow, "🧠 메모리 정리", self.do_trim, "green").pack(side="left")
        self.btn(mrow, "↻ 새로고침", self.refresh_mem, "ghost").pack(side="left", padx=8)

        # 전원 카드
        pcard = tk.Frame(f, bg="#232336")
        pcard.pack(fill="x", padx=16, pady=8)
        tk.Label(
            pcard, text="전원 관리 옵션", bg="#232336", fg=SUB,
            font=("Malgun Gothic", 10, "bold"),
        ).pack(anchor="w", padx=12, pady=(10, 2))
        self.power_lbl = tk.Label(
            pcard, text="현재: —", bg="#232336", fg=FG,
            font=("Malgun Gothic", 11, "bold"),
        )
        self.power_lbl.pack(anchor="w", padx=12)
        prow = tk.Frame(pcard, bg="#232336")
        prow.pack(fill="x", padx=12, pady=(6, 12))
        self.btn(prow, "⚡ 고성능", lambda: self.set_power("scheme_min"), "green").pack(
            side="left"
        )
        self.btn(prow, "⚖ 균형", lambda: self.set_power("scheme_balanced"), "ghost").pack(
            side="left", padx=8
        )
        self.btn(prow, "🌐 DNS 캐시 비우기", self.do_flush_dns, "ghost").pack(
            side="left", padx=8
        )
        self.btn(prow, "🔄 탐색기 다시 시작", self.restart_explorer, "ghost").pack(
            side="left", padx=8
        )

        # 프로세스 카드
        proc = tk.Frame(f, bg="#232336")
        proc.pack(fill="both", expand=True, padx=16, pady=(8, 14))
        prow2 = tk.Frame(proc, bg="#232336")
        prow2.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(
            prow2, text="백그라운드 프로그램 (메모리 많이 쓰는 순)", bg="#232336",
            fg=SUB, font=("Malgun Gothic", 10, "bold"),
        ).pack(side="left")
        self.btn(prow2, "↻ 목록", self.refresh_procs, "ghost").pack(side="right")
        self.btn(prow2, "✖ 선택 종료", self.kill_selected, "danger").pack(
            side="right", padx=8
        )

        tvf = tk.Frame(proc, bg="#232336")
        tvf.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.proc_tv = ttk.Treeview(
            tvf, columns=("mem",), show="tree headings", selectmode="extended"
        )
        self.proc_tv.heading("#0", text="프로그램")
        self.proc_tv.heading("mem", text="메모리")
        self.proc_tv.column("#0", width=380)
        self.proc_tv.column("mem", width=120, anchor="e")
        sb = ttk.Scrollbar(tvf, orient="vertical", command=self.proc_tv.yview)
        self.proc_tv.configure(yscrollcommand=sb.set)
        self.proc_tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.proc_map = {}
        self.refresh_procs()
        self.after(400, self.refresh_power)

    def refresh_mem(self):
        m = mem_status()
        used = m.ullTotalPhys - m.ullAvailPhys
        pct = m.dwMemoryLoad
        self.mem_lbl.configure(
            text="{} / {}  ({}% 사용)".format(
                human(used), human(m.ullTotalPhys), pct
            )
        )
        self.mem_bar["value"] = pct

    def do_trim(self):
        self.set_status("메모리 정리 중...")
        before = mem_status().ullAvailPhys

        def work():
            trim_working_sets()
            deep = ""
            if is_admin():
                if purge_standby_list():
                    deep = " (대기메모리 퍼지 포함)"
                flush_system_cache()
            time.sleep(0.6)
            after = mem_status().ullAvailPhys
            gained = after - before
            self.ui(self.refresh_mem)
            msg = "메모리 정리 완료" + deep
            if gained > 0:
                msg += " · 약 {} 확보".format(human(gained))
            self.ui(self.set_status, msg)

        threading.Thread(target=work, daemon=True).start()

    def do_flush_dns(self):
        if flush_dns():
            self.set_status("DNS 캐시를 비웠습니다")
        else:
            self.set_status("DNS 캐시 비우기 실패")

    def restart_explorer(self):
        if not messagebox.askyesno(
            "탐색기 다시 시작",
            "작업표시줄·바탕화면이 느리거나 먹통일 때 도움이 됩니다.\n"
            "화면이 잠깐 깜빡이며 탐색기가 재시작됩니다. 계속할까요?"):
            return
        self.set_status("탐색기 다시 시작 중...")

        def work():
            run_hidden(["taskkill", "/F", "/IM", "explorer.exe"])
            time.sleep(1.2)
            # 대부분 자동 재시작됨 — 안 됐을 때만 직접 실행 (불필요한 창 방지)
            r = run_hidden(["tasklist", "/FI", "IMAGENAME eq explorer.exe", "/NH"])
            if "explorer.exe" not in (r.stdout or "").lower():
                try:
                    subprocess.Popen(["explorer.exe"], creationflags=CREATE_NO_WINDOW)
                except Exception:
                    pass
            self.ui(self.set_status, "탐색기를 다시 시작했습니다")

        threading.Thread(target=work, daemon=True).start()

    def refresh_power(self):
        self.power_lbl.configure(text="현재: " + get_active_power_scheme())

    def set_power(self, alias):
        if set_power_scheme(alias):
            self.refresh_power()
            self.set_status("전원 옵션 변경됨")
        else:
            self.set_status("전원 옵션 변경 실패 (해당 모드가 없을 수 있음)")
            messagebox.showwarning(
                "안내",
                "전원 모드를 바꾸지 못했습니다.\n일부 노트북/PC에서는 '고성능' 모드가 숨겨져 있을 수 있어요.",
            )

    def refresh_procs(self):
        self.set_status("프로세스 목록 읽는 중...")
        threading.Thread(target=self._refresh_procs_worker, daemon=True).start()

    def _refresh_procs_worker(self):
        procs = list_processes()
        self.ui(self._fill_procs, procs)

    def _fill_procs(self, procs):
        self.proc_tv.delete(*self.proc_tv.get_children())
        self.proc_map = {}
        for name, pids, mem in procs[:60]:
            iid = self.proc_tv.insert(
                "", "end", text="  " + name, values=(human(mem),)
            )
            self.proc_map[iid] = (name, pids)
        self.set_status("프로세스 {}개".format(len(procs)))

    def kill_selected(self):
        sel = self.proc_tv.selection()
        if not sel:
            messagebox.showinfo("안내", "종료할 프로그램을 선택하세요.")
            return
        names = [self.proc_map[i][0] for i in sel if i in self.proc_map]
        if not messagebox.askyesno(
            "확인",
            "다음 프로그램을 강제 종료합니다:\n\n"
            + "\n".join("· " + n for n in names)
            + "\n\n저장하지 않은 작업은 사라질 수 있어요. 계속할까요?",
        ):
            return
        killed = 0
        for i in sel:
            if i in self.proc_map:
                killed += kill_pids(self.proc_map[i][1])
        self.set_status("{}개 프로세스 종료".format(killed))
        self.refresh_procs()
        self.refresh_mem()

    # ========================================================================
    # 탭: 게임 부스트
    # ========================================================================
    def _build_game_tab(self):
        f = self.tab_game
        self.boost_active = False
        self.boost_prev_scheme = None
        self.boost_start = 0
        self._boost_after = None
        self.game_app_vars = {}

        tk.Label(
            f, text="🎮 게임 부스트", bg=CARD, fg=FG,
            font=("Malgun Gothic", 13, "bold"),
        ).pack(anchor="w", padx=18, pady=(14, 2))
        tk.Label(
            f, text="게임 실행 전에 켜세요. 종료하면 전원 설정이 원래대로 복원됩니다.",
            bg=CARD, fg=SUB, font=("Malgun Gothic", 9),
        ).pack(anchor="w", padx=18)

        # 상태 배너
        banner = tk.Frame(f, bg="#232336")
        banner.pack(fill="x", padx=18, pady=(8, 6))
        self.boost_status = tk.Label(
            banner, text="● 부스트 꺼짐", bg="#232336", fg=SUB,
            font=("Malgun Gothic", 12, "bold"),
        )
        self.boost_status.pack(side="left", padx=14, pady=12)
        self.boost_btn = self.btn(banner, "🎮 부스트 시작", self.toggle_boost, "green")
        self.boost_btn.pack(side="right", padx=14, pady=8)

        # 옵션
        opt = tk.Frame(f, bg=CARD)
        opt.pack(fill="x", padx=18, pady=(2, 4))
        tk.Label(opt, text="적용 항목:", bg=CARD, fg=SUB,
                 font=("Malgun Gothic", 9)).pack(side="left")
        self.gb_power = tk.BooleanVar(value=True)
        self.gb_mem = tk.BooleanVar(value=True)
        self.gb_gamemode = tk.BooleanVar(value=True)
        self.gb_kill = tk.BooleanVar(value=True)
        for txt, var in (("고성능 전원", self.gb_power),
                         ("메모리 정리", self.gb_mem),
                         ("Windows 게임 모드", self.gb_gamemode),
                         ("백그라운드 앱 정리", self.gb_kill)):
            ttk.Checkbutton(opt, text=" " + txt, variable=var,
                            style="TCheckbutton").pack(side="left", padx=6)

        # 종료 대상 앱 목록
        head = tk.Frame(f, bg=CARD)
        head.pack(fill="x", padx=18, pady=(8, 0))
        tk.Label(
            head, text="정리할 백그라운드 앱 (체크한 것만 닫음)", bg=CARD, fg=FG,
            font=("Malgun Gothic", 10, "bold"),
        ).pack(side="left")
        self.btn(head, "↻ 새로고침", self.refresh_game_apps, "ghost").pack(side="right")

        listwrap = tk.Frame(f, bg="#232336")
        listwrap.pack(fill="both", expand=True, padx=18, pady=(6, 10))
        self.game_list = self._make_scrollable(listwrap, bg="#232336")

        self.refresh_game_apps()

    def refresh_game_apps(self):
        for w in self.game_list.winfo_children():
            w.destroy()
        self.game_app_vars = {}
        threading.Thread(target=self._refresh_game_worker, daemon=True).start()

    def _refresh_game_worker(self):
        apps = list_closable_apps()
        self.ui(self._fill_game_apps, apps)

    def _fill_game_apps(self, apps):
        for w in self.game_list.winfo_children():
            w.destroy()
        self.game_app_vars = {}
        if not apps:
            tk.Label(
                self.game_list, text="  정리할 백그라운드 앱이 없습니다. (이미 깔끔해요!)",
                bg="#232336", fg=SUB, font=("Malgun Gothic", 10),
            ).pack(anchor="w", padx=8, pady=10)
            return
        for name, pids, mem, default in apps:
            low = name.lower()
            var = tk.BooleanVar(value=default)
            self.game_app_vars[low] = var
            row = tk.Frame(self.game_list, bg="#232336")
            row.pack(fill="x", pady=1)
            ttk.Checkbutton(
                row, text="  " + name, variable=var, style="TCheckbutton"
            ).pack(side="left", padx=8, pady=2)
            tk.Label(
                row, text=human(mem), bg="#232336", fg=SUB,
                font=("Malgun Gothic", 9),
            ).pack(side="right", padx=14)
        self.set_status("정리 가능 앱 {}개".format(len(apps)))

    def toggle_boost(self):
        if self.boost_active:
            self.end_boost()
        else:
            self.start_boost()

    def start_boost(self):
        checked = [n for n, v in self.game_app_vars.items() if v.get()]
        do_power = self.gb_power.get()
        do_mem = self.gb_mem.get()
        do_gm = self.gb_gamemode.get()
        do_kill = self.gb_kill.get()
        if do_kill and checked:
            if not messagebox.askyesno(
                "게임 부스트",
                "선택한 백그라운드 앱 {}개를 닫고 부스트를 켭니다.\n"
                "(브라우저 등은 저장 안 된 작업이 사라질 수 있어요)\n계속할까요?".format(
                    len(checked)
                ),
            ):
                return
        self.boost_status.configure(text="● 부스트 준비 중...", fg=WARN)
        self.set_status("게임 부스트 적용 중...")

        def work():
            self.boost_prev_scheme = get_active_scheme_guid()
            if do_power:
                set_power_scheme("scheme_min")
            if do_gm:
                set_game_mode(True)
            if do_mem:
                trim_working_sets()
            closed = 0
            if do_kill and checked:
                for name, pids, mem in list_processes():
                    if name.lower() in checked:
                        closed += kill_pids(pids)
            if do_mem:
                trim_working_sets()
                if is_admin():
                    purge_standby_list()
            self.boost_active = True
            self.boost_start = time.time()
            self.ui(self._boost_ui_on, closed)
            self.ui(self.refresh_mem)
            self.ui(self.refresh_power)

        threading.Thread(target=work, daemon=True).start()

    def _boost_ui_on(self, closed):
        self.boost_btn.configure(text="🔄 부스트 종료 · 복원")
        self.set_status("게임 부스트 켜짐 · 앱 {}개 정리".format(closed))
        self._boost_tick()

    def _boost_tick(self):
        if not self.boost_active or not self._alive:
            return
        elapsed = int(time.time() - self.boost_start)
        self.boost_status.configure(
            text="● 부스트 켜짐 · 경과 {}".format(hms(elapsed)), fg=ACCENT2
        )
        self._boost_after = self.after(1000, self._boost_tick)

    def end_boost(self):
        if self._boost_after:
            self.after_cancel(self._boost_after)
            self._boost_after = None
        if self.boost_prev_scheme:
            set_power_scheme(self.boost_prev_scheme)
        self.boost_active = False
        self.boost_btn.configure(text="🎮 부스트 시작")
        self.boost_status.configure(text="● 부스트 꺼짐 (전원 설정 복원됨)", fg=SUB)
        self.refresh_power()
        self.refresh_game_apps()
        self.set_status("게임 부스트 종료 · 전원 설정 복원됨")

    # ========================================================================
    # 탭: 🛡 보안 검사 (Windows Defender)
    # ========================================================================
    def _build_security_tab(self):
        f = self.tab_security
        self.sec_busy = False
        self.sec_proc = None

        top = tk.Frame(f, bg=CARD)
        top.pack(fill="x", padx=16, pady=(14, 2))
        tk.Label(
            top, text="🛡 바이러스·보안 검사", bg=CARD, fg=FG,
            font=("Malgun Gothic", 13, "bold"),
        ).pack(side="left")
        self.btn(top, "↻ 상태", self.refresh_security, "ghost").pack(side="right")
        tk.Label(
            f, text="Windows에 내장된 백신(Microsoft Defender)으로 악성코드를 검사합니다.",
            bg=CARD, fg=SUB, font=("Malgun Gothic", 9),
        ).pack(anchor="w", padx=18)

        # 상태 카드
        stc = tk.Frame(f, bg="#232336")
        stc.pack(fill="x", padx=16, pady=(8, 6))
        self.sec_rtp = tk.Label(
            stc, text="실시간 보호: 확인 중...", bg="#232336", fg=FG,
            font=("Malgun Gothic", 11, "bold"),
        )
        self.sec_rtp.pack(anchor="w", padx=14, pady=(10, 0))
        self.sec_info = tk.Label(
            stc, text="", bg="#232336", fg=SUB, font=("Malgun Gothic", 9),
            justify="left",
        )
        self.sec_info.pack(anchor="w", padx=14, pady=(2, 10))

        # 버튼
        btns = tk.Frame(f, bg=CARD)
        btns.pack(fill="x", padx=16, pady=6)
        self.btn(btns, "⚡ 빠른 검사", lambda: self.run_scan("빠른 검사", "1"),
                 "green").pack(side="left", padx=(0, 6))
        self.btn(btns, "🔍 전체 검사", lambda: self.run_scan("전체 검사", "2"),
                 "ghost").pack(side="left", padx=6)
        self.btn(btns, "🔃 백신 정의 업데이트", self.update_defs,
                 "ghost").pack(side="left", padx=6)
        self.btn(btns, "📋 위협 기록", self.show_threats,
                 "ghost").pack(side="left", padx=6)
        self.btn(btns, "⏹ 중지", self.stop_scan, "danger").pack(side="right")

        tk.Label(
            f, text="※ 빠른 검사는 보통 1~3분, 전체 검사는 수십 분 걸릴 수 있어요. "
                    "검사 중 결과가 잠깐 멈춘 듯 보여도 정상입니다.",
            bg=CARD, fg=WARN, font=("Malgun Gothic", 9), justify="left",
        ).pack(anchor="w", padx=18, pady=(2, 0))

        outwrap = tk.Frame(f, bg=CARD)
        outwrap.pack(fill="both", expand=True, padx=16, pady=(6, 14))
        self.sec_out = tk.Text(
            outwrap, bg="#141420", fg="#9fe8c5", insertbackground=FG,
            relief="flat", font=("Consolas", 9), state="disabled", wrap="word",
        )
        ssb = ttk.Scrollbar(outwrap, orient="vertical", command=self.sec_out.yview)
        self.sec_out.configure(yscrollcommand=ssb.set)
        self.sec_out.pack(side="left", fill="both", expand=True)
        ssb.pack(side="right", fill="y")
        self._sec_append("검사 결과가 여기에 표시됩니다.\n")
        self.after(1400, self.refresh_security)

    def _sec_append(self, text):
        self.sec_out.configure(state="normal")
        self.sec_out.insert("end", text)
        self.sec_out.see("end")
        self.sec_out.configure(state="disabled")

    def refresh_security(self):
        self.set_status("보안 상태 확인 중...")

        def work():
            d = defender_status()
            self.ui(self._fill_security, d)

        threading.Thread(target=work, daemon=True).start()

    def _fill_security(self, d):
        if not d:
            self.sec_rtp.configure(
                text="Windows Defender 상태를 읽을 수 없음", fg=WARN)
            self.sec_info.configure(
                text="다른 백신 프로그램을 사용 중이거나 Defender가 꺼져 있을 수 있어요.")
            self.set_status("보안 상태 확인 실패")
            return
        rtp = d.get("RTP", "").lower() == "true"
        self.sec_rtp.configure(
            text="실시간 보호: " + ("켜짐 ✓" if rtp else "꺼짐 ✗"),
            fg=ACCENT2 if rtp else DANGER)
        mode = d.get("MODE", "")
        info = "백신 정의: {} ({})\n마지막 빠른검사: {}\n마지막 전체검사: {}".format(
            d.get("SIG", "?"), d.get("SIGDATE", "?") or "?",
            d.get("QSCAN", "") or "기록 없음",
            d.get("FSCAN", "") or "기록 없음")
        if mode and mode.lower() != "normal":
            info = "실행 모드: {} (다른 백신이 주력일 수 있음)\n".format(mode) + info
        self.sec_info.configure(text=info)
        self.set_status("보안 상태 확인 완료")

    def run_scan(self, label, scantype):
        if self.sec_busy:
            messagebox.showinfo("안내", "이미 검사가 진행 중입니다.")
            return
        mp = find_mpcmdrun()
        if not mp:
            messagebox.showwarning(
                "안내", "Windows Defender를 찾을 수 없습니다.\n"
                "다른 백신을 사용 중이면 그 프로그램으로 검사하세요.")
            return
        self.sec_busy = True
        self._sec_append("\n===== {} 시작 =====\n".format(label))
        self.set_status(label + " 진행 중...")
        cmd = [mp, "-Scan", "-ScanType", scantype]

        def work():
            self._stream(cmd, label)
            self.ui(self.refresh_security)

        threading.Thread(target=work, daemon=True).start()

    def update_defs(self):
        if self.sec_busy:
            messagebox.showinfo("안내", "이미 작업이 진행 중입니다.")
            return
        mp = find_mpcmdrun()
        if not mp:
            messagebox.showwarning("안내", "Windows Defender를 찾을 수 없습니다.")
            return
        self.sec_busy = True
        self._sec_append("\n===== 백신 정의 업데이트 =====\n")
        self.set_status("백신 정의 업데이트 중...")

        def work():
            self._stream([mp, "-SignatureUpdate"], "정의 업데이트")
            self.ui(self.refresh_security)

        threading.Thread(target=work, daemon=True).start()

    def show_threats(self):
        self._sec_append("\n----- 위협 기록 조회 -----\n")
        self.set_status("위협 기록 조회 중...")

        def work():
            names = defender_threats()
            if names:
                txt = "탐지된 위협 {}건:\n".format(len(names))
                txt += "\n".join("  · " + n for n in names[:30]) + "\n"
            else:
                txt = "탐지·격리된 위협 기록이 없습니다. 깨끗해요! ✓\n"
            self.ui(self._sec_append, txt)
            self.ui(self.set_status, "위협 기록 조회 완료")

        threading.Thread(target=work, daemon=True).start()

    def stop_scan(self):
        if not self.sec_busy or not self.sec_proc:
            self.set_status("진행 중인 검사가 없습니다")
            return
        try:
            run_hidden(["taskkill", "/PID", str(self.sec_proc.pid), "/F", "/T"])
        except Exception:
            pass
        self._sec_append("(사용자가 중지함)\n")
        self.set_status("검사 중지")

    def _stream(self, cmd, label):
        """프로세스 실행 + 실시간 출력을 보안 로그창에 표시."""
        try:
            self.sec_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW)
            for raw in iter(self.sec_proc.stdout.readline, b""):
                line = raw.replace(b"\x00", b"").decode("mbcs", "replace")
                if line.strip():
                    self.ui(self._sec_append, line)
            self.sec_proc.wait()
            rc = self.sec_proc.returncode
        except Exception as e:
            rc = -1
            self.ui(self._sec_append, "오류: {}\n".format(e))
        self.sec_proc = None
        self.sec_busy = False
        if rc == 0:
            self.ui(self._sec_append,
                    "===== {} 완료 · 위협 없음/처리됨 =====\n".format(label))
        elif rc == 2:
            self.ui(self._sec_append,
                    "===== {} 완료 · ⚠ 위협 발견! '위협 기록'을 확인하세요 =====\n"
                    .format(label))
        else:
            self.ui(self._sec_append,
                    "===== {} 종료 (코드 {}) =====\n".format(label, rc))
        self.ui(self.set_status, label + " 완료")

    # ========================================================================
    # 탭: 🔥 트윅
    # ========================================================================
    def _build_tweaks_tab(self):
        f = self.tab_tweaks
        top = tk.Frame(f, bg=CARD)
        top.pack(fill="x", padx=16, pady=(14, 2))
        tk.Label(
            top, text="🔥 성능 트윅", bg=CARD, fg=FG,
            font=("Malgun Gothic", 13, "bold"),
        ).pack(side="left")
        self.btn(top, "↻ 새로고침", self.refresh_tweaks, "ghost").pack(side="right")
        self.btn(top, "↩ 모두 되돌리기", self.revert_all_tweaks, "ghost").pack(
            side="right", padx=6
        )
        self.btn(top, "🔥 추천 모두 적용", self.apply_all_tweaks, "green").pack(
            side="right", padx=6
        )
        tk.Label(
            f, text="모든 트윅은 백업 후 적용되며, 언제든 '되돌리기'로 원상복구됩니다. 🛡 = 관리자 필요",
            bg=CARD, fg=SUB, font=("Malgun Gothic", 9),
        ).pack(anchor="w", padx=18, pady=(0, 4))

        wrap = tk.Frame(f, bg="#232336")
        wrap.pack(fill="both", expand=True, padx=16, pady=(4, 14))
        self.tweaks_list = self._make_scrollable(wrap)
        self.refresh_tweaks()

    def refresh_tweaks(self):
        def work():
            states = []
            for tw in TWEAKS:
                try:
                    states.append((tw, bool(tw["check"]())))
                except Exception:
                    states.append((tw, False))
            self.ui(self._render_tweaks, states)

        threading.Thread(target=work, daemon=True).start()

    def _render_tweaks(self, states):
        for w in self.tweaks_list.winfo_children():
            w.destroy()
        for tw, applied in states:
            row = tk.Frame(self.tweaks_list, bg="#232336")
            row.pack(fill="x", pady=3, padx=6)
            # 오른쪽 위젯을 먼저 pack해야 왼쪽 expand 프레임에 밀리지 않음
            state_lbl = tk.Label(
                row, text=("적용됨" if applied else "꺼짐"), bg="#232336",
                fg=(ACCENT2 if applied else SUB),
                font=("Malgun Gothic", 9, "bold"), width=6,
            )
            state_lbl.pack(side="right", padx=(4, 12))
            if applied:
                b = self.btn(row, "↩ 되돌리기",
                             lambda t=tw: self._tweak_do(t, True), "ghost")
            else:
                b = self.btn(row, "적용",
                             lambda t=tw: self._tweak_do(t, False), "green")
            b.pack(side="right", padx=4, pady=4)
            dot = tk.Label(
                row, text="●", bg="#232336",
                fg=(ACCENT2 if applied else "#4a4a63"),
                font=("Malgun Gothic", 12),
            )
            dot.pack(side="left", padx=(10, 6), pady=6)
            info = tk.Frame(row, bg="#232336")
            info.pack(side="left", fill="x", expand=True)
            name = tw["label"] + ("   🛡 관리자" if tw["admin"] else "")
            tk.Label(
                info, text=name, bg="#232336", fg=FG, anchor="w",
                font=("Malgun Gothic", 10, "bold"),
            ).pack(anchor="w", fill="x")
            tk.Label(
                info, text=tw["desc"], bg="#232336", fg=SUB, anchor="w",
                wraplength=600, justify="left", font=("Malgun Gothic", 9),
            ).pack(anchor="w", fill="x")
        self.set_status("트윅 {}개 · 적용됨 {}개".format(
            len(states), sum(1 for _, a in states if a)))

    def _tweak_do(self, tw, applied):
        if tw["admin"] and not is_admin():
            messagebox.showwarning(
                "권한 필요",
                "'{}' 은(는) 관리자 권한이 필요합니다.\n"
                "우측 상단 '관리자로 실행'으로 다시 열어주세요.".format(tw["label"]),
            )
            return
        self.set_status(("되돌리는 중: " if applied else "적용 중: ") + tw["label"])

        def work():
            try:
                ok = tw["revert"]() if applied else tw["apply"]()
            except Exception:
                ok = False
            msg = tw["label"] + (" 복원됨" if applied else " 적용됨") if ok \
                else tw["label"] + " 실패"
            self.ui(self.set_status, msg)
            self.ui(self.refresh_tweaks)
            self.ui(self.refresh_power)

        threading.Thread(target=work, daemon=True).start()

    def apply_all_tweaks(self):
        admin = is_admin()
        targets = [t for t in TWEAKS if admin or not t["admin"]]
        if not messagebox.askyesno(
            "추천 트윅 모두 적용",
            "{}개 트윅을 한 번에 적용합니다.\n(전부 백업되어 되돌릴 수 있어요)\n계속할까요?".format(
                len(targets)),
        ):
            return
        self.set_status("트윅 일괄 적용 중...")

        def work():
            done = 0
            for tw in targets:
                try:
                    if not tw["check"]():
                        if tw["apply"]():
                            done += 1
                except Exception:
                    pass
            self.ui(self.set_status, "트윅 {}개 적용 완료".format(done))
            self.ui(self.refresh_tweaks)
            self.ui(self.refresh_power)

        threading.Thread(target=work, daemon=True).start()

    def revert_all_tweaks(self):
        if not messagebox.askyesno(
            "모두 되돌리기", "적용된 모든 트윅을 원래대로 되돌릴까요?"
        ):
            return
        self.set_status("트윅 일괄 복원 중...")

        def work():
            done = 0
            for tw in TWEAKS:
                try:
                    if tw["check"]():
                        if tw["admin"] and not is_admin():
                            continue
                        if tw["revert"]():
                            done += 1
                except Exception:
                    pass
            self.ui(self.set_status, "트윅 {}개 복원 완료".format(done))
            self.ui(self.refresh_tweaks)
            self.ui(self.refresh_power)

        threading.Thread(target=work, daemon=True).start()

    # ========================================================================
    # 탭: 🩺 복구
    # ========================================================================
    def _build_repair_tab(self):
        f = self.tab_repair
        self.repair_busy = False
        self.repair_proc = None
        top = tk.Frame(f, bg=CARD)
        top.pack(fill="x", padx=16, pady=(14, 2))
        tk.Label(
            top, text="🩺 시스템 복구 도구", bg=CARD, fg=FG,
            font=("Malgun Gothic", 13, "bold"),
        ).pack(side="left")
        tk.Label(
            f,
            text="윈도우가 이상할 때 사용하세요. 손상된 시스템 파일을 검사·복구합니다. (전부 관리자 필요)",
            bg=CARD, fg=SUB, font=("Malgun Gothic", 9),
        ).pack(anchor="w", padx=18)
        tk.Label(
            f,
            text="※ 검사는 몇 분 걸리고, 진행 중 화면이 잠깐 멈춘 듯 보여도 정상입니다.",
            bg=CARD, fg=WARN, font=("Malgun Gothic", 9),
        ).pack(anchor="w", padx=18, pady=(2, 0))

        btns = tk.Frame(f, bg=CARD)
        btns.pack(fill="x", padx=16, pady=8)
        self.btn(
            btns, "🔎 시스템 파일 검사 (SFC)",
            lambda: self.run_repair("SFC 검사", ["sfc", "/scannow"]), "green",
        ).pack(side="left", padx=(0, 6))
        self.btn(
            btns, "🛠 윈도우 이미지 복구 (DISM)",
            lambda: self.run_repair(
                "DISM 복구",
                ["DISM", "/Online", "/Cleanup-Image", "/RestoreHealth"]),
            "ghost",
        ).pack(side="left", padx=6)
        self.btn(
            btns, "💽 디스크 검사",
            lambda: self.run_repair("디스크 검사", ["chkdsk", "C:", "/scan"]),
            "ghost",
        ).pack(side="left", padx=6)
        self.btn(
            btns, "🌐 네트워크 초기화",
            lambda: self.run_repair(
                "Winsock 초기화", ["netsh", "winsock", "reset"]),
            "ghost",
        ).pack(side="left", padx=6)

        outwrap = tk.Frame(f, bg=CARD)
        outwrap.pack(fill="both", expand=True, padx=16, pady=(4, 14))
        self.repair_out = tk.Text(
            outwrap, bg="#141420", fg="#9fe8c5", insertbackground=FG,
            relief="flat", font=("Consolas", 9), state="disabled", wrap="word",
        )
        rsb = ttk.Scrollbar(outwrap, orient="vertical",
                            command=self.repair_out.yview)
        self.repair_out.configure(yscrollcommand=rsb.set)
        self.repair_out.pack(side="left", fill="both", expand=True)
        rsb.pack(side="right", fill="y")
        self._repair_append("결과가 여기에 실시간으로 표시됩니다.\n")

    def _repair_append(self, text):
        self.repair_out.configure(state="normal")
        self.repair_out.insert("end", text)
        self.repair_out.see("end")
        self.repair_out.configure(state="disabled")

    def run_repair(self, label, cmd):
        if self.repair_busy:
            messagebox.showinfo("안내", "이미 다른 작업이 실행 중입니다.")
            return
        if not is_admin():
            messagebox.showwarning(
                "권한 필요",
                "복구 도구는 관리자 권한이 필요합니다.\n"
                "우측 상단 '관리자로 실행'으로 다시 열어주세요.",
            )
            return
        if "Winsock" in label and not messagebox.askyesno(
            "확인", "네트워크 설정을 초기화합니다.\n완전 적용에는 재부팅이 필요해요. 계속할까요?"
        ):
            return
        self.repair_busy = True
        self._repair_append("\n===== {} 시작 =====\n".format(label))
        self.set_status(label + " 실행 중... (몇 분 걸릴 수 있어요)")

        def work():
            try:
                self.repair_proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=CREATE_NO_WINDOW,
                )
                for raw in iter(self.repair_proc.stdout.readline, b""):
                    if b"\x00" in raw:
                        raw = raw.replace(b"\x00", b"")
                    line = raw.decode("mbcs", "replace")
                    if line.strip():
                        self.ui(self._repair_append, line)
                self.repair_proc.wait()
                rc = self.repair_proc.returncode
            except Exception as e:
                rc = -1
                self.ui(self._repair_append, "오류: {}\n".format(e))
            self.repair_proc = None
            self.ui(self._repair_append,
                       "===== {} 종료 (코드 {}) =====\n".format(label, rc))
            self.ui(self.set_status, label + " 완료")
            self.repair_busy = False

        threading.Thread(target=work, daemon=True).start()

    # ========================================================================
    # 탭 3: 디스크 분석 (큰 파일)
    # ========================================================================
    def _build_disk_tab(self):
        f = self.tab_disk
        top = tk.Frame(f, bg=CARD)
        top.pack(fill="x", padx=16, pady=(14, 6))
        tk.Label(
            top, text="폴더:", bg=CARD, fg=FG, font=("Malgun Gothic", 10, "bold")
        ).pack(side="left")
        self.disk_path = tk.StringVar(value=os.environ.get("USERPROFILE", "C:\\"))
        tk.Entry(
            top, textvariable=self.disk_path, bg="#232336", fg=FG,
            insertbackground=FG, relief="flat", font=("Malgun Gothic", 10),
        ).pack(side="left", fill="x", expand=True, padx=8, ipady=4)
        self.btn(top, "폴더 선택", self.pick_disk_folder, "ghost").pack(side="left")
        self.btn(top, "🔍 분석", self.scan_disk, "green").pack(side="left", padx=6)

        mode = tk.Frame(f, bg=CARD)
        mode.pack(fill="x", padx=16, pady=(0, 2))
        self.disk_mode = tk.StringVar(value="files")
        for txt, val in (("📄 큰 파일 보기", "files"), ("📁 큰 폴더 보기", "folders")):
            rb = tk.Radiobutton(
                mode, text=txt, value=val, variable=self.disk_mode,
                command=self._render_disk, bg=CARD, fg=FG, selectcolor=CARD,
                activebackground=CARD, activeforeground=FG,
                font=("Malgun Gothic", 9, "bold"), indicatoron=True,
                bd=0, highlightthickness=0,
            )
            rb.pack(side="left", padx=(0, 10))
        self._disk_files = []
        self._disk_folders = []

        tvf = tk.Frame(f, bg=CARD)
        tvf.pack(fill="both", expand=True, padx=16, pady=6)
        self.disk_tv = ttk.Treeview(
            tvf, columns=("size", "path"), show="headings", selectmode="extended"
        )
        self.disk_tv.heading("size", text="크기")
        self.disk_tv.heading("path", text="경로 (큰 순 상위 300개)")
        self.disk_tv.column("size", width=110, anchor="e")
        self.disk_tv.column("path", width=640, anchor="w")
        sb = ttk.Scrollbar(tvf, orient="vertical", command=self.disk_tv.yview)
        self.disk_tv.configure(yscrollcommand=sb.set)
        self.disk_tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.disk_tv.bind("<Double-1>", lambda e: self.open_in_explorer(self.disk_tv))

        bottom = tk.Frame(f, bg=CARD)
        bottom.pack(fill="x", padx=16, pady=(6, 14))
        self.disk_prog = ttk.Progressbar(bottom, mode="indeterminate", length=160)
        self.disk_prog.pack(side="left")
        self.btn(
            bottom, "📂 탐색기에서 열기",
            lambda: self.open_in_explorer(self.disk_tv), "ghost",
        ).pack(side="left", padx=8)
        self.btn(
            bottom, "🗑 휴지통으로 삭제",
            lambda: self.recycle_selected(self.disk_tv), "danger",
        ).pack(side="right")

    def pick_disk_folder(self):
        d = filedialog.askdirectory(initialdir=self.disk_path.get())
        if d:
            self.disk_path.set(d)

    def scan_disk(self):
        base = self.disk_path.get()
        if not os.path.isdir(base):
            messagebox.showwarning("안내", "올바른 폴더를 선택하세요.")
            return
        self.disk_tv.delete(*self.disk_tv.get_children())
        self.disk_prog.start(12)
        self.set_status("디스크 분석 중... (파일이 많으면 시간이 걸립니다)")
        threading.Thread(
            target=self._scan_disk_worker, args=(base,), daemon=True
        ).start()

    def _scan_disk_worker(self, base):
        files = []
        dirsize = {}
        basenc = os.path.normcase(os.path.abspath(base))
        for root, dirs, names in os.walk(base, topdown=True):
            # 정션/링크 폴더는 타고 들어가지 않음 (순환·중복 계산 방지)
            dirs[:] = [d for d in dirs
                       if not is_reparse_point(os.path.join(root, d))]
            for name in names:
                fp = os.path.join(root, name)
                try:
                    sz = os.path.getsize(fp)
                except Exception:
                    continue
                files.append((sz, fp))
                # 상위 폴더들(base까지)에 누적
                d = root
                while True:
                    dirsize[d] = dirsize.get(d, 0) + sz
                    if os.path.normcase(os.path.abspath(d)) == basenc:
                        break
                    parent = os.path.dirname(d)
                    if parent == d:
                        break
                    d = parent
        files.sort(reverse=True)
        folders = sorted(
            ((sz, p) for p, sz in dirsize.items()
             if os.path.normcase(os.path.abspath(p)) != basenc),
            reverse=True)
        self.ui(self._store_disk, files[:300], folders[:300])

    def _store_disk(self, files, folders):
        self.disk_prog.stop()
        self._disk_files = files
        self._disk_folders = folders
        self._render_disk()

    def _render_disk(self):
        self.disk_tv.delete(*self.disk_tv.get_children())
        data = self._disk_files if self.disk_mode.get() == "files" else self._disk_folders
        for size, fp in data:
            self.disk_tv.insert("", "end", values=(human(size), fp))
        kind = "파일" if self.disk_mode.get() == "files" else "폴더"
        self.set_status("분석 완료 · 큰 {} 상위 {}개".format(kind, len(data)))

    # ========================================================================
    # 탭 4: 중복 파일
    # ========================================================================
    def _build_dup_tab(self):
        f = self.tab_dup
        top = tk.Frame(f, bg=CARD)
        top.pack(fill="x", padx=16, pady=(14, 6))
        tk.Label(
            top, text="폴더:", bg=CARD, fg=FG, font=("Malgun Gothic", 10, "bold")
        ).pack(side="left")
        self.dup_path = tk.StringVar(
            value=os.path.join(os.environ.get("USERPROFILE", "C:\\"), "Downloads")
        )
        tk.Entry(
            top, textvariable=self.dup_path, bg="#232336", fg=FG,
            insertbackground=FG, relief="flat", font=("Malgun Gothic", 10),
        ).pack(side="left", fill="x", expand=True, padx=8, ipady=4)
        self.btn(top, "폴더 선택", self.pick_dup_folder, "ghost").pack(side="left")
        self.btn(top, "🔍 중복 찾기", self.scan_dup, "green").pack(side="left", padx=6)

        tvf = tk.Frame(f, bg=CARD)
        tvf.pack(fill="both", expand=True, padx=16, pady=6)
        self.dup_tv = ttk.Treeview(
            tvf, columns=("size",), show="tree headings", selectmode="extended"
        )
        self.dup_tv.heading("#0", text="중복 그룹 / 파일")
        self.dup_tv.heading("size", text="크기")
        self.dup_tv.column("#0", width=660)
        self.dup_tv.column("size", width=110, anchor="e")
        sb = ttk.Scrollbar(tvf, orient="vertical", command=self.dup_tv.yview)
        self.dup_tv.configure(yscrollcommand=sb.set)
        self.dup_tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.dup_tv.bind("<Double-1>", lambda e: self.open_in_explorer(self.dup_tv))

        bottom = tk.Frame(f, bg=CARD)
        bottom.pack(fill="x", padx=16, pady=(6, 14))
        self.dup_prog = ttk.Progressbar(bottom, mode="indeterminate", length=160)
        self.dup_prog.pack(side="left")
        self.dup_info = tk.Label(
            bottom, text="", bg=CARD, fg=SUB, font=("Malgun Gothic", 9)
        )
        self.dup_info.pack(side="left", padx=10)
        self.btn(
            bottom, "🗑 선택 휴지통으로 삭제",
            lambda: self.recycle_selected(self.dup_tv), "danger",
        ).pack(side="right")

    def pick_dup_folder(self):
        d = filedialog.askdirectory(initialdir=self.dup_path.get())
        if d:
            self.dup_path.set(d)

    def scan_dup(self):
        base = self.dup_path.get()
        if not os.path.isdir(base):
            messagebox.showwarning("안내", "올바른 폴더를 선택하세요.")
            return
        self.dup_tv.delete(*self.dup_tv.get_children())
        self.dup_prog.start(12)
        self.set_status("중복 파일 검사 중...")
        threading.Thread(
            target=self._scan_dup_worker, args=(base,), daemon=True
        ).start()

    def _hash_file(self, path, partial=False):
        h = hashlib.md5()
        try:
            with open(path, "rb") as fh:
                if partial:
                    h.update(fh.read(65536))
                else:
                    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                        h.update(chunk)
        except Exception:
            return None
        return h.hexdigest()

    def _scan_dup_worker(self, base):
        # 1) 같은 크기끼리 묶기
        by_size = {}
        for root, dirs, names in os.walk(base, topdown=True):
            dirs[:] = [d for d in dirs
                       if not is_reparse_point(os.path.join(root, d))]
            for name in names:
                fp = os.path.join(root, name)
                try:
                    sz = os.path.getsize(fp)
                except Exception:
                    continue
                if sz == 0:
                    continue
                by_size.setdefault(sz, []).append(fp)
        # 2) 크기 같은 것만 해시 비교
        groups = []
        wasted = 0
        for sz, paths in by_size.items():
            if len(paths) < 2:
                continue
            by_hash = {}
            for p in paths:
                hp = self._hash_file(p, partial=True)
                if hp is None:
                    continue
                by_hash.setdefault(hp, []).append(p)
            for hp, plist in by_hash.items():
                if len(plist) < 2:
                    continue
                # 부분 해시 같으면 전체 해시로 확정
                full = {}
                for p in plist:
                    fh = self._hash_file(p, partial=False)
                    if fh is None:
                        continue
                    full.setdefault(fh, []).append(p)
                for fh, group in full.items():
                    if len(group) >= 2:
                        groups.append((sz, group))
                        wasted += sz * (len(group) - 1)
        groups.sort(key=lambda g: g[0] * len(g[1]), reverse=True)
        self.ui(self._fill_dup, groups, wasted)

    def _fill_dup(self, groups, wasted):
        self.dup_prog.stop()
        for idx, (sz, group) in enumerate(groups, 1):
            parent = self.dup_tv.insert(
                "", "end",
                text="  그룹 {} · {}개 · 각 {}".format(idx, len(group), human(sz)),
                values=(human(sz * (len(group) - 1)) + " 낭비",),
                open=False,
            )
            for p in group:
                self.dup_tv.insert(parent, "end", text="    " + p, values=(human(sz),))
        self.dup_info.configure(
            text="중복 그룹 {}개 · 낭비 {} 정리 가능".format(len(groups), human(wasted))
        )
        self.set_status("중복 검사 완료")

    # ========================================================================
    # 공통: 탐색기 열기 / 휴지통 삭제
    # ========================================================================
    def _selected_paths(self, tv):
        paths = []
        for i in tv.selection():
            vals = tv.item(i, "values")
            txt = tv.item(i, "text").strip()
            # 디스크 탭: 경로가 values[1]
            if tv is self.disk_tv and len(vals) >= 2:
                paths.append(vals[1])
            else:
                # 중복 탭: 파일은 text가 경로, 그룹 헤더는 자식들
                if os.path.exists(txt):
                    paths.append(txt)
                else:
                    for c in tv.get_children(i):
                        ct = tv.item(c, "text").strip()
                        if os.path.exists(ct):
                            paths.append(ct)
        return list(dict.fromkeys(paths))

    def open_in_explorer(self, tv):
        paths = self._selected_paths(tv)
        if not paths:
            return
        p = paths[0]
        if os.path.exists(p):
            run_hidden(["explorer", "/select,", os.path.normpath(p)])

    def recycle_selected(self, tv):
        paths = [p for p in self._selected_paths(tv) if os.path.exists(p)]
        if not paths:
            messagebox.showinfo("안내", "삭제할 파일을 선택하세요.")
            return
        if not messagebox.askyesno(
            "확인",
            "{}개 파일을 휴지통으로 보냅니다.\n(휴지통에서 복구 가능)\n계속할까요?".format(len(paths)),
        ):
            return
        if recycle_delete(paths):
            for i in list(tv.selection()):
                try:
                    tv.delete(i)
                except Exception:
                    pass
            self.set_status("{}개 파일을 휴지통으로 이동".format(len(paths)))
        else:
            messagebox.showwarning(
                "안내", "일부 파일을 삭제하지 못했습니다 (사용 중이거나 권한 부족)."
            )


def main():
    if "--silent-clean" in sys.argv:
        silent_clean()
        return
    if not acquire_single_instance():
        # 이미 실행 중 → 기존 창을 띄우라고 신호했으니 조용히 종료
        return
    try:
        app = App()
        app.mainloop()
    except Exception as e:
        import traceback

        traceback.print_exc()
        try:
            ctypes.windll.user32.MessageBoxW(
                None, "오류가 발생했습니다:\n{}".format(e), "OptiBoost", 0x10
            )
        except Exception:
            pass


if __name__ == "__main__":
    main()
