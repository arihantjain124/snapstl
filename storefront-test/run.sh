#!/bin/bash
# Storefront test on this Mac: copies the change/local-parametric-model branch into ./branch
# (your SnapSTL checkout and its branches are not touched) and starts the test server.
# Add --lan to open it on your phone on the same Wi-Fi.
set -e
cd "$(dirname "$0")"
BRANCH=change/local-parametric-model
COMMIT=$(git -C .. rev-parse "$BRANCH")
if [ ! -f branch/.commit ] || [ "$(cat branch/.commit)" != "$COMMIT" ]; then
  echo "Copying $BRANCH ($COMMIT)…"
  mkdir -p branch
  git -C .. archive "$BRANCH" | tar -x -C branch
  echo "$COMMIT" > branch/.commit
fi
if ! command -v openscad >/dev/null && [ ! -x /Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD ]; then
  echo "OpenSCAD is not installed. Install the development build (with textmetrics) from https://openscad.org/downloads.html#snapshots"
  exit 1
fi
SNAPSTL_MODELS_DIR="$PWD/branch/models" exec uv run --project branch python render_server.py "$@"
