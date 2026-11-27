"""Phone access: everything the phone web app (tools/web) can see and do, run against the open Studio window.

The web server (moss_remote) runs in its own threads. Anything that touches the window or its settings is run on
the window's thread through Bridge.call(); slow work (prompt tools, upscaling, fixing words) runs in a worker thread
as a "task" the phone checks on. Pictures still go through the window's normal queue, so they use the same models,
settings, text check and file details as pictures made at the computer.
"""
import base64
import io
import itertools
import platform
import queue
import re
import secrets
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

import moss_cloud as C
import moss_describe as D
import moss_meta as M
import moss_platform as P
from moss_remote import ApiError

UPLOADS = "uploads"                         # photos sent from a phone, inside the output folder
PICTURE_NAME = re.compile(r"^(?:uploads/)?[^/\\:*?\"<>|\x00-\x1f]+\.(?:png|jpe?g)$", re.I)
MAX_COUNT = 8
_ids = itertools.count(1)


def G():
    return sys.modules.get("moss_gui") or sys.modules["__main__"]


def X():
    import moss_extras
    return moss_extras


def web_dir():
    """The phone app's files: next to this file, or inside the packaged .exe."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "web"


def clamp(value, low, high, kind=int, default=None):
    try:
        return max(low, min(high, kind(value)))
    except (TypeError, ValueError):
        return default


def price_for(spec):
    """Google's price for one Nano Banana picture of this request."""
    if spec["editing"] and spec.get("edit_src"):
        with Image.open(spec["edit_src"]) as im:
            pixels = im.width * im.height
    else:
        pixels = spec["w"] * spec["h"]
    return C.PRICE_USD[C.size_for_pixels(pixels)]


