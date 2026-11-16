#!/bin/bash
# Sets up the Bambuddy computer on a Mac or Linux: Bambuddy (print-farm manager) and its slicer service
# (Bambu Studio + OrcaSlicer sidecars) in Docker, reachable by every SnapSTL computer on the network.
# Same as scripts/windows/bambuddy-server.ps1. Safe to run again (updates to the tested versions, keeps data).
#   curl -fsSL https://raw.githubusercontent.com/arihantjain124/snapstl/main/scripts/bambuddy-server.sh | bash
# Needs Docker: Docker Desktop (Mac; with Homebrew it's installed for you) or Docker Engine (Linux).
# Never touches a Bambuddy installed another way: if one already answers on the port, only the slicer
# service is set up. Note: the slicer images are x86-64; on Apple-silicon Macs Docker runs them emulated (slower).
set -euo pipefail
RAW="https://raw.githubusercontent.com/arihantjain124/snapstl/main"
DIR="${BAMBUDDY_SERVER_DIR:-$HOME/SnapSTL-Bambuddy-docker}"
PORT="${BAMBUDDY_PORT:-8000}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"

docker_ok() { command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; }

if ! command -v docker >/dev/null 2>&1; then
  if [ "$(uname)" = Darwin ] && command -v brew >/dev/null 2>&1; then
    echo "Installing Docker Desktop…"
    brew install --cask docker
  else
    echo "Docker is needed: install Docker Desktop (https://www.docker.com/products/docker-desktop/) or, on Linux,"
    echo "Docker Engine (https://docs.docker.com/engine/install/), then run this again."
    exit 1
  fi
fi
if ! docker_ok; then
  [ "$(uname)" = Darwin ] && open -a Docker || true
  echo "Waiting for Docker to start…"
  for _ in $(seq 1 72); do docker_ok && break; sleep 5; done
  docker_ok || { echo "Docker isn't running: open Docker Desktop (finish its first-start screens), then run this again."; exit 1; }
fi

mkdir -p "$DIR"
if [ -n "$HERE" ] && [ -f "$HERE/bambuddy-docker/docker-compose.yml" ]; then
  cp "$HERE/bambuddy-docker/docker-compose.yml" "$DIR/docker-compose.yml"
  VERSION="$(tr -d '[:space:]' < "$HERE/bambuddy-version.txt")"
else
  curl -fsSL "$RAW/scripts/bambuddy-docker/docker-compose.yml" -o "$DIR/docker-compose.yml"
  VERSION="$(curl -fsSL "$RAW/scripts/bambuddy-version.txt" | tr -d '[:space:]')"
fi
VERSION="${BAMBUDDY_VERSION:-$VERSION}"; VERSION="${VERSION#v}"
TZ_NAME="$(readlink /etc/localtime 2>/dev/null | sed 's#.*zoneinfo/##')"; TZ_NAME="${TZ_NAME:-UTC}"
printf 'BAMBUDDY_TAG=%s\nSIDECAR_TAG=bambuddy-%s\nBAMBUDDY_PORT=%s\nTZ=%s\n' "$VERSION" "$VERSION" "$PORT" "$TZ_NAME" > "$DIR/.env"

SERVICES=(bambuddy bambu-studio-api orca-slicer-api)
BUSY=0
if ! docker ps -a --format '{{.Names}}' | grep -qx snapstl-bambuddy && curl -s -o /dev/null --max-time 3 "http://127.0.0.1:$PORT/"; then
  echo "Something already answers on port $PORT (a Bambuddy installed another way?) — left alone; only the slicer service is set up."
  BUSY=1; SERVICES=(bambu-studio-api orca-slicer-api)
fi

echo "Downloading Bambuddy $VERSION and its slicer service (first time: about 1 GB)…"
cd "$DIR"
docker compose pull -q "${SERVICES[@]}"
docker compose up -d --remove-orphans "${SERVICES[@]}"

echo "Waiting for the slicer service…"
for _ in $(seq 1 60); do curl -fs -o /dev/null --max-time 3 http://127.0.0.1:3001/health && break; sleep 3; done
SLICER="not answering yet — see: docker compose -f $DIR/docker-compose.yml logs bambu-studio-api"
curl -fs -o /dev/null --max-time 3 http://127.0.0.1:3001/health && SLICER="running (Bambu Studio + OrcaSlicer)"

BB="" CONF=""
if [ "$BUSY" = 0 ]; then
  echo "Waiting for Bambuddy…"
  for _ in $(seq 1 60); do curl -fs -o /dev/null --max-time 3 "http://127.0.0.1:$PORT/api/v1/auth/status" && break; sleep 3; done
  if AUTH="$(curl -fs --max-time 5 "http://127.0.0.1:$PORT/api/v1/auth/status")"; then
    BB="running, version $VERSION"
    if echo "$AUTH" | grep -q '"auth_enabled": *false'; then
      curl -fs -o /dev/null --max-time 20 -X PATCH -H 'Content-Type: application/json' \
        -d '{"use_slicer_api": true, "preferred_slicer": "bambu_studio"}' "http://127.0.0.1:$PORT/api/v1/settings" && CONF=1
    fi
  fi
fi

if [ "$(uname)" = Darwin ]; then
  IPS="$(for i in $(route -n get default 2>/dev/null | awk '/interface:/{print $2}') en0 en1; do ipconfig getifaddr "$i" 2>/dev/null; done | awk '!s[$0]++')"
else
  IPS="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+\.' | grep -v '^172\.1[7-9]\.' || true)"
fi

echo
echo "=== Bambuddy computer ready ==="
echo "Folder:          $DIR  (docker-compose.yml, .env)"
echo "Slicer service:  $SLICER"
if [ "$BUSY" = 1 ]; then
  echo "Your own Bambuddy: Settings → Slicer → Use Slicer API on, Preferred Slicer Bambu Studio, sidecar URL http://localhost:3001"
else
  echo "Bambuddy:        ${BB:-not answering yet — see: docker compose -f $DIR/docker-compose.yml logs bambuddy}"
  if [ -n "$CONF" ]; then echo "Slicing:         Use Slicer API is on (Bambu Studio)."
  elif [ -n "$BB" ]; then echo "Slicing:         in Bambuddy: Settings → Slicer → Use Slicer API on, Preferred Slicer Bambu Studio (URL fields empty)."; fi
  echo
  echo "In SnapSTL on every computer: Settings → Connections → Bambuddy address:"
  for ip in $IPS; do echo "    http://$ip:$PORT"; done
  echo "Bambuddy itself: open that address in a browser, add printers by IP, import your slicing profiles"
  echo "(Profiles → Local Profiles → Import Profiles)."
fi
echo "Update later: run the same command again."
