
# syntax=docker/dockerfile:1
# Raspberry Pi 5 / Debian 12 (Bookworm, arm64)
FROM debian:bookworm-slim

ARG DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

# ---- GNU Radio / SDR + Python build deps ----
# Notes:
# - gnuradio + gr-osmosdr provide the C++ libs and Python bindings via APT (Debian-recommended).
# - libopus0 + PyNaCl enable Discord voice.
# - python3-venv for creating a venv (PEP 668: avoid pip in system Python).
RUN apt-get update -q && \
    apt-get -y upgrade && \
    apt-get -y install -q --no-install-recommends \
        ca-certificates \
        git \
        cmake \
        build-essential \
        pkg-config \
        # RTL-SDR runtime/tooling
        librtlsdr0 \
        rtl-sdr \
        # GNU Radio & osmosdr
        gnuradio \
        gnuradio-dev \
        gr-osmosdr \
        libvolk2-dev \
        libvolk2-bin \
        # Python runtime + venv + voice deps
        python3 \
        python3-dev \
        python3-pip \
        python3-venv \
        libffi-dev \
        libnacl-dev \
        libopus0 \
        # Optional: system numpy (harmless if you ever use /usr/bin/python3)
        python3-numpy \
    && rm -rf /var/lib/apt/lists/*

# ---- Create venv that can see system site-packages (GNU Radio bindings) ----
# We install your app deps inside the venv, but expose APT-installed GNU Radio via system site-packages.
RUN python3 -m venv /opt/venv --system-site-packages && \
    /opt/venv/bin/python -m pip install --no-cache-dir --upgrade pip setuptools wheel && \
    /opt/venv/bin/python -m pip install --no-cache-dir \
        "discord.py[voice]>=2.4,<3.0" \
        "aiohttp>=3.8.5,<3.9" \
        "numpy==1.26.4" \
        pynacl

# ---- App files ----
WORKDIR /opt
# Your bot and presets
ADD stereo_fm.py /opt/stereo_fm.py
ADD presets.json /opt/presets.json

# ---- EntryPoint script: discover dist-packages, export PYTHONPATH, run bot ----
# This script:
#  1) Sets GR runtime env to silence vmcircbuf.
#  2) Uses system python to locate the 'gnuradio' module directory.
#  3) Exports PYTHONPATH accordingly.
#  4) Launches the bot with the venv Python.
RUN bash -lc 'cat > /opt/entrypoint.sh << "SH" && chmod +x /opt/entrypoint.sh
#!/usr/bin/env bash
set -euo pipefail

# Silence GNU Radio vmcircbuf warnings + console noise
export GR_VMCIRCBUF_IMPLEMENTATION="${GR_VMCIRCBUF_IMPLEMENTATION:-malloc}"
export GR_CONSOLE_LOG_ENABLE="${GR_CONSOLE_LOG_ENABLE:-0}"

# Discover the actual dist-packages path where APT installed GNU Radio
# Fallback to common Debian/Pi ARM64 locations if discovery fails.
DISCOVERED="$(/usr/bin/python3 - <<PY || true
import sys, os
try:
    import gnuradio
    print(os.path.dirname(gnuradio.__file__))
except Exception:
    pass
PY
)"

# Build PYTHONPATH with discovered path + sensible defaults
DEFAULTS="/usr/lib/python3/dist-packages:/usr/lib/aarch64-linux-gnu/python3.11/dist-packages:/usr/lib/aarch64-linux-gnu/python3/dist-packages"
if [ -n "${DISCOVERED}" ]; then
  export PYTHONPATH="${DISCOVERED}:${DEFAULTS}:${PYTHONPATH:-}"
else
  export PYTHONPATH="${DEFAULTS}:${PYTHONPATH:-}"
fi

# Log what we decided (one-time, for diagnostics)
echo "Using PYTHONPATH=${PYTHONPATH}" >&2

# Run the bot from the venv
exec /opt/venv/bin/python /opt/stereo_fm.py
SH
'

# ---- Environment defaults ----
# Put venv at the front of PATH; keep GNU Radio flags quiet; unbuffer logs.
ENV PATH="/opt/venv/bin:${PATH}" \
    GR_VMCIRCBUF_IMPLEMENTATION=malloc \
    GR_CONSOLE_LOG_ENABLE=0 \
    PYTHONUNBUFFERED=1

# ---- Build-time sanity check (fail fast if bindings aren't visible) ----
# Tries both typical dist-packages paths; final runtime still discovers dynamically in entrypoint.sh.
RUN PYTHONPATH="/usr/lib/python3/dist-packages:/usr/lib/aarch64-linux-gnu/python3.11/dist-packages:/usr/lib/aarch64-linux-gnu/python3/dist-packages" \
    /opt/venv/bin/python - <<'PY'
import gnuradio, gnuradio.gr, gnuradio.analog, gnuradio.filter
import osmosdr
print("Build sanity check: GNU Radio + osmosdr imports OK")
PY

# ---- Entrypoint ----
ENTRYPOINT ["/opt/entrypoint.sh"]
