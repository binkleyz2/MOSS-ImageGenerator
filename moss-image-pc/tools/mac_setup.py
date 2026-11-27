"""macOS (Apple Silicon) setup for MOSS Image Studio. install-mac.sh runs these; they can also be run alone:

    tools/venv/bin/python tools/mac_setup.py engines        install or update the two engines
    tools/venv/bin/python tools/mac_setup.py app            make "MOSS Image Studio.app" in ~/Applications
    tools/venv/bin/python tools/mac_setup.py check [--all]  check everything and write a report to the Desktop
                                                            (--all also times a picture with every model)
"""
import json
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))

import moss_platform as P  # noqa: E402
import moss_updates as U  # noqa: E402

APP_NAME = "MOSS Image Studio"


def say(text):
    print("\r  " + text[:110].ljust(110), end="", flush=True)


# ---------------- engines ----------------

def engines():
    for key in ("sd", "llama"):
        name = U.ENGINES[key]["name"]
        program = ROOT / U.ENGINES[key]["folder"] / P.exe(U.ENGINES[key]["program"])
        if program.exists():
            print(f"{name}: already installed ({U.installed_tag(ROOT, key) or 'version not recorded'})")
            continue
        print(f"{name}: downloading the newest Apple Silicon build...")
        item = U.install_item(key)
        U.update_engine(ROOT, item, say)
        print(f"\r  installed {item['tag']}".ljust(114))
    return 0


# ---------------- the app ----------------

def make_icns(dest):
    from PIL import Image
    with Image.open(TOOLS / "moss.ico") as im:                 # Pillow opens the largest size in the .ico
        big = im.convert("RGBA").resize((1024, 1024), Image.Resampling.LANCZOS)
    big.save(dest, format="ICNS")


