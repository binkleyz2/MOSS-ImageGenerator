"""Check for and apply updates to MOSS Image Studio's model files and engines.

Model files: each downloaded file is compared with its source on Hugging Face by SHA-256 (the X-Linked-ETag
header). Local checksums are cached in models\\.checksums.json, keyed by size and modification time, so
only new or changed files are hashed again.

Engines: stable-diffusion.cpp (bin\\) and llama.cpp (llm-bin\\) are compared with their latest GitHub releases.
An engine update unpacks into a new folder, swaps it in, keeps the previous version as <folder>-previous,
and the caller can roll back with undo_engine() if its self-test fails.

All network and disk work here is meant to run in a worker thread; progress is reported through `say(text)`.
"""
import hashlib
import json
import os
import shutil
import time
import urllib.error
import urllib.request
import zipfile

HF = "https://huggingface.co/"
UA = {"User-Agent": "MOSS-Image-Studio-updater"}

# models\ path -> Hugging Face path (repo/resolve/main/file)
MODEL_SOURCES = {
    "moss_image_sft_int8_convrot.safetensors": "Comfy-Org/MOSS-Image/resolve/main/diffusion_models/moss_image_sft_int8_convrot.safetensors",
    "flux2-vae.safetensors": "Comfy-Org/MOSS-Image/resolve/main/vae/flux2-vae.safetensors",
    "Qwen3.5-9B-Q4_K_M.gguf": "unsloth/Qwen3.5-9B-GGUF/resolve/main/Qwen3.5-9B-Q4_K_M.gguf",
    "tokenizer_qwen3.5.json": "OpenMOSS-Team/MOSS-Image-Base/resolve/main/tokenizer/tokenizer.json",
    "Qwen3.5-9B-mmproj-BF16.gguf": "unsloth/Qwen3.5-9B-GGUF/resolve/main/mmproj-BF16.gguf",
    "loras/armV5-moss-edit-lora.safetensors": "akhaliq/MOSS-Image-Edit-HQ/resolve/main/armV5-moss-edit-lora.safetensors",
    "flux2-klein-4b/flux-2-klein-4b-Q8_0.gguf": "leejet/FLUX.2-klein-4B-GGUF/resolve/main/flux-2-klein-4b-Q8_0.gguf",
    "flux2-klein-4b/Qwen3-4B-Q8_0.gguf": "unsloth/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q8_0.gguf",
    "qwen-image-2.1/qwen_image_2.1_int8_convrot.safetensors": "Comfy-Org/Qwen-Image-2.1/resolve/main/diffusion_models/qwen_image_2.1_int8_convrot.safetensors",
    "qwen-image-2.1/qwen_image_2.1_vae_bf16.safetensors": "Comfy-Org/Qwen-Image-2.1/resolve/main/vae/qwen_image_2.1_vae_bf16.safetensors",
    "qwen-image-2.1/Qwen3VL-8B-Instruct-Q4_K_M.gguf": "Qwen/Qwen3-VL-8B-Instruct-GGUF/resolve/main/Qwen3VL-8B-Instruct-Q4_K_M.gguf",
    "qwen-image-2.1/mmproj-Qwen3VL-8B-Instruct-F16.gguf": "Qwen/Qwen3-VL-8B-Instruct-GGUF/resolve/main/mmproj-Qwen3VL-8B-Instruct-F16.gguf",
}

ENGINES = {
    "sd": dict(name="Image engine (stable-diffusion.cpp)", repo="leejet/stable-diffusion.cpp", folder="bin",
               assets=lambda tag: [f"sd-master-{tag.rsplit('-', 1)[-1]}-bin-win-cuda12-x64.zip",
                                   "cudart-sd-bin-win-cu12-x64.zip"]),
    "llama": dict(name="Describer engine (llama.cpp)", repo="ggml-org/llama.cpp", folder="llm-bin",
                  prereleases=True,          # llama.cpp marks its regular bNNNNN builds as pre-releases
                  assets=lambda tag: [f"llama-{tag}-bin-win-cuda-12.4-x64.zip"]),
}
CUDA_DLLS = ["cudart64_12.dll", "cublas64_12.dll", "cublasLt64_12.dll"]   # llama.cpp borrows these from bin\


def _request(url, method="GET", timeout=60):
    return urllib.request.Request(url, method=method, headers=UA)


def fmt_size(n):
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


# ---------------- model files ----------------

