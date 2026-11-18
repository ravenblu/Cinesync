#!/bin/zsh
set -eu
cd "$(dirname "$0")/.."
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
export PIP_DISABLE_PIP_VERSION_CHECK=1
python3 -m venv .venv
.venv/bin/python -m pip install '.[desktop,build]'
.venv/bin/python scripts/build_macos.py
printf '\nBuild complete: dist/CineSync Bridge.app\nPress Enter to close.'
read -r reply
