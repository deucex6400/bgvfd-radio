
# syntax=docker/dockerfile:1
FROM debian:bookworm-slim

ARG DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

# ---- GNU Radio / SDR deps ----
RUN apt-get update -q && \
    apt-get -y upgrade && \
    apt-get -y install -q --no-install-recommends \
        ca-certificates \
        git \
        cmake \
        build-essential \
        pkg-config \
        librtlsdr0 \
        rtl-sdr \
        gnuradio \
        gnuradio-dev \
        gr-osmosdr \
        libvolk2-dev \
        libvolk2-bin \
        python3 \
        python3-dev \
        python3-pip \
        libffi-dev \
        libnacl-dev \
        libopus0 \
    && rm -rf /var/lib/apt/lists/*

# Optional: VOLK profile at build time
ARG run_volk_profile
RUN if [ -n "$run_volk_profile" ] ; then volk_profile ; fi

# ---- Python packages (modern stack: discord.py v2 + aiohttp) ----
RUN python3 -m pip install --no-cache-dir --upgrade pip setuptools wheel && \
    python3 -m pip install --no-cache-dir \
        "discord.py[voice]>=2.4,<3.0" \
        "aiohttp>=3.8.5,<3.9" \
        pynacl

# ---- App files ----
WORKDIR /opt
ADD stereo_fm.py /opt/stereo_fm.py
ADD presets.json /opt/presets.json

# ---- GNU Radio runtime tuning ----
ENV GR_VMCIRCBUF_IMPLEMENTATION=malloc \
    GR_CONSOLE_LOG_ENABLE=0 \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["/usr/bin/python3", "/opt/stereo_fm.py"]
