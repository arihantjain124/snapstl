#!/bin/bash
# Bambuddy's slicer service in Docker on a Mac or Linux (Bambu Studio + OrcaSlicer sidecars), for the
# Bambuddy that runs natively on this same computer. Like scripts/windows/bambuddy-server.ps1, which also
# installs Bambuddy itself on Windows; here Bambuddy is installed its own way (Mac:
# scripts/bambuddy-mac.sh or SNAPSTL_BAMBUDDY=server with the SnapSTL installer; Linux: Bambuddy's install
# script, https://wiki.bambuddy.cool/getting-started/installation/).
#   curl -fsSL https://raw.githubusercontent.com/arihantjain124/snapstl/main/scripts/bambuddy-server.sh | bash
# Run it again any time: it only applies settings and downloads nothing that's already there.
#   BAMBUDDY_SLICER=bambu_studio|orcaslicer   the slicer Bambuddy uses (remembered)
#   BAMBUDDY_UPGRADE=1                         move the slicer service to the version this SnapSTL was tested with
# The slicer images are x86-64; on Apple-silicon Macs Docker runs them emulated (slower).
set -euo pipefail
RAW="https://raw.githubusercontent.com/arihantjain124/snapstl/main"
DIR="${BAMBUDDY_SERVER_DIR:-$HOME/SnapSTL-Bambuddy-docker}"
PORT="${BAMBUDDY_PORT:-8000}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
SETUP="$DIR/setup.env"

docker_ok() { command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; }

if ! command -v docker >/dev/null 2>&1; then
  if [ "$(uname)" = Darwin ] && command -v brew >/dev/null 2>&1; then
    echo "Installing Docker Desktop…"; brew install --cask docker
  else
    echo "Docker is needed: Docker Desktop (https://www.docker.com/products/docker-desktop/) or, on Linux,"
    echo "Docker Engine (https://docs.docker.com/engine/install/). Install it, then run this again."; exit 1
  fi
fi
if ! docker_ok; then
  [ "$(uname)" = Darwin ] && open -a Docker || true
  echo "Waiting for Docker to start…"
  for _ in $(seq 1 72); do docker_ok && break; sleep 5; done
  docker_ok || { echo "Docker isn't running: open Docker Desktop (finish its first-start screens), then run this again."; exit 1; }
fi

mkdir -p "$DIR"
SAVED_SLICER="" SAVED_VERSION=""
[ -f "$SETUP" ] && . "$SETUP"
SLICER="${BAMBUDDY_SLICER:-${SAVED_SLICER:-bambu_studio}}"
case "$SLICER" in bambu_studio|orcaslicer) ;; *) echo "BAMBUDDY_SLICER is bambu_studio or orcaslicer"; exit 1;; esac
VERSION="$SAVED_VERSION"
if [ -z "$VERSION" ] || [ "${BAMBUDDY_UPGRADE:-0}" = 1 ]; then
  if [ -n "$HERE" ] && [ -f "$HERE/bambuddy-version.txt" ]; then VERSION="$(tr -d '[:space:]' < "$HERE/bambuddy-version.txt")"
  else VERSION="$(curl -fsSL "$RAW/scripts/bambuddy-version.txt" | tr -d '[:space:]')"; fi
fi
VERSION="${VERSION#v}"
if [ -n "$HERE" ] && [ -f "$HERE/bambuddy-docker/docker-compose.yml" ]; then cp "$HERE/bambuddy-docker/docker-compose.yml" "$DIR/docker-compose.yml"
else curl -fsSL "$RAW/scripts/bambuddy-docker/docker-compose.yml" -o "$DIR/docker-compose.yml"; fi
printf 'SIDECAR_TAG=bambuddy-%s\n' "$VERSION" > "$DIR/.env"

cd "$DIR"
NEED=()
for s in bambu-studio-api orca-slicer-api; do
  docker image inspect "ghcr.io/maziggy/$s:bambuddy-$VERSION" >/dev/null 2>&1 || NEED+=("$s")
done
if [ ${#NEED[@]} -gt 0 ]; then
  echo "Downloading the slicer service: ${NEED[*]} ($VERSION)…"
  docker compose pull -q "${NEED[@]}"
fi
docker compose up -d --remove-orphans
printf 'SAVED_SLICER=%s\nSAVED_VERSION=%s\n' "$SLICER" "$VERSION" > "$SETUP"

for _ in $(seq 1 60); do curl -fs -o /dev/null --max-time 3 http://127.0.0.1:3001/health && break; sleep 3; done
STATE="not answering yet — see: docker compose -f $DIR/docker-compose.yml logs"
curl -fs -o /dev/null --max-time 3 http://127.0.0.1:3001/health && STATE="running (Bambu Studio + OrcaSlicer)"

BB="no Bambuddy answers on port $PORT here — install it, then run this again"
if AUTH="$(curl -fs --max-time 5 "http://127.0.0.1:$PORT/api/v1/auth/status")"; then
  BB="login on — in Bambuddy: Settings → Slicer → Use Slicer API, sidecar URL http://localhost:3001"
  if echo "$AUTH" | grep -q '"auth_enabled": *false' && curl -fs -o /dev/null --max-time 20 -X PATCH \
      -H 'Content-Type: application/json' "http://127.0.0.1:$PORT/api/v1/settings" -d "{\"use_slicer_api\": true, \
\"preferred_slicer\": \"$SLICER\", \"bambu_studio_api_url\": \"http://localhost:3001\", \"orcaslicer_api_url\": \"http://localhost:3003\"}"; then
    BB="uses the slicer service ($SLICER)"
  fi
fi
echo
echo "Slicer service: $STATE"
echo "Bambuddy:       $BB"
echo "Run this again to apply changes (nothing is reinstalled)."
