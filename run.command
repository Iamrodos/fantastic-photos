#!/bin/bash
# Fantastic Photos - macOS launcher. Double-click in Finder.
cd "$(dirname "$0")"
if ! command -v uv >/dev/null 2>&1; then
  echo
  echo "  uv is not installed. Install it with:"
  echo
  echo "     curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo
  read -n 1 -s -r -p "  Press any key to close."
  exit 1
fi
uv run --no-project --python 3.12 launch.py
echo
read -n 1 -s -r -p "  The app has stopped. Press any key to close."
