"""Extra windows for MOSS Image Studio: enhancer, upscaling, wallpaper (and wallpaper of the day), queue and
compare, gallery, paint-to-edit, MOSS layout boxes, advanced settings and phone access.

Every function takes the running Studio window (`app`) and uses its settings, models and job runner.
"""
import json
import os
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, ttk

from PIL import Image, ImageDraw, ImageTk

import moss_cloud as C
import moss_describe as D
import moss_meta as M
import moss_platform as P

SAMPLERS = ["", "euler", "euler_a", "heun", "dpm2", "dpm++2s_a", "dpm++2m", "dpm++2mv2", "ipndm", "ipndm_v",
            "lcm", "ddim_trailing", "tcd"]
MOSS_NEGATIVE = "nsfw, worst quality, bad anatomy, watermark, jpeg artifact"    # MOSS's own recommendation


def G():
    """The main module (imported lazily to avoid a circular import)."""
    return sys.modules.get("moss_gui") or sys.modules["__main__"]


def run_in_thread(app, work, done, error=None):
    """Run work() in a thread; call done(result) or error(message) back on the window's thread."""
    q = queue.Queue()

    def worker():
        try:
            q.put(("ok", work()))
        except Exception as e:
            q.put(("err", str(e)))

    def poll():
        try:
            kind, value = q.get_nowait()
        except queue.Empty:
            app.root.after(150, poll)
            return
        if kind == "ok":
            done(value)
        elif error:
            error(value)
        else:
            messagebox.showerror("MOSS Image Studio", value)

    threading.Thread(target=worker, daemon=True).start()
    app.root.after(150, poll)


def gpu_free(app, what):
    if app.job:
        messagebox.showinfo("MOSS Image Studio", f"A picture is being made right now; {what} needs the graphics "
                                                 "card too. Try again when it's done.")
        return False
    return True


def base_spec(app, model, prompt, w, h, screen=None, shape="", tier="", label="", seed="random"):
    """A request built from settings rather than the window (wallpaper of the day, phone access)."""
    s = app.settings
    return dict(model=model, editing=False, mask=None, prompt=prompt, w=w, h=h, seed=seed, edit_src=None,
                out_dir=s.get("out_dir") or str(G().DEFAULT_OUT), fmt=s.get("fmt", "png"),
                save_txt=s.get("save_txt", True), authors=s.get("authors", ""), copyright=s.get("copyright", ""),
                tags=M.split_tags(s.get("tags", "MOSS-Image")), screen=screen, shape=shape, tier=tier,
                upscale=bool(screen), split=bool(screen) and len(screen["parts"]) > 1,
                ai_upscale=bool(screen) and G().UPSCALER.exists(), adv=app.advanced_for(model), label=label,
                text_check=s.get("text_check", True) and bool(D.quoted_text(prompt)))


def size_for(app, model, shape_or_screen, tier_index):
    """(w, h, screen option or None) for a model, a shape name (or screen option label) and a size tier."""
    gm = G()
    align = gm.PROFILES[model]["align"]
    if shape_or_screen in app.screen_opts:
        o = app.screen_opts[shape_or_screen]
        w, h = gm.fit_size(o["W"], o["H"], gm.TIER_PIXELS[tier_index], align=align)
        return w, h, o
    w, h = gm.ASPECTS.get(shape_or_screen, gm.ASPECTS["16:9 wide"])[tier_index]
    w, h = (w + align // 2) // align * align, (h + align // 2) // align * align
    if w * h > gm.MAX_PIXELS or max(w, h) > gm.MAX_SIDE:
        w, h = w // align * align, h // align * align
    return w, h, None


# ---------------- prompt enhancer ----------------

def open_enhance(app):
    idea = app.prompt.get("1.0", "end").strip()
    win, f = app.make_window("Improve the prompt")
    ttk.Label(f, text="Turns a short idea into a detailed prompt (subject, setting, lighting, mood and style). "
                      "Longer prompts usually give better pictures, especially with MOSS.",
              wraplength=560, justify="left").grid(row=0, column=0, columnspan=3, sticky="w")
    ttk.Label(f, text="Idea").grid(row=1, column=0, sticky="nw", pady=(10, 0))
    src = app.make_text(f, height=3, width=66, wrap="word", font=(P.UI_FONT, 10))
    src.grid(row=1, column=1, columnspan=2, sticky="we", pady=(10, 0))
    src.insert("1.0", idea)
    app.apply_theme_to_text(src)
    options = [(label + " (private, free)", "local", m) for label, m in D.enhance_options(G().ROOT)]
    key_note = "" if C.find_key(app.settings) else ", needs API key"
    options.append((f"Google Gemini 3.5 Flash-Lite (cloud, about $0.001{key_note})", "cloud", None))
    ttk.Label(f, text="Using").grid(row=2, column=0, sticky="w", pady=(8, 0))
    who = ttk.Combobox(f, state="readonly", width=52, values=[o[0] for o in options])
    who.grid(row=2, column=1, columnspan=2, sticky="w", pady=(8, 0))
    who.current(0)
    length = tk.StringVar(value=app.settings.get("describe_length", "Detailed"))
    lf = ttk.Frame(f)
    lf.grid(row=3, column=1, columnspan=2, sticky="w", pady=(6, 0))
    for opt in ("Detailed", "Short"):
        ttk.Radiobutton(lf, text=opt, value=opt, variable=length).pack(side="left", padx=(0, 12))
    go = ttk.Button(f, text="Improve", style="Go.TButton")
    go.grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))
    status = ttk.Label(f, text="" if options[0][1] == "local" else
                       "Tip: the local enhancer needs llama.cpp; get it from 'From a picture...' first.",
                       style="Hint.TLabel", wraplength=380)
    status.grid(row=4, column=2, sticky="w", pady=(10, 0))
    out = app.make_text(f, height=8, width=66, wrap="word", font=(P.UI_FONT, 10))
    out.grid(row=5, column=0, columnspan=3, sticky="nsew", pady=(10, 0))
    app.apply_theme_to_text(out)
    f.columnconfigure(2, weight=1)
    f.rowconfigure(5, weight=1)

    def improve():
        text = src.get("1.0", "end").strip()
        if not text:
            return
        label, kind, model = options[who.current()]
        if kind == "local" and not gpu_free(app, "the enhancer"):
            return
        if kind == "cloud" and not C.find_key(app.settings):
            app.edit_api_key()
            if not C.find_key(app.settings):
                return
        go.state(["disabled"])
        status.configure(text="Writing...")
        t0 = time.time()
        style = length.get()
        work = (lambda: D.enhance_local(G().ROOT, model, text, style)) if kind == "local" else \
               (lambda: D.enhance_cloud(C.find_key(app.settings), text, style))

        def done(result):
            go.state(["!disabled"])
            out.delete("1.0", "end")
            out.insert("1.0", result)
            status.configure(text=f"Done in {time.time() - t0:.0f} s. Edit it if you like, then Use.")

        def fail(msg):
            go.state(["!disabled"])
            status.configure(text="")
            messagebox.showerror("Improve the prompt", msg, parent=win)
        run_in_thread(app, work, done, fail)

    def use():
        text = out.get("1.0", "end").strip()
        if text:
            app.prompt.delete("1.0", "end")
            app.prompt.insert("1.0", text)
            app.update_tokens()
            win.destroy()

    go.configure(command=improve)
    b = ttk.Frame(f)
    b.grid(row=6, column=0, columnspan=3, sticky="e", pady=(10, 0))
    ttk.Button(b, text="Use", command=use).pack(side="left")
    ttk.Button(b, text="Close", command=win.destroy).pack(side="left", padx=(6, 0))


# ---------------- AI upscaling of a finished picture ----------------

def upscale_menu(app, widget):
    if not app.current:
        return
    menu = tk.Menu(app.root, tearoff=0)
    if not G().UPSCALER.exists():
        menu.add_command(label="The AI upscaler model isn't downloaded (run download-models.ps1)", state="disabled")
    else:
        for factor in (2, 4):
            menu.add_command(label=f"Make a {factor}x bigger copy (AI upscaling)",
                             command=lambda f=factor: upscale_current(app, f))
    menu.tk_popup(widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height())


