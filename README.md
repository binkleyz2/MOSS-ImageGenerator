# MOSS-ImageGenerator

**Make AI pictures on your own Windows PC: no cloud service, no account, no per-image cost.**

MOSS Image Studio is a desktop app for the open [MOSS-Image](https://huggingface.co/OpenMOSS-Team/MOSS-Image-SFT) text-to-image model. It runs locally on an NVIDIA graphics card through [stable-diffusion.cpp](https://github.com/leejet/stable-diffusion.cpp). Type what you want to see, pick a size, and the picture is made on your machine.

![MOSS Image Studio in dark mode](docs/images/studio-dark.png)

## Features

- **Prompt box with a live token count.** It counts with the model's own Qwen 3.5 tokenizer and handles long, multi-line prompts with quotes and special characters.
- **Sizes and shapes.** Presets for common shapes (16:9, 9:16, 1:1, 4:3, 3:2, 21:9 ...) at four sizes, from about 1 MP (around a minute) up to about 5 MP, e.g. 3440 x 1440. You can also type a custom size.
- **Pictures that fit your screens.** The app detects your monitors and offers:
  - each screen on its own;
  - neighbouring screens together;
  - all of them as one panorama.

  Spanned pictures are drawn at the screens' exact shape and scaled up to full resolution. Each screen's part can also be saved as its own wallpaper file.
- **Change a detail, keep the picture.** Reuse a seed (the random number behind a picture) with a slightly changed prompt, and you get nearly the same picture with that one thing changed. There's a one-click **Make a variation** button, and you can load the prompt, size and seed from any earlier picture.
- **Details saved inside each picture** (EXIF / XMP):
  - title, authors, copyright and tags;
  - the date;
  - the generation settings;
  - **the full prompt as the Comment**.

  Windows File Explorer shows them under *Properties > Details*. A built-in editor changes them on any PNG or JPEG without touching the picture itself.
- **Output choices.** Save to any folder, as PNG or JPEG, optionally with a `.txt` file holding the prompt and its token count.
- **Light and dark themes**, or follow the Windows setting.
- **Progress and control.** Live step-by-step progress, a time estimate for each size, and Cancel.

## Examples

All of these were made locally on an RTX 4070 Ti SUPER.

A three-screen panorama (8560 x 1440, shown scaled down):

![Panorama across three screens](docs/images/example-panorama.jpg)

Same seed, one word changed (*orange cat* to *black cat*):

| Original | Same seed, "black cat" |
| --- | --- |
| ![Orange cat in a diner window](docs/images/example-seed-before.jpg) | ![Black cat in the same diner window](docs/images/example-seed-after.jpg) |

## Requirements

- Windows 10 or 11, 64-bit.
- An NVIDIA graphics card, ideally with 16 GB of memory; it's built and tested on an RTX 4070 Ti SUPER. On smaller cards the installer may fall back to a smaller model; see *Troubleshooting*.
- About 13 GB of free disk space for the engine and models.
- An internet connection for the one-time download.

## Quick start

1. **Download this repo**: *Code > Download ZIP* and unzip it, or `git clone` it.
2. **Run the installer** from the repo folder. It downloads the engine and models (about 12 GB) into a `moss-image-pc` folder next to itself and makes a test picture. If the download is interrupted, run it again and it resumes.

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\installer-moss-image.ps1
   ```

3. **Download `MOSS-Image-Studio.exe`** from the [Releases page](../../releases/latest). Put it in the `moss-image-pc` folder, or in the repo folder next to it, and double-click it. A desktop shortcut to it is handy.

   The `.exe` isn't code-signed. The first time you run it, Windows SmartScreen may say *"Windows protected your PC"*; click **More info > Run anyway**.

If the app opens with a message that the engine or models weren't found, it's in the wrong folder or the installer hasn't finished. The message lists exactly what's missing.

## Using it

1. Type or paste a prompt. Longer, descriptive prompts work well: say what's in the scene, the lighting and the style.
2. Pick a **Shape** and **Size**, or one of your screens.
3. Click **Make picture** (or press Ctrl+Enter). A 1 MP picture takes about a minute; the largest sizes take about 7 minutes.
4. The picture appears on the right with its details. **Open picture** and **Show in folder** are below it.

**To change one detail**, click **Make a variation (same seed)**, edit a few words of the prompt and make it again. The fewer words you change, the closer the picture stays.

Pictures are saved in `moss-image-pc\outputs` unless you choose another folder. The app remembers your choices in `moss-image-pc\tools\gui_settings.json`.

## Other ways to run it

**From source, instead of the `.exe`.** You need Python 3.10 or newer:

```powershell
cd moss-image-pc
python -m venv tools\venv
tools\venv\Scripts\python -m pip install -r tools\requirements.txt
tools\venv\Scripts\pythonw.exe tools\moss_gui.py
```

**Command-window launcher.** `moss-image-pc\MOSS Image.bat` is a simple text version:

1. Paste a prompt (several lines are fine) and type `/go`.
2. Pick a size.
3. Pick a seed: press Enter for random, type `last` to reuse the last picture's seed, or type a number.

## Building the .exe

```powershell
powershell -ExecutionPolicy Bypass -File .\packaging\build-exe.ps1
```

This builds `dist\MOSS Image Studio.exe` with PyInstaller, in its own build environment. To check a build end to end, run:

```powershell
& ".\dist\MOSS Image Studio.exe" --selftest .\selftest
```

It makes one small picture through the app and writes `selftest-result.json`.

## Troubleshooting

- **"Engine or models not found."** Run the installer, then put the `.exe` in `moss-image-pc` or the folder above it.
- **Running out of graphics memory on big sizes.** Pick a smaller Size. Pictures over 2 MP are already decoded in tiles to save memory.
- **Antivirus warning about the `.exe`.** Some antivirus tools are wary of programs packaged with PyInstaller. You can build it yourself (see above) or run from source.
- **The installer fell back to the GGUF model.** The app and launcher expect the int8 model (`moss_image_sft_int8_convrot.safetensors`). Change the model name near the top of `moss_gui.py`, and in `MOSS Image.bat`.
- **Editing pictures by instruction** (e.g. "make the cat black") isn't possible yet. A MOSS edit add-on exists, but stable-diffusion.cpp doesn't support it for MOSS yet ([leejet/stable-diffusion.cpp#2203](https://github.com/leejet/stable-diffusion.cpp/pull/2203)). For now, use the same-seed method above.

## What's in this repo

| Path | What it is |
| --- | --- |
| `installer-moss-image.ps1` | Downloads and sets up the engine and models, and makes a test picture |
| `moss-image-pc/tools/moss_gui.py` | MOSS Image Studio |
| `moss-image-pc/tools/moss_meta.py` | Reads and writes picture details (EXIF / XMP / PNG text) |
| `moss-image-pc/tools/prompt_info.py` | Token counter and prompt `.txt` writer used by the launcher |
| `moss-image-pc/MOSS Image.bat` | Command-window launcher |
| `packaging/` | Build script and version info for the `.exe` |
| `docs/images/` | Screenshots and examples for this page |

The engine, models, your pictures and settings are not stored in the repo.

## Credits and licenses

The installer downloads these from their original sources. Each keeps its own license:

- [stable-diffusion.cpp](https://github.com/leejet/stable-diffusion.cpp) (MIT)
- [MOSS-Image](https://huggingface.co/OpenMOSS-Team/MOSS-Image-SFT), via [Comfy-Org/MOSS-Image](https://huggingface.co/Comfy-Org/MOSS-Image)
- [Qwen3.5-9B GGUF](https://huggingface.co/unsloth/Qwen3.5-9B-GGUF), from unsloth
- FLUX.2 VAE

The `.exe` bundles Python, Tk, [Pillow](https://python-pillow.org) and Hugging Face [tokenizers](https://github.com/huggingface/tokenizers).

The code in this repo is licensed under the **GNU General Public License v3.0**; see [LICENSE](LICENSE).

<details>
<summary>Light theme</summary>

![MOSS Image Studio in light mode](docs/images/studio-light.png)

</details>
