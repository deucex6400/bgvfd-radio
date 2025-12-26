#!/usr/bin/env bash
set -euo pipefail

# Is the Python bot process running?
if ! pgrep -f "/opt/stereo_fm.py" >/dev/null ; then
  echo "bot process not running"
  exit 1
fi

# Is USB bus visible inside the container?
if [ ! -d "/dev/bus/usb" ]; then
  echo "/dev/bus/usb not present"
  exit 1
fi

# Optional quick dongle probe (do not fail the health check if absent)
if command -v rtl_eeprom >/dev/null 2>&1; then
  timeout 2 rtl_eeprom -d 0 >/dev/null 2>&1 || true
fi

echo "ok"