def upscale_current(app, factor):
    src = Path(app.current)
    with Image.open(src) as im:
        big = im.width * factor * im.height * factor
    if big > 200e6:
        messagebox.showinfo("Upscale", f"A {factor}x copy would be {big / 1e6:.0f} megapixels, too big to handle. "
                                       "Try 2x, or a smaller picture.")
        return
    if not gpu_free(app, "AI upscaling"):
        return
    app.status.configure(text=f"AI upscaling {factor}x...")
    app.progress.configure(mode="indeterminate")
    app.progress.start(12)
    app.updating = True

    def done(out):
        app.updating = False
        app.progress.stop()
        app.status.configure(text=f"Saved a {factor}x copy: {out.name}")
        app.show_picture(out)

    def fail(msg):
        app.updating = False
        app.progress.stop()
        app.status.configure(text="Upscaling failed.")
        messagebox.showerror("Upscale", msg)
    run_in_thread(app, lambda: G().upscale_picture(src, factor, lambda m: app.events.put(("status", m))), done, fail)


# ---------------- fix the words in a finished picture ----------------

def fix_words_dialog(app):
    """Redraw just the misspelled words in the shown picture (a new copy is saved; the original is kept)."""
    gm = G()
    if not app.current:
        messagebox.showinfo("Fix words", "Open or make a picture first.")
        return
    if not D.text_reader(gm.ROOT) or not gm.fixer_profile():
        messagebox.showinfo("Fix words", "Fixing words needs Qwen Image (or another image model) and its Qwen3-VL "
                                         "files on this PC. Run download-models.ps1 to get them.")
        return
    src = Path(app.current)
    try:
        prompt = M.read(src).get("comment", "")
    except Exception:
        prompt = ""
    win, f = app.make_window("Fix words")
    ttk.Label(f, text="What should the words say? One line for each piece of text, exactly as it should read. "
                      "Only the wrong words are redrawn; the rest of the picture stays exactly as it is, and the "
                      "result is saved as a new copy.", wraplength=520, justify="left").pack(anchor="w")
    box = app.make_text(f, height=5, width=60, wrap="word", font=(P.UI_FONT, 10))
    box.pack(fill="x", pady=(8, 0))
    box.insert("1.0", "\n".join(D.quoted_text(prompt)))
    note = ttk.Label(f, text=f"Redraws with {gm.PROFILES[gm.fixer_profile()]['name']}. About a minute for each "
                             "wrong piece of text.", style="Hint.TLabel", wraplength=520)
    note.pack(anchor="w", pady=(6, 0))
    b = ttk.Frame(f)
    b.pack(anchor="e", pady=(10, 0))

    def go():
        expected = [ln.strip() for ln in box.get("1.0", "end").splitlines() if ln.strip()]
        if not expected:
            messagebox.showinfo("Fix words", "Type the words the picture should show.", parent=win)
            return
        if not gpu_free(app, "fixing words"):
            return
        win.destroy()
        out = gm.fix_name(src)
        app.updating = True
        app.progress.configure(mode="indeterminate")
        app.progress.start(12)
        say = lambda m: app.events.put(("status", m))  # noqa: E731

        def work():
            gm.fix_words(src, expected, say, out, prompt=prompt)
            reader = D.text_reader(gm.ROOT)
            seen = D.read_text_local(gm.ROOT, reader[0], reader[1], out)
            return D.text_score(expected, seen) + (seen,)

        def done(result):
            ok, _score, _details, seen = result
            app.updating = False
            app.progress.stop()
            app.status.configure(text=f"Saved {out.name}. " + ("Words checked: correct." if ok else
                                 "Some words are still not right: it reads " + " / ".join(seen)[:100]))
            app.show_picture(out)

        def fail(msg):
            app.updating = False
            app.progress.stop()
            app.status.configure(text="Fixing words didn't work.")
            messagebox.showinfo("Fix words", msg)
        run_in_thread(app, work, done, fail)

    ttk.Button(b, text="Fix the words", command=go).pack(side="left")
    ttk.Button(b, text="Cancel", command=win.destroy).pack(side="left", padx=(6, 0))


# ---------------- wallpaper ----------------

def _wallpaper():
    if P.IS_MAC:
        import moss_wallpaper_mac
        return moss_wallpaper_mac
    import moss_wallpaper
    return moss_wallpaper


def screen_parts(path):
    """Per-screen files made with a screen picture: {screen short name: path}."""
    path = Path(path)
    stem = path.stem
    parts = {}
    for p in path.parent.glob(f"{stem}_part*-*{path.suffix}"):
        name = p.stem.rsplit("-", 1)[-1]
        parts[name] = p
    return parts


def backup_wallpaper(app):
    if "wallpaper_backup" not in app.settings:
        try:
            app.settings["wallpaper_backup"] = _wallpaper().current()
            app.save_settings()
        except Exception:
            pass


def apply_wallpaper(app, path, how, screen=None):
    """how: 'per-screen', 'span', 'all', or 'one' (with screen). Returns a short description."""
    W = _wallpaper()
    path = Path(path).resolve()
    backup_wallpaper(app)
    if how == "per-screen":
        parts = screen_parts(path)
        rects = {s["short"]: (s["x"], s["y"], s["w"], s["h"]) for s in app.screens}
        pictures = {rects[n]: str(p) for n, p in parts.items() if n in rects}
        if not pictures:
            raise RuntimeError("This picture's per-screen files don't match the screens connected now.")
        n = W.set_per_monitor(pictures, "fill")
        return f"Set one picture on each of {n} screens."
    if how == "span":
        W.set_span(str(path))
        return "Spanned across all screens."
    if how == "one":
        W.set_per_monitor({(screen["x"], screen["y"], screen["w"], screen["h"]): str(path)}, "fill")
        return f"Set on the {screen['name']} screen."
    W.set_all(str(path), "fill")
    return "Set on every screen."


def wallpaper_menu(app, widget):
    if not app.current:
        return
    path = Path(app.current)
    menu = tk.Menu(app.root, tearoff=0)

    def do(how, screen=None):
        try:
            app.status.configure(text=apply_wallpaper(app, path, how, screen))
        except Exception as e:
            messagebox.showerror("Wallpaper", str(e))

    parts = screen_parts(path)
    if parts:
        menu.add_command(label=f"One per screen ({len(parts)} pictures, sharpest)", command=lambda: do("per-screen"))
    if len(app.screens) > 1:
        menu.add_command(label="Span this picture across all screens", command=lambda: do("span"))
    menu.add_command(label="Same picture on every screen", command=lambda: do("all"))
    if len(app.screens) > 1:
        for s in app.screens:
            menu.add_command(label=f"Only on the {s['name']} screen", command=lambda s=s: do("one", s))
    if "wallpaper_backup" in app.settings:
        menu.add_separator()
        menu.add_command(label="Put my previous wallpaper back", command=lambda: restore_wallpaper(app))
    menu.tk_popup(widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height())


def restore_wallpaper(app):
    try:
        _wallpaper().restore(app.settings["wallpaper_backup"])
        app.settings.pop("wallpaper_backup", None)
        app.save_settings()
        app.status.configure(text="Your previous wallpaper is back.")
    except Exception as e:
        messagebox.showerror("Wallpaper", str(e))


# ---------------- wallpaper of the day ----------------

def wotd_settings(app):
    d = dict(enabled=False, time="08:00", prompts="A sweeping panoramic landscape at golden hour, mist in the valleys",
             vary=True, model="flux2-klein-4b", tier=2, how="per-screen")
    d.update(app.settings.get("wotd", {}))
    return d


def task_args():
    """What the daily task runs: this app with --wallpaper-of-the-day."""
    gm = G()
    if gm.FROZEN:
        return [sys.executable, "--wallpaper-of-the-day"]
    python = Path(sys.executable)
    if P.IS_WIN and python.with_name("pythonw.exe").exists():
        python = python.with_name("pythonw.exe")             # no console window
    return [str(python), str(Path(gm.__file__).resolve()), "--wallpaper-of-the-day"]


def schedule_task(enabled, at):
    """Create or remove the daily task (only when the user turns it on or off)."""
    P.schedule_daily(enabled, at, task_args())


