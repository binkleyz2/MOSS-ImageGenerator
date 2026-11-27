#!/bin/bash
# download-models.sh: add models to MOSS Image Studio on a Mac (run install-mac.sh first).
#   ./download-models.sh                 asks which ones
#   ./download-models.sh flux3 zimage    no questions
# Names: core, moss-edit, flux, flux3, zimage, upscaler, qwen. Finished files are skipped.
set -euo pipefail
cd "$(dirname "$0")"
VPY="moss-image-pc/tools/venv/bin/python"
if [ ! -x "$VPY" ]; then echo "Run ./install-mac.sh first."; exit 1; fi
exec "$VPY" moss-image-pc/tools/download_models.py "$@"
