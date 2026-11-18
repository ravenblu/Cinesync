#!/bin/zsh
cd "$(dirname "$0")/../.." || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if command -v python3 >/dev/null 2>&1; then
    python3 -B tests/manual/self_test.py
else
    echo "Python 3 is missing. Install Python 3.9+ first."
fi
printf '\nPress Enter to close...'
read -r reply
