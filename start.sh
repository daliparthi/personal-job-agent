#!/usr/bin/env bash
# Job Agent for macOS and Linux (Windows: start.bat). Options are passed to run.py, e.g.  ./start.sh --profile Alex
# The first run creates a Python environment for this computer and installs the packages (a few minutes).
set -u
cd "$(dirname "$0")" || exit 1

OS="$(uname -s)"
fail() { echo; echo "Setup failed: $*"; exit 1; }

# ---------------------------------------------------------------- Python 3.10 or newer
candidates="python3 python3.14 python3.13 python3.12 python3.11 python3.10 python"
if [ "$OS" = "Darwin" ]; then
  # Homebrew and python.org installs, in case this was opened from Finder with a short PATH.
  candidates="$candidates /opt/homebrew/bin/python3 /usr/local/bin/python3 /Library/Frameworks/Python.framework/Versions/Current/bin/python3"
fi
PY=""
for c in $candidates; do
  p="$(command -v "$c" 2>/dev/null)" || continue
  # macOS's /usr/bin/python3 is a stub that pops up a developer-tools installer (and is 3.9); skip it.
  if [ "$OS" = "Darwin" ] && [ "$p" = "/usr/bin/python3" ] && ! xcode-select -p >/dev/null 2>&1; then continue; fi
  if "$p" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then PY="$p"; break; fi
done
if [ -z "$PY" ]; then
  echo "Job Agent needs Python 3.10 or newer."
  if [ "$OS" = "Darwin" ]; then
    echo "Install it from https://www.python.org/downloads/ (or with Homebrew: brew install python), then run this again."
  else
    echo "Install it with your package manager, e.g.  sudo apt install python3 python3-venv  /  sudo dnf install python3"
  fi
  exit 1
fi

# ---------------------------------------------------------------- environment for this OS
# One per OS (.venv-darwin / .venv-linux; Windows uses .venv), so a folder shared between computers still works.
VENV=".venv-$(echo "$OS" | tr '[:upper:]' '[:lower:]')"
VPY="$VENV/bin/python"
download() {  # url dest
  if command -v curl >/dev/null 2>&1; then curl -fsSL "$1" -o "$2"
  elif command -v wget >/dev/null 2>&1; then wget -q "$1" -O "$2"
  else "$PY" -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' "$1" "$2"
  fi
}
if [ ! -x "$VPY" ]; then
  echo "Creating the Python environment ($VENV) with $("$PY" --version 2>&1)..."
  rm -rf "$VENV"
  if ! "$PY" -m venv "$VENV" >/dev/null 2>&1; then
    # Debian/Ubuntu ship Python without pip for venvs unless python3-venv is installed (needs admin rights).
    echo "This Python can't add pip to a new environment (Debian/Ubuntu: sudo apt install python3-venv)."
    echo "Continuing without admin rights: installing pip into $VENV with the official installer from bootstrap.pypa.io..."
    rm -rf "$VENV"
    "$PY" -m venv --without-pip "$VENV" || fail "could not create $VENV"
    download https://bootstrap.pypa.io/get-pip.py "$VENV/get-pip.py" || fail "could not download get-pip.py"
    "$VPY" "$VENV/get-pip.py" --quiet --disable-pip-version-check || fail "could not install pip"
    rm -f "$VENV/get-pip.py"
  fi
fi
# (Re)install packages on the first run and whenever requirements.txt changes.
if ! cmp -s requirements.txt "$VENV/requirements.installed"; then
  echo "Installing Python packages..."
  "$VPY" -m pip install --quiet --disable-pip-version-check --upgrade pip
  "$VPY" -m pip install --disable-pip-version-check -r requirements.txt || fail "package install failed (see above)"
  cp requirements.txt "$VENV/requirements.installed"
fi

# ---------------------------------------------------------------- bundled model files
ONNX=models/onnx/Qwen2.5-0.5B-Instruct/onnx/model_quantized.onnx  # stored as .part1, .part2... in the repository
if [ ! -f static/vendor/web-llm/index.js ] || { [ ! -f "$ONNX" ] && [ ! -f "$ONNX.part1" ]; }; then
  echo "Bundling the Qwen2.5-0.5B model files - one time, about 1.1 GB..."
  "$VPY" fetch_models.py || fail "could not download the model files"
fi

# ---------------------------------------------------------------- browser for applying / PDFs
have_browser() {
  if [ "$OS" = "Darwin" ]; then
    for a in "Google Chrome" "Microsoft Edge" "Chromium" "Brave Browser"; do
      [ -d "/Applications/$a.app" ] || [ -d "$HOME/Applications/$a.app" ] && return 0
    done
  else
    for b in google-chrome google-chrome-stable microsoft-edge microsoft-edge-stable chromium chromium-browser brave-browser; do
      command -v "$b" >/dev/null 2>&1 && return 0
    done
  fi
  [ -n "$(ls -d "$HOME"/.cache/ms-playwright/chromium-* "$HOME"/Library/Caches/ms-playwright/chromium-* 2>/dev/null)" ]
}
if ! have_browser; then
  echo "Note: no Chrome, Edge or Chromium found. Search and tailoring work in any browser, but"
  echo "      'Apply with autofill' and PDF export need one. Install Chrome or Chromium, or run:"
  echo "      $VPY -m playwright install chromium"
fi

exec "$VPY" run.py "$@"
