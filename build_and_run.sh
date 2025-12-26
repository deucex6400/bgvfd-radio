#!/usr/bin/env bash
set -euo pipefail
IMG=${IMG:-bgvfd-radio}
TOK=${DISCORD_TOKEN:-}
GUILD=${GUILD_ID:-}

# Build
docker build --no-cache -t "$IMG" --build-arg run_volk_profile=1 .

# Run
if [ -n "$TOK" ]; then
  docker run --rm --name stereo-fm --device /dev/bus/usb -e DISCORD_TOKEN="$TOK" ${GUILD:+-e GUILD_ID="$GUILD"} "$IMG"
else
  echo "Set DISCORD_TOKEN env or pass token as a positional arg after the image name"
  echo "Example: docker run --rm --name stereo-fm --device /dev/bus/usb $IMG '<TOKEN>'"
fi
