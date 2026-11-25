"""Set Windows desktop wallpapers from MOSS Image Studio, one per monitor or spanned across all of them.

Uses the Windows IDesktopWallpaper COM interface directly through ctypes (no extra packages).
Monitor rectangles are read in real pixels (per-monitor DPI awareness for the call).
"""
import ctypes
from ctypes import wintypes

CLSID_DesktopWallpaper = "{C2CF3110-460E-4fc1-B9D0-8A1C0C9CC4BD}"
IID_IDesktopWallpaper = "{B92B56A9-8B55-4E14-9A89-0199BBB6F93B}"
POSITIONS = dict(center=0, tile=1, stretch=2, fit=3, fill=4, span=5)

ole32 = ctypes.windll.ole32
HRESULT = ctypes.c_long


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


def _guid(text):
    g = GUID()
    if ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g)) != 0:
        raise OSError(f"bad GUID {text}")
    return g


class _Wallpaper:
    """Minimal IDesktopWallpaper wrapper (vtable calls by index)."""
    # vtable indexes after IUnknown's 3 methods
    SET_WALLPAPER, GET_WALLPAPER, GET_MONITOR_AT, GET_MONITOR_COUNT, GET_MONITOR_RECT = 3, 4, 5, 6, 7
    SET_POSITION, GET_POSITION = 10, 11

    def __enter__(self):
        ole32.CoInitialize(None)
        self.ptr = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(ctypes.byref(_guid(CLSID_DesktopWallpaper)), None, 1 | 4,   # inproc | local
                                    ctypes.byref(_guid(IID_IDesktopWallpaper)), ctypes.byref(self.ptr))
        if hr != 0 or not self.ptr:
            raise OSError(f"Windows' wallpaper service isn't available (0x{hr & 0xFFFFFFFF:08X})")
        self.vtbl = ctypes.cast(ctypes.cast(self.ptr, ctypes.POINTER(ctypes.c_void_p))[0],
                                ctypes.POINTER(ctypes.c_void_p))
        return self

    def __exit__(self, *exc):
        self._fn(2, wintypes.ULONG)(self.ptr)        # Release
        ole32.CoUninitialize()

    def _fn(self, index, restype, *argtypes):
        return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(self.vtbl[index])

    def _check(self, hr, what):
        if hr != 0:
            raise OSError(f"Windows couldn't {what} (0x{hr & 0xFFFFFFFF:08X})")

    def monitors(self):
        """[(monitor id, (x, y, w, h))] in real pixels."""
        count = wintypes.UINT()
        self._check(self._fn(self.GET_MONITOR_COUNT, HRESULT, ctypes.POINTER(wintypes.UINT))(
            self.ptr, ctypes.byref(count)), "count the monitors")
        out = []
        for i in range(count.value):
            mid = wintypes.LPWSTR()
            self._check(self._fn(self.GET_MONITOR_AT, HRESULT, wintypes.UINT, ctypes.POINTER(wintypes.LPWSTR))(
                self.ptr, i, ctypes.byref(mid)), "list the monitors")
            monitor_id = mid.value
            ole32.CoTaskMemFree(mid)
            rect = wintypes.RECT()
            hr = self._fn(self.GET_MONITOR_RECT, HRESULT, wintypes.LPCWSTR, ctypes.POINTER(wintypes.RECT))(
                self.ptr, monitor_id, ctypes.byref(rect))
            if hr == 0:                               # inactive monitors have no rectangle
                out.append((monitor_id, (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)))
        return out

    def get(self, monitor_id):
        path = wintypes.LPWSTR()
        self._check(self._fn(self.GET_WALLPAPER, HRESULT, wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPWSTR))(
            self.ptr, monitor_id, ctypes.byref(path)), "read the wallpaper")
        value = path.value or ""
        ole32.CoTaskMemFree(path)
        return value

    def set(self, monitor_id, path):
        self._check(self._fn(self.SET_WALLPAPER, HRESULT, wintypes.LPCWSTR, wintypes.LPCWSTR)(
            self.ptr, monitor_id, str(path)), "set the wallpaper")

    def get_position(self):
        pos = ctypes.c_int()
        self._check(self._fn(self.GET_POSITION, HRESULT, ctypes.POINTER(ctypes.c_int))(
            self.ptr, ctypes.byref(pos)), "read the wallpaper position")
        return pos.value

    def set_position(self, position):
        self._check(self._fn(self.SET_POSITION, HRESULT, ctypes.c_int)(self.ptr, position),
                    "set the wallpaper position")


def _per_monitor_pixels():
    try:
        return ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        return None


def _restore_dpi(prev):
    if prev:
        try:
            ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(prev))
        except Exception:
            pass


def current():
    """What's set now: {'position': int, 'monitors': [{'id', 'rect', 'path'}]} (for undo)."""
    prev = _per_monitor_pixels()
    try:
        with _Wallpaper() as wp:
            return dict(position=wp.get_position(),
                        monitors=[dict(id=mid, rect=list(rect), path=wp.get(mid)) for mid, rect in wp.monitors()])
    finally:
        _restore_dpi(prev)


def restore(saved):
    """Put back a state saved by current()."""
    prev = _per_monitor_pixels()
    try:
        with _Wallpaper() as wp:
            wp.set_position(saved["position"])
            ids = {mid for mid, _ in wp.monitors()}
            if saved["position"] == POSITIONS["span"]:
                path = next((m["path"] for m in saved["monitors"] if m["path"]), "")
                if path:
                    wp.set(None, path)
            else:
                for m in saved["monitors"]:
                    if m["id"] in ids and m["path"]:
                        wp.set(m["id"], m["path"])
    finally:
        _restore_dpi(prev)


def set_span(path):
    """One picture across all monitors."""
    prev = _per_monitor_pixels()
    try:
        with _Wallpaper() as wp:
            wp.set_position(POSITIONS["span"])
            wp.set(None, path)
    finally:
        _restore_dpi(prev)


def set_all(path, position="fill"):
    """The same picture on every monitor."""
    prev = _per_monitor_pixels()
    try:
        with _Wallpaper() as wp:
            wp.set_position(POSITIONS[position])
            for mid, _ in wp.monitors():
                wp.set(mid, path)
    finally:
        _restore_dpi(prev)


def set_per_monitor(pictures, position="fill"):
    """pictures: {(x, y, w, h) monitor rectangle: path}. Monitors not listed keep their wallpaper.
    Returns how many monitors were set."""
    prev = _per_monitor_pixels()
    try:
        with _Wallpaper() as wp:
            wp.set_position(POSITIONS[position])
            done = 0
            for mid, rect in wp.monitors():
                path = pictures.get(tuple(rect))
                if path:
                    wp.set(mid, path)
                    done += 1
            return done
    finally:
        _restore_dpi(prev)
