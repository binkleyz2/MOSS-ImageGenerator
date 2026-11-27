#!/bin/bash
# install-mac.sh: set up MOSS Image Studio on a Mac with Apple Silicon (M1 or newer).
#
#   ./install-mac.sh             asks which extra models to download
#   ./install-mac.sh flux zimage also downloads those (see download-models.sh for the names)
#
# What it does (safe to run again; finished steps are skipped):
#   1. finds Python 3.11+ with Tk and makes a private Python environment in moss-image-pc/tools/venv
#   2. downloads the image engine (stable-diffusion.cpp) and the describer engine (llama.cpp), Apple Silicon builds
#   3. downloads MOSS-Image (about 11 GB), plus any extra models you choose
#   4. makes "MOSS Image Studio" in your Applications folder
#   5. runs a self-check and saves a report to your Desktop
set -euo pipefail
cd "$(dirname "$0")"
HERE="$(pwd)"
PC="$HERE/moss-image-pc"
TOOLS="$PC/tools"

bold() { printf '\n\033[1m%s\033[0m\n' "$1"; }
fail() { printf '\n\033[31m%s\033[0m\n' "$1"; exit 1; }

if [ "$(uname -s)" != "Darwin" ]; then fail "This installer is for macOS. On Windows use installer-moss-image.ps1."; fi
if [ "$(uname -m)" != "arm64" ]; then fail "MOSS Image Studio needs a Mac with Apple Silicon (M1 or newer)."; fi
echo "macOS $(sw_vers -productVersion), $(sysctl -n machdep.cpu.brand_string), $(( $(sysctl -n hw.memsize) / 1073741824 )) GB memory"

bold "1. Python"
PY=""
for c in /opt/homebrew/bin/python3.14 /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 \
         /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 \
         /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
         /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
         /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 \
         "$(command -v python3 || true)"; do
    [ -n "$c" ] && [ -x "$c" ] || continue
    if "$c" -c 'import sys, tkinter; assert sys.version_info >= (3, 11) and tkinter.TkVersion >= 8.6' 2>/dev/null; then
        PY="$c"; break
    fi
done
if [ -z "$PY" ]; then
    cat <<'EOF'
No Python 3.11 or newer with Tk was found. Install one of these, then run this again:
  - Homebrew:   brew install python@3.13 python-tk@3.13
  - or the macOS installer from https://www.python.org/downloads/macos/
(Apple's built-in /usr/bin/python3 doesn't include a recent Tk.)
EOF
    exit 1
fi
echo "Using $PY ($("$PY" -c 'import sys, tkinter; print(sys.version.split()[0], "with Tk", tkinter.TkVersion)'))"
if [ ! -x "$TOOLS/venv/bin/python" ]; then
    "$PY" -m venv "$TOOLS/venv"
fi
"$TOOLS/venv/bin/python" -m pip install --quiet --upgrade pip
"$TOOLS/venv/bin/python" -m pip install --quiet -r "$TOOLS/requirements.txt"
VPY="$TOOLS/venv/bin/python"
echo "Python packages are ready."

bold "2. Engines"
"$VPY" "$TOOLS/mac_setup.py" engines

bold "3. Models"
"$VPY" "$TOOLS/download_models.py" core
if [ $# -gt 0 ]; then
    "$VPY" "$TOOLS/download_models.py" "$@"
else
    echo
    echo "Optional extra models (you can add more later with ./download-models.sh):"
    "$VPY" "$TOOLS/download_models.py" || true
fi

bold "4. The app"
"$VPY" "$TOOLS/mac_setup.py" app

bold "5. Self-check"
"$VPY" "$TOOLS/mac_setup.py" check || true

bold "Done."
echo "Open MOSS Image Studio from your Applications folder (or Spotlight)."
echo "The first picture with each model takes longer while macOS prepares the graphics code."
