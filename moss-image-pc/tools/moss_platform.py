"""What differs between Windows and macOS (Apple Silicon) for MOSS Image Studio.

Everything else in the app is plain Python and Tk. Windows code here is what the app has always used;
the macOS side uses standard tools (open, sysctl, vm_stat, defaults, launchd) and, where available,
pyobjc (AppKit, for screens and wallpapers) and keyring (the macOS Keychain).
"""
import ctypes
import os
import re
import subprocess
import sys
from pathlib import Path

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
EXE = ".exe" if IS_WIN else ""
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)       # 0 (nothing) on macOS
UI_FONT = "Segoe UI" if IS_WIN else "Helvetica Neue"
MONO_FONT = "Consolas" if IS_WIN else "Menlo"
OS_NAME = "Windows" if IS_WIN else "macOS"
# On Apple Silicon the graphics chip shares the computer's memory, so moving model parts "to the CPU"
# saves nothing and only copies them; the engine is told to keep everything in place.
OFFLOAD_ARGS = ["--offload-to-cpu"] if IS_WIN else []


def exe(name):
    """A program's file name on this system: sd-cli.exe on Windows, sd-cli on macOS."""
    return name + EXE


def open_path(path):
    if path and Path(path).exists():
        if IS_WIN:
            os.startfile(str(path))
        else:
            subprocess.Popen(["open", str(path)])


def show_in_folder(path):
    if not path:
        return
    if IS_WIN:
        subprocess.Popen(["explorer", "/select,", str(path)])
    else:
        subprocess.Popen(["open", "-R", str(path)])


def make_dpi_aware():
    if IS_WIN:
        try:  # sharp text on high-DPI screens
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def right_click_events():
    """Tk events for a right click: Button-3 on Windows; Button-2 (and Control-click) on macOS."""
    return ["<Button-3>"] if IS_WIN else ["<Button-2>", "<Control-Button-1>"]


def wheel_units(event):
    """Scroll steps for a mouse-wheel event (Windows sends multiples of 120; macOS sends small numbers)."""
    return -event.delta // 120 if IS_WIN else -event.delta


# ---------------- screens ----------------

def detect_screens():
    """Connected screens in real pixels: list of dicts with x, y, w, h, primary."""
    return _screens_win() if IS_WIN else _screens_mac()


def _screens_win():
    from ctypes import wintypes
    user = ctypes.windll.user32
    prev = None
    try:   # ask for per-monitor pixel sizes, even though the window itself is only system-DPI aware
        prev = user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]

    found = []
    proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                   ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def callback(hmon, _hdc, _rect, _data):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if user.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            found.append(dict(x=r.left, y=r.top, w=r.right - r.left, h=r.bottom - r.top,
                              primary=bool(info.dwFlags & 1)))
        return True

    try:
        user.EnumDisplayMonitors(None, None, proc_type(callback), 0)
    finally:
        if prev:
            user.SetThreadDpiAwarenessContext(ctypes.c_void_p(prev))
    return sorted(found, key=lambda s: (s["x"], s["y"]))


def mac_screens():
    """[(NSScreen, (x, y, w, h))]: sizes in real pixels, positions laid out left to right / top down, sorted
    like detect_screens(). macOS measures screens in points from the bottom left; pictures need pixels from
    the top left. Wallpapers use the same rectangles to find each screen."""
    from AppKit import NSScreen
    screens = list(NSScreen.screens())
    if not screens:
        return []
    main_h = screens[0].frame().size.height                  # the first screen holds the menu bar
    found = []
    for i, s in enumerate(screens):
        f, scale = s.frame(), s.backingScaleFactor()
        top = main_h - (f.origin.y + f.size.height)          # points, from the top of the main screen
        found.append(dict(ns=s, primary=i == 0, x=round(f.origin.x * scale), y=round(top * scale),
                          w=round(f.size.width * scale), h=round(f.size.height * scale)))
    found = close_gaps(sorted(found, key=lambda d: (d["x"], d["y"])))
    return [(d["ns"], (d["x"], d["y"], d["w"], d["h"]), d["primary"]) for d in found]


def _screens_mac():
    try:
        return [dict(x=r[0], y=r[1], w=r[2], h=r[3], primary=primary) for _ns, r, primary in mac_screens()]
    except Exception:
        return close_gaps(sorted(_screens_mac_fallback(), key=lambda d: (d["x"], d["y"])))


def close_gaps(found):
    """Screens side by side with different pixel densities don't line up exactly in pixels; snap neighbours
    that are meant to touch (same top edge, small gap or overlap) so "all screens" pictures fit them."""
    for a, b in zip(found, found[1:]):
        if abs(a["y"] - b["y"]) <= 4 and abs(a["x"] + a["w"] - b["x"]) < min(a["w"], b["w"]) // 2:
            b["x"] = a["x"] + a["w"]
            if a["h"] == b["h"]:
                b["y"] = a["y"]
    return found


