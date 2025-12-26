
# syntax=docker/dockerfile:1.4
# Raspberry Pi 5 (arm64) — Debian 12 (Bookworm)
FROM debian:bookworm-slim

ARG DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

# Use bash for RUN with pipefail (safer)
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# ---- GNU Radio / SDR + Python deps ----
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
        # GNU Radio + osmosdr (APT provides Python bindings under dist-packages)
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

# ---- Create venv that can see system site-packages (for GNU Radio bindings) ----
RUN python3 -m venv /opt/venv --system-site-packages && \
    /opt/venv/bin/python -m pip install --no-cache-dir --upgrade pip setuptools wheel && \
    /opt/venv/bin/python -m pip install --no-cache-dir \
        "discord.py[voice]>=2.4,<3.0" \
        "aiohttp>=3.8.5,<3.9" \
        "numpy==1.26.4" \
        pynacl

# ---- App files ----
WORKDIR /opt
ADD stereo_fm.py /opt/stereo_fm.py
ADD presets.json /opt/presets.json

# ---- Create /opt/entrypoint.sh using nested heredocs (BuildKit) ----
# Outer heredoc: the commands to run when building; inner heredoc: the file content we write.
RUN <<'CMDS'
cat > /opt/entrypoint.sh <<'SH'
#!/usr/bin/env bash
set -euo pipefail

# Silence GNU Radio vmcircbuf warnings + console noise
export GR_VMCIRCBUF_IMPLEMENTATION="${GR_VMCIRCBUF_IMPLEMENTATION:-malloc}"
export GR_CONSOLE_LOG_ENABLE="${GR_CONSOLE_LOG_ENABLE:-0}"

# Discover where APT installed GNU Radio's Python package; fallback to common ARM64 paths.
DISCOVERED="$(/usr/bin/python3 - <<'PY'
import sys, os
try:
    import gnuradio
    print(os.path.dirname(gnuradio.__file__))
except Exception:
    pass
PY
)"

DEFAULTS="/usr/lib/python3/dist-packages:/usr/lib/aarch64-linux-gnu/python3.11/dist-packages:/usr/lib/aarch64-linux-gnu/python3/dist-packages"

if [[ -n "${DISCOVERED}" ]]; then
  export PYTHONPATH="${DISCOVERED}:${DEFAULTS}:${PYTHONPATH:-}"
else
  export PYTHONPATH="${DEFAULTS}:${PYTHONPATH:-}"
fi

echo "Using PYTHONPATH=${PYTHONPATH}" >&2

# Run the bot from the venv
exec /opt/venv/bin/python /opt/stereo_fm.py
SH

chmod +x /opt/entrypoint.sh
CMDS

# ---- Environment defaults ----
ENV PATH="/opt/venv/bin:${PATH}" \
    GR_VMCIRCBUF_IMPLEMENTATION=malloc \
    GR_CONSOLE_LOG_ENABLE=0 \
    PYTHONUNBUFFERED=1

# ---- Build-time sanity check (fail fast if bindings aren't visible to venv) ----
RUN PYTHONPATH="/usr/lib/python3/dist-packages:/usr/lib/aarch64-linux-gnu/python3.11/dist-packages:/usr/lib/aarch64-linux-gnu/python3/dist-packages" \
    /opt/venv/bin/python - <<'PY'
import gnuradio, gnuradio.gr, gnuradio.analog, gnuradio.filter
import osmosdr
print("Build sanity check: GNU Radio + osmosdr imports OK")
PY

# ---- Entrypoint ----
ENTRYPOINT ["/opt/entrypoint.sh"]
