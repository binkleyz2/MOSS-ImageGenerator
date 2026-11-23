# MOSS Image on a Windows PC

Run the [MOSS-Image](https://huggingface.co/OpenMOSS-Team/MOSS-Image-SFT) text-to-image model locally on Windows with an NVIDIA GPU, using [stable-diffusion.cpp](https://github.com/leejet/stable-diffusion.cpp). The repo has three parts:

- **`installer-moss-image.ps1`** downloads the engine and the model files (about 12 GB), then makes a test picture.
- **`moss-image-pc/MOSS Image.bat`** is a simple command-window launcher.
- **`moss-image-pc/tools/moss_gui.py`** is **MOSS Image Studio**, a desktop app.

## What MOSS Image Studio does

- Prompt box with a live token count, using the model's own Qwen 3.5 tokenizer.
- Picture shape and size presets up to about 5 MP (for example 3440 x 1440), or a custom size.
- **Screen-sized pictures.** It detects your monitors and offers each screen, neighbouring screens together, and all screens as one panorama.
  - Spanned pictures are drawn at the screens' exact shape, then scaled up to full resolution.
  - Each screen's part can also be saved as a separate wallpaper.
- **Seed control.** Pick a new random seed, reuse the last picture's seed, or enter a seed number. Reusing a seed with a slightly changed prompt keeps the picture and changes a detail, e.g. *orange cat* to *black cat*.
- **Output.** Choose the output folder, PNG or JPEG, and an optional `.txt` file with the prompt and its token count.
- **Picture details.** Each picture stores its details (EXIF / XMP): title, the prompt as the comment, authors, copyright, tags, date and generation settings.
  - Windows File Explorer shows them under Properties > Details.
  - You can edit them for any PNG or JPEG. Saving the details never touches the picture itself.
- Light and dark themes, or follow the Windows setting.

## Requirements

- Windows 10 or 11 (64-bit) with an NVIDIA GPU. It was built and tested on an RTX 4070 Ti SUPER (16 GB).
- About 13 GB of free disk space.
- Python 3.10 or newer, for the app only.

## Setup

1. Clone or download this repo, then run the installer from the repo folder:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\installer-moss-image.ps1
   ```

   It puts the engine in `moss-image-pc\bin` and the models in `moss-image-pc\models`. If a download is interrupted, re-run it and it picks up where it left off.

2. Set up the app's Python environment:

   ```powershell
   cd moss-image-pc
   python -m venv tools\venv
   tools\venv\Scripts\python -m pip install -r tools\requirements.txt
   ```

3. Start the app:

   ```powershell
   tools\venv\Scripts\pythonw.exe tools\moss_gui.py
   ```

   A desktop shortcut that runs this command makes it easy to start. `tools\moss.ico` is its icon.

The command-window launcher is `moss-image-pc\MOSS Image.bat`. Paste a prompt (several lines are fine), type `/go`, then pick a size and seed.

## Notes

- Pictures are saved in `moss-image-pc\outputs` unless you choose another folder. The app keeps its settings in `tools\gui_settings.json`.
- **Model choice.** The launcher and the app use the int8 MOSS model (`moss_image_sft_int8_convrot.safetensors`).
  - If the installer had to fall back to the smaller GGUF model on your GPU, change the model name in `moss_gui.py` and `MOSS Image.bat` to match.
- **Model limits.** The model makes pictures up to about 5 MP. Bigger screen panoramas are scaled up afterwards with Lanczos resampling.
- **Edit add-on.** An image-edit LoRA for MOSS exists, but stable-diffusion.cpp doesn't support MOSS editing yet ([leejet/stable-diffusion.cpp#2203](https://github.com/leejet/stable-diffusion.cpp/pull/2203)).

## Third-party software and models

The installer downloads files from their original sources. Each keeps its own license:

- stable-diffusion.cpp (MIT)
- MOSS-Image, from OpenMOSS / Comfy-Org on Hugging Face
- Qwen3.5-9B GGUF, from unsloth on Hugging Face
- FLUX.2 VAE

None of these files are stored in this repo.
