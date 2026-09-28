#!/usr/bin/env bash
# Builds Re-Search for macOS (dist/Re-Search.app) or Linux (dist/Re-Search).
# Run from a terminal:  bash build.sh
set -e
cd "$(dirname "$0")"
echo "Using: $(python3 --version)"
python3 -m pip install --quiet --upgrade pyinstaller pywebview
ICON=()
if [[ "$(uname)" == "Darwin" ]]; then
  ICON=(--icon re-search.icns --osx-bundle-identifier com.anametheus.re-search)
fi
python3 -m PyInstaller --onefile --windowed "${ICON[@]}" --collect-all webview \
  --name Re-Search --clean --noconfirm re_search.py
echo
if [[ "$(uname)" == "Darwin" && -d dist/Re-Search.app ]]; then
  echo "Built: $(pwd)/dist/Re-Search.app"
  echo "First launch: right-click the app, choose Open, then Open again (it is unsigned)."
elif [[ -f dist/Re-Search ]]; then
  echo "Built: $(pwd)/dist/Re-Search"
else
  echo "Build failed - see the messages above."
fi