def _screens_mac_fallback():
    """Without pyobjc: the main screen's resolution from system_profiler."""
    try:
        import json
        out = subprocess.run(["system_profiler", "SPDisplaysDataType", "-json"], capture_output=True,
                             timeout=20).stdout
        found, x = [], 0
        for gpu in json.loads(out).get("SPDisplaysDataType", []):
            for d in gpu.get("spdisplays_ndrvs", []):
                m = re.search(r"(\d+)\s*x\s*(\d+)", d.get("_spdisplays_pixels", "") or d.get("_spdisplays_resolution", ""))
                if m:
                    w, h = int(m.group(1)), int(m.group(2))
                    found.append(dict(x=x, y=0, w=w, h=h, primary=d.get("spdisplays_main") == "spdisplays_yes"))
                    x += w
        return found
    except Exception:
        return []


# ---------------- memory ----------------

def memory_free_gb():
    """(free RAM, what programs can still get) in GB. On Windows the second number is free commit memory.
    On macOS memory is shared with the graphics chip: it's free + reclaimable memory, capped by how much the
    graphics chip may use in total."""
    return _memory_win() if IS_WIN else _memory_mac()


def _memory_win():
    from ctypes import wintypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    m = MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(m)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
        return None, None
    return m.ullAvailPhys / 2**30, m.ullAvailPageFile / 2**30


def _sysctl(name):
    try:
        return int(subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=5).stdout.strip())
    except Exception:
        return None


def mac_total_gb():
    total = _sysctl("hw.memsize")
    return total / 2**30 if total else None


def mac_gpu_limit_gb():
    """How much memory the graphics chip may use. macOS allows about two-thirds of the memory on Macs with
    up to 36 GB and three-quarters above that, unless iogpu.wired_limit_mb is set."""
    total = mac_total_gb()
    if not total:
        return None
    wired = _sysctl("iogpu.wired_limit_mb")
    if wired:
        return wired / 1024
    return total * (0.67 if total <= 36 else 0.75)


def parse_vm_stat(text):
    """Free + reclaimable memory in GB from vm_stat's output."""
    page = int(re.search(r"page size of (\d+) bytes", text).group(1))
    pages = {}
    for line in text.splitlines():
        m = re.match(r'"?([^:"]+)"?:\s+(\d+)\.', line.strip())
        if m:
            pages[m.group(1).strip().lower()] = int(m.group(2))
    free = sum(pages.get(k, 0) for k in ("pages free", "pages inactive", "pages speculative", "pages purgeable"))
    return free * page / 2**30


def _memory_mac():
    try:
        text = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
        free = parse_vm_stat(text)
    except Exception:
        return None, None
    limit = mac_gpu_limit_gb()
    return free, (min(free, limit) if limit else free)


def memory_tips():
    """What helps when a picture runs out of memory, for memory_advice."""
    if IS_WIN:
        return ("  - Restart Windows if it has been running for days: some Windows services slowly hold on "
                "to more and more memory, and a restart frees it.\n")
    return ("  - Quit other apps, especially browsers with many tabs: on a Mac the graphics chip shares the "
            "computer's memory.\n")


# ---------------- appearance ----------------

def uses_dark():
    if IS_WIN:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
                return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
        except OSError:
            return False
    try:
        out = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True,
                             timeout=5).stdout
        return out.strip().lower() == "dark"
    except Exception:
        return False


# ---------------- the Gemini API key ----------------
# Windows: encrypted for this Windows account with DPAPI and kept in the settings file.
# macOS: kept in the login Keychain; the settings file only records that it's there.

KEYCHAIN_SERVICE = "MOSS Image Studio"
KEYCHAIN_ACCOUNT = "gemini-api-key"
KEYCHAIN_MARK = "keychain"


def _dpapi(data, protect):
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    src = ctypes.create_string_buffer(data, len(data))
    blob_in = _Blob(len(data), ctypes.cast(src, ctypes.POINTER(ctypes.c_char)))
    blob_out = _Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("Windows could not " + ("encrypt" if protect else "decrypt") + " the API key")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def store_key(key):
    """Keep the API key safe; returns what to save in the settings file."""
    import base64
    if IS_WIN:
        return base64.b64encode(_dpapi(key.encode("utf-8"), True)).decode("ascii")
    import keyring
    keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT, key)
    return KEYCHAIN_MARK


def load_key(stored):
    import base64
    if stored == KEYCHAIN_MARK:
        import keyring
        return keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
    if IS_WIN:
        return _dpapi(base64.b64decode(stored), False).decode("utf-8")
    return None


def forget_key(stored):
    if stored == KEYCHAIN_MARK:
        try:
            import keyring
            keyring.delete_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
        except Exception:
            pass


# ---------------- daily task (wallpaper of the day) ----------------

TASK_NAME = "MOSS Image Studio - wallpaper of the day"
LAUNCHD_LABEL = "com.moss-image-studio.wallpaper-of-the-day"


