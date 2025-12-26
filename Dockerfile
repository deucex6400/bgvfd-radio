FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Base GNU Radio + SDR + build tooling
RUN apt-get update -q && \
    apt-get install -y -q --no-install-recommends \
      git ca-certificates cmake build-essential pkg-config \
      librtlsdr0 gr-osmosdr gnuradio gnuradio-dev \
      libvolk2-dev libvolk2-bin \
      libffi-dev libopus-dev \
      python3 python3-pip python3-dev \
      python3-cffi python3-nacl \
      rtl-sdr usbutils && \
    apt-get clean && apt-get autoclean

# Optional: VOLK profile
ARG run_volk_profile
RUN if [ -n "$run_volk_profile" ] ; then volk_profile ; fi

# pip tooling + discord.py 2.x without [voice] extra; aiohttp pinned to 3.7.4.post0
RUN python3 -m pip install --upgrade pip setuptools wheel
RUN python3 -m pip install 'discord.py>=2.6.0' 'aiohttp==3.7.4.post0'

# App files
COPY stereo_fm.py /opt/stereo_fm.py
COPY presets.json /opt/presets.json
COPY healthcheck.sh /usr/local/bin/healthcheck.sh
RUN chmod +x /usr/local/bin/healthcheck.sh

# Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD /usr/local/bin/healthcheck.sh

ENTRYPOINT ["/usr/bin/python3", "/opt/stereo_fm.py"]
