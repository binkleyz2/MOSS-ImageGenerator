"""Set Mac desktop pictures from MOSS Image Studio: one per screen, the same on all, or one picture spanned
across all screens (macOS has no span mode, so the picture is cut into one piece per screen).

Same functions as moss_wallpaper (Windows). Uses AppKit through pyobjc. macOS sets the picture for the
current Space (desktop) of each screen.
"""
from pathlib import Path

from PIL import Image

import moss_platform as P

CACHE = Path.home() / "Library" / "Caches" / "MOSS Image Studio" / "wallpapers"


def _options(position):
    from AppKit import (NSImageScaleAxesIndependently, NSImageScaleNone, NSImageScaleProportionallyUpOrDown,
                        NSWorkspaceDesktopImageAllowClippingKey, NSWorkspaceDesktopImageScalingKey)
    scaling = {"fill": NSImageScaleProportionallyUpOrDown, "fit": NSImageScaleProportionallyUpOrDown,
               "stretch": NSImageScaleAxesIndependently, "center": NSImageScaleNone}.get(
        position, NSImageScaleProportionallyUpOrDown)
    return {NSWorkspaceDesktopImageScalingKey: scaling,
            NSWorkspaceDesktopImageAllowClippingKey: position != "fit"}


def _set(screen, path, position="fill"):
    from AppKit import NSWorkspace
    from Foundation import NSURL
    url = NSURL.fileURLWithPath_(str(Path(path).resolve()))
    ok, error = NSWorkspace.sharedWorkspace().setDesktopImageURL_forScreen_options_error_(
        url, screen, _options(position), None)
    if not ok:
        raise OSError(f"macOS couldn't set the desktop picture: {error}")


def _screen_for(rect, screens):
    """The screen whose rectangle matches (exactly, or failing that the one it overlaps most)."""
    for ns, r, _primary in screens:
        if tuple(r) == tuple(rect):
            return ns
    x, y, w, h = rect

    def overlap(r):
        dx = min(x + w, r[0] + r[2]) - max(x, r[0])
        dy = min(y + h, r[1] + r[3]) - max(y, r[1])
        return max(dx, 0) * max(dy, 0)
    best = max(screens, key=lambda s: overlap(s[1]), default=None)
    return best[0] if best and overlap(best[1]) > 0 else None


def current():
    """What's set now, for undo: {'position': 'fill', 'monitors': [{'id', 'rect', 'path'}]}."""
    from AppKit import NSWorkspace
    ws = NSWorkspace.sharedWorkspace()
    out = []
    for i, (ns, rect, _primary) in enumerate(P.mac_screens()):
        url = ws.desktopImageURLForScreen_(ns)
        out.append(dict(id=i, rect=list(rect), path=str(url.path()) if url else ""))
    return dict(position="fill", monitors=out)


def restore(saved):
    screens = P.mac_screens()
    for m in saved.get("monitors", []):
        ns = _screen_for(m["rect"], screens)
        if ns is not None and m.get("path") and Path(m["path"]).exists():
            _set(ns, m["path"])


def set_all(path, position="fill"):
    for ns, _rect, _primary in P.mac_screens():
        _set(ns, path, position)


def set_per_monitor(pictures, position="fill"):
    """pictures: {(x, y, w, h) screen rectangle: path}. Screens not listed keep their picture.
    Returns how many screens were set."""
    screens = P.mac_screens()
    done = 0
    for rect, path in pictures.items():
        ns = _screen_for(rect, screens)
        if ns is not None:
            _set(ns, path, position)
            done += 1
    return done


def set_span(path):
    """One picture across all screens: cut it into a piece for each screen, laid out as the screens are."""
    screens = P.mac_screens()
    if not screens:
        raise OSError("No screens found.")
    x0 = min(r[0] for _n, r, _p in screens)
    y0 = min(r[1] for _n, r, _p in screens)
    W = max(r[0] + r[2] for _n, r, _p in screens) - x0
    H = max(r[1] + r[3] for _n, r, _p in screens) - y0
    CACHE.mkdir(parents=True, exist_ok=True)
    with Image.open(path) as im:
        full = im.convert("RGB").resize((W, H), Image.Resampling.LANCZOS)
    stem = Path(path).stem
    for i, (ns, (x, y, w, h), _primary) in enumerate(screens):
        piece = CACHE / f"{stem}_span{i + 1}.png"        # a new name per picture, so macOS doesn't reuse a cached one
        full.crop((x - x0, y - y0, x - x0 + w, y - y0 + h)).save(piece, "PNG")
        _set(ns, piece, "fill")