def open_wotd(app):
    gm = G()
    cfg = wotd_settings(app)
    win, f = app.make_window("Wallpaper of the day")
    ttk.Label(f, text="Makes a fresh wallpaper every day at the time you choose, from one of your prompts (taking "
                      "them in turn), and sets it on your screens. Your computer needs to be on and you signed in.",
              wraplength=560, justify="left").grid(row=0, column=0, columnspan=3, sticky="w")
    enabled = tk.BooleanVar(value=cfg["enabled"])
    ttk.Checkbutton(f, text="Make a new wallpaper every day at", variable=enabled).grid(
        row=1, column=0, sticky="w", pady=(10, 0))
    at = tk.StringVar(value=cfg["time"])
    ttk.Entry(f, textvariable=at, width=7).grid(row=1, column=1, sticky="w", pady=(10, 0))
    ttk.Label(f, text="Prompts (one per line)").grid(row=2, column=0, sticky="nw", pady=(8, 0))
    prompts = app.make_text(f, height=6, width=60, wrap="word", font=(P.UI_FONT, 10))
    prompts.grid(row=2, column=1, columnspan=2, sticky="we", pady=(8, 0))
    prompts.insert("1.0", cfg["prompts"])
    app.apply_theme_to_text(prompts)
    vary = tk.BooleanVar(value=cfg["vary"])
    ttk.Checkbutton(f, text="Vary each prompt with the local enhancer, so no two days look the same",
                    variable=vary).grid(row=3, column=1, columnspan=2, sticky="w", pady=(4, 0))
    keys = [k for k in gm.PROFILES if not gm.PROFILES[k].get("cloud")]
    ttk.Label(f, text="Model").grid(row=4, column=0, sticky="w", pady=(8, 0))
    model = ttk.Combobox(f, state="readonly", width=30, values=[gm.PROFILES[k]["name"] for k in keys])
    model.grid(row=4, column=1, columnspan=2, sticky="w", pady=(8, 0))
    model.current(keys.index(cfg["model"]) if cfg["model"] in keys else 0)
    ttk.Label(f, text="Size").grid(row=5, column=0, sticky="w", pady=(6, 0))
    tier = ttk.Combobox(f, state="readonly", width=30, values=gm.TIERS)
    tier.grid(row=5, column=1, columnspan=2, sticky="w", pady=(6, 0))
    tier.current(cfg["tier"])
    how = tk.StringVar(value=cfg["how"] if len(app.screens) > 1 else "all")
    hf = ttk.Frame(f)
    hf.grid(row=6, column=1, columnspan=2, sticky="w", pady=(6, 0))
    if len(app.screens) > 1:
        ttk.Radiobutton(hf, text="One panorama across all screens (a slice on each)", value="per-screen",
                        variable=how).pack(anchor="w")
    ttk.Radiobutton(hf, text="The same picture on every screen", value="all", variable=how).pack(anchor="w")
    status = ttk.Label(f, text="", style="Hint.TLabel", wraplength=560)
    status.grid(row=7, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def collect():
        return dict(enabled=enabled.get(), time=at.get().strip(), prompts=prompts.get("1.0", "end").strip(),
                    vary=vary.get(), model=keys[model.current()], tier=tier.current(), how=how.get())

    def save():
        new = collect()
        try:
            datetime.strptime(new["time"], "%H:%M")
        except ValueError:
            messagebox.showinfo("Wallpaper of the day", "Use a time like 08:00 or 17:30.", parent=win)
            return
        if not [p for p in new["prompts"].splitlines() if p.strip()]:
            messagebox.showinfo("Wallpaper of the day", "Add at least one prompt.", parent=win)
            return
        was = app.settings.get("wotd", {})
        if new["enabled"] != was.get("enabled", False) or (new["enabled"] and new["time"] != was.get("time")):
            try:
                schedule_task(new["enabled"], new["time"])
            except Exception as e:
                messagebox.showerror("Wallpaper of the day", f"{P.OS_NAME} couldn't schedule it:\n{e}", parent=win)
                return
        app.settings["wotd"] = new
        app.save_settings()
        status.configure(text=("Scheduled daily at " + new["time"] + ".") if new["enabled"] else
                         "Saved. It isn't scheduled; use Make one now to try it.")

    def now():
        current = collect()
        current["enabled"] = app.settings.get("wotd", {}).get("enabled", False)
        app.settings["wotd"] = current
        app.save_settings()
        status.configure(text="Making today's wallpaper...")
        run_wotd(app, lambda msg: status.configure(text=msg) if win.winfo_exists() else None)

    b = ttk.Frame(f)
    b.grid(row=8, column=0, columnspan=3, sticky="e", pady=(10, 0))
    ttk.Button(b, text="Save", command=save).pack(side="left")
    ttk.Button(b, text="Make one now", command=now).pack(side="left", padx=6)
    ttk.Button(b, text="Close", command=win.destroy).pack(side="left")


def run_wotd(app, on_done):
    """Make one wallpaper-of-the-day picture through the queue; on_done(message) when it's set (or failed)."""
    gm = G()
    cfg = wotd_settings(app)
    lines = [p.strip() for p in cfg["prompts"].splitlines() if p.strip()]
    if not lines:
        on_done("No prompts to use.")
        return
    index = app.settings.get("wotd_index", 0) % len(lines)
    app.settings["wotd_index"] = index + 1
    app.save_settings()
    prompt = lines[index]
    model = cfg["model"] if cfg["model"] in gm.PROFILES else "flux2-klein-4b"
    multi = len(app.screens) > 1 and cfg["how"] == "per-screen"
    if multi:
        shape = next((k for k in app.screen_opts if k.startswith("Screens: all")), "16:9 wide")
    else:
        shape = next((k for k in app.screen_opts if k.startswith("Screen")), "16:9 wide")

    def go(final_prompt):
        w, h, screen = size_for(app, model, shape, cfg["tier"])
        spec = base_spec(app, model, final_prompt, w, h, screen=screen, shape=shape,
                         tier=gm.TIERS[cfg["tier"]], label="wallpaper of the day")
        gid = f"wotd-{time.time():.0f}"
        app.wotd_callbacks = getattr(app, "wotd_callbacks", {})
        app.wotd_callbacks[gid] = ("per-screen" if multi else "all", on_done)
        app.queue_add(spec, label="wallpaper of the day", group=gid)
        app.queue_start()

    if cfg["vary"] and D.enhance_options(gm.ROOT) and not app.job:
        enh_model = D.enhance_options(gm.ROOT)[0][1]
        run_in_thread(app, lambda: D.enhance_local(gm.ROOT, enh_model, prompt, "Detailed"), go,
                      lambda msg: go(prompt))
    else:
        go(prompt)


# ---------------- queue, variations and compare ----------------

def queue_menu(app, widget):
    menu = tk.Menu(app.root, tearoff=0)
    menu.add_command(label="Add to the queue", command=lambda: add_current(app))
    menu.add_command(label="Make 4 variations (different seeds)", command=lambda: variations(app, 4))
    menu.add_command(label="Compare models with this prompt...", command=lambda: compare_dialog(app))
    menu.add_separator()
    menu.add_command(label=f"Show the queue ({len(app.queue)} waiting)...", command=lambda: open_queue_window(app))
    menu.tk_popup(widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height())


def auto_label(spec):
    return f"{G().PROFILES[spec['model']]['name']}: {spec['prompt'][:40]}"


def add_current(app):
    spec = app.collect_spec()
    if spec:
        app.queue_add(spec, label=auto_label(spec))
        app.status.configure(text=f"Added to the queue ({len(app.queue)} waiting).")
        open_queue_window(app)


def variations(app, n):
    gm = G()
    spec = app.collect_spec()
    if not spec:
        return
    cloud = gm.PROFILES[spec["model"]].get("cloud")
    if cloud and not messagebox.askokcancel(
            "Variations", f"That's {n} Nano Banana pictures, each billed to your API key. Continue?"):
        return
    gid = f"var-{time.time():.0f}"
    for i in range(n):
        app.queue_add(dict(spec, seed=spec["seed"] if cloud else "random"),
                      label=f"variation {i + 1} of {n}", group=gid)
    app.groups[gid]["title"] = f"{n} variations"
    app.queue_start()


def compare_dialog(app):
    gm = G()
    base = app.collect_spec()
    if not base:
        return
    if base["editing"]:
        messagebox.showinfo("Compare models", "Compare works with new pictures. Switch to New picture first.")
        return
    win, f = app.make_window("Compare models")
    ttk.Label(f, text="Makes the same prompt, at the same shape and size, with each model you tick, then shows "
                      "the results side by side.", wraplength=480, justify="left").grid(row=0, column=0, sticky="w")
    picks = []
    for key, p in gm.PROFILES.items():
        usable = bool(C.find_key(app.settings)) if p.get("cloud") else not app.missing_files(key)
        var = tk.BooleanVar(value=usable and not p.get("cloud"))
        cb = ttk.Checkbutton(f, text=p["name"] + ("" if usable else "  (not available)"), variable=var)
        cb.grid(row=len(picks) + 1, column=0, sticky="w", pady=(4, 0))
        if not usable:
            cb.state(["disabled"])
        picks.append((key, var))

    def go():
        chosen = [k for k, v in picks if v.get()]
        if len(chosen) < 2:
            messagebox.showinfo("Compare models", "Tick at least two models.", parent=win)
            return
        uses_cloud = any(gm.PROFILES[k].get("cloud") for k in chosen)
        if uses_cloud and not app.settings.get("cloud_ok") and not messagebox.askokcancel(
                "Compare models", "Nano Banana sends the prompt to Google and is billed to your API key. "
                                  "Continue?", parent=win):
            return
        if uses_cloud:
            app.settings["cloud_ok"] = True
        gid = f"cmp-{time.time():.0f}"
        tier = gm.TIERS.index(base["tier"]) if base["tier"] in gm.TIERS else 0
        for k in chosen:
            if base["shape"] in app.screen_opts or base["shape"] in gm.ASPECTS:
                w, h, screen = size_for(app, k, base["shape"], tier)
            else:
                a = gm.PROFILES[k]["align"]
                w, h, screen = base["w"] // a * a, base["h"] // a * a, None
            spec = dict(base, model=k, w=w, h=h, screen=screen, adv=app.advanced_for(k),
                        seed="n/a" if gm.PROFILES[k].get("cloud") else "random",
                        upscale=bool(screen) and base["upscale"], split=bool(screen) and base["split"],
                        ai_upscale=bool(screen) and base["ai_upscale"])
            app.queue_add(spec, label=gm.PROFILES[k]["name"], group=gid)
        app.groups[gid]["title"] = "Model comparison"
        win.destroy()
        app.queue_start()

    b = ttk.Frame(f)
    b.grid(row=len(picks) + 2, column=0, sticky="e", pady=(10, 0))
    ttk.Button(b, text="Compare", command=go).pack(side="left")
    ttk.Button(b, text="Cancel", command=win.destroy).pack(side="left", padx=(6, 0))


def show_group(app, gid):
    group = app.groups.get(gid)
    if not group:
        return
    if gid.startswith("wotd-"):
        how, on_done = getattr(app, "wotd_callbacks", {}).pop(gid, ("all", lambda m: None))
        spec, file, error = group["results"][-1]
        if file:
            try:
                on_done("Wallpaper set. " + apply_wallpaper(app, file, how))
            except Exception as e:
                on_done(f"The picture was made, but the wallpaper couldn't be set: {e}")
        else:
            on_done(f"No wallpaper today: {error}")
        return
    win, f = app.make_window(group.get("title", "Results"))
    keep = []
    results = group["results"]
    cols = 2 if len(results) <= 4 else 3
    for i, (spec, file, error) in enumerate(results):
        cell = ttk.Frame(f, padding=6)
        cell.grid(row=i // cols, column=i % cols, sticky="n")
        if file and Path(file).exists():
            with Image.open(file) as im:
                im = im.convert("RGB")
                im.thumbnail((420, 300), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(im)
            keep.append(photo)
            lbl = ttk.Label(cell, image=photo, cursor="hand2")
            lbl.pack()
            lbl.bind("<Button-1>", lambda e, p=file: app.show_picture(p))
            ttk.Label(cell, text=spec.get("label", ""), style="Status.TLabel").pack()
            ttk.Label(cell, text=Path(file).name, style="Hint.TLabel").pack()
        else:
            ttk.Label(cell, text=f"{spec.get('label', '')}\nNo picture: {error}", wraplength=400).pack()
    ttk.Label(f, text="Click a picture to open it in the main window.", style="Hint.TLabel").grid(
        row=99, column=0, columnspan=cols, sticky="w", pady=(6, 0))
    win.keep = keep


def open_queue_window(app):
    if getattr(app, "queue_win", None) and app.queue_win.winfo_exists():
        app.queue_win.lift()
        refresh_queue_window(app)
        return
    win, f = app.make_window("Queue")
    app.queue_win = win
    ttk.Label(f, text="Waiting", style="Status.TLabel").grid(row=0, column=0, sticky="w")
    waiting = tk.Listbox(f, height=8, width=80, activestyle="none", selectmode="extended")
    waiting.grid(row=1, column=0, sticky="we")
    ttk.Label(f, text="Finished, newest first (double-click to open)", style="Status.TLabel").grid(
        row=2, column=0, sticky="w", pady=(8, 0))
    finished = tk.Listbox(f, height=8, width=80, activestyle="none")
    finished.grid(row=3, column=0, sticky="we")
    win.lists = (waiting, finished)
    c = app.colors
    for lb in win.lists:
        lb.configure(background=c["text_bg"], foreground=c["text_fg"], selectbackground=c["select"],
                     highlightthickness=1, highlightbackground=c["border"], borderwidth=0)

    def remove():
        for i in reversed(list(waiting.curselection())):
            if i < len(app.queue):
                spec = app.queue.pop(i)
                if spec.get("group") in app.groups:
                    app.groups[spec["group"]]["total"] -= 1
        refresh_queue_window(app)

    def open_finished(_):
        sel = finished.curselection()
        done = list(reversed(app.queue_done))
        if sel and sel[0] < len(done) and done[sel[0]][1]:
            app.show_picture(done[sel[0]][1])

    finished.bind("<Double-Button-1>", open_finished)
    b = ttk.Frame(f)
    b.grid(row=4, column=0, sticky="e", pady=(10, 0))
    ttk.Button(b, text="Run the queue", command=app.queue_start).pack(side="left")
    ttk.Button(b, text="Stop after this picture",
               command=lambda: setattr(app, "queue_running", False)).pack(side="left", padx=6)
    ttk.Button(b, text="Remove selected", command=remove).pack(side="left")
    ttk.Button(b, text="Close", command=win.destroy).pack(side="left", padx=(6, 0))
    refresh_queue_window(app)


def refresh_queue_window(app):
    win = getattr(app, "queue_win", None)
    if not win or not win.winfo_exists():
        return
    waiting, finished = win.lists
    waiting.delete(0, "end")
    for spec in app.queue:
        waiting.insert("end", spec.get("label") or auto_label(spec))
    finished.delete(0, "end")
    for spec, file, error in reversed(app.queue_done):
        finished.insert("end", (spec.get("label") or auto_label(spec)) + ": " +
                        (Path(file).name if file else f"failed ({error})"))


# ---------------- paint an area to change (inpainting) ----------------

def open_mask_painter(app):
    if not app.edit_src or not Path(app.edit_src).exists():
        messagebox.showinfo("Paint an area", "Choose the picture to edit first.")
        return
    src = Path(app.edit_src)
    with Image.open(src) as im:
        picture = im.convert("RGB")
    W, H = picture.size
    scale = min(900 / W, 620 / H, 1.0)
    cw, ch = max(1, int(W * scale)), max(1, int(H * scale))
    mask = Image.new("L", (W, H), 0)
    if app.edit_mask and Path(app.edit_mask).exists():
        with Image.open(app.edit_mask) as m:
            mask = m.convert("L").resize((W, H))
    win, f = app.make_window("Paint the area to change")
    ttk.Label(f, text="Paint over the part of the picture to change. Right mouse button, or Shift while "
                      "painting, erases. "
                      "Then describe what should be there instead and click Edit picture.",
              wraplength=cw, justify="left").grid(row=0, column=0, columnspan=4, sticky="w")
    canvas = tk.Canvas(f, width=cw, height=ch, highlightthickness=0, cursor="crosshair")
    canvas.grid(row=1, column=0, columnspan=4, pady=(8, 0))
    state = dict(photo=None)
    brush = tk.IntVar(value=max(8, int(min(W, H) / 25)))

    def redraw():
        tint = Image.new("RGB", (W, H), (255, 40, 40))
        alpha = mask.point(lambda v: int(v * 0.5))
        over = picture.copy()
        over.paste(tint, (0, 0), alpha)
        state["photo"] = ImageTk.PhotoImage(over.resize((cw, ch), Image.Resampling.BILINEAR))
        canvas.delete("all")
        canvas.create_image(0, 0, anchor="nw", image=state["photo"])

    def paint(e, value):
        x, y, r = e.x / scale, e.y / scale, brush.get()
        ImageDraw.Draw(mask).ellipse((x - r, y - r, x + r, y + r), fill=value)
        rr = r * scale
        canvas.create_oval(e.x - rr, e.y - rr, e.x + rr, e.y + rr, outline="", fill="#ff3030" if value else "#202020",
                           stipple="gray50")

    canvas.bind("<B1-Motion>", lambda e: paint(e, 255))
    canvas.bind("<Button-1>", lambda e: paint(e, 255))
    canvas.bind("<Shift-B1-Motion>", lambda e: paint(e, 0))
    canvas.bind("<Shift-Button-1>", lambda e: paint(e, 0))
    canvas.bind("<ButtonRelease-1>", lambda e: redraw())
    for b in ("3",) if P.IS_WIN else ("2", "3"):             # the right button is Button-2 on a Mac
        canvas.bind(f"<B{b}-Motion>", lambda e: paint(e, 0))
        canvas.bind(f"<Button-{b}>", lambda e: paint(e, 0))
        canvas.bind(f"<ButtonRelease-{b}>", lambda e: redraw())
    ttk.Label(f, text="Brush size").grid(row=2, column=0, sticky="w", pady=(8, 0))
    ttk.Scale(f, from_=4, to=max(40, min(W, H) // 6), variable=brush, orient="horizontal", length=220).grid(
        row=2, column=1, sticky="w", pady=(8, 0))

    def clear():
        ImageDraw.Draw(mask).rectangle((0, 0, W, H), fill=0)
        redraw()

    def save():
        if not mask.getbbox():
            app.edit_mask = None
            app.edit_label_refresh()
            win.destroy()
            return
        folder = Path(app.out_dir.get() or G().DEFAULT_OUT) / ".masks"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{src.stem}_mask.png"
        mask.save(path)
        app.edit_mask = path
        app.edit_label_refresh()
        win.destroy()

    b = ttk.Frame(f)
    b.grid(row=2, column=2, columnspan=2, sticky="e", pady=(8, 0))
    ttk.Button(b, text="Clear", command=clear).pack(side="left")
    ttk.Button(b, text="Use this area", command=save).pack(side="left", padx=6)
    ttk.Button(b, text="Cancel", command=win.destroy).pack(side="left")
    redraw()


# ---------------- MOSS layout boxes ----------------

def compile_layout(summary, items, w, h):
    """MOSS-Image's layout JSON: boxes as [y_min, x_min, y_max, x_max] measured from the picture's centre."""
    out_items = []
    for it in items:
        x0, y0, x1, y1 = it["box"]                    # picture pixels, from the top-left
        entry = {"label": it["kind"], "caption": it["caption"] or it.get("text", "")}
        if it["kind"] == "text" and it.get("text"):
            entry["text"] = it["text"]
        entry["bbox"] = [int(round(y0 - h / 2)), int(round(x0 - w / 2)), int(round(y1 - h / 2)), int(round(x1 - w / 2))]
        out_items.append(entry)
    data = {"summary": {"meta": ["photo"], "canvas": "single", "caption": summary},
            "items": out_items, "bbox_coord_space": "abs_centralized", "bbox_format": "yxyx"}
    return json.dumps(data, ensure_ascii=False)


def open_layout(app):
    if app.model_key != "moss":
        if not messagebox.askokcancel("Layout boxes", "Layout boxes are a MOSS-Image feature; other models will "
                                                      "read them as plain text. Switch to MOSS-Image now?"):
            return
        app.select_model("moss")
    size, err = app.checked_size()
    if err:
        messagebox.showinfo("Layout boxes", err)
        return
    W, H = size
    scale = min(760 / W, 460 / H)
    cw, ch = int(W * scale), int(H * scale)
    win, f = app.make_window("Layout boxes (MOSS-Image)")
    ttk.Label(f, text=f"Drag on the canvas to draw a box for each thing you want in a specific place "
                      f"({W} x {H} picture). Pick a box in the list to describe it. Text boxes get the exact words "
                      "to write.", wraplength=cw + 300, justify="left").grid(row=0, column=0, columnspan=2, sticky="w")
    canvas = tk.Canvas(f, width=cw, height=ch, background="#30343a", highlightthickness=0, cursor="crosshair")
    canvas.grid(row=1, column=0, rowspan=6, pady=(8, 0), sticky="n")
    items, state = [], dict(start=None, rect=None, sel=None)
    side = ttk.Frame(f)
    side.grid(row=1, column=1, sticky="nw", padx=(12, 0), pady=(8, 0))
    lb = tk.Listbox(side, height=8, width=40, activestyle="none")
    lb.pack(anchor="w")
    c = app.colors
    lb.configure(background=c["text_bg"], foreground=c["text_fg"], selectbackground=c["select"], borderwidth=0)
    kind = tk.StringVar(value="object")
    kf = ttk.Frame(side)
    kf.pack(anchor="w", pady=(6, 0))
    ttk.Radiobutton(kf, text="Object", value="object", variable=kind).pack(side="left")
    ttk.Radiobutton(kf, text="Text", value="text", variable=kind).pack(side="left", padx=(8, 0))
    ttk.Label(side, text="What's in this box").pack(anchor="w", pady=(6, 0))
    caption = tk.StringVar()
    ttk.Entry(side, textvariable=caption, width=42).pack(anchor="w")
    ttk.Label(side, text="Exact text to write (text boxes)").pack(anchor="w", pady=(6, 0))
    words = tk.StringVar()
    ttk.Entry(side, textvariable=words, width=42).pack(anchor="w")
    ttk.Label(f, text="The whole picture").grid(row=7, column=0, sticky="w", pady=(10, 0))
    summary = app.make_text(f, height=3, width=90, wrap="word", font=(P.UI_FONT, 10))
    summary.grid(row=8, column=0, columnspan=2, sticky="we")
    app.apply_theme_to_text(summary)
    current = app.prompt.get("1.0", "end").strip()
    if current and not current.startswith("{"):
        summary.insert("1.0", current)

    def to_canvas(box):
        return [v * scale for v in box]

    def redraw():
        canvas.delete("box")
        for i, it in enumerate(items):
            x0, y0, x1, y1 = to_canvas(it["box"])
            color = "#4c8dff" if i == state["sel"] else ("#ffb000" if it["kind"] == "text" else "#3fae5a")
            canvas.create_rectangle(x0, y0, x1, y1, outline=color, width=2, tags="box")
            canvas.create_text(x0 + 4, y0 + 4, anchor="nw", fill=color, tags="box",
                               text=f"{i + 1}. {it.get('text') or it['caption'] or it['kind']}"[:40])
        lb.delete(0, "end")
        for i, it in enumerate(items):
            lb.insert("end", f"{i + 1}. [{it['kind']}] {it.get('text') or it['caption'] or '(describe me)'}")
        if state["sel"] is not None:
            lb.selection_set(state["sel"])

    def store():
        if state["sel"] is not None and state["sel"] < len(items):
            it = items[state["sel"]]
            it.update(kind=kind.get(), caption=caption.get().strip(), text=words.get().strip())

    def select(i):
        store()
        state["sel"] = i
        it = items[i]
        kind.set(it["kind"])
        caption.set(it["caption"])
        words.set(it.get("text", ""))
        redraw()

    def down(e):
        state["start"] = (e.x, e.y)
        state["rect"] = canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="#ffffff", dash=(3, 2))

    def drag(e):
        if state["rect"]:
            x0, y0 = state["start"]
            canvas.coords(state["rect"], x0, y0, e.x, e.y)

    def up(e):
        if not state["start"]:
            return
        x0, y0 = state["start"]
        canvas.delete(state["rect"])
        state["start"] = state["rect"] = None
        x0, x1 = sorted((max(0, min(cw, x0)), max(0, min(cw, e.x))))
        y0, y1 = sorted((max(0, min(ch, y0)), max(0, min(ch, e.y))))
        if x1 - x0 < 6 or y1 - y0 < 6:
            return
        store()
        items.append(dict(kind=kind.get(), caption="", text="", box=[x0 / scale, y0 / scale, x1 / scale, y1 / scale]))
        state["sel"] = None
        select(len(items) - 1)

    canvas.bind("<Button-1>", down)
    canvas.bind("<B1-Motion>", drag)
    canvas.bind("<ButtonRelease-1>", up)
    lb.bind("<<ListboxSelect>>", lambda e: lb.curselection() and select(lb.curselection()[0]))

    def delete():
        if state["sel"] is not None and state["sel"] < len(items):
            items.pop(state["sel"])
            state["sel"] = None
            caption.set("")
            words.set("")
            redraw()

    def use():
        store()
        text = summary.get("1.0", "end").strip()
        if not text:
            messagebox.showinfo("Layout boxes", "Describe the whole picture first.", parent=win)
            return
        if not items:
            messagebox.showinfo("Layout boxes", "Draw at least one box.", parent=win)
            return
        app.prompt.delete("1.0", "end")
        app.prompt.insert("1.0", compile_layout(text, items, W, H))
        app.update_tokens()
        app.status.configure(text=f"Layout prompt with {len(items)} boxes. Keep the size at {W} x {H}.")
        win.destroy()

    b = ttk.Frame(side)
    b.pack(anchor="w", pady=(10, 0))
    ttk.Button(b, text="Save box", command=lambda: (store(), redraw())).pack(side="left")
    ttk.Button(b, text="Delete box", command=delete).pack(side="left", padx=6)
    bb = ttk.Frame(f)
    bb.grid(row=9, column=0, columnspan=2, sticky="e", pady=(10, 0))
    ttk.Button(bb, text="Use layout as prompt", command=use).pack(side="left")
    ttk.Button(bb, text="Cancel", command=win.destroy).pack(side="left", padx=(6, 0))
    redraw()


# ---------------- advanced settings ----------------

def style_loras():
    folder = G().MODELS / "loras" / "styles"
    folder.mkdir(parents=True, exist_ok=True)
    return sorted(p.name for p in folder.glob("*.safetensors"))


def open_advanced(app):
    gm = G()
    key = app.model_key
    p = gm.PROFILES[key]
    cur = app.advanced_for(key)
    win, f = app.make_window(f"Advanced settings: {p['name']}")
    if p.get("cloud"):
        ttk.Label(f, text="Nano Banana runs on Google's servers and doesn't use these settings.",
                  wraplength=420).grid(row=0, column=0, sticky="w")
        ttk.Button(f, text="Close", command=win.destroy).grid(row=1, column=0, sticky="e", pady=(10, 0))
        return
    ttk.Label(f, text=f"These apply to new pictures made with {p['name']}. Model defaults: {p['steps']} steps, "
                      f"guidance {p['cfg']}.", wraplength=520, justify="left").grid(row=0, column=0, columnspan=3,
                                                                                     sticky="w")
    steps = tk.IntVar(value=cur["steps"])
    cfg = tk.DoubleVar(value=cur["cfg"])
    rows = [("Steps", ttk.Spinbox(f, from_=1, to=150, textvariable=steps, width=8)),
            ("Guidance (CFG)", ttk.Spinbox(f, from_=0.0, to=20.0, increment=0.5, textvariable=cfg, width=8))]
    for i, (label, w) in enumerate(rows, 1):
        ttk.Label(f, text=label).grid(row=i, column=0, sticky="w", pady=(6, 0))
        w.grid(row=i, column=1, sticky="w", pady=(6, 0))
    ttk.Label(f, text="Sampler").grid(row=3, column=0, sticky="w", pady=(6, 0))
    sampler = ttk.Combobox(f, state="readonly", width=16, values=["(model default)"] + SAMPLERS[1:])
    sampler.grid(row=3, column=1, sticky="w", pady=(6, 0))
    sampler.set(cur["sampler"] or "(model default)")
    ttk.Label(f, text="Avoid (negative prompt)").grid(row=4, column=0, sticky="w", pady=(6, 0))
    negative = tk.StringVar(value=cur["negative"])
    ttk.Entry(f, textvariable=negative, width=56).grid(row=4, column=1, columnspan=2, sticky="we", pady=(6, 0))
    if key == "moss":
        ttk.Button(f, text="Use MOSS's recommended", command=lambda: negative.set(MOSS_NEGATIVE)).grid(
            row=5, column=1, sticky="w", pady=(4, 0))
    if p["cfg"] <= 1.0:
        ttk.Label(f, text="This model runs without guidance (CFG 1), so a negative prompt has little effect.",
                  style="Hint.TLabel").grid(row=5, column=2, sticky="w", pady=(4, 0))
    keep = tk.BooleanVar(value=cur["keep_breaks"])
    ttk.Checkbutton(f, text="Keep line breaks in the prompt (controls line breaks in text drawn in the picture)",
                    variable=keep).grid(row=6, column=0, columnspan=3, sticky="w", pady=(8, 0))
    ttk.Label(f, text="Style LoRA").grid(row=7, column=0, sticky="w", pady=(8, 0))
    loras = style_loras()
    lora = ttk.Combobox(f, state="readonly", width=40, values=["(none)"] + loras)
    lora.grid(row=7, column=1, columnspan=2, sticky="w", pady=(8, 0))
    lora.set(cur["lora"] if cur["lora"] in loras else "(none)")
    weight = tk.DoubleVar(value=cur["lora_weight"])
    ttk.Label(f, text="Strength").grid(row=8, column=0, sticky="w", pady=(6, 0))
    ttk.Spinbox(f, from_=0.0, to=2.0, increment=0.1, textvariable=weight, width=8).grid(row=8, column=1, sticky="w",
                                                                                       pady=(6, 0))
    ttk.Label(f, text="A LoRA only works with the model it was made for. Put .safetensors files in "
                      "models\\loras\\styles.", style="Hint.TLabel", wraplength=520).grid(
        row=9, column=0, columnspan=3, sticky="w", pady=(4, 0))

    def save():
        try:
            st, cf, wt = int(steps.get()), float(cfg.get()), float(weight.get())
        except (tk.TclError, ValueError):
            messagebox.showinfo("Advanced settings", "Steps, guidance and strength must be numbers.", parent=win)
            return
        app.settings.setdefault("advanced", {})[key] = dict(
            steps=st, cfg=cf, sampler="" if sampler.get() == "(model default)" else sampler.get(),
            negative=negative.get().strip(), keep_breaks=keep.get(),
            lora="" if lora.get() == "(none)" else lora.get(), lora_weight=wt)
        app.save_settings()
        app.update_size_label()
        app.refresh_advanced_note()
        win.destroy()

    def reset():
        app.settings.setdefault("advanced", {}).pop(key, None)
        app.save_settings()
        app.update_size_label()
        app.refresh_advanced_note()
        win.destroy()

    b = ttk.Frame(f)
    b.grid(row=10, column=0, columnspan=3, sticky="e", pady=(12, 0))
    ttk.Button(b, text="Open LoRA folder", command=lambda: P.open_path(gm.MODELS / "loras" / "styles")).pack(
        side="left")
    ttk.Button(b, text="Reset to defaults", command=reset).pack(side="left", padx=6)
    ttk.Button(b, text="Save", command=save).pack(side="left")
    ttk.Button(b, text="Cancel", command=win.destroy).pack(side="left", padx=(6, 0))


# ---------------- gallery ----------------

def gallery_items(folder):
    """Pictures in the output folder, newest first, with their prompt and model (cached in .thumbs\\index.json)."""
    folder = Path(folder)
    thumbs = folder / ".thumbs"
    thumbs.mkdir(exist_ok=True)
    try:
        index = json.loads((thumbs / "index.json").read_text(encoding="utf-8"))
    except Exception:
        index = {}
    files = [p for p in folder.iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg")
             and "_part" not in p.stem and not p.name.endswith((".engine.png", ".tmp.png"))]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    items, changed = [], False
    for p in files:
        key, mtime = p.name, p.stat().st_mtime
        entry = index.get(key)
        if not entry or entry.get("mtime") != mtime:
            try:
                meta = M.read(p)
            except Exception:
                meta = {}
            settings = meta.get("settings", "")
            model = next((v["name"] for v in G().PROFILES.values() if f"model {v['name']}" in settings), "")
            entry = dict(mtime=mtime, prompt=meta.get("comment", ""), title=meta.get("title", ""), model=model)
            index[key] = entry
            changed = True
        items.append(dict(path=p, **entry))
    if changed:
        try:
            (thumbs / "index.json").write_text(json.dumps(index), encoding="utf-8")
        except OSError:
            pass
    return items


def thumbnail(path, size=180):
    thumbs = path.parent / ".thumbs"
    t = thumbs / (path.stem + ".jpg")
    if not t.exists() or t.stat().st_mtime < path.stat().st_mtime:
        with Image.open(path) as im:
            im.draft("RGB", (size * 2, size * 2))
            im = im.convert("RGB")
            im.thumbnail((size, size), Image.Resampling.LANCZOS)
            im.save(t, "JPEG", quality=85)
    return t


def open_gallery(app):
    gm = G()
    folder = Path(app.out_dir.get() or gm.DEFAULT_OUT)
    if not folder.exists():
        messagebox.showinfo("Gallery", "The output folder doesn't exist yet.")
        return
    win, f = app.make_window(f"Gallery: {folder}")
    win.geometry("1180x780")
    top = ttk.Frame(f)
    top.pack(fill="x")
    ttk.Label(top, text="Search").pack(side="left")
    search = tk.StringVar()
    ttk.Entry(top, textvariable=search, width=40).pack(side="left", padx=6)
    model = ttk.Combobox(top, state="readonly", width=30,
                         values=["All models"] + [p["name"] for p in gm.PROFILES.values()])
    model.current(0)
    model.pack(side="left", padx=6)
    count = ttk.Label(top, text="Loading...", style="Hint.TLabel")
    count.pack(side="left", padx=12)
    outer = ttk.Frame(f)
    outer.pack(fill="both", expand=True, pady=(8, 0))
    canvas = tk.Canvas(outer, highlightthickness=0, background=app.colors.get("bg") or P.WINDOW_BG)
    bar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=bar.set)
    bar.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)
    grid = ttk.Frame(canvas)
    canvas.create_window(0, 0, anchor="nw", window=grid)
    grid.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind_all("<MouseWheel>", lambda e: canvas.winfo_exists() and canvas.yview_scroll(P.wheel_units(e), "units"))
    state = dict(items=[], photos={}, shown=0)
    PAGE, COLS = 60, 6

    def matches(it):
        q = search.get().strip().lower()
        m = model.get()
        return (not q or q in (it["prompt"] + " " + it["title"] + " " + it["path"].name).lower()) and \
               (m == "All models" or it["model"] == m)

    def menu_for(it, widget, e):
        menu = tk.Menu(app.root, tearoff=0)
        menu.add_command(label="Show in the main window", command=lambda: app.show_picture(it["path"]))
        menu.add_command(label="Reuse its prompt, size and seed", command=lambda: (app.apply_reuse(it["path"]),
                                                                                   app.show_picture(it["path"])))
        menu.add_command(label="Edit this picture", command=lambda: edit_this(app, it["path"]))
        menu.add_command(label="Write a prompt from it", command=lambda: app.open_describe(it["path"]))
        menu.add_command(label="Open", command=lambda: P.open_path(it["path"]))
        menu.add_command(label="Show in folder", command=lambda: P.show_in_folder(it["path"]))
        menu.tk_popup(e.x_root, e.y_root)

    def render():
        for w in grid.winfo_children():
            w.destroy()
        shown = [it for it in state["items"] if matches(it)][:state["shown"] or PAGE]
        for i, it in enumerate(shown):
            cell = ttk.Frame(grid, padding=4)
            cell.grid(row=i // COLS, column=i % COLS, sticky="n")
            photo = state["photos"].get(it["path"])
            lbl = ttk.Label(cell, image=photo, text="" if photo else "...", cursor="hand2", width=24 if not photo else 0)
            lbl.pack()
            name = it["title"] or it["path"].stem
            ttk.Label(cell, text=name[:30], style="Hint.TLabel", wraplength=180).pack()
            lbl.bind("<Button-1>", lambda e, it=it: app.show_picture(it["path"]))
            for ev in P.right_click_events():
                lbl.bind(ev, lambda e, it=it, w=lbl: menu_for(it, w, e))
            it["label"] = lbl
        total = sum(1 for it in state["items"] if matches(it))
        count.configure(text=f"{min(len(shown), total)} of {total} pictures. Click to open, right-click for more.")
        if total > len(shown):
            ttk.Button(grid, text="Show more", command=lambda: (state.update(shown=len(shown) + PAGE), render())).grid(
                row=len(shown) // COLS + 1, column=0, columnspan=COLS, pady=8)
        load_thumbs(shown)

    def load_thumbs(shown):
        todo = [it for it in shown if it["path"] not in state["photos"]]

        def work():
            return [(it, thumbnail(it["path"])) for it in todo]

        def done(pairs):
            for it, t in pairs:
                try:
                    with Image.open(t) as im:
                        state["photos"][it["path"]] = ImageTk.PhotoImage(im.copy())
                    if it.get("label") and it["label"].winfo_exists():
                        it["label"].configure(image=state["photos"][it["path"]], text="", width=0)
                except Exception:
                    pass
        if todo:
            run_in_thread(app, work, done, lambda m: None)

    def load():
        run_in_thread(app, lambda: gallery_items(folder), lambda items: (state.update(items=items), render()))

    def listing():
        try:
            return sorted((e.name, e.stat().st_mtime) for e in os.scandir(folder)
                          if e.is_file() and e.name.lower().endswith((".png", ".jpg", ".jpeg"))
                          and not e.name.endswith((".engine.png", ".tmp.png")) and "_part" not in e.name)
        except OSError:
            return []

    def watch():
        """New or changed pictures (made by the main window, wallpaper of the day, phone access or another
        program) show up within a few seconds."""
        if not win.winfo_exists():
            return
        now = listing()
        if now != state.get("listing"):
            if state.get("listing") is not None:
                load()
            state["listing"] = now
        win.after(4000, watch)

    search.trace_add("write", lambda *a: render())
    model.bind("<<ComboboxSelected>>", lambda e: render())
    win.bind("<Destroy>", lambda e: e.widget is win and canvas.unbind_all("<MouseWheel>"))
    load()
    watch()


def edit_this(app, path):
    app.show_picture(path)
    app.mode.set("edit")
    app.on_mode_change()
    app.set_edit_src(path)
    app.prompt.delete("1.0", "end")
    app.prompt.focus_set()


# ---------------- phone access ----------------

def local_addresses():
    import socket
    found = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    return sorted(a for a in found if not a.startswith("127."))


class Bridge:
    """What the phone page may see and do. Everything it reads is prepared on the window's thread."""

    def __init__(self, app):
        self.app = app

    def state(self):
        return dict(getattr(self.app, "remote_state", {}))

    def options(self):
        return dict(getattr(self.app, "remote_options", {}))

    def submit(self, params):
        self.app.events.put(("remote", params))
        return "Added to the queue. It will appear under Recent pictures when it's done."

    def picture(self, name, thumb):
        recent = {p["name"]: p["path"] for p in getattr(self.app, "remote_recent", [])}
        path = recent.get(name)
        if not path or not Path(path).exists():
            return None
        return thumbnail(Path(path), 320) if thumb else Path(path)


def remote_tick(app):
    """Called on the window's thread a few times a second: refresh what the phone page can read."""
    try:
        app.remote_state = dict(status=app.status.cget("text"), busy=bool(app.job), waiting=len(app.queue),
                                recent=[dict(name=p["name"], prompt=p["prompt"]) for p in app.remote_recent[:24]])
    except Exception:
        pass


def remote_note_picture(app, path, prompt):
    if not hasattr(app, "remote_recent"):
        return
    app.remote_recent = [p for p in app.remote_recent if p["name"] != Path(path).name]
    app.remote_recent.insert(0, dict(name=Path(path).name, path=str(path), prompt=prompt))
    app.remote_recent = app.remote_recent[:48]


def remote_request(app, params):
    """A picture request from the phone page (window thread)."""
    gm = G()
    key = params.get("model")
    if key not in gm.PROFILES:
        return
    p = gm.PROFILES[key]
    if p.get("cloud") and not (C.find_key(app.settings) and app.settings.get("cloud_ok")):
        app.status.configure(text="Phone request skipped: Nano Banana isn't set up in the window yet.")
        return
    if not p.get("cloud") and app.missing_files(key):
        app.status.configure(text=f"Phone request skipped: {p['name']} isn't downloaded.")
        return
    shape = params.get("shape") if (params.get("shape") in gm.ASPECTS or params.get("shape") in app.screen_opts) \
        else "16:9 wide"
    tier = max(0, min(len(gm.TIERS) - 1, int(params.get("tier", 0))))
    w, h, screen = size_for(app, key, shape, tier)
    spec = base_spec(app, key, params["prompt"], w, h, screen=screen, shape=shape, tier=gm.TIERS[tier],
                     label="from phone: " + params["prompt"][:30],
                     seed="n/a" if p.get("cloud") else "random")
    app.queue_add(spec, label=spec["label"])
    app.status.configure(text=f"Phone request added ({len(app.queue)} waiting).")
    app.queue_start()


def start_remote(app):
    import moss_remote as R
    stop_remote(app)
    cfg = app.settings.get("remote", {})
    if not cfg.get("enabled") or not cfg.get("digest"):
        return None
    gm = G()
    host = "0.0.0.0" if cfg.get("scope") == "network" else "127.0.0.1"
    port = int(cfg.get("port", 8780))
    try:
        app.remote_recent = [dict(name=it["path"].name, path=str(it["path"]), prompt=it["prompt"])
                             for it in gallery_items(Path(app.out_dir.get() or gm.DEFAULT_OUT))[:24]]
    except Exception:
        app.remote_recent = []
    models = [(k, p["name"]) for k, p in gm.PROFILES.items()
              if (C.find_key(app.settings) if p.get("cloud") else not app.missing_files(k))]
    shapes = [(k, k) for k in gm.ASPECTS] + [(k, k) for k in app.screen_opts]
    app.remote_options = dict(models=models, shapes=shapes, tiers=[[i, t] for i, t in enumerate(gm.TIERS)],
                              model=app.model_key)
    remote_tick(app)
    server = R.RemoteServer(Bridge(app), host, port, cfg["salt"], cfg["digest"])
    server.start()
    app.remote_server = server
    return host, port


def stop_remote(app):
    server = getattr(app, "remote_server", None)
    if server:
        try:
            server.stop()
        except Exception:
            pass
        app.remote_server = None


def open_remote(app):
    import moss_remote as R
    cfg = dict(enabled=False, scope="network", port=8780)
    cfg.update(app.settings.get("remote", {}))
    win, f = app.make_window("Phone access")
    ttk.Label(f, text="Lets you make pictures from your phone's web browser while this app is open on your PC. "
                      "Use it on your home network or through your WireGuard tunnel; it isn't meant for the open "
                      "internet. Pictures still run on this PC.", wraplength=560, justify="left").grid(
        row=0, column=0, columnspan=3, sticky="w")
    enabled = tk.BooleanVar(value=cfg["enabled"])
    ttk.Checkbutton(f, text="Turn on phone access", variable=enabled).grid(row=1, column=0, columnspan=3, sticky="w",
                                                                         pady=(10, 0))
    scope = tk.StringVar(value=cfg["scope"])
    ttk.Radiobutton(f, text="Phones on my network or WireGuard can connect", value="network",
                    variable=scope).grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))
    ttk.Radiobutton(f, text="Only this PC (for testing)", value="pc", variable=scope).grid(
        row=3, column=0, columnspan=3, sticky="w")
    ttk.Label(f, text="Port").grid(row=4, column=0, sticky="w", pady=(8, 0))
    port = tk.IntVar(value=cfg["port"])
    ttk.Entry(f, textvariable=port, width=8).grid(row=4, column=1, sticky="w", pady=(8, 0))
    ttk.Label(f, text="Password" + (" (leave empty to keep the current one)" if cfg.get("digest") else "")).grid(
        row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))
    pw1, pw2 = tk.StringVar(), tk.StringVar()
    ttk.Entry(f, textvariable=pw1, show="*", width=30).grid(row=6, column=0, columnspan=2, sticky="w")
    ttk.Entry(f, textvariable=pw2, show="*", width=30).grid(row=7, column=0, columnspan=2, sticky="w", pady=(4, 0))
    ttk.Label(f, text="(type it twice)", style="Hint.TLabel").grid(row=7, column=2, sticky="w")
    where = ttk.Label(f, text="", wraplength=560, justify="left")
    where.grid(row=8, column=0, columnspan=3, sticky="w", pady=(10, 0))

    def show_where():
        server = getattr(app, "remote_server", None)
        if not server:
            where.configure(text="Phone access is off.")
            return
        if server.host == "127.0.0.1":
            urls = [f"http://127.0.0.1:{server.port}/"]
        else:
            urls = [f"http://{a}:{server.port}/" for a in local_addresses()] or [f"http://<this PC's address>:{server.port}/"]
        where.configure(text="On. Open one of these on your phone:\n  " + "\n  ".join(urls) +
                        (("\n\nIf Windows asks whether to allow network access, allow it on private networks."
                          if P.IS_WIN else "\n\nIf macOS asks whether to accept incoming network connections, "
                          "click Allow.") if server.host != "127.0.0.1" else ""))

    def save():
        new = dict(cfg, enabled=enabled.get(), scope=scope.get())
        try:
            new["port"] = int(port.get())
            if not 1024 <= new["port"] <= 65535:
                raise ValueError
        except (tk.TclError, ValueError):
            messagebox.showinfo("Phone access", "Use a port between 1024 and 65535.", parent=win)
            return
        if pw1.get() or pw2.get():
            if pw1.get() != pw2.get():
                messagebox.showinfo("Phone access", "The two passwords don't match.", parent=win)
                return
            if len(pw1.get()) < 8:
                messagebox.showinfo("Phone access", "Use at least 8 characters.", parent=win)
                return
            new["salt"], new["digest"] = R.hash_password(pw1.get())
        if new["enabled"] and not new.get("digest"):
            messagebox.showinfo("Phone access", "Set a password first.", parent=win)
            return
        app.settings["remote"] = new
        app.save_settings()
        pw1.set("")
        pw2.set("")
        try:
            start_remote(app) if new["enabled"] else stop_remote(app)
        except OSError as e:
            messagebox.showerror("Phone access", f"Couldn't start: {e}\n\nIs the port already in use?", parent=win)
        show_where()

    b = ttk.Frame(f)
    b.grid(row=9, column=0, columnspan=3, sticky="e", pady=(10, 0))
    ttk.Button(b, text="Save", command=save).pack(side="left")
    ttk.Button(b, text="Close", command=win.destroy).pack(side="left", padx=(6, 0))
    show_where()


