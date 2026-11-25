"""MOSS Image Studio: a window for making MOSS-Image pictures on this PC.

Start it with the "MOSS Image Studio" shortcut on the desktop, or:
    tools\\venv\\Scripts\\pythonw.exe tools\\moss_gui.py
It runs the same engine (bin\\sd-cli.exe) and models as "MOSS Image.bat".
"""
import ctypes
import json
import os
import queue
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

import moss_cloud as C
import moss_describe as D
import moss_meta as M
import moss_updates as U
import moss_extras as X

__version__ = "1.4.0"
FROZEN = getattr(sys, "frozen", False)          # True when running as the packaged .exe
RESOURCES = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))   # bundled icon


def find_root():
    """The moss-image-pc folder made by the installer (it holds bin\\ and models\\)."""
    if not FROZEN:
        return Path(__file__).resolve().parent.parent
    exe_dir = Path(sys.executable).resolve().parent
    for folder in (exe_dir, exe_dir / "moss-image-pc", exe_dir.parent, exe_dir.parent / "moss-image-pc"):
        if (folder / "bin" / "sd-cli.exe").exists():
            return folder
    return exe_dir


ROOT = find_root()
HERE = ROOT / "tools"
SD_CLI = ROOT / "bin" / "sd-cli.exe"
MODELS = ROOT / "models"
TOKENIZER = MODELS / "tokenizer_qwen3.5.json"   # used for token counts (exact for MOSS, close for the others)
SETTINGS_FILE = HERE / "gui_settings.json"
UPSCALER = MODELS / "upscalers" / "RealESRGAN_x4plus.pth"     # Real-ESRGAN x4plus (general photos)
DEFAULT_OUT = ROOT / "outputs"

MAX_PIXELS = 3440 * 1440          # largest picture made here (about 5 MP)
MAX_SIDE = 3440                   # longest side for normal pictures
MAX_SPAN_SIDE = 6144              # longest side when drawing for several screens side by side
TILE_ABOVE = 2_000_000            # bigger pictures are decoded in tiles to save video memory
SOFTWARE = f"MOSS Image Studio {__version__} (stable-diffusion.cpp)"

# Models this app can run. "files" are engine options -> paths under models\.
# "edit" adds what the model needs to change an existing picture (passed with -r).
# step_secs: seconds per step for a ~1 MP picture on an RTX 4070 Ti SUPER (for time estimates).
PROFILES = {
    "moss": dict(
        name="MOSS-Image SFT", prefix="moss", license="Apache-2.0",
        files={"--diffusion-model": "moss_image_sft_int8_convrot.safetensors",
               "--llm": "Qwen3.5-9B-Q4_K_M.gguf", "--vae": "flux2-vae.safetensors",
               "--tokenizer": "tokenizer_qwen3.5.json"},
        steps=50, cfg=3.0, sampler=None, align=16, step_secs=0.58, exact_tokens=True,
        note="Detailed scenes and readable text. 50 steps.",
        edit=dict(files={"--llm_vision": "Qwen3.5-9B-mmproj-BF16.gguf",
                         "--lora-model-dir": "loras"},
                  need=["loras/armV5-moss-edit-lora.safetensors"],
                  prompt_suffix="<lora:armV5-moss-edit-lora:1.0>", steps=50, cfg=3.0, extra_secs=35)),
    "flux2-klein-4b": dict(
        name="FLUX.2 Klein 4B", prefix="flux2klein", license="Apache-2.0",
        files={"--diffusion-model": "flux2-klein-4b/flux-2-klein-4b-Q8_0.gguf",
               "--llm": "flux2-klein-4b/Qwen3-4B-Q8_0.gguf", "--vae": "flux2-vae.safetensors"},
        steps=4, cfg=1.0, sampler="euler", align=16, step_secs=0.5, exact_tokens=False,
        note="Very fast: 4 steps. Photos and general pictures.",
        edit=dict(files={}, need=[], prompt_suffix="", steps=4, cfg=1.0, extra_secs=2)),
    "qwen-image-2.1": dict(
        name="Qwen Image 2.1", prefix="qwenimage", license="Qwen Research (non-commercial)",
        files={"--diffusion-model": "qwen-image-2.1/qwen_image_2.1_int8_convrot.safetensors",
               "--vae": "qwen-image-2.1/qwen_image_2.1_vae_bf16.safetensors",
               "--llm": "qwen-image-2.1/Qwen3VL-8B-Instruct-Q4_K_M.gguf"},
        steps=40, cfg=6.0, sampler="euler", align=32, step_secs=0.75, size_power=1.45, exact_tokens=False,
        note="Strong at text in pictures and complex layouts. 40 steps. "
             "Edits work well, but very dark pictures can come out over-brightened.",
        # Editing needs plenty of free Windows commit memory (see memory_advice); with it, a 1 MP
        # edit takes about 50 s. Very dark sources get blown-out shadows, so MOSS or FLUX suit those.
        edit=dict(files={"--llm_vision": "qwen-image-2.1/mmproj-Qwen3VL-8B-Instruct-F16.gguf"},
                  need=[], prompt_suffix="", steps=40, cfg=6.0, extra_secs=10)),
    "nano-banana": dict(
        name="Nano Banana 2.1 (Google cloud)", prefix="nanobanana", cloud=True,
        license="Google Gemini API terms",
        files={}, steps=0, cfg=0, sampler=None, align=16, step_secs=0, exact_tokens=False,
        note="Runs on Google's servers with your Gemini API key, about $0.03-0.11 per picture. "
             "Your prompt (and picture, when editing) goes to Google; pictures carry its SynthID watermark.",
        edit=dict(files={}, need=[], prompt_suffix="", steps=0, cfg=0)),
}
DEFAULT_PROFILE = "moss"


def profile_files(profile, edit=False):
    """Paths of every file a model needs (for new pictures, or for editing too)."""
    if profile.get("cloud"):
        return []
    paths = [MODELS / p for opt, p in profile["files"].items()]
    if edit and profile["edit"]:
        paths += [MODELS / p for opt, p in profile["edit"]["files"].items() if opt != "--lora-model-dir"]
        paths += [MODELS / p for p in profile["edit"]["need"]]
    return paths

TIERS = ["Standard (~1 MP)", "Large (~2 MP)", "Extra large (~3.7 MP)", "Maximum (~5 MP)"]
TIER_PIXELS = [1_040_000, 2_070_000, 3_690_000, MAX_PIXELS]
ASPECTS = {   # name: sizes for each tier; all multiples of 16 and within MAX_PIXELS
    "16:9 wide":       [(1360, 768), (1920, 1088), (2560, 1440), (2960, 1664)],
    "9:16 tall":       [(768, 1360), (1088, 1920), (1440, 2560), (1664, 2960)],
    "1:1 square":      [(1024, 1024), (1440, 1440), (1920, 1920), (2224, 2224)],
    "4:3 wide":        [(1152, 896), (1664, 1248), (2208, 1664), (2560, 1920)],
    "3:4 tall":        [(896, 1152), (1248, 1664), (1664, 2208), (1920, 2560)],
    "3:2 wide":        [(1216, 832), (1760, 1168), (2352, 1568), (2720, 1808)],
    "2:3 tall":        [(832, 1216), (1168, 1760), (1568, 2352), (1808, 2720)],
    "21:9 ultrawide":  [(1536, 640), (2240, 960), (2944, 1264), (3440, 1440)],
}
CUSTOM = "Custom"
THEMES = ["Match Windows", "Light", "Dark"]

PROGRESS_RE = re.compile(r"\|[=\-> ]*\|\s*(\d+)/(\d+)\s*-\s*([\d.]+)\s*(it/s|s/it)")
SETTINGS_RE = re.compile(r"(\d+)\s*x\s*(\d+).*?seed\s*(\d+)", re.I)
NAME_RE = re.compile(r"_(\d+)x(\d+)_seed(\d+)", re.I)

LIGHT = dict(hint="#555555", error="#b00020", text_bg="#ffffff", text_fg="#000000", select="#cce4f7",
             border="#a0a0a0", accent="#0067c0", bg=None)
DARK = dict(bg="#1f2023", field="#2b2d31", fg="#e8e8e8", hint="#a0a4ab", error="#ff7b7b", border="#3b3e44",
            accent="#4c8dff", button="#33363c", button_active="#40444b", pressed="#4a4e56", disabled="#6b6f76",
            select="#3b5b8f", progress="#3fae5a", text_bg="#2b2d31", text_fg="#e8e8e8")


# ---------------- helpers ----------------

def estimate(w, h, profile=None, editing=False):
    """Rough time on this PC (RTX 4070 Ti SUPER), from earlier runs."""
    profile = profile or PROFILES[DEFAULT_PROFILE]
    if profile.get("cloud"):
        return "usually 10-40 s"
    steps = profile["edit"]["steps"] if editing and profile["edit"] else profile["steps"]
    secs = 10 + steps * profile["step_secs"] * ((w * h / 1e6) / 1.04) ** profile.get("size_power", 1.25)
    if editing and profile["edit"]:
        secs += profile["edit"].get("extra_secs", 0)
    if secs < 50:
        return "under a minute"
    mins = secs / 60
    return f"about {mins:.0f} min" if mins >= 1.5 else "about 1 min"


def tidy_prompt(text):
    """Line breaks become spaces, like the .bat launcher."""
    return " ".join(line.strip() for line in text.splitlines() if line.strip())


def auto_title(prompt, words=8):
    parts = prompt.split()
    title = " ".join(parts[:words]).rstrip(",.;:")
    return title + ("..." if len(parts) > words else "")


