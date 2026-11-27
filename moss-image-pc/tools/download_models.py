"""Download MOSS Image Studio's models into moss-image-pc/models (used on macOS; Windows uses the .ps1 scripts).

    python download_models.py                 asks which optional models to get
    python download_models.py core flux qwen  no questions ("core" = MOSS-Image, needed for everything)

Each file is checked against Hugging Face's published size and SHA-256 and goes to a .part file first, so a
half-finished download never looks installed. Finished files are skipped, so it's safe to run again.
"""
import sys
from pathlib import Path

import moss_updates as U

HF = "https://huggingface.co/"
MODELS = Path(__file__).resolve().parent.parent / "models"

# name -> (description, size, license, [(path under models, Hugging Face path)])
CATALOG = {
    "core": ("MOSS-Image SFT (the main model; needed by the app)", "11.4 GB", "Apache-2.0", [
        ("moss_image_sft_int8_convrot.safetensors",
         "Comfy-Org/MOSS-Image/resolve/main/diffusion_models/moss_image_sft_int8_convrot.safetensors"),
        ("flux2-vae.safetensors", "Comfy-Org/MOSS-Image/resolve/main/vae/flux2-vae.safetensors"),
        ("Qwen3.5-9B-Q4_K_M.gguf", "unsloth/Qwen3.5-9B-GGUF/resolve/main/Qwen3.5-9B-Q4_K_M.gguf"),
        ("tokenizer_qwen3.5.json", "OpenMOSS-Team/MOSS-Image-Base/resolve/main/tokenizer/tokenizer.json")]),
    "moss-edit": ("MOSS-Image editing add-on", "1.4 GB", "Apache-2.0", [
        ("loras/armV5-moss-edit-lora.safetensors",
         "akhaliq/MOSS-Image-Edit-HQ/resolve/main/armV5-moss-edit-lora.safetensors"),
        ("Qwen3.5-9B-mmproj-BF16.gguf", "unsloth/Qwen3.5-9B-GGUF/resolve/main/mmproj-BF16.gguf")]),
    "flux": ("FLUX.2 Klein 4B (fast, also edits)", "8.6 GB", "Apache-2.0", [
        ("flux2-klein-4b/flux-2-klein-4b-Q8_0.gguf",
         "leejet/FLUX.2-klein-4B-GGUF/resolve/main/flux-2-klein-4b-Q8_0.gguf"),
        ("flux2-klein-4b/Qwen3-4B-Q8_0.gguf", "unsloth/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q8_0.gguf")]),
    "flux3": ("FLUX.3 Image Klein (more detail, better edits)", "10.4 GB",
              "FLUX Kommunity (non-commercial; small businesses OK)", [
        ("flux3-klein/flux3-image-klein-Q8_0.gguf",
         "leejet/flux-3-image-klein-GGUF/resolve/main/flux3-image-klein-Q8_0.gguf"),
        ("flux3-klein/Qwen3VL-4B-Instruct-Q8_0.gguf",
         "Qwen/Qwen3-VL-4B-Instruct-GGUF/resolve/main/Qwen3VL-4B-Instruct-Q8_0.gguf")]),
    "zimage": ("Z-Image Turbo (fast photos, good text; shares a file with FLUX.2)", "6.9 GB (+4.3 GB without FLUX)",
               "Apache-2.0", [
        ("z-image-turbo/z_image_turbo-Q8_0.gguf", "leejet/Z-Image-Turbo-GGUF/resolve/main/z_image_turbo-Q8_0.gguf"),
        ("z-image-turbo/ae.safetensors", "Comfy-Org/Z-Image-Turbo/resolve/main/split_files/vae/ae.safetensors"),
        ("flux2-klein-4b/Qwen3-4B-Q8_0.gguf", "unsloth/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q8_0.gguf")]),
    "upscaler": ("AI upscaler (Real-ESRGAN x4plus, for sharper big pictures)", "67 MB", "BSD-3-Clause", [
        ("upscalers/RealESRGAN_x4plus.pth",
         "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth|67040989")]),
    "qwen": ("Qwen Image 2.1 (text in pictures, also edits)", "14.1 GB", "Qwen Research (non-commercial)", [
        ("qwen-image-2.1/qwen_image_2.1_int8_convrot.safetensors",
         "Comfy-Org/Qwen-Image-2.1/resolve/main/diffusion_models/qwen_image_2.1_int8_convrot.safetensors"),
        ("qwen-image-2.1/qwen_image_2.1_vae_bf16.safetensors",
         "Comfy-Org/Qwen-Image-2.1/resolve/main/vae/qwen_image_2.1_vae_bf16.safetensors"),
        ("qwen-image-2.1/Qwen3VL-8B-Instruct-Q4_K_M.gguf",
         "Qwen/Qwen3-VL-8B-Instruct-GGUF/resolve/main/Qwen3VL-8B-Instruct-Q4_K_M.gguf"),
        ("qwen-image-2.1/mmproj-Qwen3VL-8B-Instruct-F16.gguf",
         "Qwen/Qwen3-VL-8B-Instruct-GGUF/resolve/main/mmproj-Qwen3VL-8B-Instruct-F16.gguf")]),
}


def say(text):
    print("\r  " + text[:110].ljust(110), end="", flush=True)


def get_file(rel, source):
    dest = MODELS / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if "|" in source:                                   # not on Hugging Face: exact size given, no checksum
        url, size = source.split("|")
        size, sha = int(size), None
    else:
        url = HF + source
        size, sha = U.remote_model_info(source)
    if dest.exists() and size and dest.stat().st_size == size:
        print(f"  already have {rel}")
        return
    U._download(url, dest, size, sha, say, rel)
    print(f"\r  got {rel}".ljust(114))


def main(packs):
    if not packs:
        print("Models for MOSS Image Studio:")
        keys = list(CATALOG)
        for i, k in enumerate(keys, 1):
            name, size, lic, _files = CATALOG[k]
            print(f"  {i}. {name}  ({size}, license: {lic})")
        answer = input("Which ones? Type numbers separated by spaces (e.g. 1 3 4), or press Return to skip: ")
        packs = [keys[int(n) - 1] for n in answer.replace(",", " ").split() if n.isdigit() and 1 <= int(n) <= len(keys)]
        if not packs:
            print("Nothing chosen.")
            return 0
    for p in packs:
        if p not in CATALOG:
            print(f"Unknown model '{p}'. Choose from: {', '.join(CATALOG)}")
            return 1
        name, size, _lic, files = CATALOG[p]
        print(f"\n=== {name} ({size}) ===")
        for rel, source in files:
            try:
                get_file(rel, source)
            except Exception as e:
                print(f"\n  FAILED: {rel}: {e}\n  Check your connection and run this again.")
                return 1
        print(f"  {name} is ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