def remote_model_info(hf_path):
    """(size, sha256 or None) of a Hugging Face file, without downloading it."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    try:
        reply = opener.open(_request(HF + hf_path, "HEAD"), timeout=30)
    except urllib.error.HTTPError as e:
        reply = e                                   # 302 to the file store is expected
    size = reply.headers.get("X-Linked-Size") or reply.headers.get("Content-Length")
    etag = (reply.headers.get("X-Linked-ETag") or "").strip('"')
    return (int(size) if size else None), (etag if len(etag) == 64 else None)


def _checksum_cache(models):
    try:
        return json.loads((models / ".checksums.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def local_sha256(models, rel, cache, say=None):
    path = models / rel
    st = path.stat()
    entry = cache.get(rel)
    if entry and entry.get("size") == st.st_size and entry.get("mtime") == st.st_mtime_ns:
        return entry["sha256"]
    h, done = hashlib.sha256(), 0
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(16 << 20), b""):
            h.update(block)
            done += len(block)
            if say and done % (1 << 30) < (16 << 20):
                say(f"Checking {rel} on this PC ({done / st.st_size:.0%})...")
    cache[rel] = dict(size=st.st_size, mtime=st.st_mtime_ns, sha256=h.hexdigest())
    return cache[rel]["sha256"]


def check_models(root, say):
    """List of dicts: rel, status ('current', 'update', 'unknown', 'error'), size, detail."""
    models = root / "models"
    cache = _checksum_cache(models)
    results = []
    present = [rel for rel in MODEL_SOURCES if (models / rel).exists()]
    for i, rel in enumerate(present, 1):
        say(f"Checking model files ({i} of {len(present)}): {rel}")
        try:
            size, sha = remote_model_info(MODEL_SOURCES[rel])
            mine = local_sha256(models, rel, cache, say)
            if sha:
                status = "current" if sha == mine else "update"
            else:
                status = "current" if size == (models / rel).stat().st_size else "update"
            results.append(dict(rel=rel, status=status, size=size or 0, sha=sha, detail=""))
        except Exception as e:
            results.append(dict(rel=rel, status="error", size=0, sha=None, detail=str(e)))
    try:
        (models / ".checksums.json").write_text(json.dumps(cache, indent=1), encoding="utf-8")
    except OSError:
        pass
    return results


def _download(url, dest, size, sha, say, label):
    """Fresh download to dest.part, checked by size and SHA-256, then renamed. Old file is kept until then."""
    part = dest.with_name(dest.name + ".part")
    for attempt in range(1, 4):
        try:
            h, done, t0 = hashlib.sha256(), 0, time.time()
            with urllib.request.urlopen(_request(url), timeout=60) as r, open(part, "wb") as f:
                while True:
                    block = r.read(8 << 20)
                    if not block:
                        break
                    f.write(block)
                    h.update(block)
                    done += len(block)
                    rate = done / max(time.time() - t0, 0.1) / 1e6
                    say(f"Downloading {label}: {fmt_size(done)}" + (f" of {fmt_size(size)}" if size else "")
                        + f" ({rate:.0f} MB/s)")
            if size and done != size:
                raise IOError(f"got {done} bytes, expected {size}")
            if sha and h.hexdigest() != sha:
                raise IOError("checksum mismatch")
            part.replace(dest)
            return
        except Exception as e:
            say(f"Download problem ({e}); trying again ({attempt} of 3)...")
            time.sleep(3)
    part.unlink(missing_ok=True)
    raise IOError(f"Could not download {label} correctly. The old file was kept.")


def update_model(root, item, say):
    models = root / "models"
    rel = item["rel"]
    size, sha = remote_model_info(MODEL_SOURCES[rel])
    _download(HF + MODEL_SOURCES[rel], models / rel, size, sha, say, rel)
    cache = _checksum_cache(models)
    st = (models / rel).stat()
    if sha:
        cache[rel] = dict(size=st.st_size, mtime=st.st_mtime_ns, sha256=sha)
        (models / ".checksums.json").write_text(json.dumps(cache, indent=1), encoding="utf-8")


# ---------------- engines ----------------

def installed_tag(root, key):
    folder = root / ENGINES[key]["folder"]
    try:
        words = (folder / "build.txt").read_text(encoding="utf-8").split()
    except OSError:
        return None
    for w in words:                                  # e.g. "nvidia master-1127-79dc7ca" or "b12728"
        if w.startswith("master-") or (w.startswith("b") and w[1:].isdigit()):
            return w
    return None


def _tag_number(tag):
    digits = tag.split("-")[1] if tag.startswith("master-") else tag.lstrip("b")
    return int(digits) if digits.isdigit() else -1


def latest_release(key):
    """(tag, assets by name) of the newest release that has this engine's Windows CUDA files.
    GitHub's "latest" marker isn't used: llama.cpp's can point at an unrelated release."""
    eng = ENGINES[key]
    url = f"https://api.github.com/repos/{eng['repo']}/releases?per_page=15"
    with urllib.request.urlopen(_request(url), timeout=30) as r:
        releases = json.loads(r.read().decode("utf-8"))
    best = None
    for rel in releases:
        tag = rel.get("tag_name", "")
        if rel.get("draft") or (rel.get("prerelease") and not eng.get("prereleases")) or _tag_number(tag) < 0:
            continue
        assets = {a["name"]: a for a in rel.get("assets", [])}
        if all(n in assets for n in eng["assets"](tag)) and (best is None or _tag_number(tag) > _tag_number(best[0])):
            best = (tag, assets)
    if not best:
        raise IOError("no recent release has the Windows CUDA files")
    return best


