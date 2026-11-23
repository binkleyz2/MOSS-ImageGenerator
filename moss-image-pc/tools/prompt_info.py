"""Saves the prompt next to a MOSS image, with its token count.

Called by "MOSS Image.bat" before each picture is made:
    python prompt_info.py <text file to write> <width> <height> <seed> <steps> <cfg>
The prompt itself comes from the PROMPT_TEXT environment variable, so quotes,
&, | and other special characters reach this script untouched.

Tokens are counted with the same Qwen 3.5 tokenizer the model uses
(models/tokenizer_qwen3.5.json), counting only the prompt text.
"""
import os
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOKENIZER = HERE.parent / "models" / "tokenizer_qwen3.5.json"


def count_tokens(text):
    try:
        from tokenizers import Tokenizer
        tok = Tokenizer.from_file(str(TOKENIZER))
        return len(tok.encode(text, add_special_tokens=False).ids)
    except Exception as err:  # still save the prompt if counting fails
        print(f"(Could not count tokens: {err})")
        return None


def main():
    if len(sys.argv) != 7:
        print(__doc__)
        return 2
    out_txt, width, height, seed, steps, cfg = sys.argv[1:]
    prompt = os.environ.get("PROMPT_TEXT", "")
    tokens = count_tokens(prompt)
    token_line = f"{tokens:,}" if tokens is not None else "unknown"

    image_name = Path(out_txt).with_suffix(".png").name
    lines = [
        "Prompt:",
        prompt,
        "",
        f"Tokens: {token_line} (Qwen 3.5 tokenizer, prompt text only)",
        f"Words: {len(prompt.split()):,}",
        f"Characters: {len(prompt):,}",
        "",
        f"Image: {image_name}",
        f"Size: {width} x {height}",
        f"Seed: {seed}",
        f"Steps: {steps}",
        f"CFG scale: {cfg}",
        f"Made: {datetime.now():%Y-%m-%d %H:%M:%S}",
    ]
    Path(out_txt).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Prompt length: {token_line} tokens")
    return 0


if __name__ == "__main__":
    sys.exit(main())