def launch_agent_path():
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def launchd_plist(args, at, log):
    """The launchd job that runs `args` every day at `at` (HH:MM)."""
    import plistlib
    hour, minute = (int(v) for v in at.split(":"))
    return plistlib.dumps(dict(Label=LAUNCHD_LABEL, ProgramArguments=[str(a) for a in args],
                               StartCalendarInterval=dict(Hour=hour, Minute=minute),
                               StandardOutPath=str(log), StandardErrorPath=str(log)))


def schedule_daily(enabled, at, args):
    """Create or remove the daily job (only when the user turns it on or off).
    Windows: Task Scheduler. macOS: a launchd agent in ~/Library/LaunchAgents."""
    if IS_WIN:
        command = " ".join(str(a) if str(a).startswith("--") else f'"{a}"' for a in args)
        if enabled:
            run = subprocess.run(["schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", at, "/TN", TASK_NAME,
                                  "/TR", command], capture_output=True, text=True, creationflags=NO_WINDOW)
        else:
            run = subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME], capture_output=True, text=True,
                                 creationflags=NO_WINDOW)
            err = (run.stderr or "").lower()
            if "cannot find" in err or "does not exist" in err:
                return
        if run.returncode != 0:
            raise RuntimeError((run.stderr or run.stdout or "schtasks failed").strip())
        return
    plist = launch_agent_path()
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"], capture_output=True)
    if not enabled:
        plist.unlink(missing_ok=True)
        return
    plist.parent.mkdir(parents=True, exist_ok=True)
    log = Path.home() / "Library" / "Logs" / "MOSS Image Studio wallpaper of the day.log"
    plist.write_bytes(launchd_plist(args, at, log))
    run = subprocess.run(["launchctl", "bootstrap", domain, str(plist)], capture_output=True, text=True)
    if run.returncode != 0:
        raise RuntimeError((run.stderr or run.stdout or "launchctl failed").strip())


# ---------------- start at sign-in (for phone access) ----------------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "MOSS Image Studio"
LOGIN_LABEL = "com.moss-image-studio.login"


def login_plist(args):
    import plistlib
    return plistlib.dumps(dict(Label=LOGIN_LABEL, ProgramArguments=[str(a) for a in args], RunAtLoad=True,
                               ProcessType="Interactive"))


def start_at_login(enabled, args):
    """Start the app (minimized) when the user signs in, or stop doing so (only when the user asks).
    Windows: the user's Run key. macOS: a launch agent that runs at login."""
    if IS_WIN:
        import winreg
        command = " ".join(str(a) if str(a).startswith("--") else f'"{a}"' for a in args)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(k, RUN_VALUE)
                except FileNotFoundError:
                    pass
        return
    plist = Path.home() / "Library" / "LaunchAgents" / f"{LOGIN_LABEL}.plist"
    if enabled:
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(login_plist(args))       # launchd reads it at the next login
    else:
        plist.unlink(missing_ok=True)


def starts_at_login():
    if IS_WIN:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.QueryValueEx(k, RUN_VALUE)
            return True
        except OSError:
            return False
    return (Path.home() / "Library" / "LaunchAgents" / f"{LOGIN_LABEL}.plist").exists()


# ---------------- keep the computer awake while a picture is made ----------------
_awake = {"on": False, "proc": None}


def keep_awake(on):
    """Stop the computer going to sleep mid-picture (pictures started from a phone run unattended).
    The screen may still turn off. Call from the window's thread."""
    if on == _awake["on"]:
        return
    _awake["on"] = on
    try:
        if IS_WIN:
            ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
        elif IS_MAC:
            if on:
                _awake["proc"] = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
            elif _awake["proc"]:
                _awake["proc"].terminate()
                _awake["proc"] = None
    except Exception:
        pass


# ---------------- Recycle Bin / Trash ----------------

def move_to_trash(path):
    """Move a file to the Recycle Bin (Windows) or the Trash (macOS), so it can be put back."""
    path = Path(path).resolve()
    if IS_WIN:
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND), ("wFunc", ctypes.c_uint), ("pFrom", wintypes.LPCWSTR),
                        ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                        ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]
        FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
        op = SHFILEOPSTRUCTW(None, FO_DELETE, str(path) + "\0", None,
                             FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI, False, None, None)
        code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if code or op.fAnyOperationsAborted:
            raise OSError(f"Couldn't move {path.name} to the Recycle Bin (code {code}).")
        return
    from Foundation import NSURL, NSFileManager
    ok, _url, err = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
        NSURL.fileURLWithPath_(str(path)), None, None)
    if not ok:
        raise OSError(f"Couldn't move {path.name} to the Trash: {err}")


# ---------------- Tk colours and themes ----------------
LIGHT_THEMES = ["vista", "clam"] if IS_WIN else ["aqua", "clam"]
WINDOW_BG = "SystemButtonFace" if IS_WIN else "systemWindowBackgroundColor"
SELECT_BG = "SystemHighlight" if IS_WIN else "systemSelectedTextBackgroundColor"
SELECT_FG = "SystemHighlightText" if IS_WIN else "systemSelectedTextColor"
MATCH_SYSTEM = f"Match {OS_NAME}"