def app():
    """A small app that starts the window with this folder's Python. It's made on this Mac, so macOS opens it
    without the "unidentified developer" question. Run again if you move the MOSS folder."""
    python = TOOLS / "venv" / "bin" / "python"
    if not python.exists():
        print("Run install-mac.sh first (it makes tools/venv).")
        return 1
    apps = Path.home() / "Applications"
    bundle = apps / f"{APP_NAME}.app"
    shutil.rmtree(bundle, ignore_errors=True)
    (bundle / "Contents" / "MacOS").mkdir(parents=True)
    (bundle / "Contents" / "Resources").mkdir()
    launcher = bundle / "Contents" / "MacOS" / APP_NAME
    launcher.write_text(f'''#!/bin/bash
# Starts MOSS Image Studio from {ROOT}
cd "{ROOT}"
exec "{python}" "{TOOLS / 'moss_gui.py'}" "$@"
''', encoding="utf-8")
    launcher.chmod(0o755)
    try:
        make_icns(bundle / "Contents" / "Resources" / "moss.icns")
    except Exception as e:
        print(f"(No app icon: {e})")
    import moss_gui
    info = dict(CFBundleName=APP_NAME, CFBundleDisplayName=APP_NAME, CFBundleExecutable=APP_NAME,
                CFBundleIdentifier="com.moss-image-studio.app", CFBundlePackageType="APPL",
                CFBundleIconFile="moss.icns", CFBundleShortVersionString=moss_gui.__version__,
                CFBundleVersion=moss_gui.__version__, LSMinimumSystemVersion="13.0", NSHighResolutionCapable=True,
                LSApplicationCategoryType="public.app-category.graphics-design")
    (bundle / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    print(f"Made {bundle}\nOpen it from Applications (or Spotlight), and drag it to the Dock to keep it handy.")
    return 0


# ---------------- self-check ----------------

def run(cmd, timeout=60):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return -1, str(e)


def check(all_models=False):
    lines, problems = [], []

    def note(text):
        print(text, flush=True)
        lines.append(text)

    def problem(text):
        note("PROBLEM: " + text)
        problems.append(text)

    import moss_gui as G
    note(f"MOSS Image Studio {G.__version__} Mac check, {time.strftime('%Y-%m-%d %H:%M')}")
    note(f"macOS {platform.mac_ver()[0]} on {platform.machine()}; "
         f"chip: {run(['sysctl', '-n', 'machdep.cpu.brand_string'])[1]}")
    total, gpu = P.mac_total_gb(), P.mac_gpu_limit_gb()
    free, usable = P.memory_free_gb()
    note(f"Memory: {total:.0f} GB total; the graphics chip may use about {gpu:.0f} GB; "
         f"{free:.1f} GB free or reclaimable right now" if total and gpu and free is not None else "Memory: unknown")
    if platform.machine() != "arm64":
        problem("This isn't an Apple Silicon Mac; the engines here are built for M-series chips.")
    import tkinter
    note(f"Python {platform.python_version()}, Tk {tkinter.TkVersion}")
    for mod in ("AppKit", "keyring", "PIL", "tokenizers"):
        try:
            __import__(mod)
            note(f"  {mod}: ok")
        except Exception as e:
            problem(f"{mod} can't be imported ({e}); run install-mac.sh again")
    try:
        screens = P.detect_screens()
        note("Screens: " + "; ".join(f"{s['w']}x{s['h']} at ({s['x']},{s['y']})" + (" main" if s["primary"] else "")
                                     for s in screens))
        if not screens:
            problem("no screens found")
    except Exception as e:
        problem(f"screens: {e}")
    note(f"Dark mode: {P.uses_dark()}")

    for key in ("sd", "llama"):
        prog = ROOT / U.ENGINES[key]["folder"] / P.exe(U.ENGINES[key]["program"])
        if not prog.exists():
            problem(f"{U.ENGINES[key]['name']} is missing ({prog})")
            continue
        code, out = run([str(prog), "--help"], timeout=60)
        first = (out.splitlines() or [""])[0][:120]
        note(f"{U.ENGINES[key]['name']} {U.installed_tag(ROOT, key)}: runs (exit {code}) {first}")
        if code not in (0, 1) or "Library not loaded" in out or "built for" in out:
            problem(f"{U.ENGINES[key]['name']} doesn't start: {out[-300:]}")

    installed = [k for k, p in G.PROFILES.items() if not p.get("cloud") and not [f for f in G.profile_files(p)
                                                                                    if not f.exists()]]
    note("Models downloaded: " + (", ".join(G.PROFILES[k]["name"] for k in installed) or "none"))

    try:      # Keychain: store and read back a test entry (not your real key), then delete it
        import keyring
        keyring.set_password("MOSS Image Studio self-check", "test", "abc123")
        ok = keyring.get_password("MOSS Image Studio self-check", "test") == "abc123"
        keyring.delete_password("MOSS Image Studio self-check", "test")
        note(f"Keychain: {'ok' if ok else 'read back the wrong value'}")
        if not ok:
            problem("Keychain didn't read back the test value")
    except Exception as e:
        problem(f"Keychain: {e}")
    with tempfile.TemporaryDirectory() as tmp:
        plist = Path(tmp) / "test.plist"
        plist.write_bytes(P.launchd_plist([sys.executable, "--wallpaper-of-the-day"], "08:00", Path(tmp) / "log"))
        code, out = run(["plutil", "-lint", str(plist)])
        note(f"Wallpaper-of-the-day schedule file: {out}")
        if code != 0:
            problem("the launchd schedule file isn't valid")
    try:
        import moss_wallpaper_mac as W
        now = W.current()
        note("Desktop pictures now (read only, nothing changed): " +
             "; ".join(Path(m["path"]).name or "(none)" for m in now["monitors"]))
    except Exception as e:
        problem(f"reading the desktop pictures: {e}")

    out_dir = Path(tempfile.mkdtemp(prefix="moss-selftest-"))
    t0 = time.time()
    code, out = run([sys.executable, str(TOOLS / "moss_gui.py"), "--selftest", str(out_dir)], timeout=1800)
    try:
        result = json.loads((out_dir / "selftest-result.json").read_text(encoding="utf-8"))
    except Exception:
        result = {"ok": False, "status": out[-400:]}
    note(f"Self-test (MOSS, 256x256, 50 steps): {'passed' if result.get('ok') else 'FAILED'} in "
         f"{time.time() - t0:.0f} s; {result.get('status')}")
    if not result.get("ok"):
        problem("the self-test picture wasn't made")

    for key in (installed if all_models else [k for k in ("flux2-klein-4b", "z-image-turbo") if k in installed][:1]):
        p = G.PROFILES[key]
        cmd = [str(G.SD_CLI)]
        for opt, rel in p["files"].items():
            cmd += [opt, str(G.MODELS / rel)]
        dest = out_dir / f"{key}.png"
        cmd += ["--cfg-scale", str(p["cfg"]), "--steps", str(p["steps"]), "-W", "1024", "-H", "1024", "-s", "42",
                "-p", "A photo of a red fox in fresh snow, golden morning light", "-o", str(dest), *P.OFFLOAD_ARGS]
        if p["sampler"]:
            cmd += ["--sampling-method", p["sampler"]]
        t0 = time.time()
        code, out = run(cmd, timeout=3600)
        secs = time.time() - t0
        if code == 0 and dest.exists():
            ref = G.base_secs(1024, 1024, p)
            note(f"{p['name']} at 1024x1024: {secs:.0f} s (about {secs / ref:.1f}x the RTX 4070 Ti SUPER's {ref:.0f} s)")
        else:
            err = [ln for ln in out.splitlines() if "[E" in ln or "error" in ln.lower()][-3:]
            problem(f"{p['name']} at 1024x1024 failed after {secs:.0f} s: {' | '.join(err) or out[-300:]}")

    note("")
    note("All good." if not problems else f"{len(problems)} problem(s) found (see PROBLEM lines).")
    report = Path.home() / "Desktop" / "MOSS Mac check.txt"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nReport saved to {report}")
    return 0 if not problems else 1


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else ""
    if what == "engines":
        sys.exit(engines())
    if what == "app":
        sys.exit(app())
    if what == "check":
        sys.exit(check("--all" in sys.argv))
    print(__doc__)
    sys.exit(2)
