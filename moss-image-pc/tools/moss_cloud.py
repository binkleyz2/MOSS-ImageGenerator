"""Nano Banana (Google Gemini image models) for MOSS Image Studio, using only the standard library.

Requests go to Google's Interactions API with the user's own Gemini API key. On Windows the key is stored
encrypted with DPAPI (only the same Windows account on the same PC can read it); on a Mac it is kept in
the Keychain (see moss_platform).
Set MOSS_CLOUD_URL to point at a different endpoint (used for offline testing).
"""
import base64
import json
import math
import os
import urllib.error
import urllib.request

import moss_platform as P

API_URL = os.environ.get("MOSS_CLOUD_URL", "https://generativelanguage.googleapis.com/v1beta/interactions")
API_REVISION = "2026-05-20"
MODEL_ID = "gemini-nano-banana-2.1"
KEY_PAGE = "https://aistudio.google.com/apikey"

# Output sizes for Nano Banana 2.1 at "1K"; "2K" and "4K" are exactly 2x and 4x (from Google's table).
SIZES_1K = {"1:1": (1024, 1024), "1:4": (512, 2048), "1:8": (384, 3072), "2:3": (848, 1264),
            "3:2": (1264, 848), "3:4": (896, 1200), "4:1": (2048, 512), "4:3": (1200, 896),
            "4:5": (928, 1152), "5:4": (1152, 928), "8:1": (3072, 384), "9:16": (768, 1376),
            "16:9": (1376, 768), "21:9": (1584, 672)}
SCALE = {"1K": 1, "2K": 2, "4K": 4}
PRICE_USD = {"1K": 0.0336, "2K": 0.0504, "4K": 0.113}   # Google's standard price per picture


class CloudError(Exception):
    pass


# ---------------- sizes ----------------

def nearest_aspect(w, h):
    return min(SIZES_1K, key=lambda a: abs(math.log((w / h) / (SIZES_1K[a][0] / SIZES_1K[a][1]))))


def size_for_pixels(pixels):
    """Smallest Google size that gives at least about the wanted number of pixels."""
    for name in ("1K", "2K", "4K"):
        if (1024 * SCALE[name]) ** 2 >= pixels * 0.9:
            return name
    return "4K"


def plan(w, h):
    """(aspect ratio, size name, Google's output w x h, final w x h after trimming to w:h)."""
    aspect = nearest_aspect(w, h)
    size = size_for_pixels(w * h)
    ow, oh = SIZES_1K[aspect][0] * SCALE[size], SIZES_1K[aspect][1] * SCALE[size]
    return aspect, size, (ow, oh), crop_size(ow, oh, w / h)


def crop_size(ow, oh, ratio):
    """Largest ratio-shaped size that fits in ow x oh (the picture is trimmed to it)."""
    if ow / oh > ratio:
        return round(oh * ratio), oh
    return ow, round(ow / ratio)


# ---------------- API key (Windows DPAPI, or the macOS Keychain) ----------------

def encrypt_key(key):
    """Keep the key safe; returns what goes in the settings file."""
    return P.store_key(key)


def decrypt_key(stored):
    return P.load_key(stored)


def forget_key(stored):
    P.forget_key(stored)


def find_key(settings):
    """The API key: from the GEMINI_API_KEY environment variable, or the encrypted saved one."""
    env = os.environ.get("GEMINI_API_KEY", "").strip()
    if env:
        return env
    stored = settings.get("gemini_key")
    if stored:
        try:
            return decrypt_key(stored)
        except Exception:
            return None
    return None


# ---------------- the request ----------------

def _walk(node, images, texts):
    """Collect base64 images and text blocks anywhere in the response, in order."""
    if isinstance(node, dict):
        kind = node.get("type")
        data = node.get("data")
        mime = node.get("mime_type") or node.get("mimeType") or ""
        if isinstance(data, str) and (kind == "image" or mime.startswith("image/")):
            images.append(data)
        elif kind == "text" and isinstance(node.get("text"), str):
            texts.append(node["text"])
        for key in ("inline_data", "inlineData"):          # older Gemini response style
            inner = node.get(key)
            if isinstance(inner, dict) and isinstance(inner.get("data"), str):
                images.append(inner["data"])
        for key, value in node.items():
            if key not in ("inline_data", "inlineData") and not (key == "data" and isinstance(value, str)):
                _walk(value, images, texts)
    elif isinstance(node, list):
        for item in node:
            _walk(item, images, texts)


def extract(response):
    """(image bytes or None, list of text the model returned). The last image is the final one."""
    steps = response.get("steps") if isinstance(response, dict) else None
    images, texts = [], []
    if isinstance(steps, list):
        outputs = [s for s in steps if isinstance(s, dict) and s.get("type") == "model_output"]
        _walk(outputs or steps, images, texts)
    if not images:                     # look through the whole response instead
        all_images, all_texts = [], []
        _walk(response, all_images, all_texts)
        images, texts = all_images, texts or all_texts
    return (base64.b64decode(images[-1]) if images else None), texts


def generate(api_key, prompt, aspect=None, size="1K", image_bytes=None, image_mime="image/png", timeout=300):
    """Make (or, with image_bytes, edit) a picture. Returns the picture's bytes; raises CloudError."""
    parts = [{"type": "text", "text": prompt}]
    if image_bytes:
        parts.append({"type": "image", "mime_type": image_mime,
                      "data": base64.b64encode(image_bytes).decode("ascii")})
    # Google only returns JPEG here ("image/png" is refused); the app re-saves it as PNG or JPEG.
    response_format = {"type": "image", "mime_type": "image/jpeg", "image_size": size}
    if aspect:
        response_format["aspect_ratio"] = aspect
    body = {"model": MODEL_ID, "input": parts, "response_format": response_format}
    data = post(api_key, body, timeout)
    picture, texts = extract(data)
    if picture is None:
        said = " ".join(t.strip() for t in texts if t.strip())
        raise CloudError("Google returned no picture" + (f". It said: {said[:600]}" if said else
                         ". The prompt may have been blocked by Google's content rules."))
    return picture


def post(api_key, body, timeout=300):
    """Send one Interactions API request; returns the JSON reply or raises CloudError in plain words."""
    request = urllib.request.Request(
        API_URL, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json", "Api-Revision": API_REVISION})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            return json.loads(reply.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
        except Exception:
            detail = ""
        if e.code == 429 and "free tier" in detail.lower():
            raise CloudError("Your API key is on Google's free tier, which can't make images (0 per day). "
                             "Turn on billing for the key's Google Cloud project: open "
                             "https://aistudio.google.com/apikey, find the key's project and choose "
                             "\"Set up billing\". Then try again; the same key keeps working.") from None
        hint = {400: "The request was refused", 401: "The API key was not accepted",
                403: "The API key isn't allowed to use this model (is billing enabled?)",
                429: "Too many requests or no remaining quota"}.get(e.code, "Google returned an error")
        raise CloudError(f"{hint} (HTTP {e.code}).{(' ' + detail) if detail else ''}") from None
    except urllib.error.URLError as e:
        raise CloudError(f"Couldn't reach Google: {e.reason}. Check the internet connection.") from None
    except TimeoutError:
        raise CloudError("Google took too long to answer. Try again.") from None