def install_item(key):
    """An update item for installing an engine that isn't on this PC yet (e.g. the describer)."""
    tag, assets = latest_release(key)
    files = [assets[n] for n in ENGINES[key]["assets"](tag)]
    return dict(key=key, status="update", mine=None, tag=tag, assets=files,
                size=sum(a["size"] for a in files), detail="")


def check_engines(root, say):
    results = []
    for key, eng in ENGINES.items():
        if not (root / eng["folder"]).exists():
            continue
        say(f"Checking {eng['name']}...")
        mine = installed_tag(root, key)
        try:
            tag, assets = latest_release(key)
            wanted = eng["assets"](tag)
            files = [assets.get(n) for n in wanted]
            if not all(files):
                results.append(dict(key=key, status="error", mine=mine, tag=tag, size=0,
                                    detail="the new release doesn't have the Windows CUDA files yet"))
                continue
            newer = mine is None or _tag_number(tag) > _tag_number(mine)
            size = sum(a["size"] for a in files if a["name"] != "cudart-sd-bin-win-cu12-x64.zip")
            results.append(dict(key=key, status="update" if newer else "current", mine=mine, tag=tag,
                                size=size, assets=files, detail=""))
        except Exception as e:
            results.append(dict(key=key, status="error", mine=mine, tag=None, size=0, detail=str(e)))
    return results


def update_engine(root, item, say):
    """Download, unpack and swap in a new engine. The old one is kept as <folder>-previous."""
    key, eng = item["key"], ENGINES[item["key"]]
    folder = root / eng["folder"]
    staging = root / f"{eng['folder']}-update"
    new = root / f"{eng['folder']}-new"
    previous = root / f"{eng['folder']}-previous"
    staging.mkdir(exist_ok=True)
    shutil.rmtree(new, ignore_errors=True)
    new.mkdir()
    for asset in item["assets"]:
        name = asset["name"]
        if name == "cudart-sd-bin-win-cu12-x64.zip" and all((folder / d).exists() for d in CUDA_DLLS):
            for d in CUDA_DLLS:                      # same CUDA 12 runtime: reuse instead of 560 MB download
                shutil.copy2(folder / d, new / d)
            continue
        digest = (asset.get("digest") or "").replace("sha256:", "") or None
        zip_path = staging / name
        _download(asset["browser_download_url"], zip_path, asset["size"], digest, say, name)
        say(f"Unpacking {name}...")
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(new)
    if key == "llama":
        for d in CUDA_DLLS:
            if (root / "bin" / d).exists():
                shutil.copy2(root / "bin" / d, new / d)
    exe = "sd-cli.exe" if key == "sd" else "llama-mtmd-cli.exe"
    if not (new / exe).exists():
        found = next(new.rglob(exe), None)
        if not found:
            raise IOError(f"{exe} wasn't in the download")
        for p in found.parent.iterdir():
            shutil.move(str(p), str(new / p.name))
    old_build = (folder / "build.txt").read_text(encoding="utf-8").split() if (folder / "build.txt").exists() else []
    prefix = [w for w in old_build if not (w.startswith("master-") or (w.startswith("b") and w[1:].isdigit()))]
    (new / "build.txt").write_text(" ".join(prefix + [item["tag"]]) + "\r\n", encoding="utf-8")
    say("Switching to the new version...")
    if folder.exists():                              # an update: keep the old one for undo
        shutil.rmtree(previous, ignore_errors=True)
        os.replace(folder, previous)
    os.replace(new, folder)
    shutil.rmtree(staging, ignore_errors=True)


def can_undo(root, key):
    return (root / f"{ENGINES[key]['folder']}-previous").exists()


