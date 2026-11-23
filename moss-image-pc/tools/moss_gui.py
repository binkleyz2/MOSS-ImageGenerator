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
import threading
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

import moss_meta as M

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SD_CLI = ROOT / "bin" / "sd-cli.exe"
MODELS = ROOT / "models"
TOKENIZER = MODELS / "tokenizer_qwen3.5.json"
SETTINGS_FILE = HERE / "gui_settings.json"
DEFAULT_OUT = ROOT / "outputs"

STEPS, CFG = 50, 3.0
MAX_PIXELS = 3440 * 1440          # largest picture the model makes here (about 5 MP)
MAX_SIDE = 3440                   # longest side for normal pictures
MAX_SPAN_SIDE = 6144              # longest side when drawing for several screens side by side
TILE_ABOVE = 2_000_000            # bigger pictures are decoded in tiles to save video memory
SOFTWARE = "MOSS-Image SFT (stable-diffusion.cpp)"

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

def estimate(w, h):
    """Rough time on this PC (RTX 4070 Ti SUPER), from earlier runs."""
    secs = 8 + 55 * ((w * h / 1e6) / 1.04) ** 1.25
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


def fit_size(full_w, full_h, target_pixels, max_side=MAX_SPAN_SIDE):
    """Largest drawing size with the shape of full_w x full_h, multiples of 16, within the limits."""
    target = min(target_pixels, MAX_PIXELS, full_w * full_h)
    r = full_w / full_h
    h = (target / r) ** 0.5
    w = h * r
    if w > max_side:
        w, h = max_side, max_side / r
    if h > max_side:
        h, w = max_side, max_side * r
    gw = max(256, int(w // 16) * 16)
    gh = max(256, int(round(gw / r / 16)) * 16)
    while gw * gh > min(MAX_PIXELS, target * 1.02) and (gw > 256 or gh > 256):
        if gh > 256 and gh * r >= gw:
            gh -= 16
        elif gw > 256:
            gw -= 16
        else:
            gh -= 16
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
    lines = ["Prompt:", job["prompt"], "",
             f"Tokens: {tokens} (Qwen 3.5 tokenizer, prompt text only)",
             f"Words: {len(job['prompt'].split()):,}", f"Characters: {len(job['prompt']):,}", "",
             f"Image: {final.name}", f"Size: {job['w']} x {job['h']}"]
    if job.get("screen"):
        lines.append(f"Made for: {job['shape']}" + (" (scaled up)" if job["upscale"] else ""))
    lines += [f"Seed: {job['seed']}", f"Steps: {STEPS}", f"CFG scale: {CFG}", f"Made: {job['when']:%Y-%m-%d %H:%M:%S}"]
    try:
        final.with_suffix(".txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def finish_files(job, say):
    """After the engine is done: scale up for screens, save with details, split per screen. Returns files."""
    raw, final, scr = job["raw"], job["final"], job.get("screen")
    if scr and job["upscale"] and (scr["W"], scr["H"]) != (job["w"], job["h"]):
        say(f"Scaling up to {scr['W']} x {scr['H']}...")
        with Image.open(raw) as im:
            big = im.convert("RGB").resize((scr["W"], scr["H"]), Image.Resampling.LANCZOS)
        big.save(raw, "PNG", compress_level=1)
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
    def __init__(self, root):
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

        root.title("MOSS Image Studio")
        root.geometry(self.settings.get("geometry", "1360x900"))
        root.minsize(1100, 850)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        try:
            root.iconbitmap(default=str(HERE / "moss.ico"))
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
                 geometry=self.root.geometry())
        try:
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
        self.prompt = self.make_text(box, height=8, width=58, wrap="word", font=("Segoe UI", 10), undo=True)
        sb = ttk.Scrollbar(box, command=self.prompt.yview)
        self.prompt.configure(yscrollcommand=sb.set)
        self.prompt.grid(row=0, column=0, columnspan=3, sticky="nsew")
        sb.grid(row=0, column=3, sticky="ns")
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        self.token_label = ttk.Label(box, text="Tokens: loading the tokenizer...", style="Hint.TLabel")
        self.token_label.grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Button(box, text="Clear", command=lambda: (self.prompt.delete("1.0", "end"), self.update_tokens())
                   ).grid(row=1, column=1, sticky="e", pady=(6, 0), padx=4)
        ttk.Button(box, text="Use an earlier picture...", command=self.reuse_from_file
                   ).grid(row=1, column=2, columnspan=2, sticky="e", pady=(6, 0))
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
            sp.bind("<KeyRelease>", lambda e: self.on_manual_size())
        self.size_label = ttk.Label(size, text="", style="Hint.TLabel", wraplength=430)
        self.size_label.grid(row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))

        self.screen_box = ttk.Frame(size)
        self.screen_box.grid(row=4, column=0, columnspan=4, sticky="w", pady=(4, 0))
        self.upscale = tk.BooleanVar(value=True)
        self.split = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.screen_box, text="Scale up to the screens' full resolution", variable=self.upscale,
                        command=self.update_size_label).pack(anchor="w")
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
        self.pic_buttons = [
            ttk.Button(btns, text="Open picture", command=lambda: self.open_path(self.current)),
            ttk.Button(btns, text="Show in folder", command=self.show_in_folder),
            ttk.Button(btns, text="Make a variation (same seed)", command=self.variation),
        ]
        for b in self.pic_buttons:
            b.pack(side="left", padx=(0, 6))
            b.state(["disabled"])
        ttk.Button(btns, text="Open another picture...", command=self.open_other).pack(side="right")

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
        self.apply_preset()
        self.on_seed_mode()

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
            msg = f"Tokens: {n:,}   Words: {len(text.split()):,}   Characters: {len(text):,}"
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

    def apply_preset(self):
        name = self.aspect.get()
        scr = self.screen_choice()
        if name == CUSTOM:
            self.tier_box.state(["disabled"])
        else:
            self.tier_box.state(["!disabled", "readonly"])
            if scr:
                w, h = fit_size(scr["W"], scr["H"], TIER_PIXELS[self.tier_index()])
            else:
                w, h = ASPECTS[name][self.tier_index()]
            self.width.set(w)
            self.height.set(h)
        if scr:
            self.screen_box.grid()
            self.split_check.state(["!disabled"] if len(scr["parts"]) > 1 else ["disabled"])
        else:
            self.screen_box.grid_remove()
        self.update_size_label()

    def on_manual_size(self):
        try:
            w, h = self.width.get(), self.height.get()
        except tk.TclError:
            return
        scr = self.screen_choice()
        if scr and fit_size(scr["W"], scr["H"], TIER_PIXELS[self.tier_index()]) == (w, h):
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
        """The size to draw, rounded to multiples of 16, or an error message."""
        try:
            w, h = int(self.width.get()), int(self.height.get())
        except (tk.TclError, ValueError):
            return None, "Width and height must be whole numbers."
        w, h = (w + 8) // 16 * 16, (h + 8) // 16 * 16
        longest = MAX_SPAN_SIDE if self.screen_choice() else MAX_SIDE
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
        note = "" if exact else f"  (rounded to {w} x {h}: sizes are multiples of 16)"
        text = f"{w} x {h}  ·  {w * h / 1e6:.2f} MP  ·  {estimate(w, h)}{note}"
        scr = self.screen_choice()
        if scr:
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
            files = sorted((p for p in folder.glob("moss_*") if p.suffix.lower() in (".png", ".jpg", ".jpeg")),
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
            shape, tier = self.settings.get("last_shape"), self.settings.get("last_tier")
            if shape in self.screen_opts and tier in TIERS and \
                    fit_size(self.screen_opts[shape]["W"], self.screen_opts[shape]["H"], TIER_PIXELS[TIERS.index(tier)]) == last[1:]:
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
        if self.job:
            return
        prompt = tidy_prompt(self.prompt.get("1.0", "end"))
        if not prompt:
            messagebox.showinfo("MOSS Image Studio", "Type what you want to see first.")
            return
        size, err = self.checked_size()
        if err:
            messagebox.showwarning("Picture size", err)
            return
        seed, err = self.chosen_seed()
        if err:
            messagebox.showwarning("Seed", err)
            return
        out_dir = Path(self.out_dir.get().strip() or DEFAULT_OUT)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Save to", f"Can't use that folder:\n{e}")
            return
        w, h = size
        scr = self.screen_choice()
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        tail = ""
        if scr:
            tail = f"_{'screens' if len(scr['parts']) > 1 else 'screen'}{scr['W']}x{scr['H']}"
        final = out_dir / f"moss_{stamp}_{w}x{h}_seed{seed}{tail}.{self.fmt.get()}"
        tokens = self.count_tokens(prompt)
        when = datetime.now()
        settings = (f"{w}x{h}, seed {seed}, steps {STEPS}, cfg {CFG}, "
                    f"tokens {tokens if tokens is not None else 'unknown'}, model MOSS-Image SFT int8")
        if scr:
            names = " + ".join(f"{p['name']} {p['w']}x{p['h']}" for p in scr["parts"])
            settings += f", for screens: {names}" + (f", scaled up to {scr['W']}x{scr['H']}" if self.upscale.get() else "")
        meta = dict(title=auto_title(prompt), subject="", comment=prompt,
                    authors=self.authors.get().strip(), copyright=self.copyright.get().strip(),
                    tags=M.split_tags(self.tags.get()), software=SOFTWARE,
                    date_taken=when.strftime("%Y:%m:%d %H:%M:%S"), settings=settings)
        job = dict(prompt=prompt, w=w, h=h, seed=seed, final=final, raw=final.with_name(final.stem + ".engine.png"),
                   tokens=tokens, save_txt=self.save_txt.get(), when=when, meta=meta,
                   screen=scr, shape=self.aspect.get(), tier=self.tier.get(),
                   upscale=bool(scr) and self.upscale.get(), split=bool(scr) and self.split.get(), cancelled=False)
        cmd = [str(SD_CLI),
               "--diffusion-model", str(MODELS / "moss_image_sft_int8_convrot.safetensors"),
               "--llm", str(MODELS / "Qwen3.5-9B-Q4_K_M.gguf"),
               "--vae", str(MODELS / "flux2-vae.safetensors"),
               "--tokenizer", str(TOKENIZER),
               "--cfg-scale", str(CFG), "--steps", str(STEPS),
               "-H", str(h), "-W", str(w), "-s", str(seed), "--offload-to-cpu"]
        if w * h > TILE_ABOVE:
            cmd.append("--vae-tiling")
        cmd += ["-p", prompt, "-o", str(job["raw"])]

        self.save_settings()
        self.log_lines = []
        self.set_log("")
        self.go_btn.state(["disabled"])
        self.cancel_btn.state(["!disabled"])
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.status.configure(text=f"Starting: {w} x {h}, seed {seed}...")
        self.started = time.time()
        try:
            job["proc"] = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            self.finish_ui()
            messagebox.showerror("MOSS Image Studio", f"Could not start the image engine:\n{e}")
            return
        self.job = job
        threading.Thread(target=self.run_job, args=(job,), daemon=True).start()

    def run_job(self, job):
        """Worker thread: read the engine's output, then finish the files."""
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
                elif kind == "tokenizer":
                    self.tokenizer = data
                    self.update_tokens()
        except queue.Empty:
            pass
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
        if job["cancelled"]:
            self.status.configure(text="Cancelled.")
            return
        if error:
            self.status.configure(text="No picture was made." if files is None else "Problem saving.")
            tail = "\n".join(self.log_lines[-12:])
            messagebox.showerror("MOSS Image Studio", f"Sorry: {error}\n\nLast engine messages:\n{tail}")
            return
        secs = int(time.time() - self.started)
        extra = f" ({len(files) - 1} screen pictures too)" if len(files) > 1 else ""
        self.status.configure(text=f"Done in {secs // 60}:{secs % 60:02d}.{extra}")
        self.progress["value"] = 100
        self.settings.update(last_seed=job["seed"], last_w=job["w"], last_h=job["h"],
                             last_shape=job["shape"], last_tier=job["tier"])
        self.save_settings()
        self.on_seed_mode()
        self.show_picture(files[0])

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
        """(prompt, w, h, seed) from a picture's details or file name."""
        meta = M.read(path)
        m = SETTINGS_RE.search(meta.get("settings", "")) or NAME_RE.search(Path(path).name)
        if not m:
            return meta.get("comment", ""), None, None, None
        return meta.get("comment", ""), int(m.group(1)), int(m.group(2)), int(m.group(3))

    def apply_reuse(self, path):
        try:
            prompt, w, h, seed = self.settings_from(path)
        except Exception as e:
            messagebox.showerror("Use an earlier picture", f"Could not read that picture:\n{e}")
            return
        if prompt:
            self.prompt.delete("1.0", "end")
            self.prompt.insert("1.0", prompt)
            self.update_tokens()
        if seed is not None:
            picked = None
            m = re.search(r"_screens?(\d+)x(\d+)", Path(path).name)
            if m:   # a screen picture: pick the same screen choice if those screens are still connected
                for label, o in self.screen_opts.items():
                    for t, px in enumerate(TIER_PIXELS):
                        if (o["W"], o["H"]) == (int(m.group(1)), int(m.group(2))) and fit_size(o["W"], o["H"], px) == (w, h):
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

    def on_close(self):
        if self.job and not messagebox.askyesno("MOSS Image Studio", "A picture is still being made. Stop it and close?"):
            return
        self.cancel()
        self.save_settings()
        self.root.destroy()


def main():
    try:  # sharp text on high-DPI screens
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    Studio(root)
    root.mainloop()


if __name__ == "__main__":
    main()