# ---------------- the "More" menu ----------------

def more_menu(app, widget):
    menu = tk.Menu(app.root, tearoff=0)
    menu.add_command(label="Gallery...", command=lambda: open_gallery(app))
    menu.add_command(label=f"Queue ({len(app.queue)} waiting)...", command=lambda: open_queue_window(app))
    menu.add_command(label="Advanced settings...", command=lambda: open_advanced(app))
    menu.add_separator()
    menu.add_command(label="Wallpaper of the day...", command=lambda: open_wotd(app))
    if "wallpaper_backup" in app.settings:
        menu.add_command(label="Put my previous wallpaper back", command=lambda: restore_wallpaper(app))
    menu.add_command(label="Phone access...", command=lambda: open_remote(app))
    menu.add_separator()
    menu.add_command(label="Check for updates...", command=app.open_updates)
    menu.tk_popup(widget.winfo_rootx(), widget.winfo_rooty() - 10)


def start_from_menu(app, widget):
    menu = tk.Menu(app.root, tearoff=0)
    menu.add_command(label="An earlier picture (its prompt, size and seed)...", command=app.reuse_from_file)
    menu.add_command(label="Write a prompt from a picture...", command=lambda: app.open_describe(None))
    menu.add_command(label="Layout boxes (MOSS: put things in exact places)...", command=lambda: open_layout(app))
    menu.tk_popup(widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height())