def undo_engine(root, key):
    """Put the previous engine back (the newer one is removed)."""
    eng = ENGINES[key]
    folder, previous = root / eng["folder"], root / f"{eng['folder']}-previous"
    if not previous.exists():
        raise IOError("There is no previous version to go back to.")
    broken = root / f"{eng['folder']}-removed"
    shutil.rmtree(broken, ignore_errors=True)
    os.replace(folder, broken)
    os.replace(previous, folder)
    shutil.rmtree(broken, ignore_errors=True)


# ---------------- this app ----------------
# The project is private on GitHub, so the app is checked and downloaded through the GitHub CLI (gh), which
# uses the sign-in already on this PC. Without gh it just says so.

APP_REPO = "binkleyz2/MOSS-ImageGenerator"
APP_ASSET = "MOSS-Image-Studio.exe"


def item_name(item):
    if item.get("key") == "app":
        return "MOSS Image Studio (this app)"
    return ENGINES[item["key"]]["name"] if "key" in item else item.get("rel", "")


def _gh():
    found = shutil.which("gh")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("LOCALAPPDATA", "")):
        p = os.path.join(base, "GitHub CLI", "gh.exe")
        if base and os.path.exists(p):
            return p
    return None


def _run_gh(args, timeout=60):
    import subprocess
    gh = _gh()
    if not gh:
        raise IOError("needs the GitHub CLI (gh), signed in, because the project is private")
    run = subprocess.run([gh] + args, capture_output=True, timeout=timeout,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if run.returncode != 0:
        err = run.stderr.decode("utf-8", "replace").strip()
        if "auth" in err.lower() or "login" in err.lower():
            raise IOError("the GitHub CLI isn't signed in (run: gh auth login)")
        raise IOError((err.splitlines() or ["GitHub CLI failed"])[-1])
    return run.stdout.decode("utf-8", "replace")


def _version(v):
    return tuple(int(x) if x.isdigit() else 0 for x in v.lstrip("v").split("."))


def check_app(version):
    """Update item for this app, compared with the project's latest release."""
    try:
        info = json.loads(_run_gh(["release", "view", "--repo", APP_REPO, "--json", "tagName,assets"]))
        tag = info["tagName"]
        asset = next((a for a in info.get("assets", []) if a["name"] == APP_ASSET), None)
        if not asset:
            return dict(key="app", status="error", mine=version, tag=tag, size=0,
                        detail=f"release {tag} has no {APP_ASSET}")
        newer = _version(tag) > _version(version)
        sha = (asset.get("digest") or "").split(":", 1)[-1]
        return dict(key="app", status="update" if newer else "current", mine=version, tag=tag,
                    size=asset["size"], sha=sha if len(sha) == 64 else None, detail="")
    except Exception as e:
        return dict(key="app", status="error", mine=version, tag=None, size=0, detail=str(e))


def _exe():
    import sys
    return os.path.abspath(sys.executable)


def update_app(item, say):
    """Download the new .exe and put it in place of the running one (Windows allows renaming a running .exe;
    the old one is kept as MOSS-Image-Studio.old.exe until the next start)."""
    import sys
    import tempfile
    if not getattr(sys, "frozen", False):
        raise IOError("this copy runs from source; update it with git pull")
    exe = _exe()
    folder = os.path.dirname(exe)
    tmp = tempfile.mkdtemp(prefix="moss-app-", dir=folder)
    try:
        say(f"Downloading MOSS Image Studio {item['tag']} ({fmt_size(item['size'])})...")
        _run_gh(["release", "download", item["tag"], "--repo", APP_REPO, "--pattern", APP_ASSET,
                 "--dir", tmp, "--clobber"], timeout=1800)
        new = os.path.join(tmp, APP_ASSET)
        if os.path.getsize(new) != item["size"]:
            raise IOError("the download is incomplete")
        if item.get("sha"):
            h = hashlib.sha256()
            with open(new, "rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    h.update(block)
            if h.hexdigest() != item["sha"]:
                raise IOError("the download doesn't match its checksum")
        old = os.path.join(folder, "MOSS-Image-Studio.old.exe")
        if os.path.exists(old):
            os.remove(old)
        os.replace(exe, old)
        try:
            shutil.move(new, exe)
        except Exception:
            os.replace(old, exe)
            raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def remove_old_app():
    """Delete the .exe an app update replaced, once the new one is running."""
    import sys
    if getattr(sys, "frozen", False):
        try:
            os.remove(os.path.join(os.path.dirname(_exe()), "MOSS-Image-Studio.old.exe"))
        except OSError:
            pass