class Bridge:
    def __init__(self, app):
        self.app = app
        self.snapshot = {}
        self.folder = Path(G().DEFAULT_OUT)
        self.tasks = {}                    # id -> task dict
        self.lock = threading.Lock()
        self.gallery_lock = threading.Lock()
        self.last_tick = 0.0
        self.pending_start = False         # start the queue once the graphics card is free again

    # ---------------- running things on the window's thread ----------------
    def call(self, fn, timeout=30):
        if threading.current_thread() is threading.main_thread():
            return fn()
        box = queue.Queue()
        self.app.events.put(("call", (fn, box)))
        try:
            kind, value = box.get(timeout=timeout)
        except queue.Empty:
            raise ApiError("The computer's window didn't answer. Is it busy with a dialog?", 503) from None
        if kind == "err":
            raise value
        return value

    def busy(self):
        """What's using the graphics card right now, or ''. Window thread."""
        app = self.app
        if app.job:
            return "picture"
        if getattr(app, "updating", False):
            return "tool"
        return ""

    def tick(self):
        """Window thread, several times a second: refresh what the phone can read."""
        now = time.time()
        if now - self.last_tick < 0.4:
            return
        self.last_tick = now
        app, gm = self.app, G()
        if self.pending_start and not self.busy():
            self.pending_start = False
            app.queue_start()
        try:
            self.folder = Path(app.out_dir.get().strip() or gm.DEFAULT_OUT)
            job = app.job
            current = None
            if job:
                spec = job.get("spec") or {}
                status = app.status.cget("text")
                drawing = status.startswith("Drawing") and str(app.progress.cget("mode")) == "determinate"
                current = dict(label=spec.get("label") or "", prompt=job.get("prompt", "")[:300],
                               model=job["profile"]["name"], w=job.get("w"), h=job.get("h"),
                               elapsed=round(now - (spec.get("t0") or app.started)),
                               progress=round(float(app.progress["value"]), 1) if drawing else None,
                               phone=bool(spec.get("phone")), fix=bool(job.get("fix")),
                               estimate=gm.estimate(job["w"], job["h"], job["profile"],
                                                    bool(spec.get("editing")) and not spec.get("mask")))
            waiting = [dict(qid=s.get("qid"), label=s.get("label") or X().auto_label(s),
                            model=gm.PROFILES[s["model"]]["name"], phone=bool(s.get("phone")))
                       for s in app.queue]
            finished = []
            for spec, file, error in reversed(app.queue_done[-40:]):
                name = self.name_of(file) if file else None
                finished.append(dict(label=spec.get("label") or X().auto_label(spec), name=name,
                                     error=error if not file else None,
                                     model=gm.PROFILES.get(spec.get("model"), {}).get("name", "")))
            self.snapshot = dict(
                status=app.status.cget("text"), busy=self.busy(), job=current, queue=waiting,
                queue_running=bool(app.queue_running), finished=finished,
                done_count=len(app.queue_done), version=gm.__version__,
                wallpaper_backup="wallpaper_backup" in app.settings)
        except Exception:
            pass

    def name_of(self, path):
        """The name the phone uses for a picture: its file name, or uploads/<name>."""
        path = Path(path)
        try:
            rel = path.resolve().relative_to(self.folder.resolve())
        except ValueError:
            return None
        parts = rel.parts
        if len(parts) == 1:
            return parts[0]
        if len(parts) == 2 and parts[0] == UPLOADS:
            return f"{UPLOADS}/{parts[1]}"
        return None

    def resolve(self, name):
        """A picture the phone may use: in the output folder (top level) or its uploads folder."""
        if not isinstance(name, str) or not PICTURE_NAME.match(name) or ".." in name:
            return None
        path = self.folder / Path(name)
        return path if path.is_file() else None

    def need(self, name):
        path = self.resolve(name)
        if not path:
            raise ApiError("That picture isn't on the computer any more.", 404)
        return path

    # ---------------- reading ----------------
    def state(self):
        with self.lock:
            tasks = [dict(t) for t in sorted(self.tasks.values(), key=lambda t: -t["started"])[:12]]
        return dict(self.snapshot, tasks=tasks, time=time.time())

    def options(self):
        def build():
            app, gm = self.app, G()
            has_key = bool(C.find_key(app.settings))
            models = []
            for key, p in gm.PROFILES.items():
                cloud = bool(p.get("cloud"))
                available = has_key if cloud else not app.missing_files(key)
                adv = app.advanced_for(key)
                models.append(dict(
                    key=key, name=p["name"], note=p.get("note", ""), license=p.get("license", ""), cloud=cloud,
                    available=available, edit=bool(p["edit"]) and (cloud or not app.missing_files(key, edit=True)),
                    no_edit=p.get("no_edit") or "", paint=not cloud, steps=p["steps"], cfg=p["cfg"],
                    sampler=p["sampler"] or "", adv=dict(steps=adv["steps"], cfg=adv["cfg"], negative=adv["negative"],
                                                         sampler=adv["sampler"])))
            screens = [dict(label=label, W=o["W"], H=o["H"], parts=len(o["parts"]))
                       for label, o in app.screen_opts.items()]
            here = lambda label: label.replace("on this PC", "on the computer")  # noqa: E731
            describers = [dict(id=f"local:{i}", label=here(label)) for i, (label, _m, _p) in
                          enumerate(D.local_options(gm.ROOT))]
            enhancers = [dict(id=f"local:{i}", label=here(label) + " (private, free)") for i, (label, _m) in
                         enumerate(D.enhance_options(gm.ROOT))]
            if has_key:
                describers.append(dict(id="cloud", label=f"Google Gemini (cloud, {D.CLOUD_PRICE})"))
                enhancers.append(dict(id="cloud", label="Google Gemini (cloud, about $0.001)"))
            return dict(
                models=models, model=app.model_key, shapes=list(gm.ASPECTS), screens=screens, tiers=gm.TIERS,
                samplers=X().SAMPLERS, describers=describers, enhancers=enhancers,
                upscaler=gm.UPSCALER.exists(), fixer=bool(D.text_reader(gm.ROOT) and gm.fixer_profile()),
                fixer_name=gm.PROFILES[gm.fixer_profile()]["name"] if gm.fixer_profile() else "",
                text_check=bool(app.settings.get("text_check", True)), cloud_key=has_key,
                pc=dict(name=platform.node(), os=P.OS_NAME, version=gm.__version__,
                        screens=[dict(name=s["name"], w=s["w"], h=s["h"]) for s in app.screens]),
                limits=dict(max_pixels=gm.MAX_PIXELS, max_side=gm.MAX_SIDE, count=MAX_COUNT))
        return self.call(build)

    def tokens(self, text):
        try:
            return self.app.count_tokens(G().tidy_prompt(text[:8000]))
        except Exception:
            return None

    def estimate(self, q):
        """Size and time for the form's current choices."""
        def work():
            app, gm = self.app, G()
            key = q.get("model") if q.get("model") in gm.PROFILES else app.model_key
            p = gm.PROFILES[key]
            editing = q.get("mode") == "edit"
            masked = q.get("masked") == "1"
            if editing:
                src = self.resolve(q.get("source", ""))
                if not src:
                    return dict(w=None, h=None, text="Choose the picture to edit.")
                w, h = self.edit_size(src, key)
            else:
                w, h, _screen, _shape, _tier = self.new_size(key, q)
            spec = dict(model=key, editing=editing, mask=masked, w=w, h=h,
                        edit_src=str(src) if editing else None)
            text = f"{w} x {h}, {gm.estimate(w, h, p, editing and not masked)}"
            if p.get("cloud"):
                text = f"{w} x {h} (Google picks the exact size), about ${price_for(spec):.2f} each"
            warn = self.memory_warning(spec) if not p.get("cloud") else ""
            return dict(w=w, h=h, text=text, warning=warn)
        try:
            return self.call(work)
        except ValueError as e:
            return dict(w=None, h=None, text=str(e))

    def gallery(self, q, offset, limit):
        folder = self.folder
        if not folder.exists():
            return dict(items=[], total=0)
        with self.gallery_lock:
            items = X().gallery_items(folder)
        words = [w for w in q.lower().split() if w]
        if words:
            items = [it for it in items if all(w in (it["prompt"] + " " + it["title"] + " " + it["model"] + " " +
                                                     it["path"].name).lower() for w in words)]
        limit = clamp(limit, 1, 200, default=60)
        offset = clamp(offset, 0, 10**7, default=0)
        page = items[offset:offset + limit]
        return dict(total=len(items), offset=offset,
                    items=[dict(name=it["path"].name, prompt=it["prompt"][:400], title=it["title"],
                                model=it["model"], mtime=int(it["mtime"])) for it in page])

    def info(self, name):
        path = self.need(name)
        gm = G()
        try:
            meta = M.read(path)
        except Exception:
            meta = {}
        try:
            prompt, w, h, seed, model = self.app.settings_from(path)
        except Exception:
            prompt, w, h, seed, model = meta.get("comment", ""), None, None, None, None
        with Image.open(path) as im:
            size = im.size
        shape, tier = "", None
        if w and h:
            for a, sizes in gm.ASPECTS.items():
                for t, s in enumerate(sizes):
                    if s == (w, h):
                        shape, tier = a, t
            m = re.search(r"_screens?(\d+)x(\d+)", path.name)
            if m and not shape:
                for label, o in getattr(self.app, "screen_opts", {}).items():
                    if (o["W"], o["H"]) == (int(m.group(1)), int(m.group(2))):
                        for t in range(len(gm.TIERS)):
                            if gm.fit_size(o["W"], o["H"], gm.TIER_PIXELS[t],
                                           align=gm.PROFILES.get(model or "moss", gm.PROFILES["moss"])["align"]) == (w, h):
                                shape, tier = label, t
        parts = X().screen_parts(path)
        return dict(name=self.name_of(path), file=path.name, title=meta.get("title", ""), prompt=prompt or "",
                    settings=meta.get("settings", ""), tags=meta.get("tags", []), date=meta.get("date_taken", ""),
                    authors=meta.get("authors", ""), width=size[0], height=size[1],
                    bytes=path.stat().st_size, mtime=int(path.stat().st_mtime),
                    reuse=dict(model=model, w=w, h=h, seed=seed, shape=shape, tier=tier),
                    words=D.quoted_text(prompt or ""), screen_parts=sorted(parts))

    def picture(self, name, kind):
        """The file to send: the picture itself, a phone-sized copy (view) or a thumbnail."""
        path = self.resolve(name)
        if not path:
            return None
        if kind == "full":
            return path
        size, tag = (1800, "v") if kind == "view" else (420, "m")
        cache = path.parent / ".thumbs" / f"{path.stem}.{tag}.jpg"
        try:
            if not cache.exists() or cache.stat().st_mtime < path.stat().st_mtime:
                cache.parent.mkdir(exist_ok=True)
                with Image.open(path) as im:
                    im.draft("RGB", (size * 2, size * 2))
                    im = im.convert("RGB")
                    im.thumbnail((size, size), Image.Resampling.LANCZOS)
                    tmp = cache.with_suffix(f".{secrets.token_hex(3)}.tmp")
                    im.save(tmp, "JPEG", quality=85 if kind == "thumb" else 88)
                    tmp.replace(cache)
        except OSError:
            return path
        return cache

    # ---------------- sizes ----------------
    def new_size(self, key, q):
        """(w, h, screen option or None, shape, tier label) for a new picture. Window thread."""
        gm = G()
        shape = q.get("shape") or "16:9 wide"
        tier = clamp(q.get("tier", 0), 0, len(gm.TIERS) - 1, default=0)
        if shape == gm.CUSTOM:
            a = gm.PROFILES[key]["align"]
            w, h = clamp(q.get("w"), 1, 99999), clamp(q.get("h"), 1, 99999)
            if not w or not h:
                raise ValueError("Type a width and height.")
            rw, rh = (w + a // 2) // a * a, (h + a // 2) // a * a
            if rw * rh > gm.MAX_PIXELS or max(rw, rh) > gm.MAX_SIDE:
                rw, rh = w // a * a, h // a * a
            if not (256 <= rw <= gm.MAX_SIDE and 256 <= rh <= gm.MAX_SIDE):
                raise ValueError(f"Each side must be between 256 and {gm.MAX_SIDE}.")
            if rw * rh > gm.MAX_PIXELS:
                raise ValueError(f"{rw} x {rh} is too big: the most is {gm.MAX_PIXELS:,} pixels.")
            return rw, rh, None, gm.CUSTOM, ""
        if shape not in gm.ASPECTS and shape not in self.app.screen_opts:
            shape = "16:9 wide"
        w, h, screen = X().size_for(self.app, key, shape, tier)
        return w, h, screen, shape, gm.TIERS[tier]

    def edit_size(self, src, key):
        gm = G()
        align = gm.PROFILES[key]["align"]
        with Image.open(src) as im:
            sw, sh = im.size
        if sw * sh <= gm.MAX_PIXELS and sw % align == 0 and sh % align == 0 and max(sw, sh) <= gm.MAX_SIDE:
            return sw, sh
        return gm.fit_size(sw, sh, min(sw * sh, gm.MAX_PIXELS), max_side=gm.MAX_SIDE, align=align)

    def memory_warning(self, spec):
        try:
            need, free = self.app.memory_need(spec)
        except Exception:
            return ""
        if free is None or free >= need:
            return ""
        return (f"The computer only has about {free:.0f} GB of memory free, and this usually needs about {need:.0f} GB, "
                "so it may fail. A smaller size or a FLUX model needs less" +
                (" (restarting Windows also helps if it has been on for days)." if P.IS_WIN else "."))

    # ---------------- making pictures ----------------
    def make(self, params):
        return self.call(lambda: self._make(params))

    def _make(self, params):
        """Window thread: check the phone's request, add it to the queue, and start the queue."""
        app, gm = self.app, G()
        confirmed = set(params.get("confirmed") or [])
        editing = params.get("mode") == "edit"
        prompt_raw = str(params.get("prompt", ""))[:6000].strip()
        if not prompt_raw:
            raise ApiError("Describe what should change first." if editing else "Type what you want to see first.")
        keys = [k for k in (params.get("compare") or []) if k in gm.PROFILES]
        keys = list(dict.fromkeys(keys))
        if len(keys) < 2:
            keys = [params.get("model")]
        if keys[0] not in gm.PROFILES:
            raise ApiError("Choose a model.")
        count = 1 if len(keys) > 1 else clamp(params.get("count", 1), 1, MAX_COUNT, default=1)

        src = mask = None
        if editing:
            src = self.need(params.get("source", ""))
            mask = self.save_mask(params.get("mask"), src)
        specs = []
        for key in keys:
            p = gm.PROFILES[key]
            if p.get("cloud"):
                if not C.find_key(app.settings):
                    raise ApiError("Nano Banana needs your Gemini API key. Add it in the window on the computer first.")
                if mask:
                    raise ApiError("Nano Banana can't use a painted area. Clear the painted area, or choose a model "
                                   "that runs on the computer.")
            else:
                if editing and not mask and not p["edit"]:
                    raise ApiError((p.get("no_edit") or f"{p['name']} can't follow edit instructions.") +
                                   " Paint the area to change, or pick another model.")
                missing = app.missing_files(key, edit=editing and not mask)
                if missing:
                    raise ApiError(f"{p['name']} isn't fully downloaded on the computer "
                                   f"({len(missing)} file{'s' if len(missing) > 1 else ''} missing).")
            adv = app.advanced_for(key)
            o = params.get("adv") or {}
            if len(keys) == 1 and isinstance(o, dict) and not p.get("cloud"):
                adv["steps"] = clamp(o.get("steps"), 1, 150, default=adv["steps"]) or adv["steps"]
                adv["cfg"] = clamp(o.get("cfg"), 0.0, 30.0, float, default=adv["cfg"]) or adv["cfg"]
                if isinstance(o.get("negative"), str):
                    adv["negative"] = o["negative"][:2000].strip()
                if o.get("sampler") in X().SAMPLERS:
                    adv["sampler"] = o["sampler"] or (p["sampler"] or "")
            prompt = prompt_raw if adv["keep_breaks"] else gm.tidy_prompt(prompt_raw)
            if editing:
                w, h = self.edit_size(src, key)
                screen, shape, tier = None, "", ""
            else:
                try:
                    w, h, screen, shape, tier = self.new_size(key, params)
                except ValueError as e:
                    raise ApiError(str(e)) from None
            seed_in = params.get("seed", "random")
            seed = "random" if seed_in in (None, "", "random") else clamp(seed_in, 0, 2**31 - 1)
            if seed is None:
                raise ApiError("The seed must be a whole number.")
            if p.get("cloud"):
                seed = "n/a"
            short = prompt.replace("\n", " ")[:34]
            for i in range(count):
                label = (f"phone, {p['name']}: {short}" if len(keys) > 1 else
                         f"phone: {short}" + (f" ({i + 1} of {count})" if count > 1 else ""))
                spec = X().base_spec(app, key, prompt, w, h, screen=screen, shape=shape, tier=tier, label=label,
                                     seed=seed if not isinstance(seed, int) else seed + i)
                spec.update(editing=editing, edit_src=str(src) if editing else None,
                            mask=str(mask) if mask else None, adv=adv, phone=True,
                            text_check=bool(params.get("text_check", True)) and
                            (bool(D.quoted_text(prompt)) or (editing and not mask)))
                if screen:
                    up = bool(params.get("screen_upscale", True))
                    spec.update(upscale=up, split=bool(params.get("screen_split", True)) and len(screen["parts"]) > 1,
                                ai_upscale=up and gm.UPSCALER.exists())
                specs.append(spec)

        cloud = [s for s in specs if gm.PROFILES[s["model"]].get("cloud")]
        if cloud and "cost" not in confirmed:
            total = sum(price_for(s) for s in cloud)
            sent = "your prompt and the picture" if editing else "your prompt"
            raise ApiError(f"Nano Banana sends {sent} to Google, and is billed to your Gemini API key: about "
                           f"${total:.2f} for {len(cloud)} picture{'s' if len(cloud) > 1 else ''}. Pictures from Google "
                           "carry its invisible SynthID watermark. Go ahead?", 409, confirm="cost")
        if "memory" not in confirmed:
            warn = next((w for w in (self.memory_warning(s) for s in specs if s not in cloud) if w), "")
            if warn:
                raise ApiError(warn + ". Try anyway?", 409, confirm="memory")
        if cloud:
            app.settings["cloud_ok"] = True
        for spec in specs:
            app.queue_add(spec, label=spec["label"])
        tool_busy = self.busy() == "tool"
        ahead = len(app.queue) - len(specs) + (1 if app.job else 0)
        if tool_busy:
            self.pending_start = True
        else:
            app.queue_start()
        n = len(specs)
        what = f"{n} pictures" if n > 1 else "Your picture"
        if tool_busy:
            msg = f"{what} will start when the computer finishes what it's doing."
        elif ahead:
            msg = f"{what} {'are' if n > 1 else 'is'} in the queue, after {ahead} other{'s' if ahead > 1 else ''}."
        else:
            msg = f"{'Started ' + str(n) + ' pictures' if n > 1 else 'Started'}."
        app.status.configure(text=f"Phone request added ({len(app.queue)} waiting)." if app.queue else app.status.cget("text"))
        return dict(message=msg, queued=n)

    def save_mask(self, data, src):
        """The painted area from the phone (a PNG data URL, white = change) as a mask file, or None."""
        if not data:
            return None
        if not isinstance(data, str) or not data.startswith("data:image/png;base64,"):
            raise ApiError("The painted area couldn't be read. Paint it again.")
        try:
            raw = base64.b64decode(data.split(",", 1)[1], validate=True)
            with Image.open(io.BytesIO(raw)) as m:
                if m.width * m.height > 40_000_000:
                    raise ValueError
                mask = m.convert("L")
            with Image.open(src) as im:
                size = im.size
            if mask.size != size:
                mask = mask.resize(size, Image.Resampling.BILINEAR)
            mask = mask.point(lambda v: 255 if v >= 128 else 0)
        except Exception:
            raise ApiError("The painted area couldn't be read. Paint it again.") from None
        if not mask.getbbox():
            return None
        folder = self.folder / ".masks"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{src.stem}_phone{datetime.now():%H%M%S}_mask.png"
        mask.save(path)
        return path

    def cancel(self, params):
        def work():
            if not self.app.job:
                return dict(message="Nothing is being made right now.")
            self.app.cancel()
            return dict(message="Cancelled. The queue is paused." if self.app.queue else "Cancelled.")
        return self.call(work)

    def queue_action(self, params):
        def work():
            app = self.app
            action = params.get("action")
            if action == "remove":
                for i, spec in enumerate(app.queue):
                    if spec.get("qid") == params.get("qid"):
                        app.queue.pop(i)
                        if spec.get("group") in app.groups:
                            app.groups[spec["group"]]["total"] -= 1
                        break
                msg = "Removed."
            elif action == "clear":
                n = len(app.queue)
                for spec in app.queue:
                    if spec.get("group") in app.groups:
                        app.groups[spec["group"]]["total"] -= 1
                app.queue.clear()
                msg = f"Removed {n} waiting."
            elif action == "run":
                if self.busy() == "tool":
                    self.pending_start = True
                    return dict(message="The queue will run when the computer finishes what it's doing.")
                app.queue_start()
                msg = "Running the queue." if app.queue or app.job else "The queue is empty."
            elif action == "stop":
                app.queue_running = False
                msg = "The queue will stop after the current picture."
            else:
                raise ApiError("Unknown queue action.")
            X().refresh_queue_window(app)
            self.last_tick = 0
            self.tick()
            return dict(message=msg)
        return self.call(work)

    # ---------------- photos from the phone ----------------
    def upload(self, data):
        if not data:
            raise ApiError("No picture was sent.")
        gm = G()
        try:
            with Image.open(io.BytesIO(data)) as im:
                if im.width * im.height > 120_000_000:
                    raise ApiError("That picture is too big.")
                im = ImageOps.exif_transpose(im)
                im = im.convert("RGB")
        except ApiError:
            raise
        except Exception:
            raise ApiError("That picture couldn't be read. Try a JPEG or PNG (on an iPhone, choose "
                           "\"Most Compatible\" in Settings > Camera > Formats, or take a screenshot of it).") from None
        if im.width * im.height > gm.MAX_PIXELS:               # bigger than any picture made here: shrink it
            scale = (gm.MAX_PIXELS / (im.width * im.height)) ** 0.5
            im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))), Image.Resampling.LANCZOS)
        if min(im.size) < 64:
            raise ApiError("That picture is too small.")
        folder = self.folder / UPLOADS
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"phone_{datetime.now():%Y-%m-%d_%H-%M-%S}_{secrets.token_hex(2)}.png"
        im.save(path, "PNG", compress_level=3)
        return dict(name=f"{UPLOADS}/{path.name}", width=im.width, height=im.height)

    # ---------------- picture actions ----------------
    def delete(self, params):
        path = self.need(params.get("name", ""))
        def in_use():
            app = self.app
            job = app.job or {}
            used = {str(s.get("edit_src")) for s in app.queue} | {str((job.get("spec") or {}).get("edit_src"))}
            return str(path) in used
        if self.call(in_use):
            raise ApiError("That picture is being edited (or waiting to be). Try again when it's done.", 409)
        extras = [path.with_suffix(".txt")] + list(X().screen_parts(path).values())
        P.move_to_trash(path)
        for extra in extras:
            if extra.exists():
                try:
                    P.move_to_trash(extra)
                except OSError:
                    pass
        for cache in path.parent.joinpath(".thumbs").glob(f"{path.stem}.*jpg"):
            cache.unlink(missing_ok=True)
        where = "Recycle Bin" if P.IS_WIN else "Trash"
        return dict(message=f"Moved to the {where} on the computer.")

    def wallpaper(self, params):
        path = self.need(params.get("name", ""))
        how = params.get("how")
        if how not in ("per-screen", "span", "all", "one"):
            raise ApiError("Choose how to set the wallpaper.")

        def work():
            screen = None
            if how == "one":
                i = clamp(params.get("screen"), 0, 99)
                if i is None or i >= len(self.app.screens):
                    raise ApiError("That screen isn't connected.")
                screen = self.app.screens[i]
            try:
                message = X().apply_wallpaper(self.app, path, how, screen)
            except Exception as e:
                raise ApiError(f"The wallpaper couldn't be set: {e}") from None
            self.app.status.configure(text="From the phone: " + message)
            return dict(message=message)
        return self.call(work)

    def wallpaper_restore(self, params):
        def work():
            app = self.app
            if "wallpaper_backup" not in app.settings:
                return dict(message="There's no earlier wallpaper saved.")
            try:
                X()._wallpaper().restore(app.settings["wallpaper_backup"])
            except Exception as e:
                raise ApiError(f"The previous wallpaper couldn't be put back: {e}") from None
            app.settings.pop("wallpaper_backup", None)
            app.save_settings()
            return dict(message="The previous wallpaper is back.")
        return self.call(work)

    # ---------------- slower tools, run as tasks ----------------
    def task(self, task_id):
        with self.lock:
            t = self.tasks.get(task_id)
            if not t:
                raise ApiError("That job isn't known (the app may have restarted).", 404)
            return dict(t)

    def start_task(self, params):
        kind = params.get("kind")
        gm = G()
        if kind == "enhance":
            idea = str(params.get("idea", "")).strip()[:4000]
            if not idea:
                raise ApiError("Type an idea first.")
            using, length = params.get("using", ""), "Short" if params.get("length") == "Short" else "Detailed"
            gpu = using.startswith("local:")
            if gpu:
                opts = D.enhance_options(gm.ROOT)
                i = clamp(using[6:], 0, 99)
                if i is None or i >= len(opts):
                    raise ApiError("That enhancer isn't on the computer.")
                model = opts[i][1]
                work = lambda say: D.enhance_local(gm.ROOT, model, idea, length)  # noqa: E731
            else:
                key = self.call(lambda: C.find_key(self.app.settings))
                if not key:
                    raise ApiError("The cloud enhancer needs your Gemini API key (add it on the computer).")
                work = lambda say: D.enhance_cloud(key, idea, length)  # noqa: E731
            title = "Improving the prompt"
        elif kind == "describe":
            src = self.need(params.get("name", ""))
            using, length = params.get("using", ""), "Short" if params.get("length") == "Short" else "Detailed"
            gpu = using.startswith("local:")
            if gpu:
                opts = D.local_options(gm.ROOT)
                i = clamp(using[6:], 0, 99)
                if i is None or i >= len(opts):
                    raise ApiError("That describer isn't on the computer.")
                _label, model, proj = opts[i]
                work = lambda say: D.describe_local(gm.ROOT, model, proj, src, length)  # noqa: E731
            else:
                key = self.call(lambda: C.find_key(self.app.settings))
                if not key:
                    raise ApiError("The cloud describer needs your Gemini API key (add it on the computer).")
                work = lambda say: D.describe_cloud(key, src, length)  # noqa: E731
            title = "Writing a prompt from the picture"
        elif kind == "upscale":
            src = self.need(params.get("name", ""))
            factor = 4 if params.get("factor") == 4 else 2
            if not gm.UPSCALER.exists():
                raise ApiError("The AI upscaler isn't downloaded on the computer.")
            with Image.open(src) as im:
                big = im.width * factor * im.height * factor
            if big > 200e6:
                raise ApiError(f"A {factor}x copy would be {big / 1e6:.0f} megapixels, too big. Try 2x.")
            gpu = True
            work = lambda say: self.name_of(gm.upscale_picture(src, factor, say))  # noqa: E731
            title = f"Making a {factor}x bigger copy"
        elif kind == "fixwords":
            src = self.need(params.get("name", ""))
            expected = [ln.strip() for ln in str(params.get("words", "")).splitlines() if ln.strip()][:20]
            if not expected:
                raise ApiError("Type the words the picture should show.")
            if not (D.text_reader(gm.ROOT) and gm.fixer_profile()):
                raise ApiError("Fixing words needs Qwen Image (or another image model) and its Qwen3-VL files on "
                               "the computer.")
            try:
                prompt = M.read(src).get("comment", "")
            except Exception:
                prompt = ""
            gpu = True

            def work(say):
                out = gm.fix_name(src)
                gm.fix_words(src, expected, say, out, prompt=prompt)
                reader = D.text_reader(gm.ROOT)
                seen = D.read_text_local(gm.ROOT, reader[0], reader[1], out)
                ok = D.text_score(expected, seen)[0]
                return dict(name=self.name_of(out), ok=ok, seen=" / ".join(seen)[:200])
            title = "Fixing the words"
        else:
            raise ApiError("Unknown tool.")

        task = dict(id=secrets.token_hex(6), kind=kind, title=title, state="running", message=title + "...",
                    result=None, error=None, started=time.time(), finished=None)

        def claim():
            if gpu:
                what = self.busy()
                if what:
                    raise ApiError(("The computer is busy making a picture" if what == "picture" else
                                    "The graphics card is busy with another job") +
                                   ". Try again when it's done" + (" (or use the cloud option)."
                                                                   if kind in ("enhance", "describe") else "."), 409)
                self.app.updating = True
                self.app.progress.configure(mode="indeterminate")
                self.app.progress.start(12)
                self.app.status.configure(text=f"From the phone: {title.lower()}...")
                P.keep_awake(True)
        self.call(claim)
        with self.lock:
            self.tasks[task["id"]] = task
            for old in sorted(self.tasks.values(), key=lambda t: t["started"])[:-20]:
                self.tasks.pop(old["id"], None)

        def say(message):
            task["message"] = message
            self.app.events.put(("status", message))

        def release(note):
            if gpu:
                self.app.updating = False
                self.app.progress.stop()
                self.app.progress.configure(mode="determinate")
                self.app.progress["value"] = 0
                self.app.status.configure(text=note)
                P.keep_awake(False)

        def run():
            try:
                result = work(say)
                task.update(state="done", result=result, message="Done.", finished=time.time())
                note = f"From the phone: {title.lower()}: done."
                if kind in ("upscale", "fixwords"):
                    name = result if isinstance(result, str) else result.get("name")
                    note = f"From the phone: saved {Path(name or '').name}."
            except Exception as e:
                task.update(state="failed", error=str(e) or e.__class__.__name__, message="Didn't work.",
                            finished=time.time())
                note = f"From the phone: {title.lower()} didn't work."
            self.app.events.put(("call", (lambda: release(note), queue.Queue())))
        threading.Thread(target=run, daemon=True).start()
        return dict(task)
