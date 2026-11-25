"""Picture-to-prompt for MOSS Image Studio: look at a picture and write a prompt that recreates it.

Local describers run llama.cpp (llm-bin\\llama-mtmd-cli.exe) with a Qwen vision model already on the PC:
  - Qwen 3.5 9B + its vision projector (downloaded with the MOSS editing add-on), or
  - Qwen3-VL 8B + its vision projector (downloaded with Qwen Image 2.1).
The cloud describer sends the picture to Google Gemini with the user's own API key.
"""
import base64
import io
import json
import re
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

import moss_cloud as C

CLOUD_MODEL = "gemini-3.5-flash-lite"      # Google's fast, low-cost multimodal text model
CLOUD_PRICE = "about $0.001 per picture"

STYLES = {
    "Detailed": "as one flowing paragraph of 80 to 150 words",
    "Short": "as one paragraph of 30 to 60 words",
}

INSTRUCTION = (
    "Look at this picture and write one text-to-image prompt that would recreate it {length}. "
    "Describe the main subject, the setting, the composition and camera angle, the lighting, the colors, the mood, "
    "and the style or medium: for a photograph, mention the photographic look (for example the lens, depth of "
    "field or film feel); for art or illustration, name the art style. Copy any readable text exactly, in double "
    "quotes. Describe what is visible; don't name real people. Output only the prompt, with no introduction, "
    "heading or explanation."
)


def instruction(style="Detailed"):
    return INSTRUCTION.format(length=STYLES.get(style, STYLES["Detailed"]))


def clean(text):
    """Strip thinking tags, labels and quotes around the whole prompt."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"^\s*\**\s*(text-to-image\s+)?prompt\s*\**\s*:\s*\**\s*", "", text.strip(), flags=re.I)
    text = text.strip()
    if len(text) > 2 and text[0] == text[-1] and text[0] in "\"'“”":
        text = text[1:-1].strip()
    return " ".join(text.split())


def small_copy(path, longest=1536):
    """A JPEG copy no larger than `longest` pixels, so big pictures (e.g. panoramas) stay quick to read."""
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((longest, longest), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=92)
    return buf.getvalue()


# ---------------- local (llama.cpp) ----------------

def local_options(root):
    """Describers available on this PC: list of (label, model path, projector path)."""
    llama = root / "llm-bin" / "llama-mtmd-cli.exe"
    if not llama.exists():
        return []
    models = root / "models"
    candidates = [
        ("Qwen 3.5 on this PC (private, free)", models / "Qwen3.5-9B-Q4_K_M.gguf", models / "Qwen3.5-9B-mmproj-BF16.gguf"),
        ("Qwen3-VL on this PC (private, free)", models / "qwen-image-2.1" / "Qwen3VL-8B-Instruct-Q4_K_M.gguf",
         models / "qwen-image-2.1" / "mmproj-Qwen3VL-8B-Instruct-F16.gguf"),
    ]
    return [(label, m, p) for label, m, p in candidates if m.exists() and p.exists()]


def describe_local(root, model, projector, picture, style="Detailed", timeout=300):
    llama = root / "llm-bin" / "llama-mtmd-cli.exe"
    with tempfile.TemporaryDirectory() as tmp:
        img = Path(tmp) / "picture.jpg"
        img.write_bytes(small_copy(picture))
        cmd = [str(llama), "-m", str(model), "--mmproj", str(projector), "--image", str(img),
               "-p", instruction(style), "-ngl", "99", "--temp", "0.3", "-n", "1500", "-c", "8192", "--no-warmup"]
        try:
            run = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=str(root),
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            raise RuntimeError("The local describer took too long.") from None
    text = clean(run.stdout.decode("utf-8", "replace"))
    if run.returncode != 0 or not text:
        err = run.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
        raise RuntimeError("The local describer didn't produce a prompt.\n" + "\n".join(err))
    return text


# ---------------- cloud (Gemini) ----------------

def describe_cloud(api_key, picture, style="Detailed", timeout=120):
    body = {"model": CLOUD_MODEL,
            "input": [{"type": "text", "text": instruction(style)},
                      {"type": "image", "mime_type": "image/jpeg",
                       "data": base64.b64encode(small_copy(picture)).decode("ascii")}],
            "generation_config": {"thinking_level": "minimal"}}
    data = C.post(api_key, body, timeout=timeout)
    _, texts = C.extract(data)
    text = clean(" ".join(texts)) if texts else ""
    if not text and isinstance(data, dict) and isinstance(data.get("output_text"), str):
        text = clean(data["output_text"])
    if not text:
        raise C.CloudError("Google returned no description. " + json.dumps(data)[:300])
    return text