def fit_size(full_w, full_h, target_pixels, max_side=MAX_SPAN_SIDE, align=16):
    """Largest drawing size with the shape of full_w x full_h, multiples of align, within the limits."""
    target = min(target_pixels, MAX_PIXELS, full_w * full_h)
    r = full_w / full_h
    h = (target / r) ** 0.5
    w = h * r
    if w > max_side:
        w, h = max_side, max_side / r
    if h > max_side:
        h, w = max_side, max_side * r
    gw = max(256, int(w // align) * align)
    gh = max(256, int(round(gw / r / align)) * align)
    while gw * gh > min(MAX_PIXELS, target * 1.02) and (gw > 256 or gh > 256):
        if gh > 256 and gh * r >= gw:
            gh -= align
        elif gw > 256:
            gw -= align
        else:
            gh -= align
    return gw, gh


def detect_screens():
    """Connected screens in real pixels: list of dicts with x, y, w, h, primary."""
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


def screen_options(screens):
    """Shape choices for the detected screens: each screen alone, neighbours together, and all of them."""
    if not screens:
        return {}
    in_a_row = all(s["y"] == screens[0]["y"] and s["h"] == screens[0]["h"] for s in screens) and \
        all(a["x"] + a["w"] == b["x"] for a, b in zip(screens, screens[1:]))
    n = len(screens)
    if in_a_row and n == 2:
        names = ["left", "right"]
    elif in_a_row and n == 3:
        names = ["left", "middle", "right"]
    else:
        names = [f"screen {i + 1}" for i in range(n)]
    for s, name in zip(screens, names):
        s["name"] = name + (" (main)" if s["primary"] and n > 1 else "")
        s["short"] = name

    def option(group):
        x0, y0 = min(s["x"] for s in group), min(s["y"] for s in group)
        x1, y1 = max(s["x"] + s["w"] for s in group), max(s["y"] + s["h"] for s in group)
        parts = [dict(name=s["short"], x=s["x"] - x0, y=s["y"] - y0, w=s["w"], h=s["h"]) for s in group]
        return dict(W=x1 - x0, H=y1 - y0, parts=parts)

    opts = {}
    if n > 1:
        o = option(screens)
        opts[f"Screens: all {n} ({o['W']} x {o['H']})"] = o
        if in_a_row and n > 2:
            for size in range(n - 1, 1, -1):
                for i in range(n - size + 1):
                    group = screens[i:i + size]
                    o = option(group)
                    opts[f"Screens: {' + '.join(s['short'] for s in group)} ({o['W']} x {o['H']})"] = o
    for s in screens:
        o = option([s])
        label = "Screen" if n == 1 else f"Screen: {s['name']}"
        opts[f"{label} ({o['W']} x {o['H']})"] = o
    return opts


MEMORY_ERROR_RE = re.compile(r"capacity exceeded|out of memory|cannot make enough memory available|"
                             r"failed during (weight preparation|workspace allocation)", re.I)


def memory_free_gb():
    """(free RAM, free commit) in GB, as Windows reports them; commit = what programs can still reserve."""
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


def memory_advice(job):
    ram, commit = memory_free_gb()
    mem = (f"Right now Windows has {ram:.0f} GB of RAM free and can promise programs "
           f"{commit:.0f} GB more memory in total.\n\n") if ram is not None else ""
    return (f"{job['profile']['name']} ran out of memory at {job['w']} x {job['h']} "
            f"({job['w'] * job['h'] / 1e6:.1f} MP), so no picture was made.\n\n{mem}"
            "What helps:\n"
            "  - Choose a smaller Size (Standard ~1 MP needs the least).\n"
            "  - Close other big programs.\n"
            "  - Restart Windows if it has been running for days: some Windows services slowly hold on "
            "to more and more memory, and a restart frees it.\n"
            "  - Or use another model: FLUX.2 Klein needs the least memory.")


def windows_uses_dark():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def write_txt(final, job):
    tokens = f"{job['tokens']:,}" if job["tokens"] is not None else "unknown"
    how = "exact" if job["profile"]["exact_tokens"] else "approximate"
    lines = ["Edit instruction:" if job["edit_src"] else "Prompt:", job["prompt"], "",
             f"Tokens: {tokens} (Qwen 3.5 tokenizer, prompt text only; {how} for this model)",
             f"Words: {len(job['prompt'].split()):,}", f"Characters: {len(job['prompt']):,}", "",
             f"Image: {final.name}", f"Model: {job['profile']['name']}", f"Size: {job['w']} x {job['h']}"]
    if job["edit_src"]:
        lines.append(f"Edited from: {job['edit_src']}")
    if job.get("screen"):
        lines.append(f"Made for: {job['shape']}" + (" (scaled up)" if job["upscale"] else ""))
    lines += [f"Seed: {job['seed']}", f"Steps: {job['steps']}", f"CFG scale: {job['cfg']}",
              f"Made: {job['when']:%Y-%m-%d %H:%M:%S}"]
    try:
        final.with_suffix(".txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def ai_upscale(src, dest):
    """4x Real-ESRGAN upscale with the image engine. Raises RuntimeError on failure."""
    cmd = [str(SD_CLI), "-M", "upscale", "--upscale-model", str(UPSCALER), "-i", str(src), "-o", str(dest)]
    run = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, timeout=1800,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if run.returncode != 0 or not Path(dest).exists():
        tail = run.stdout.decode("utf-8", "replace").strip().splitlines()[-3:]
        raise RuntimeError("AI upscaling failed: " + " ".join(tail))


def upscale_picture(src, factor, say):
    """Make a new, factor-times-bigger copy of a picture with AI upscaling. Returns the new file."""
    src = Path(src)
    out = src.with_name(f"{src.stem}_x{factor}{src.suffix}")
    tmp = src.with_name(f"{src.stem}.x4.tmp.png")
    say(f"AI upscaling {src.name} (this can take a minute for big pictures)...")
    ai_upscale(src, tmp)
    with Image.open(src) as im:
        target = (im.width * factor, im.height * factor)
    if factor != 4:
        with Image.open(tmp) as im:
            im.convert("RGB").resize(target, Image.Resampling.LANCZOS).save(tmp, "PNG", compress_level=1)
    meta = M.read(src)
    meta["settings"] = (meta.get("settings", "") + f"; upscaled {factor}x to {target[0]}x{target[1]} "
                        "with Real-ESRGAN").lstrip("; ")
    meta["title"] = (meta.get("title") or src.stem) + f" ({factor}x)"
    say("Saving...")
    M.write(out, meta, source=tmp)
    tmp.unlink(missing_ok=True)
    return out


def finish_files(job, say):
    """After the engine is done: scale up for screens, save with details, split per screen. Returns files."""
    raw, final, scr = job["raw"], job["final"], job.get("screen")
    if scr and job["upscale"] and (scr["W"], scr["H"]) != (job["w"], job["h"]):
        source = raw
        if job.get("ai_upscale") and UPSCALER.exists() and scr["W"] > job["w"] * 1.05:
            say(f"Sharpening with AI upscaling, then fitting to {scr['W']} x {scr['H']}...")
            source = raw.with_name(raw.stem + ".x4.png")
            try:
                ai_upscale(raw, source)
            except Exception:
                source = raw                        # fall back to ordinary scaling
        else:
            say(f"Scaling up to {scr['W']} x {scr['H']}...")
        with Image.open(source) as im:
            big = im.convert("RGB").resize((scr["W"], scr["H"]), Image.Resampling.LANCZOS)
        big.save(raw, "PNG", compress_level=1)
        if source != raw:
            Path(source).unlink(missing_ok=True)
    say("Saving the picture and its details...")
    M.write(final, job["meta"], source=raw)
    files = [final]
    if scr and job["split"] and len(scr["parts"]) > 1:
        say("Saving a picture for each screen...")
        with Image.open(raw) as im:
            full = im.convert("RGB")
        sx, sy = full.width / scr["W"], full.height / scr["H"]
        n = len(scr["parts"])
        for i, p in enumerate(scr["parts"], 1):
            box = (round(p["x"] * sx), round(p["y"] * sy), round((p["x"] + p["w"]) * sx), round((p["y"] + p["h"]) * sy))
            tmp = final.with_name(f"{final.stem}_part{i}.tmp.png")
            full.crop(box).save(tmp, "PNG", compress_level=1)
            out = final.with_name(f"{final.stem}_part{i}-{p['name']}{final.suffix}")
            meta = dict(job["meta"], title=f"{job['meta']['title']} ({p['name']} screen)",
                        settings=job["meta"]["settings"] + f"; part {i} of {n}: {p['name']} screen {p['w']}x{p['h']}")
            M.write(out, meta, source=tmp)
            tmp.unlink(missing_ok=True)
            files.append(out)
    raw.unlink(missing_ok=True)
    if job["save_txt"]:
        write_txt(final, job)
    return files


# ---------------- the window ----------------

class Studio:
    def __init__(self, root, check=True):
        self.root = root
        self.settings = self.load_settings()
        self.tokenizer = None
        self.job = None                # the run in progress
        self.events = queue.Queue()
        self.current = None            # picture shown on the right
        self.preview_img = None
        self.token_job = None
        self.started = 0.0
        self.log_lines = []
        self.screens = []
        self.screen_opts = {}
        self.colors = LIGHT
        self.texts = []
        self.combos = []
        key = self.settings.get("model", DEFAULT_PROFILE)
        self.model_key = key if key in PROFILES else DEFAULT_PROFILE
        self.mode = tk.StringVar(value="new")
        self.edit_src = None           # picture to edit (Edit mode)
        self.edit_mask = None          # painted area for inpainting (Edit mode)
        self.queue = []                # requests waiting to run
        self.queue_running = False
        self.queue_done = []           # (request, file or None, error or None), newest last
        self.groups = {}               # compare/variation groups: id -> dict(total, results)
        self.spins = []

        root.title("MOSS Image Studio")
        root.geometry(self.settings.get("geometry", "1360x900"))
        root.minsize(1100, 900)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        try:
            root.iconbitmap(default=str(RESOURCES / "moss.ico"))
        except tk.TclError:
            pass
        self.style = ttk.Style()

        self.theme = tk.StringVar(value=self.settings.get("theme", "Match Windows"))
        body = ttk.Frame(root)
        body.pack(fill="both", expand=True)
        self.body = body
        self.build_left(body)
        self.build_right(body)
        self.refresh_screens(initial=True)
        self.restore_choices()
        self.apply_theme()
        threading.Thread(target=self.load_tokenizer, daemon=True).start()
        self.root.after(100, self.poll_events)
        if check:
            self.root.after(400, self.check_install)
            self.root.after(800, self.autostart_remote)

    def autostart_remote(self):
        if self.settings.get("remote", {}).get("enabled"):
            try:
                X.start_remote(self)
            except OSError as e:
                self.status.configure(text=f"Phone access couldn't start: {e}")

    # ---------------- install check ----------------
    @property
    def profile(self):
        return PROFILES[self.model_key]

    def missing_files(self, key=None, edit=False):
        profile = PROFILES[key or self.model_key]
        if profile.get("cloud"):
            return []
        return [p for p in [SD_CLI] + profile_files(profile, edit) if not p.exists()]

    def check_install(self, edit=False):
        """Warn if the engine or the chosen model's files are missing. Returns True when all are there."""
        missing = self.missing_files(edit=edit)
        if not missing:
            return True
        names = "\n".join(f"  {p.relative_to(ROOT)}" for p in missing[:8])
        if not SD_CLI.exists():
            text = ("The image engine was not found, so pictures can't be made yet.\n\n"
                    f"Looked in: {ROOT}\nMissing:\n{names}\n\n"
                    "Run installer-moss-image.ps1 first. Then put MOSS Image Studio.exe in the "
                    "moss-image-pc folder it creates (next to the bin and models folders), "
                    "or in the folder that holds moss-image-pc.")
        else:
            what = "editing pictures with" if edit else "using"
            text = (f"Some files needed for {what} {self.profile['name']} aren't downloaded yet.\n\n"
                    f"Missing:\n{names}\n\n"
                    "Run download-models.ps1 (next to the installer) to get them, "
                    "or choose another model.")
        messagebox.showwarning("MOSS Image Studio", text + "\n\nYou can still open pictures and edit their details.")
        return False

    def model_label(self, key):
        if PROFILES[key].get("cloud"):
            return PROFILES[key]["name"] + ("" if C.find_key(self.settings) else "  (needs API key)")
        installed = not self.missing_files(key)
        return PROFILES[key]["name"] + ("" if installed else "  (not downloaded)")

    # ---------------- Nano Banana (cloud) API key ----------------
    def edit_api_key(self):
        """Ask for the Gemini API key. It is stored encrypted for this Windows account only."""
        win = tk.Toplevel(self.root)
        win.title("Gemini API key")
        win.transient(self.root)
        win.resizable(False, False)
        if self.colors is DARK:
            win.configure(background=DARK["bg"])
        frame = ttk.Frame(win, padding=14)
        frame.pack(fill="both", expand=True)
        has_key = bool(self.settings.get("gemini_key"))
        env = bool(os.environ.get("GEMINI_API_KEY", "").strip())
        text = ("Nano Banana runs on Google's servers and is billed to your own Gemini API key.\n"
                f"Get a key at {C.KEY_PAGE} (billing must be turned on for image generation).\n\n"
                "The key is saved encrypted, readable only by your Windows account on this PC.")
        if env:
            text += "\n\nThe GEMINI_API_KEY environment variable is set; it is used instead of a saved key."
        ttk.Label(frame, text=text, wraplength=460, justify="left").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(frame, text="API key").grid(row=1, column=0, sticky="w", pady=(12, 0))
        key_var = tk.StringVar()
        entry = ttk.Entry(frame, textvariable=key_var, show="*", width=48)
        entry.grid(row=1, column=1, columnspan=2, sticky="we", pady=(12, 0), padx=(8, 0))
        entry.focus_set()
        status = ttk.Label(frame, text="A key is saved." if has_key else "No key saved yet.", style="Hint.TLabel")
        status.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))

        def save():
            key = key_var.get().strip()
            if not key:
                messagebox.showinfo("Gemini API key", "Paste your key first.", parent=win)
                return
            try:
                self.settings["gemini_key"] = C.encrypt_key(key)
            except OSError as e:
                messagebox.showerror("Gemini API key", str(e), parent=win)
                return
            self.save_settings()
            self.refresh_model_list()
            win.destroy()

        def remove():
            self.settings.pop("gemini_key", None)
            self.save_settings()
            self.refresh_model_list()
            win.destroy()

        buttons = ttk.Frame(frame)
        buttons.grid(row=3, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Save key", command=save).pack(side="left")
        ttk.Button(buttons, text="Remove saved key", command=remove,
                   state="normal" if has_key else "disabled").pack(side="left", padx=6)
        ttk.Button(buttons, text="Cancel", command=win.destroy).pack(side="left")
        win.bind("<Return>", lambda e: save())
        win.grab_set()
        self.root.wait_window(win)

    # ---------------- settings ----------------
    def load_settings(self):
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save_settings(self):
        s = self.settings
        s.update(out_dir=self.out_dir.get(), fmt=self.fmt.get(), save_txt=self.save_txt.get(),
                 authors=self.authors.get(), copyright=self.copyright.get(), tags=self.tags.get(),
                 aspect=self.aspect.get(), tier=self.tier.get(), width=self.width.get(), height=self.height.get(),
                 upscale=self.upscale.get(), split=self.split.get(), theme=self.theme.get(),
                 model=self.model_key, geometry=self.root.geometry())
        try:
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")
        except Exception:
            pass

    # ---------------- layout ----------------
    def make_text(self, parent, **kw):
        t = tk.Text(parent, relief="flat", borderwidth=0, highlightthickness=1, padx=6, pady=4, **kw)
        self.texts.append(t)
        return t

    def build_left(self, parent):
        left = ttk.Frame(parent, padding=(12, 8, 6, 10))
        left.pack(side="left", fill="both", expand=False)
        # Grid, so that on a short window the prompt box shrinks and the buttons stay visible.
        left.rowconfigure(0, weight=1, minsize=150)
        left.columnconfigure(0, weight=1)

        box = ttk.LabelFrame(left, text=" What do you want to see? ", padding=8)
        box.grid(row=0, column=0, sticky="nsew")
        self.prompt_box = box
        # Model and mode
        top = ttk.Frame(box)
        top.grid(row=0, column=0, columnspan=4, sticky="we", pady=(0, 6))
        ttk.Label(top, text="Model").pack(side="left")
        self.model_var = tk.StringVar()
        self.model_box = ttk.Combobox(top, textvariable=self.model_var, state="readonly", width=30)
        self.model_box.pack(side="left", padx=(6, 12))
        self.model_box.bind("<<ComboboxSelected>>", lambda e: self.on_model_change())
        self.combos.append(self.model_box)
        ttk.Radiobutton(top, text="New picture", value="new", variable=self.mode,
                        command=self.on_mode_change).pack(side="left")
        ttk.Radiobutton(top, text="Edit a picture", value="edit", variable=self.mode,
                        command=self.on_mode_change).pack(side="left", padx=(8, 0))
        self.model_note = ttk.Label(box, text="", style="Hint.TLabel", wraplength=440)
        self.model_note.grid(row=1, column=0, columnspan=4, sticky="w")
        self.key_btn = ttk.Button(box, text="API key...", command=self.edit_api_key)
        # Picture to edit (Edit mode only)
        self.edit_row = ttk.Frame(box)
        self.edit_row.grid(row=2, column=0, columnspan=4, sticky="we", pady=(6, 0))
        ttk.Label(self.edit_row, text="Picture to edit:").pack(side="left")
        self.edit_label = ttk.Label(self.edit_row, text="none chosen", style="Hint.TLabel", width=20)
        self.edit_label.pack(side="left", padx=6)
        ttk.Button(self.edit_row, text="Paint area...", command=lambda: X.open_mask_painter(self)).pack(side="right")
        ttk.Button(self.edit_row, text="Choose...", command=self.choose_edit_src).pack(side="right", padx=(0, 4))
        ttk.Button(self.edit_row, text="Use the picture shown", command=self.use_shown_as_src
                   ).pack(side="right", padx=4)
        self.edit_row.grid_remove()

        self.prompt = self.make_text(box, height=8, width=58, wrap="word", font=("Segoe UI", 10), undo=True)
        sb = ttk.Scrollbar(box, command=self.prompt.yview)
        self.prompt.configure(yscrollcommand=sb.set)
        self.prompt.grid(row=3, column=0, columnspan=3, sticky="nsew", pady=(6, 0))
        sb.grid(row=3, column=3, sticky="ns", pady=(6, 0))
        box.rowconfigure(3, weight=1)
        box.columnconfigure(0, weight=1)
        self.token_label = ttk.Label(box, text="Tokens: loading the tokenizer...", style="Hint.TLabel")
        self.token_label.grid(row=4, column=0, sticky="w", pady=(6, 0))
        ttk.Button(box, text="Clear", command=lambda: (self.prompt.delete("1.0", "end"), self.update_tokens())
                   ).grid(row=4, column=1, sticky="e", pady=(6, 0), padx=4)
        row4 = ttk.Frame(box)
        row4.grid(row=4, column=2, columnspan=2, sticky="e", pady=(6, 0))
        ttk.Button(row4, text="Improve", command=lambda: X.open_enhance(self)).pack(side="left")
        start_btn = ttk.Button(row4, text="Start from \u25be")
        start_btn.configure(command=lambda: X.start_from_menu(self, start_btn))
        start_btn.pack(side="left", padx=(4, 0))
        self.prompt.bind("<<Modified>>", self.on_prompt_change)

        # Size
        size = ttk.LabelFrame(left, text=" Picture size ", padding=8)
        size.grid(row=1, column=0, sticky="we", pady=(8, 0))
        ttk.Label(size, text="Shape").grid(row=0, column=0, sticky="w")
        self.aspect = tk.StringVar()
        self.aspect_box = ttk.Combobox(size, textvariable=self.aspect, state="readonly", width=34)
        self.aspect_box.grid(row=0, column=1, sticky="w", padx=(6, 0))
        self.aspect_box.bind("<<ComboboxSelected>>", lambda e: self.apply_preset())
        self.combos.append(self.aspect_box)
        ttk.Label(size, text="Size").grid(row=1, column=0, sticky="w", pady=(6, 0))
        row = ttk.Frame(size)
        row.grid(row=1, column=1, sticky="w", padx=(6, 0), pady=(6, 0))
        self.tier = tk.StringVar()
        self.tier_box = ttk.Combobox(row, textvariable=self.tier, state="readonly", width=20, values=TIERS)
        self.tier_box.pack(side="left")
        self.tier_box.bind("<<ComboboxSelected>>", lambda e: self.apply_preset())
        self.combos.append(self.tier_box)
        self.width = tk.IntVar(value=1360)
        self.height = tk.IntVar(value=768)
        for i, var in enumerate((self.width, self.height)):
            if i:
                ttk.Label(row, text="x").pack(side="left", padx=4)
            sp = ttk.Spinbox(row, from_=256, to=MAX_SPAN_SIDE, increment=16, textvariable=var, width=6,
                             command=self.on_manual_size)
            sp.pack(side="left", padx=(12 if not i else 0, 0))
            self.spins.append(sp)
            sp.bind("<KeyRelease>", lambda e: self.on_manual_size())
        self.size_label = ttk.Label(size, text="", style="Hint.TLabel", wraplength=430)
        self.size_label.grid(row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self.adv_note = ttk.Label(size, text="", style="Hint.TLabel", wraplength=430)
        self.adv_note.grid(row=6, column=0, columnspan=4, sticky="w")

        self.screen_box = ttk.Frame(size)
        self.screen_box.grid(row=4, column=0, columnspan=4, sticky="w", pady=(4, 0))
        self.upscale = tk.BooleanVar(value=True)
        self.split = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.screen_box, text="Scale up to the screens' full resolution", variable=self.upscale,
                        command=self.on_upscale_toggle).pack(anchor="w")
        self.ai_upscale = tk.BooleanVar(value=True)
        self.ai_check = ttk.Checkbutton(self.screen_box, text="Use AI upscaling for a sharper result (Real-ESRGAN)",
                                        variable=self.ai_upscale)
        self.ai_check.pack(anchor="w")
        self.split_check = ttk.Checkbutton(self.screen_box, text="Also save each screen's part as its own picture",
                                           variable=self.split)
        self.split_check.pack(anchor="w")
        self.screen_box.grid_remove()

        sr = ttk.Frame(size)
        sr.grid(row=5, column=0, columnspan=4, sticky="we", pady=(6, 0))
        self.screens_label = ttk.Label(sr, text="", style="Hint.TLabel", wraplength=330)
        self.screens_label.pack(side="left")
        ttk.Button(sr, text="Detect screens", command=self.refresh_screens).pack(side="right")

        # Seed
        seed = ttk.LabelFrame(left, text=" Seed (reuse one to keep a picture and change a detail) ", padding=8)
        seed.grid(row=2, column=0, sticky="we", pady=(8, 0))
        self.seed_mode = tk.StringVar(value="random")
        ttk.Radiobutton(seed, text="New random picture", value="random", variable=self.seed_mode,
                        command=self.on_seed_mode).grid(row=0, column=0, sticky="w")
        self.last_radio = ttk.Radiobutton(seed, text="Same as the last picture", value="last",
                                          variable=self.seed_mode, command=self.on_seed_mode)
        self.last_radio.grid(row=1, column=0, sticky="w")
        row = ttk.Frame(seed)
        row.grid(row=2, column=0, sticky="w")
        ttk.Radiobutton(row, text="This seed:", value="number", variable=self.seed_mode,
                        command=self.on_seed_mode).pack(side="left")
        self.seed_value = tk.StringVar()
        e = ttk.Entry(row, textvariable=self.seed_value, width=14)
        e.pack(side="left", padx=6)
        e.bind("<FocusIn>", lambda ev: self.seed_mode.set("number"))
        self.seed_hint = ttk.Label(seed, text="", style="Hint.TLabel", wraplength=430)
        self.seed_hint.grid(row=3, column=0, sticky="w", pady=(4, 0))

        # Output
        out = ttk.LabelFrame(left, text=" Save to ", padding=8)
        out.grid(row=3, column=0, sticky="we", pady=(8, 0))
        self.out_dir = tk.StringVar()
        ttk.Entry(out, textvariable=self.out_dir, width=44).grid(row=0, column=0, columnspan=3, sticky="we")
        ttk.Button(out, text="Browse...", command=self.pick_folder).grid(row=0, column=3, padx=(6, 0))
        ttk.Button(out, text="Open", command=lambda: self.open_path(self.out_dir.get())).grid(row=0, column=4, padx=(4, 0))
        self.fmt = tk.StringVar(value="png")
        ttk.Label(out, text="Format").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Radiobutton(out, text="PNG (best quality)", value="png", variable=self.fmt).grid(row=1, column=1, sticky="w", pady=(8, 0))
        ttk.Radiobutton(out, text="JPEG (smaller)", value="jpg", variable=self.fmt).grid(row=1, column=2, sticky="w", pady=(8, 0))
        self.save_txt = tk.BooleanVar(value=True)
        ttk.Checkbutton(out, text="Also save the prompt as a .txt file (with token count)", variable=self.save_txt
                        ).grid(row=2, column=0, columnspan=5, sticky="w", pady=(6, 0))
        out.columnconfigure(0, weight=1)

        # Default details
        det = ttk.LabelFrame(left, text=" Details saved into every new picture ", padding=8)
        det.grid(row=4, column=0, sticky="we", pady=(8, 0))
        self.authors, self.copyright, self.tags = tk.StringVar(), tk.StringVar(), tk.StringVar()
        for r, (label, var) in enumerate((("Authors", self.authors), ("Copyright", self.copyright),
                                          ("Tags", self.tags))):
            ttk.Label(det, text=label).grid(row=r, column=0, sticky="w", pady=2)
            ttk.Entry(det, textvariable=var, width=46).grid(row=r, column=1, sticky="we", padx=(8, 0), pady=2)
        ttk.Label(det, text="Comment = the prompt, Title = its first words. Separate tags with ;",
                  style="Hint.TLabel", wraplength=430
                  ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        det.columnconfigure(1, weight=1)

        # Go
        go = ttk.Frame(left)
        go.grid(row=5, column=0, sticky="we", pady=(10, 0))
        self.go_btn = ttk.Button(go, text="Make picture", style="Go.TButton", command=self.start)
        self.go_btn.pack(side="left")
        self.cancel_btn = ttk.Button(go, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=8)
        queue_btn = ttk.Button(go, text="Queue \u25be")
        queue_btn.configure(command=lambda: X.queue_menu(self, queue_btn))
        queue_btn.pack(side="left")
        self.status = ttk.Label(go, text="Ready.", style="Status.TLabel")
        self.status.pack(side="left", padx=8)
        self.progress = ttk.Progressbar(left, mode="determinate", maximum=100)
        self.progress.grid(row=6, column=0, sticky="we", pady=(8, 0))
        self.root.bind("<Control-Return>", lambda e: self.start())

    def build_right(self, parent):
        right = ttk.Frame(parent, padding=(6, 8, 12, 10))
        right.pack(side="left", fill="both", expand=True)

        pv = ttk.LabelFrame(right, text=" Picture ", padding=8)
        pv.pack(fill="both", expand=True)
        self.preview = ttk.Label(pv, text="Your picture will appear here.", anchor="center")
        self.preview.pack(fill="both", expand=True)
        self.preview.bind("<Configure>", lambda e: self.show_preview())
        self.preview.bind("<Double-Button-1>", lambda e: self.current and self.open_path(self.current))
        self.file_label = ttk.Label(pv, text="", style="Hint.TLabel")
        self.file_label.pack(fill="x", pady=(6, 0))
        btns = ttk.Frame(pv)
        btns.pack(fill="x", pady=(6, 0))
        wall_btn = ttk.Button(btns, text="Set as wallpaper \u25be")
        wall_btn.configure(command=lambda: X.wallpaper_menu(self, wall_btn))
        up_btn = ttk.Button(btns, text="Upscale \u25be")
        up_btn.configure(command=lambda: X.upscale_menu(self, up_btn))
        self.pic_buttons = [
            ttk.Button(btns, text="Open", command=lambda: self.open_path(self.current)),
            ttk.Button(btns, text="Show in folder", command=self.show_in_folder),
            ttk.Button(btns, text="Variation (same seed)", command=self.variation),
            ttk.Button(btns, text="Prompt from this", command=lambda: self.open_describe(self.current)),
            wall_btn, up_btn,
        ]
        for b in self.pic_buttons:
            b.pack(side="left", padx=(0, 6))
            b.state(["disabled"])
        btns2 = ttk.Frame(pv)
        btns2.pack(fill="x", pady=(6, 0))
        ttk.Button(btns2, text="Gallery...", command=lambda: X.open_gallery(self)).pack(side="left")
        ttk.Button(btns2, text="Open another picture...", command=self.open_other).pack(side="left", padx=6)

        ed = ttk.LabelFrame(right, text=" Details stored in this picture (EXIF / XMP) ", padding=8)
        ed.pack(fill="x", pady=(8, 0))
        self.meta_vars = {}
        rows = [("title", "Title"), ("subject", "Subject"), ("authors", "Authors"),
                ("copyright", "Copyright"), ("tags", "Tags"), ("date_taken", "Date taken"),
                ("software", "Program"), ("settings", "Settings")]
        for r, (key, label) in enumerate(rows):
            ttk.Label(ed, text=label).grid(row=r, column=0, sticky="w", pady=1)
            var = tk.StringVar()
            ttk.Entry(ed, textvariable=var).grid(row=r, column=1, sticky="we", padx=(8, 0), pady=1)
            self.meta_vars[key] = var
        ttk.Label(ed, text="Comment").grid(row=len(rows), column=0, sticky="nw", pady=(3, 0))
        self.comment = self.make_text(ed, height=4, wrap="word", font=("Segoe UI", 9))
        self.comment.grid(row=len(rows), column=1, sticky="we", padx=(8, 0), pady=(3, 0))
        ed.columnconfigure(1, weight=1)
        eb = ttk.Frame(ed)
        eb.grid(row=len(rows) + 1, column=0, columnspan=2, sticky="we", pady=(6, 0))
        self.save_meta_btn = ttk.Button(eb, text="Save details", command=self.save_meta)
        self.save_meta_btn.pack(side="left")
        self.revert_btn = ttk.Button(eb, text="Undo changes", command=lambda: self.load_meta(self.current))
        self.revert_btn.pack(side="left", padx=6)
        ttk.Label(eb, text="Date taken: YYYY:MM:DD HH:MM:SS. Saving never changes the picture itself.",
                  style="Hint.TLabel").pack(side="left", padx=6)
        for b in (self.save_meta_btn, self.revert_btn):
            b.state(["disabled"])

        lg = ttk.Frame(right)
        lg.pack(fill="x", pady=(8, 0))
        self.show_log = tk.BooleanVar(value=False)
        ttk.Checkbutton(lg, text="Show engine messages", variable=self.show_log,
                        command=self.toggle_log).pack(side="left")
        cb = ttk.Combobox(lg, textvariable=self.theme, values=THEMES, state="readonly", width=15)
        cb.pack(side="right")
        cb.bind("<<ComboboxSelected>>", lambda e: (self.apply_theme(), self.save_settings()))
        self.combos.append(cb)
        ttk.Label(lg, text="Theme").pack(side="right", padx=(0, 6))
        more_btn = ttk.Button(lg, text="More \u25be")
        more_btn.configure(command=lambda: X.more_menu(self, more_btn))
        more_btn.pack(side="right", padx=(0, 16))
        self.log = self.make_text(right, height=8, wrap="none", font=("Consolas", 8), state="disabled")

    # ---------------- theme ----------------
    def apply_theme(self):
        choice = self.theme.get()
        dark = choice == "Dark" or (choice == "Match Windows" and windows_uses_dark())
        st = self.style
        if dark:
            c = self.colors = DARK
            st.theme_use("clam")
            st.configure(".", background=c["bg"], foreground=c["fg"], fieldbackground=c["field"],
                         bordercolor=c["border"], darkcolor=c["bg"], lightcolor=c["bg"], troughcolor=c["field"],
                         focuscolor=c["accent"], selectbackground=c["select"], selectforeground=c["fg"],
                         insertcolor=c["fg"], arrowcolor=c["fg"])
            st.map(".", foreground=[("disabled", c["disabled"])], background=[("disabled", c["bg"])])
            st.configure("TButton", background=c["button"], lightcolor=c["button"], darkcolor=c["button"],
                         bordercolor=c["border"], padding=(8, 2))
            st.map("TButton", background=[("disabled", c["bg"]), ("pressed", c["pressed"]), ("active", c["button_active"])],
                   lightcolor=[("active", c["button_active"])], darkcolor=[("active", c["button_active"])],
                   foreground=[("disabled", c["disabled"])])
            st.configure("TLabelframe", background=c["bg"], bordercolor=c["border"], lightcolor=c["bg"], darkcolor=c["bg"])
            st.configure("TLabelframe.Label", background=c["bg"], foreground=c["fg"])
            for name in ("TEntry", "TSpinbox", "TCombobox"):
                st.configure(name, fieldbackground=c["field"], foreground=c["fg"], insertcolor=c["fg"],
                             bordercolor=c["border"], lightcolor=c["field"], darkcolor=c["field"],
                             background=c["button"], arrowcolor=c["fg"])
            st.map("TCombobox", fieldbackground=[("readonly", c["field"]), ("disabled", c["bg"])],
                   foreground=[("disabled", c["disabled"])], background=[("active", c["button_active"])],
                   selectbackground=[("readonly", c["field"])], selectforeground=[("readonly", c["fg"])])
            st.map("TEntry", fieldbackground=[("disabled", c["bg"])])
            for name in ("TCheckbutton", "TRadiobutton"):
                st.configure(name, background=c["bg"], foreground=c["fg"], indicatorbackground=c["field"],
                             indicatorforeground="#ffffff", upperbordercolor=c["border"], lowerbordercolor=c["border"])
                st.map(name, indicatorbackground=[("selected", c["accent"]), ("active", c["button_active"])],
                       background=[("active", c["bg"])])
            st.configure("Horizontal.TProgressbar", background=c["progress"], troughcolor=c["field"],
                         bordercolor=c["border"], lightcolor=c["progress"], darkcolor=c["progress"])
            st.configure("Vertical.TScrollbar", background=c["button"], troughcolor=c["bg"], bordercolor=c["border"],
                         lightcolor=c["button"], darkcolor=c["button"], arrowcolor=c["fg"])
            st.configure("Go.TButton", background=c["accent"], lightcolor=c["accent"], darkcolor=c["accent"],
                         foreground="#ffffff")
            st.map("Go.TButton", background=[("disabled", c["button"]), ("active", "#6aa0ff")],
                   foreground=[("disabled", c["disabled"])])
            self.root.configure(background=c["bg"])
        else:
            c = self.colors = LIGHT
            st.theme_use("vista" if "vista" in st.theme_names() else "clam")
            self.root.configure(background="SystemButtonFace")
        st.configure("Go.TButton", font=("Segoe UI", 11, "bold"), padding=(18, 8))
        st.configure("Hint.TLabel", foreground=c["hint"])
        st.configure("Status.TLabel", font=("Segoe UI", 10, "bold"))
        st.configure("Title.TLabel", font=("Segoe UI", 12, "bold"))
        self.texts = [t for t in self.texts if t.winfo_exists()]      # drop boxes from closed windows
        for t in self.texts:
            t.configure(background=c["text_bg"], foreground=c["text_fg"], insertbackground=c["text_fg"],
                        selectbackground=c["select"], selectforeground=c["text_fg"],
                        highlightbackground=c["border"], highlightcolor=c["accent"])
        for cb in self.combos:   # the drop-down lists are plain tk listboxes
            try:
                pop = self.root.tk.call("ttk::combobox::PopdownWindow", cb)
                self.root.tk.call(f"{pop}.f.l", "configure", "-background", c["text_bg"], "-foreground", c["text_fg"],
                                  "-selectbackground", c["select"] if dark else "SystemHighlight",
                                  "-selectforeground", c["text_fg"] if dark else "SystemHighlightText")
            except tk.TclError:
                pass
        self.update_size_label()
        self.set_title_bar(dark)

    def set_title_bar(self, dark):
        try:
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            value = ctypes.c_int(1 if dark else 0)
            for attribute in (20, 19):   # DWMWA_USE_IMMERSIVE_DARK_MODE (new and old Windows builds)
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value),
                                                              ctypes.sizeof(value)) == 0:
                    break
            # Nudge the window so Windows repaints the title bar now.
            w, h = self.root.winfo_width(), self.root.winfo_height()
            if w > 1:
                self.root.geometry(f"{w + 1}x{h}")
                self.root.after(10, lambda: self.root.geometry(f"{w}x{h}"))
        except Exception:
            pass

    # ---------------- restoring choices ----------------
    def restore_choices(self):
        s = self.settings
        self.out_dir.set(s.get("out_dir") or str(DEFAULT_OUT))
        self.fmt.set(s.get("fmt", "png"))
        self.save_txt.set(s.get("save_txt", True))
        self.authors.set(s.get("authors", ""))
        self.copyright.set(s.get("copyright", ""))
        self.tags.set(s.get("tags", "MOSS-Image"))
        self.upscale.set(s.get("upscale", True))
        self.split.set(s.get("split", True))
        self.tier.set(s.get("tier", TIERS[0]) if s.get("tier") in TIERS else TIERS[0])
        aspect = s.get("aspect", "16:9 wide")
        if aspect not in ASPECTS and aspect not in self.screen_opts:
            aspect = CUSTOM if aspect == CUSTOM else "16:9 wide"
        self.aspect.set(aspect)
        if aspect == CUSTOM:
            self.width.set(s.get("width", 1360))
            self.height.set(s.get("height", 768))
        self.refresh_model_list()
        self.apply_preset()
        self.on_seed_mode()

    # ---------------- model and mode ----------------
    def on_upscale_toggle(self):
        self.ai_check.state(["!disabled"] if UPSCALER.exists() and self.upscale.get() else ["disabled"])
        self.update_size_label()

    def refresh_advanced_note(self):
        p, a = self.profile, self.advanced_for()
        if p.get("cloud"):
            self.adv_note.configure(text="")
            return
        bits = []
        if a["steps"] != p["steps"] or a["cfg"] != p["cfg"]:
            bits.append(f"{a['steps']} steps, guidance {a['cfg']}")
        if a["sampler"] and a["sampler"] != (p["sampler"] or ""):
            bits.append(f"sampler {a['sampler']}")
        if a["negative"]:
            bits.append("negative prompt")
        if a["keep_breaks"]:
            bits.append("line breaks kept")
        if a["lora"]:
            bits.append(f"style {Path(a['lora']).stem} {a['lora_weight']}")
        self.adv_note.configure(text=("Advanced: " + ", ".join(bits) + "  (More > Advanced settings)") if bits else "")

    def refresh_model_list(self):
        self.model_box.configure(values=[self.model_label(k) for k in PROFILES])
        self.model_var.set(self.model_label(self.model_key))
        p = self.profile
        if p.get("cloud"):           # the key button sits beside the description
            self.model_note.grid_configure(columnspan=2)
            self.model_note.configure(wraplength=330)
            self.key_btn.grid(row=1, column=2, columnspan=2, sticky="ne", padx=(8, 0))
        else:
            self.key_btn.grid_remove()
            self.model_note.grid_configure(columnspan=4)
            self.model_note.configure(wraplength=440)
        self.model_note.configure(text=f"{p['note']}  License: {p['license']}.")
        for sp in self.spins:
            sp.configure(increment=p["align"])
        if hasattr(self, "adv_note"):
            self.refresh_advanced_note()

    def on_model_change(self):
        labels = [self.model_label(k) for k in PROFILES]
        chosen = self.model_box.current()
        if chosen < 0:
            return
        self.model_key = list(PROFILES)[chosen]
        self.refresh_model_list()
        if self.mode.get() == "edit" and not self.can_edit():
            self.mode.set("new")
            self.on_mode_change()
        if self.mode.get() == "edit" and self.edit_src:
            self.set_edit_src(self.edit_src)
        else:
            self.apply_preset()
        self.update_tokens()
        self.save_settings()
        if labels[chosen].endswith("(not downloaded)"):
            self.check_install()

    def can_edit(self, warn=True):
        if self.profile["edit"]:
            return True
        if warn:
            others = ", ".join(p["name"] for p in PROFILES.values() if p["edit"])
            messagebox.showinfo("Edit a picture", f"{self.profile['name']} can't edit pictures in this app yet: "
                                f"on this setup it runs out of memory. Use {others} for editing.")
        return False

    def on_mode_change(self):
        if self.mode.get() == "edit" and not self.can_edit():
            self.mode.set("new")
        editing = self.mode.get() == "edit"
        self.prompt_box.configure(text=(" What goes in the painted area? " if self.edit_mask else
                                        " What should change? ") if editing else " What do you want to see? ")
        self.go_btn.configure(text="Edit picture" if editing else "Make picture")
        if editing:
            self.edit_row.grid()
            for w in (self.aspect_box, self.tier_box, *self.spins):
                w.state(["disabled"])
            self.screen_box.grid_remove()
            if not self.edit_src and self.current:
                self.set_edit_src(self.current)
            self.update_size_label()
        else:
            self.edit_row.grid_remove()
            self.aspect_box.state(["!disabled", "readonly"])
            for w in self.spins:
                w.state(["!disabled"])
            self.apply_preset()

    def set_edit_src(self, path):
        """Use path as the picture to edit; the new picture keeps its size (within the limits)."""
        try:
            with Image.open(path) as im:
                sw, sh = im.size
        except Exception as e:
            messagebox.showerror("Edit a picture", f"Can't open that picture:\n{e}")
            return
        if self.edit_src != Path(path):
            self.edit_mask = None
        self.edit_src = Path(path)
        self.edit_label_refresh()
        align = self.profile["align"]
        if sw * sh <= MAX_PIXELS and sw % align == 0 and sh % align == 0 and max(sw, sh) <= MAX_SIDE:
            w, h = sw, sh
        else:
            w, h = fit_size(sw, sh, min(sw * sh, MAX_PIXELS), max_side=MAX_SIDE, align=align)
        self.width.set(w)
        self.height.set(h)
        self.update_size_label()

    def edit_label_refresh(self):
        name = self.edit_src.name if self.edit_src else "none chosen"
        name = name if len(name) <= 26 else name[:23] + "..."
        if self.edit_mask:
            name += "  + painted area"
        self.edit_label.configure(text=name)
        if self.mode.get() == "edit":
            self.prompt_box.configure(text=" What goes in the painted area? " if self.edit_mask
                                      else " What should change? ")

    def choose_edit_src(self):
        path = filedialog.askopenfilename(
            initialdir=self.out_dir.get() or str(DEFAULT_OUT), title="Choose the picture to edit",
            filetypes=[("Pictures", "*.png *.jpg *.jpeg *.webp"), ("All files", "*.*")])
        if path:
            self.set_edit_src(path)
            if self.edit_src:
                self.show_picture(self.edit_src)

    def use_shown_as_src(self):
        if self.current:
            self.set_edit_src(self.current)
        else:
            messagebox.showinfo("Edit a picture", "No picture is shown yet. Use Choose... to pick one.")

    # ---------------- prompt and tokens ----------------
    def load_tokenizer(self):
        try:
            from tokenizers import Tokenizer
            tok = Tokenizer.from_file(str(TOKENIZER))
            self.events.put(("tokenizer", tok))
        except Exception as err:
            self.events.put(("tokenizer", err))

    def on_prompt_change(self, _event=None):
        if self.prompt.edit_modified():
            self.prompt.edit_modified(False)
            if self.token_job:
                self.root.after_cancel(self.token_job)
            self.token_job = self.root.after(300, self.update_tokens)

    def count_tokens(self, text):
        if isinstance(self.tokenizer, Exception) or self.tokenizer is None:
            return None
        return len(self.tokenizer.encode(text, add_special_tokens=False).ids)

    def update_tokens(self):
        self.token_job = None
        text = tidy_prompt(self.prompt.get("1.0", "end"))
        n = self.count_tokens(text)
        if self.tokenizer is None:
            msg = "Tokens: loading the tokenizer..."
        elif n is None:
            msg = "Tokens: could not load the tokenizer"
        else:
            approx = "" if self.profile["exact_tokens"] else "about "
            msg = f"Tokens: {approx}{n:,}   Words: {len(text.split()):,}   Characters: {len(text):,}"
        self.token_label.configure(text=msg)

    # ---------------- screens ----------------
    def refresh_screens(self, initial=False):
        try:
            self.screens = detect_screens()
        except Exception:
            self.screens = []
        self.screen_opts = screen_options(self.screens)
        self.aspect_box.configure(values=list(ASPECTS) + list(self.screen_opts) + [CUSTOM])
        if self.screens:
            parts = ", ".join(f"{s['w']}x{s['h']}" for s in self.screens)
            n = len(self.screens)
            self.screens_label.configure(text=f"{n} screen{'s' if n != 1 else ''} found: {parts}")
        else:
            self.screens_label.configure(text="No screens could be detected.")
        if not initial and self.aspect.get().startswith("Screen") and self.aspect.get() not in self.screen_opts:
            self.aspect.set(CUSTOM)
            self.apply_preset()

    def screen_choice(self):
        return self.screen_opts.get(self.aspect.get())

    # ---------------- size ----------------
    def tier_index(self):
        return TIERS.index(self.tier.get()) if self.tier.get() in TIERS else 0

    def screen_fit(self, scr, tier=None):
        tier = self.tier_index() if tier is None else tier
        return fit_size(scr["W"], scr["H"], TIER_PIXELS[tier], align=self.profile["align"])

    def apply_preset(self):
        if self.mode.get() == "edit":
            self.update_size_label()
            return
        name = self.aspect.get()
        scr = self.screen_choice()
        if name == CUSTOM:
            self.tier_box.state(["disabled"])
        else:
            self.tier_box.state(["!disabled", "readonly"])
            if scr:
                w, h = self.screen_fit(scr)
            else:
                w, h = ASPECTS[name][self.tier_index()]
            self.width.set(w)
            self.height.set(h)
        if scr:
            self.screen_box.grid()
            self.split_check.state(["!disabled"] if len(scr["parts"]) > 1 else ["disabled"])
            self.ai_check.state(["!disabled"] if UPSCALER.exists() and self.upscale.get() else ["disabled"])
        else:
            self.screen_box.grid_remove()
        self.update_size_label()

    def on_manual_size(self):
        try:
            w, h = self.width.get(), self.height.get()
        except tk.TclError:
            return
        scr = self.screen_choice()
        if self.mode.get() == "edit" or (scr and self.screen_fit(scr) == (w, h)):
            self.update_size_label()
            return
        match = [(a, t) for a, sizes in ASPECTS.items() for t, s in enumerate(sizes) if s == (w, h)]
        if match:
            self.aspect.set(match[0][0])
            self.tier.set(TIERS[match[0][1]])
            self.tier_box.state(["!disabled", "readonly"])
        else:
            self.aspect.set(CUSTOM)
            self.tier_box.state(["disabled"])
        self.screen_box.grid_remove()
        self.update_size_label()

    def checked_size(self):
        """The size to draw, rounded to the model's multiple (16 or 32), or an error message."""
        if self.mode.get() == "edit" and not self.edit_src:
            return None, "Choose the picture to edit (above)."
        try:
            w, h = int(self.width.get()), int(self.height.get())
        except (tk.TclError, ValueError):
            return None, "Width and height must be whole numbers."
        a = self.profile["align"]
        longest = MAX_SPAN_SIDE if (self.screen_choice() and self.mode.get() != "edit") else MAX_SIDE
        rw, rh = (w + a // 2) // a * a, (h + a // 2) // a * a      # nearest multiple...
        if rw * rh > MAX_PIXELS or max(rw, rh) > longest:            # ...unless that goes past a limit:
            rw, rh = w // a * a, h // a * a                          # then round down instead
        w, h = rw, rh
        if not (256 <= w <= longest and 256 <= h <= longest):
            return None, f"Each side must be between 256 and {longest}."
        if w * h > MAX_PIXELS:
            return None, f"{w} x {h} is too big: the most is {MAX_PIXELS:,} pixels (like 3440 x 1440)."
        return (w, h), None

    def update_size_label(self):
        if not hasattr(self, "size_label"):
            return
        size, err = self.checked_size()
        if err:
            self.size_label.configure(text=err, foreground=self.colors["error"])
            return
        w, h = size
        try:
            exact = (w, h) == (self.width.get(), self.height.get())
        except tk.TclError:
            exact = False
        note = "" if exact else f"  (rounded to {w} x {h}: {self.profile['name']} needs multiples of {self.profile['align']})"
        if self.profile.get("cloud"):
            if self.mode.get() == "edit":
                size = C.size_for_pixels(w * h)
                text = (f"Google keeps the picture's shape, at {size} size  ·  about ${C.PRICE_USD[size]:.2f}  ·  "
                        "usually 10-40 s  ·  no seeds")
            else:
                scr = self.screen_choice()
                rw, rh = (scr["W"], scr["H"]) if scr else (w, h)
                aspect, size = C.nearest_aspect(rw, rh), C.size_for_pixels(w * h)
                ow, oh = C.SIZES_1K[aspect][0] * C.SCALE[size], C.SIZES_1K[aspect][1] * C.SCALE[size]
                cw, ch = C.crop_size(ow, oh, rw / rh)
                trim = "" if (cw, ch) == (ow, oh) else f", trimmed to {cw} x {ch}"
                after = ""
                if scr and self.upscale.get():
                    after = f", then scaled to {scr['W']} x {scr['H']}"
                text = (f"Google makes {ow} x {oh} ({aspect}, {size}){trim}{after}  ·  about ${C.PRICE_USD[size]:.2f}  ·  "
                        "usually 10-40 s  ·  no seeds")
            self.size_label.configure(text=text, foreground=self.colors["hint"])
            return
        text = f"{w} x {h}  ·  {w * h / 1e6:.2f} MP  ·  {estimate(w, h, self.profile, self.mode.get() == "edit")}{note}"
        scr = self.screen_choice()
        if self.mode.get() == "edit":
            text = f"Same shape as the picture being edited: {text}"
        elif scr:
            if (scr["W"], scr["H"]) == (w, h):
                text = f"Drawn at the screens' exact size: {text}"
            elif self.upscale.get():
                text = f"Drawn at {text}, then scaled up to {scr['W']} x {scr['H']}"
            else:
                text = f"Drawn and saved at {text} (the screens' shape, not their full size)"
        self.size_label.configure(text=text, foreground=self.colors["hint"])

    # ---------------- seed ----------------
    def last_picture(self):
        """(seed, w, h) of the last picture: from this app, or else the newest one in the output folder."""
        s = self.settings
        if s.get("last_seed"):
            return int(s["last_seed"]), int(s["last_w"]), int(s["last_h"])
        try:
            folder = Path(self.out_dir.get() or DEFAULT_OUT)
            files = sorted((p for p in folder.glob("*_seed*") if p.suffix.lower() in (".png", ".jpg", ".jpeg")),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            files = []
        for p in files:
            m = NAME_RE.search(p.name)
            if m:
                return int(m.group(3)), int(m.group(1)), int(m.group(2))
        return None

    def on_seed_mode(self):
        last = self.last_picture()
        if last:
            self.last_radio.configure(text=f"Same as the last picture (seed {last[0]}, {last[1]} x {last[2]})")
        else:
            self.last_radio.configure(text="Same as the last picture (none yet)")
        mode = self.seed_mode.get()
        if mode == "last" and last:
            self.select_model(self.settings.get("last_model"))
            shape, tier = self.settings.get("last_shape"), self.settings.get("last_tier")
            if shape in self.screen_opts and tier in TIERS and \
                    self.screen_fit(self.screen_opts[shape], TIERS.index(tier)) == last[1:]:
                self.aspect.set(shape)
                self.tier.set(tier)
                self.apply_preset()
            else:
                self.width.set(last[1])
                self.height.set(last[2])
                self.on_manual_size()
            self.seed_hint.configure(text="The size was set to match. Change a few words of the prompt to vary the picture.")
        elif mode == "number":
            self.seed_hint.configure(text="Use the same size as the picture that seed came from.")
        else:
            self.seed_hint.configure(text="")

    def chosen_seed(self):
        if self.profile.get("cloud"):
            return None, None          # Nano Banana has no seed setting
        mode = self.seed_mode.get()
        if mode == "last":
            last = self.last_picture()
            if not last:
                return None, "There is no last picture yet. Choose a new random picture."
            return last[0], None
        if mode == "number":
            text = self.seed_value.get().strip().lower().replace("seed", "").strip()
            if not text.isdigit() or len(text) > 18:
                return None, "A seed is a whole number, like 935799499."
            return int(text), None
        return random.randint(0, 2**30 - 1), None

    # ---------------- output ----------------
    def pick_folder(self):
        folder = filedialog.askdirectory(initialdir=self.out_dir.get() or str(DEFAULT_OUT),
                                         title="Choose where to save pictures")
        if folder:
            self.out_dir.set(str(Path(folder)))

    def open_path(self, path):
        if path and Path(path).exists():
            os.startfile(path)

    def show_in_folder(self):
        if self.current:
            subprocess.Popen(["explorer", "/select,", str(self.current)])

    # ---------------- making a picture ----------------
    def start(self):
        """Make picture: build a request from the window and run it now."""
        if self.job:
            return
        if getattr(self, "updating", False):
            messagebox.showinfo("MOSS Image Studio", "An update is being installed. Try again when it's done.")
            return
        spec = self.collect_spec()
        if spec:
            self.launch(spec, interactive=True)

    # ---------------- requests ----------------
    def advanced_for(self, key=None):
        """Advanced settings for a model, with the model's defaults filled in."""
        key = key or self.model_key
        p = PROFILES[key]
        saved = self.settings.get("advanced", {}).get(key, {})
        return dict(steps=saved.get("steps") or p["steps"], cfg=saved.get("cfg") or p["cfg"],
                    negative=saved.get("negative", ""), sampler=saved.get("sampler") or p["sampler"] or "",
                    keep_breaks=saved.get("keep_breaks", False), lora=saved.get("lora", ""),
                    lora_weight=saved.get("lora_weight", 0.8))

    def collect_spec(self, quiet=False):
        """Everything about one picture, read from the window and checked. None if something is missing."""
        def problem(title, text):
            if not quiet:
                messagebox.showinfo(title, text)
            return None
        editing = self.mode.get() == "edit"
        mask = self.edit_mask if (editing and self.edit_mask and Path(self.edit_mask).exists()) else None
        if editing and not mask and not self.can_edit(warn=not quiet):
            return None
        if mask and self.profile.get("cloud"):
            return problem("Paint an area", "Nano Banana can't use a painted area. Clear the painted area, or "
                                            "choose a model on this PC.")
        if not quiet and not self.check_install(edit=editing and not mask):
            return None
        adv = self.advanced_for()
        raw_prompt = self.prompt.get("1.0", "end").strip()
        prompt = raw_prompt if adv["keep_breaks"] else tidy_prompt(raw_prompt)
        if not prompt:
            return problem("MOSS Image Studio", "Describe what should change first." if editing
                           else "Type what you want to see first.")
        if editing and (not self.edit_src or not self.edit_src.exists()):
            return problem("Edit a picture", "Choose the picture to edit first.")
        size, err = self.checked_size()
        if err:
            return problem("Picture size", err)
        seed, err = self.chosen_seed()
        if err:
            return problem("Seed", err)
        if self.seed_mode.get() == "random" and not self.profile.get("cloud"):
            seed = "random"                    # chosen when it runs, so queued copies differ
        out_dir = Path(self.out_dir.get().strip() or DEFAULT_OUT)
        scr = None if editing else self.screen_choice()
        return dict(model=self.model_key, editing=editing, mask=str(mask) if mask else None,
                    prompt=prompt, w=size[0], h=size[1], seed=seed,
                    edit_src=str(self.edit_src) if editing else None, out_dir=str(out_dir),
                    fmt=self.fmt.get(), save_txt=self.save_txt.get(), authors=self.authors.get().strip(),
                    copyright=self.copyright.get().strip(), tags=M.split_tags(self.tags.get()),
                    screen=scr, shape=self.aspect.get(), tier=self.tier.get(),
                    upscale=bool(scr) and self.upscale.get(), split=bool(scr) and self.split.get(),
                    ai_upscale=bool(scr) and self.upscale.get() and self.ai_upscale.get() and UPSCALER.exists(),
                    adv=adv, label="")

    def memory_ok(self, spec):
        """Warn before a big job if Windows can't promise enough memory (see memory_advice)."""
        profile = PROFILES[spec["model"]]
        if profile.get("cloud"):
            return True
        files_gb = sum(p.stat().st_size for p in profile_files(profile, spec["editing"]) if p.exists()) / 2**30
        need = files_gb * 1.1 + 3 * spec["w"] * spec["h"] / 1e6 + 1
        ram, commit = memory_free_gb()
        if commit is None or commit >= need:
            return True
        return messagebox.askyesno(
            "Low memory", f"Windows can only promise programs {commit:.0f} GB more memory right now, and "
            f"{profile['name']} at {spec['w']} x {spec['h']} usually needs about {need:.0f} GB, so it may fail.\n\n"
            "Restarting Windows usually fixes this if it has been running for days. "
            "A smaller size or FLUX needs less.\n\nTry anyway?")

    def launch(self, spec, interactive=True):
        """Run one request. Returns True if it started."""
        profile = PROFILES[spec["model"]]
        if interactive and not self.memory_ok(spec):
            return False
        try:
            Path(spec["out_dir"]).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return self.launch_failed(spec, f"Can't use the output folder: {e}", interactive)
        if profile.get("cloud"):
            return self.launch_cloud(spec, interactive)
        if not SD_CLI.exists() or any(not p.exists() for p in profile_files(profile, spec["editing"] and not spec["mask"])):
            return self.launch_failed(spec, f"{profile['name']}'s files aren't all downloaded.", interactive)
        editing, mask = spec["editing"], spec["mask"]
        adv, w, h = spec["adv"], spec["w"], spec["h"]
        seed = random.randint(0, 2**30 - 1) if spec["seed"] == "random" else int(spec["seed"])
        scr = spec["screen"]
        use_edit = editing and not mask                  # instruction edit (reference picture)
        if use_edit:
            steps, cfg = profile["edit"]["steps"], profile["edit"]["cfg"]
        else:
            steps, cfg = adv["steps"], adv["cfg"]
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        tail = ""
        if scr:
            tail = f"_{'screens' if len(scr['parts']) > 1 else 'screen'}{scr['W']}x{scr['H']}"
        if editing:
            tail = "_inpaint" if mask else "_edit"
        final = Path(spec["out_dir"]) / f"{profile['prefix']}_{stamp}_{w}x{h}_seed{seed}{tail}.{spec['fmt']}"
        prompt = spec["prompt"]
        tokens = self.count_tokens(prompt)
        when = datetime.now()
        settings = (f"{w}x{h}, seed {seed}, steps {steps}, cfg {cfg}, "
                    f"tokens {tokens if tokens is not None else 'unknown'}, model {profile['name']}")
        if adv["negative"] and not use_edit:
            settings += f", negative: {adv['negative']}"
        if adv["lora"]:
            settings += f", style LoRA {adv['lora']} at {adv['lora_weight']}"
        if scr:
            names = " + ".join(f"{p['name']} {p['w']}x{p['h']}" for p in scr["parts"])
            settings += f", for screens: {names}" + (f", scaled up to {scr['W']}x{scr['H']}" if spec["upscale"] else "")
            if spec["ai_upscale"]:
                settings += " with AI upscaling"
        src_name = Path(spec["edit_src"]).name if editing else None
        if editing:
            settings += f", {'inpaint (painted area)' if mask else 'edit'} of {src_name}"
        meta = dict(title=("Edit: " if editing else "") + auto_title(prompt),
                    subject=f"Edited from {src_name}" if editing else "", comment=prompt,
                    authors=spec["authors"], copyright=spec["copyright"], tags=spec["tags"], software=SOFTWARE,
                    date_taken=when.strftime("%Y:%m:%d %H:%M:%S"), settings=settings)
        job = dict(prompt=prompt, w=w, h=h, seed=seed, final=final, raw=final.with_name(final.stem + ".engine.png"),
                   tokens=tokens, save_txt=spec["save_txt"], when=when, meta=meta, profile=profile,
                   steps=steps, cfg=cfg, edit_src=src_name, screen=scr, shape=spec["shape"], tier=spec["tier"],
                   model=spec["model"], upscale=spec["upscale"], split=spec["split"], ai_upscale=spec["ai_upscale"],
                   cancelled=False, spec=spec)
        cmd = [str(SD_CLI)]
        files = dict(profile["files"])
        if use_edit:
            files.update(profile["edit"]["files"])
        if adv["lora"]:
            files["--lora-model-dir"] = "loras"
        for option, rel in files.items():
            cmd += [option, str(MODELS / rel)]
        cmd += ["--cfg-scale", str(cfg), "--steps", str(steps),
                "-H", str(h), "-W", str(w), "-s", str(seed), "--offload-to-cpu"]
        sampler = adv["sampler"] if not use_edit else profile["sampler"]
        if sampler:
            cmd += ["--sampling-method", sampler]
        if w * h > TILE_ABOVE:
            cmd.append("--vae-tiling")
        engine_prompt = prompt
        if use_edit:
            cmd += ["-r", spec["edit_src"]]
            engine_prompt += profile["edit"]["prompt_suffix"]
        elif mask:
            cmd += ["-i", spec["edit_src"], "--mask", mask, "--strength", "1.0"]
        if adv["negative"] and not use_edit:
            cmd += ["-n", adv["negative"]]
        if adv["lora"]:
            engine_prompt += f"<lora:styles/{Path(adv['lora']).stem}:{adv['lora_weight']}>"
        cmd += ["-p", engine_prompt, "-o", str(job["raw"])]
        self.begin_ui(f"Starting: {w} x {h}, seed {seed}..." + (f"  [{spec['label']}]" if spec["label"] else ""))
        try:
            job["proc"] = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            self.finish_ui()
            return self.launch_failed(spec, f"Could not start the image engine: {e}", interactive)
        self.job = job
        threading.Thread(target=self.run_job, args=(job,), daemon=True).start()
        return True

    def begin_ui(self, text):
        self.save_settings()
        self.log_lines = []
        self.set_log("")
        self.go_btn.state(["disabled"])
        self.cancel_btn.state(["!disabled"])
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.status.configure(text=text)
        self.started = time.time()

    def launch_failed(self, spec, message, interactive):
        if interactive:
            messagebox.showerror("MOSS Image Studio", message)
        else:
            self.status.configure(text=f"Skipped: {message}")
            self.queue_result(spec, None, message)
        return False

    def launch_cloud(self, spec, interactive):
        """Nano Banana: send the request to Google in a worker thread."""
        api_key = C.find_key(self.settings)
        if not api_key and interactive:
            self.edit_api_key()
            api_key = C.find_key(self.settings)
        if not api_key:
            return self.launch_failed(spec, "Nano Banana needs your Gemini API key.", interactive)
        profile = PROFILES[spec["model"]]
        editing, scr, w, h = spec["editing"], spec["screen"], spec["w"], spec["h"]
        if editing:
            with Image.open(spec["edit_src"]) as im:
                sw, sh = im.size
            aspect, size, target = None, C.size_for_pixels(sw * sh), None
            label = f"{size}_edit"
        else:
            rw, rh = (scr["W"], scr["H"]) if scr else (w, h)
            aspect, size, target = C.nearest_aspect(rw, rh), C.size_for_pixels(w * h), rw / rh
            label = f"{aspect.replace(':', 'x')}_{size}"
            if scr:
                label += f"_{'screens' if len(scr['parts']) > 1 else 'screen'}{scr['W']}x{scr['H']}"
        price = C.PRICE_USD[size]
        if not self.settings.get("cloud_ok"):
            if not interactive:
                return self.launch_failed(spec, "Nano Banana hasn't been confirmed in the window yet.", False)
            sent = "your prompt and the picture" if editing else "your prompt"
            if not messagebox.askokcancel(
                    "Nano Banana (Google cloud)",
                    f"This sends {sent} to Google, and each picture is billed to your Gemini API key "
                    f"(about ${price:.2f} at {size} size).\n\nPictures from Google carry its invisible SynthID "
                    "watermark. Continue? (You won't be asked again.)"):
                return False
            self.settings["cloud_ok"] = True
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        final = Path(spec["out_dir"]) / f"{profile['prefix']}_{stamp}_{label}.{spec['fmt']}"
        when = datetime.now()
        prompt = spec["prompt"]
        src_name = Path(spec["edit_src"]).name if editing else None
        meta = dict(title=("Edit: " if editing else "") + auto_title(prompt),
                    subject=f"Edited from {src_name}" if editing else "", comment=prompt,
                    authors=spec["authors"], copyright=spec["copyright"], tags=spec["tags"], software=SOFTWARE,
                    date_taken=when.strftime("%Y:%m:%d %H:%M:%S"), settings="")
        job = dict(prompt=prompt, w=w, h=h, seed="n/a", final=final, raw=final.with_name(final.stem + ".engine.png"),
                   tokens=self.count_tokens(prompt), save_txt=spec["save_txt"], when=when, meta=meta, profile=profile,
                   steps="n/a", cfg="n/a", edit_src=src_name, edit_path=spec["edit_src"] if editing else None,
                   screen=scr, shape=spec["shape"], tier=spec["tier"], model=spec["model"],
                   upscale=spec["upscale"], split=spec["split"], ai_upscale=spec["ai_upscale"],
                   cancelled=False, cloud=True, proc=None, api_key=api_key,
                   aspect=aspect, size=size, target=target, price=price, spec=spec)
        self.begin_ui("Sending to Google..." + (f"  [{spec['label']}]" if spec["label"] else ""))
        self.job = job
        threading.Thread(target=self.run_job, args=(job,), daemon=True).start()
        return True

    def run_cloud(self, job):
        """Worker thread for Nano Banana: ask Google, trim to shape, then finish the files like a local picture."""
        say = lambda msg: self.events.put(("status", msg))
        say("Waiting for Google (usually 10-40 seconds)...")
        try:
            source = mime = None
            if job["edit_path"]:
                source = Path(job["edit_path"]).read_bytes()
                mime = "image/jpeg" if Path(job["edit_path"]).suffix.lower() in (".jpg", ".jpeg") else "image/png"
            picture = C.generate(job["api_key"], job["prompt"], aspect=job["aspect"], size=job["size"],
                                 image_bytes=source, image_mime=mime or "image/png")
        except C.CloudError as e:
            self.events.put(("done", (job, None, str(e))))
            return
        if job["cancelled"]:
            self.events.put(("done", (job, None, None)))
            return
        try:
            import io
            with Image.open(io.BytesIO(picture)) as im:
                im = im.convert("RGB")
                if job["target"]:                     # trim to the exact shape asked for
                    cw, ch = C.crop_size(im.width, im.height, job["target"])
                    left, top = (im.width - cw) // 2, (im.height - ch) // 2
                    im = im.crop((left, top, left + cw, top + ch))
                im.save(job["raw"], "PNG", compress_level=1)
                job["w"], job["h"] = im.size
            settings = (f"{job['w']}x{job['h']}, Google {job['size']}" + (f" {job['aspect']}" if job["aspect"] else "")
                        + f", about ${job['price']:.2f}, tokens {job['tokens'] if job['tokens'] is not None else 'unknown'}, "
                        f"model {job['profile']['name']}")
            if job["screen"]:
                names = " + ".join(f"{p['name']} {p['w']}x{p['h']}" for p in job["screen"]["parts"])
                settings += f", for screens: {names}" + (f", scaled to {job['screen']['W']}x{job['screen']['H']}"
                                                         if job["upscale"] else "")
            if job["edit_src"]:
                settings += f", edit of {job['edit_src']}"
            job["meta"]["settings"] = settings
            self.events.put(("saving", job))
            files = finish_files(job, say)
            self.events.put(("done", (job, files, None)))
        except Exception as e:
            self.events.put(("done", (job, None, f"Google's picture could not be saved: {e}")))

    def run_job(self, job):
        """Worker thread: read the engine's output, then finish the files."""
        if job.get("cloud"):
            self.run_cloud(job)
            return
        proc, buf = job["proc"], b""
        while True:
            chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for part in parts:
                line = part.decode("utf-8", "replace").replace("\x1b[K", "").rstrip()
                if line:
                    self.events.put(("line", line))
        code = proc.wait()
        if job["cancelled"]:
            job["raw"].unlink(missing_ok=True)
            self.events.put(("done", (job, None, None)))
            return
        if code != 0 or not job["raw"].exists():
            job["raw"].unlink(missing_ok=True)
            self.events.put(("done", (job, None, f"The engine stopped (code {code}).")))
            return
        self.events.put(("saving", job))
        try:
            files = finish_files(job, lambda msg: self.events.put(("status", msg)))
            self.events.put(("done", (job, files, None)))
        except Exception as e:
            self.events.put(("done", (job, None, f"The picture was made but could not be saved: {e}")))

    def poll_events(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "line":
                    self.on_engine_line(data)
                elif kind == "status":
                    self.status.configure(text=data)
                elif kind == "saving":
                    if data is self.job:
                        self.cancel_btn.state(["disabled"])
                        self.progress.configure(mode="indeterminate")
                        self.progress.start(12)
                elif kind == "done":
                    self.on_job_done(*data)
                elif kind == "remote":
                    X.remote_request(self, data)
                elif kind == "tokenizer":
                    self.tokenizer = data
                    self.update_tokens()
        except queue.Empty:
            pass
        if getattr(self, "remote_server", None):
            X.remote_tick(self)
        if self.job:
            secs = int(time.time() - self.started)
            self.root.title(f"MOSS Image Studio - working {secs // 60}:{secs % 60:02d}")
        self.root.after(100, self.poll_events)

    def on_engine_line(self, line):
        m = PROGRESS_RE.search(line)
        if m and ("it/s" in line or "s/it" in line):
            step, total = int(m.group(1)), int(m.group(2))
            if self.progress.cget("mode") != "determinate":
                self.progress.stop()
                self.progress.configure(mode="determinate")
            self.progress["value"] = 100 * step / max(total, 1)
            self.status.configure(text=f"Drawing: step {step} of {total}  ({m.group(3)} {m.group(4)})")
            return
        if "|" in line and ("GB/s" in line or "MB/s" in line):
            return  # model loading progress: too chatty for the log
        if "get_learned_condition" in line or "loading tensors" in line:
            self.status.configure(text="Loading the models and reading the prompt...")
        elif "decoding" in line and "latent" in line:
            self.status.configure(text="Finishing the picture...")
        self.log_lines.append(line)
        self.log_lines = self.log_lines[-300:]
        if self.show_log.get():
            self.append_log(line)

    def on_job_done(self, job, files, error):
        if job is not self.job:          # an earlier run that was cancelled
            return
        self.job = None
        self.finish_ui()
        spec = job.get("spec") or {}
        unattended = spec.get("queued")
        if job["cancelled"]:
            self.status.configure(text="Cancelled.")
            self.queue_result(spec, None, "cancelled")
            self.queue_running = False             # Cancel also stops the queue
            return
        if error:
            self.status.configure(text="No picture was made." if files is None else "Problem saving.")
            log = "\n".join(self.log_lines)
            if MEMORY_ERROR_RE.search(log):
                self.status.configure(text="Not enough memory for this size.")
                message = memory_advice(job)
            elif job.get("cloud"):
                message = f"Sorry: {error}"
            else:
                tail = "\n".join(self.log_lines[-12:])
                message = f"Sorry: {error}\n\nLast engine messages:\n{tail}"
            self.queue_result(spec, None, message.splitlines()[0])
            if not unattended:
                messagebox.showerror("MOSS Image Studio", message)
            self.after_job()
            return
        secs = int(time.time() - self.started)
        extra = f" ({len(files) - 1} screen pictures too)" if len(files) > 1 else ""
        self.status.configure(text=f"Done in {secs // 60}:{secs % 60:02d}.{extra}")
        self.progress["value"] = 100
        if not job.get("cloud"):     # cloud pictures have no seed to reuse
            self.settings.update(last_seed=job["seed"], last_w=job["w"], last_h=job["h"],
                                 last_shape=job["shape"], last_tier=job["tier"], last_model=job["model"])
        self.save_settings()
        self.on_seed_mode()
        self.show_picture(files[0])
        self.queue_result(spec, files[0], None)
        self.after_job()

    # ---------------- queue ----------------
    def queue_add(self, spec, label="", group=None):
        spec = dict(spec, label=label, group=group)
        self.queue.append(spec)
        if group:
            self.groups.setdefault(group, dict(total=0, results=[]))["total"] += 1
        return spec

    def queue_start(self):
        if self.queue and not self.job:
            self.queue_running = True
            self.run_next_queued()

    def run_next_queued(self):
        if self.job or not self.queue:
            return
        spec = self.queue.pop(0)
        spec["queued"] = True
        if not self.launch(spec, interactive=False):
            self.after_job()

    def after_job(self):
        if self.queue_running and self.queue:
            self.root.after(800, self.run_next_queued)
        else:
            self.queue_running = False

    def queue_result(self, spec, file, error):
        if file:
            X.remote_note_picture(self, file, (spec or {}).get("prompt", ""))
        if not spec:
            return
        self.queue_done.append((spec, file, error))
        self.queue_done = self.queue_done[-200:]
        group = self.groups.get(spec.get("group"))
        if group is not None:
            group["results"].append((spec, file, error))
            if len(group["results"]) >= group["total"]:
                X.show_group(self, spec["group"])
        X.refresh_queue_window(self)

    def finish_ui(self):
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.progress["value"] = 0
        self.go_btn.state(["!disabled"])
        self.cancel_btn.state(["disabled"])
        self.root.title("MOSS Image Studio")

    def cancel(self):
        job = self.job
        if job:
            job["cancelled"] = True       # its worker thread removes the unfinished file
            try:
                if job.get("proc"):
                    job["proc"].kill()
            except OSError:
                pass
            self.job = None               # ready for a new picture right away
            self.finish_ui()
            self.status.configure(text="Cancelled.")

    # ---------------- picture and details ----------------
    def show_picture(self, path):
        self.current = Path(path)
        self.file_label.configure(text=str(self.current))
        for b in self.pic_buttons + [self.save_meta_btn, self.revert_btn]:
            b.state(["!disabled"])
        self.show_preview()
        self.load_meta(self.current)

    def show_preview(self):
        if not self.current or not self.current.exists():
            return
        box_w = max(self.preview.winfo_width() - 4, 100)
        box_h = max(self.preview.winfo_height() - 4, 100)
        try:
            with Image.open(self.current) as im:
                im.draft("RGB", (box_w, box_h))
                im = im.convert("RGB")
                im.thumbnail((box_w, box_h), Image.Resampling.LANCZOS)
                self.preview_img = ImageTk.PhotoImage(im)
            self.preview.configure(image=self.preview_img, text="")
        except Exception as e:
            self.preview.configure(image="", text=f"Can't show this picture: {e}")

    def load_meta(self, path):
        if not path:
            return
        try:
            meta = M.read(path)
        except Exception as e:
            messagebox.showerror("Details", f"Could not read this picture's details:\n{e}")
            return
        for key, var in self.meta_vars.items():
            value = meta.get(key, "")
            var.set("; ".join(value) if key == "tags" else value)
        self.comment.delete("1.0", "end")
        self.comment.insert("1.0", meta.get("comment", ""))

    def save_meta(self):
        if not self.current:
            return
        meta = {k: v.get().strip() for k, v in self.meta_vars.items()}
        meta["tags"] = M.split_tags(meta["tags"])
        meta["comment"] = self.comment.get("1.0", "end").strip()
        if meta["date_taken"] and not re.fullmatch(r"\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2}", meta["date_taken"]):
            messagebox.showwarning("Date taken", "Use the form YYYY:MM:DD HH:MM:SS, e.g. 2026:11:13 21:30:00, or leave it empty.")
            return
        try:
            M.write(self.current, meta)
        except PermissionError:
            messagebox.showerror("Details", "The picture is open in another program. Close it there and try again.")
            return
        except Exception as e:
            messagebox.showerror("Details", f"Could not save the details:\n{e}")
            return
        self.load_meta(self.current)
        self.status.configure(text="Details saved.")

    def open_other(self):
        path = filedialog.askopenfilename(
            initialdir=self.out_dir.get() or str(DEFAULT_OUT), title="Open a picture",
            filetypes=[("Pictures", "*.png *.jpg *.jpeg"), ("All files", "*.*")])
        if path:
            self.show_picture(path)

    def settings_from(self, path):
        """(prompt, w, h, seed, model key) from a picture's details or file name."""
        meta = M.read(path)
        text = meta.get("settings", "")
        model = next((k for k, p in PROFILES.items() if f"model {p['name']}" in text), None)
        if model is None:   # older pictures: tell by the file name, else MOSS
            model = next((k for k, p in PROFILES.items() if Path(path).name.startswith(p["prefix"] + "_")), None)
        m = SETTINGS_RE.search(text) or NAME_RE.search(Path(path).name)
        if not m:
            return meta.get("comment", ""), None, None, None, model
        return meta.get("comment", ""), int(m.group(1)), int(m.group(2)), int(m.group(3)), model

    def select_model(self, key):
        if key in PROFILES and key != self.model_key:
            self.model_key = key
            self.refresh_model_list()
            self.update_tokens()

    def apply_reuse(self, path):
        try:
            prompt, w, h, seed, model = self.settings_from(path)
        except Exception as e:
            messagebox.showerror("Use an earlier picture", f"Could not read that picture:\n{e}")
            return
        if self.mode.get() == "edit":
            self.mode.set("new")
            self.on_mode_change()
        self.select_model(model)
        if prompt:
            self.prompt.delete("1.0", "end")
            self.prompt.insert("1.0", prompt)
            self.update_tokens()
        if seed is not None:
            picked = None
            m = re.search(r"_screens?(\d+)x(\d+)", Path(path).name)
            if m:   # a screen picture: pick the same screen choice if those screens are still connected
                for label, o in self.screen_opts.items():
                    for t in range(len(TIER_PIXELS)):
                        if (o["W"], o["H"]) == (int(m.group(1)), int(m.group(2))) and self.screen_fit(o, t) == (w, h):
                            picked = (label, TIERS[t])
            if picked:
                self.aspect.set(picked[0])
                self.tier.set(picked[1])
                self.apply_preset()
            else:
                self.width.set(w)
                self.height.set(h)
                self.on_manual_size()
            self.seed_value.set(str(seed))
            self.seed_mode.set("number")
            self.seed_hint.configure(text=f"Seed and size ({w} x {h}) come from that picture. "
                                          "Change a few words to make a variation.")
        else:
            self.seed_hint.configure(text="That picture has no seed in its details or name; only the prompt was loaded.")
        self.prompt.focus_set()

    def reuse_from_file(self):
        path = filedialog.askopenfilename(
            initialdir=self.out_dir.get() or str(DEFAULT_OUT), title="Use the prompt, size and seed of a picture",
            filetypes=[("Pictures", "*.png *.jpg *.jpeg"), ("All files", "*.*")])
        if path:
            self.apply_reuse(path)
            self.show_picture(path)

    def variation(self):
        if self.current:
            self.apply_reuse(self.current)

    # ---------------- engine messages ----------------
    def toggle_log(self):
        if self.show_log.get():
            self.log.pack(fill="x", pady=(4, 0))
            self.set_log("\n".join(self.log_lines))
        else:
            self.log.pack_forget()

    def set_log(self, text):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def append_log(self, line):
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---------------- small helper windows ----------------
    def make_window(self, title):
        win = tk.Toplevel(self.root)
        win.title(title)
        win.transient(self.root)
        try:
            win.iconbitmap(str(RESOURCES / "moss.ico"))
        except tk.TclError:
            pass
        if self.colors is DARK:
            win.configure(background=DARK["bg"])
        frame = ttk.Frame(win, padding=14)
        frame.pack(fill="both", expand=True)
        return win, frame

    def poll_worker(self, q, handlers, win):
        """Pass messages from a worker thread to handlers on the window's thread."""
        try:
            while True:
                kind, data = q.get_nowait()
                handlers[kind](data)
        except queue.Empty:
            pass
        if win.winfo_exists():
            win.after(150, lambda: self.poll_worker(q, handlers, win))

    # ---------------- picture to prompt ----------------
    def describer_choices(self):
        """[(label, kind, model, projector)]; kind is 'local', 'cloud' or 'get-local'."""
        choices = [(label, "local", m, p) for label, m, p in D.local_options(ROOT)]
        if not choices:
            choices.append(("Qwen on this PC: get the local describer (about 360 MB)", "get-local", None, None))
        key_note = "" if C.find_key(self.settings) else ", needs API key"
        choices.append((f"Google Gemini 3.5 Flash-Lite (cloud, {D.CLOUD_PRICE}{key_note})", "cloud", None, None))
        return choices

    def open_describe(self, picture=None):
        win, f = self.make_window("Write a prompt from a picture")
        state = dict(picture=Path(picture) if picture else None, thumb=None, busy=False)
        q = queue.Queue()

        top = ttk.Frame(f)
        top.grid(row=0, column=0, columnspan=3, sticky="we")
        thumb = ttk.Label(top, text="No picture chosen.", anchor="center", width=40)
        thumb.pack(side="left")
        side = ttk.Frame(top)
        side.pack(side="left", fill="y", padx=(12, 0))
        name = ttk.Label(side, text="", style="Hint.TLabel", wraplength=260)
        name.pack(anchor="w")
        ttk.Label(side, text="Works with photos and AI pictures. The prompt describes what is visible, in a "
                             "form all four models understand.", style="Hint.TLabel", wraplength=260
                  ).pack(anchor="w", pady=(6, 10))

        def set_picture(path):
            try:
                with Image.open(path) as im:
                    im = im.convert("RGB")
                    im.thumbnail((300, 220), Image.Resampling.LANCZOS)
                    state["thumb"] = ImageTk.PhotoImage(im)
            except Exception as e:
                messagebox.showerror("Write a prompt", f"Can't open that picture:\n{e}", parent=win)
                return
            state["picture"] = Path(path)
            thumb.configure(image=state["thumb"], text="")
            name.configure(text=Path(path).name)

        def choose():
            path = filedialog.askopenfilename(parent=win, initialdir=self.out_dir.get() or str(DEFAULT_OUT),
                                              title="Choose a picture to describe",
                                              filetypes=[("Pictures", "*.png *.jpg *.jpeg *.webp *.bmp"),
                                                         ("All files", "*.*")])
            if path:
                set_picture(path)

        ttk.Button(side, text="Choose a picture...", command=choose).pack(anchor="w")
        ttk.Button(side, text="Use the picture shown",
                   command=lambda: self.current and set_picture(self.current)).pack(anchor="w", pady=(4, 0))

        ttk.Label(f, text="Describer").grid(row=1, column=0, sticky="w", pady=(12, 0))
        choices = self.describer_choices()
        who = tk.StringVar()
        who_box = ttk.Combobox(f, textvariable=who, state="readonly", width=58, values=[c[0] for c in choices])
        who_box.grid(row=1, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=(12, 0))
        saved = self.settings.get("describer", "")
        who_box.current(next((i for i, c in enumerate(choices) if c[0].split(" (")[0] == saved), 0))
        ttk.Label(f, text="Length").grid(row=2, column=0, sticky="w", pady=(6, 0))
        length = tk.StringVar(value=self.settings.get("describe_length", "Detailed"))
        lf = ttk.Frame(f)
        lf.grid(row=2, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=(6, 0))
        for opt, tip in (("Detailed", "80-150 words"), ("Short", "30-60 words")):
            ttk.Radiobutton(lf, text=f"{opt} ({tip})", value=opt, variable=length).pack(side="left", padx=(0, 12))

        go = ttk.Button(f, text="Write prompt", style="Go.TButton")
        go.grid(row=3, column=0, columnspan=2, sticky="w", pady=(12, 0))
        status = ttk.Label(f, text="", style="Hint.TLabel", wraplength=420)
        status.grid(row=3, column=2, sticky="w", pady=(12, 0))
        result = self.make_text(f, height=9, width=70, wrap="word", font=("Segoe UI", 10))
        result.grid(row=4, column=0, columnspan=3, sticky="nsew", pady=(10, 0))
        self.apply_theme_to_text(result)
        f.rowconfigure(4, weight=1)
        f.columnconfigure(2, weight=1)

        def use():
            text = result.get("1.0", "end").strip()
            if not text:
                return
            if self.mode.get() == "edit":
                self.mode.set("new")
                self.on_mode_change()
            self.prompt.delete("1.0", "end")
            self.prompt.insert("1.0", text)
            self.update_tokens()
            win.destroy()

        def copy():
            self.root.clipboard_clear()
            self.root.clipboard_append(result.get("1.0", "end").strip())
            status.configure(text="Copied.")

        bottom = ttk.Frame(f)
        bottom.grid(row=5, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(bottom, text="Use as prompt", command=use).pack(side="left")
        ttk.Button(bottom, text="Copy", command=copy).pack(side="left", padx=6)
        ttk.Button(bottom, text="Close", command=win.destroy).pack(side="left")

        def worker(kind, model, projector, picture, style):
            try:
                if kind == "get-local":
                    q.put(("status", "Getting the local describer..."))
                    U.update_engine(ROOT, U.install_item("llama"), lambda m: q.put(("status", m)))
                    q.put(("installed", None))
                    return
                if kind == "local":
                    text = D.describe_local(ROOT, model, projector, picture, style)
                else:
                    text = D.describe_cloud(C.find_key(self.settings), picture, style)
                q.put(("text", text))
            except Exception as e:
                q.put(("error", str(e)))

        def finish():
            state["busy"] = False
            go.state(["!disabled"])
            win.configure(cursor="")

        def on_text(text):
            finish()
            result.delete("1.0", "end")
            result.insert("1.0", text)
            secs = time.time() - state["t0"]
            status.configure(text=f"Done in {secs:.0f} s. Edit it if you like, then Use as prompt.")

        def on_error(msg):
            finish()
            status.configure(text="")
            messagebox.showerror("Write a prompt", msg, parent=win)

        def on_installed(_):
            finish()
            new = self.describer_choices()
            who_box.configure(values=[c[0] for c in new])
            choices[:] = new
            who_box.current(0)
            status.configure(text="The local describer is ready. Click Write prompt.")

        def start():
            if state["busy"]:
                return
            label, kind, model, projector = choices[who_box.current()]
            if kind != "get-local" and not state["picture"]:
                messagebox.showinfo("Write a prompt", "Choose a picture first.", parent=win)
                return
            if kind == "local" and self.job:
                messagebox.showinfo("Write a prompt", "A picture is being made right now, and the local describer "
                                    "needs the graphics card too. Try again when it's done, or use Gemini.", parent=win)
                return
            if kind == "cloud":
                if not C.find_key(self.settings):
                    self.edit_api_key()
                    if not C.find_key(self.settings):
                        return
                if not self.settings.get("describe_cloud_ok"):
                    if not messagebox.askokcancel("Google Gemini", "This sends the picture to Google, billed to "
                                                  f"your Gemini API key ({D.CLOUD_PRICE}). Continue? "
                                                  "(You won't be asked again.)", parent=win):
                        return
                    self.settings["describe_cloud_ok"] = True
            if kind == "get-local" and not messagebox.askokcancel(
                    "Local describer", "This downloads llama.cpp (about 360 MB) from its official GitHub "
                    "releases into the llm-bin folder. It uses the Qwen vision files you already have. "
                    "Continue?", parent=win):
                return
            self.settings["describer"] = label.split(" (")[0]
            self.settings["describe_length"] = length.get()
            self.save_settings()
            state["busy"], state["t0"] = True, time.time()
            go.state(["disabled"])
            win.configure(cursor="watch")
            status.configure(text="Looking at the picture..." if kind != "get-local" else "")
            threading.Thread(target=worker, args=(kind, model, projector, state["picture"], length.get()),
                             daemon=True).start()

        go.configure(command=start)
        if picture:
            set_picture(picture)
        self.poll_worker(q, dict(text=on_text, error=on_error, status=lambda m: status.configure(text=m),
                                 installed=on_installed), win)

    def apply_theme_to_text(self, t):
        c = self.colors
        t.configure(background=c["text_bg"], foreground=c["text_fg"], insertbackground=c["text_fg"],
                    selectbackground=c["select"], selectforeground=c["text_fg"],
                    highlightbackground=c["border"], highlightcolor=c["accent"])

    # ---------------- updates ----------------
    def open_updates(self):
        win, f = self.make_window("Check for updates")
        q = queue.Queue()
        state = dict(items=[], vars=[], busy=False)
        ttk.Label(f, text="Compares your model files with their sources on Hugging Face (by checksum), and the "
                          "engines with their latest releases on GitHub. Nothing is changed until you choose "
                          "Install.", wraplength=620, justify="left").grid(row=0, column=0, columnspan=2, sticky="w")
        listing = ttk.Frame(f)
        listing.grid(row=1, column=0, columnspan=2, sticky="we", pady=(10, 0))
        status = ttk.Label(f, text="Checking...", style="Hint.TLabel", wraplength=620)
        status.grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))
        bar = ttk.Progressbar(f, mode="indeterminate")
        bar.grid(row=3, column=0, columnspan=2, sticky="we", pady=(6, 0))
        buttons = ttk.Frame(f)
        buttons.grid(row=4, column=0, columnspan=2, sticky="e", pady=(12, 0))
        install_btn = ttk.Button(buttons, text="Install selected")
        install_btn.pack(side="left")
        undo_btn = ttk.Button(buttons, text="Undo last image-engine update")
        undo_btn.pack(side="left", padx=6)
        ttk.Button(buttons, text="Check again", command=lambda: check()).pack(side="left")
        ttk.Button(buttons, text="Close", command=win.destroy).pack(side="left", padx=(6, 0))

        def busy(on):
            state["busy"] = on
            self.updating = on
            for b in (install_btn, undo_btn):
                b.state(["disabled"] if on else ["!disabled"])
            if on:
                bar.start(12)
            else:
                bar.stop()
                bar["value"] = 0
                undo_btn.state(["!disabled"] if U.can_undo(ROOT, "sd") else ["disabled"])

        def show(items):
            for w in listing.winfo_children():
                w.destroy()
            state["items"], state["vars"] = items, []
            for r, it in enumerate(items):
                if "key" in it:
                    label = U.ENGINES[it["key"]]["name"]
                    mine = it.get("mine") or "not recorded"
                    what = {"current": f"up to date ({mine})",
                            "update": f"update: {mine} -> {it.get('tag')} ({U.fmt_size(it['size'])})",
                            "error": f"couldn't check: {it['detail']}"}[it["status"]]
                else:
                    label = it["rel"]
                    what = {"current": "up to date", "update": f"changed at the source ({U.fmt_size(it['size'])})",
                            "error": f"couldn't check: {it['detail']}"}.get(it["status"], it["status"])
                var = tk.BooleanVar(value=it["status"] == "update")
                cb = ttk.Checkbutton(listing, text=label, variable=var)
                cb.grid(row=r, column=0, sticky="w")
                if it["status"] != "update":
                    cb.state(["disabled"])
                ttk.Label(listing, text=what, style="Hint.TLabel").grid(row=r, column=1, sticky="w", padx=(16, 0))
                state["vars"].append(var)
            n = sum(1 for it in items if it["status"] == "update")
            status.configure(text=("Everything is up to date." if not n else
                                   f"{n} update{'s' if n != 1 else ''} available. Tick what to install.")
                             + "  Models not downloaded yet aren't listed; use download-models.ps1 for those.")

        def check_worker():
            try:
                say = lambda m: q.put(("status", m))
                items = U.check_engines(ROOT, say) + U.check_models(ROOT, say)
                q.put(("checked", items))
            except Exception as e:
                q.put(("failed", f"Couldn't check for updates: {e}"))

        def check():
            if state["busy"]:
                return
            busy(True)
            status.configure(text="Checking (the first check reads every model file once, about half a minute)...")
            threading.Thread(target=check_worker, daemon=True).start()

        def engine_selftest(key):
            """True if the newly installed engine still works."""
            if key == "sd":
                out = Path(tempfile.mkdtemp(prefix="moss-selftest-"))
                cmd = [sys.executable, "--selftest", str(out)] if FROZEN else \
                      [sys.executable, str(Path(__file__).resolve()), "--selftest", str(out)]
                subprocess.run(cmd, timeout=600, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                try:
                    return json.loads((out / "selftest-result.json").read_text(encoding="utf-8")).get("ok", False)
                except Exception:
                    return False
            options = D.local_options(ROOT)
            if not options:
                return (ROOT / "llm-bin" / "llama-mtmd-cli.exe").exists()
            sample = Path(tempfile.mkdtemp(prefix="moss-selftest-")) / "red.png"
            Image.new("RGB", (256, 256), (200, 30, 30)).save(sample)
            try:
                return bool(D.describe_local(ROOT, options[0][1], options[0][2], sample, "Short"))
            except Exception:
                return False

        def install_worker(chosen):
            say = lambda m: q.put(("status", m))
            done, problems = [], []
            for it in chosen:
                try:
                    if "key" in it:
                        U.update_engine(ROOT, it, say)
                        say(f"Testing the new {U.ENGINES[it['key']]['name']}...")
                        if engine_selftest(it["key"]):
                            done.append(f"{U.ENGINES[it['key']]['name']} -> {it['tag']}")
                        else:
                            U.undo_engine(ROOT, it["key"])
                            problems.append(f"{U.ENGINES[it['key']]['name']}: the new version failed its test, "
                                            "so the previous version was put back.")
                    else:
                        U.update_model(ROOT, it, say)
                        done.append(it["rel"])
                except Exception as e:
                    problems.append(f"{it.get('rel') or U.ENGINES[it['key']]['name']}: {e}")
            q.put(("installed", (done, problems)))

        def install():
            chosen = [it for it, var in zip(state["items"], state["vars"]) if var.get() and it["status"] == "update"]
            if not chosen or state["busy"]:
                return
            if self.job:
                messagebox.showinfo("Updates", "A picture is being made. Install when it's done.", parent=win)
                return
            total = sum(it["size"] for it in chosen)
            if not messagebox.askokcancel("Updates", f"Download and install {len(chosen)} update"
                                          f"{'s' if len(chosen) != 1 else ''} ({U.fmt_size(total)})? Engines are "
                                          "tested after installing and put back if the test fails.", parent=win):
                return
            busy(True)
            threading.Thread(target=install_worker, args=(chosen,), daemon=True).start()

        def on_installed(result):
            done, problems = result
            busy(False)
            self.refresh_model_list()
            text = ("Installed:\n  " + "\n  ".join(done) if done else "Nothing was installed.")
            if problems:
                text += "\n\nProblems:\n  " + "\n  ".join(problems)
            (messagebox.showwarning if problems else messagebox.showinfo)("Updates", text, parent=win)
            check()

        def undo():
            if state["busy"] or not U.can_undo(ROOT, "sd"):
                return
            if not messagebox.askokcancel("Undo", "Put the previous image engine back?", parent=win):
                return
            try:
                U.undo_engine(ROOT, "sd")
                messagebox.showinfo("Undo", "The previous image engine is back.", parent=win)
            except Exception as e:
                messagebox.showerror("Undo", str(e), parent=win)
            check()

        install_btn.configure(command=install)
        undo_btn.configure(command=undo)
        self.poll_worker(q, dict(status=lambda m: status.configure(text=m),
                                 checked=lambda items: (busy(False), show(items)),
                                 failed=lambda m: (busy(False), status.configure(text=m)),
                                 installed=on_installed), win)
        check()

    def on_close(self):
        if self.job and not messagebox.askyesno("MOSS Image Studio", "A picture is still being made. Stop it and close?"):
            return
        self.cancel()
        X.stop_remote(self)
        self.save_settings()
        self.root.destroy()


def selftest(root, out_dir):
    """MOSS Image Studio.exe --selftest <folder>: make one small picture through the normal window,
    write selftest-result.json into the folder, then close. Leaves the normal settings untouched."""
    global SETTINGS_FILE
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE = out / "selftest-settings.json"
    result = {"version": __version__, "frozen": FROZEN, "root": str(ROOT), "ok": False}
    app = Studio(root, check=False)
    app.out_dir.set(str(out))
    app.prompt.insert("1.0", 'Self-test: a red apple on a wooden table, "OK" written in chalk')
    app.aspect.set(CUSTOM)
    app.width.set(256)
    app.height.set(256)
    app.on_manual_size()
    app.fmt.set("png")
    app.seed_mode.set("number")
    app.seed_value.set("42")

    def finish(status):
        result["status"] = status
        if app.current:
            meta = M.read(app.current)
            result.update(file=app.current.name, title=meta["title"], comment=meta["comment"],
                          settings=meta["settings"], ok=meta["comment"].startswith("Self-test"))
        (out / "selftest-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        root.destroy()

    def wait():
        if app.job is None and app.go_btn.instate(["!disabled"]):
            finish(app.status.cget("text"))
        else:
            root.after(500, wait)

    def go():
        result["engine_found"] = app.missing_files() == []
        if not result["engine_found"]:
            finish("engine or models missing")
            return
        app.start()
        root.after(1000, wait)

    root.after(1500, go)


def wallpaper_of_the_day(root):
    """Run by Windows Task Scheduler: make today's wallpaper without showing the window, set it, and quit."""
    root.withdraw()
    app = Studio(root, check=False)
    log = HERE / "wallpaper-of-the-day.log"

    def done(message):
        try:
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M}  {message}\n")
        except OSError:
            pass
        root.after(1500, root.destroy)

    if app.missing_files(X.wotd_settings(app)["model"]):
        done("Skipped: the chosen model isn't downloaded.")
        return
    root.after(1500, lambda: X.run_wotd(app, done))


def main():
    try:  # sharp text on high-DPI screens
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    if len(sys.argv) == 3 and sys.argv[1] == "--selftest":
        selftest(root, sys.argv[2])
    elif len(sys.argv) == 2 and sys.argv[1] == "--wallpaper-of-the-day":
        wallpaper_of_the_day(root)
    else:
        Studio(root)
    root.mainloop()


if __name__ == "__main__":
    main()
