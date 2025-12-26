
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

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

ARG run_volk_profile
RUN if [ -n "$run_volk_profile" ] ; then volk_profile ; fi

# Keep pip tooling current
RUN python3 -m pip install --upgrade pip setuptools wheel

# Install discord.py WITHOUT the [voice] extra, and aiohttp explicitly
RUN python3 -m pip install 'discord.py==1.7.2' 'aiohttp==3.7.4.post0'

# OPTIONAL: sanity check that pip won’t try to install PyNaCl
RUN python3 -m pip check || true

COPY stereo_fm.py /opt/stereo_fm.py
COPY healthcheck.sh /usr/local/bin/healthcheck.sh
COPY presets.json /opt/presets.json
RUN chmod +x /usr/local/bin/healthcheck.sh

HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD /usr/local/bin/healthcheck.sh
ENTRYPOINT ["/usr/bin/python3", "/opt/stereo_fm.py"]