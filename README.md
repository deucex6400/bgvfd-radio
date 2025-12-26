# Stereo FM / VHF Radio Discord Bot

A Dockerized GNU Radio + RTL‑SDR bot that streams radio audio into a Discord voice channel. Built for ARM64/Ubuntu 22.04 with gr‑osmosdr and RTLSDR Blog V4 dongles. Includes:

- **WFM** (broadcast FM) and **NFM** (public safety VHF) demodulation
- Named **presets** (navfire, navmed, fg1, fg2, so1) – configurable via JSON
- Runtime controls: `!vol`, `!squelch`, `!gain`, `!mode`, `!fm`, `!preset`, `!listpresets`, `!stop`
- Health check script and USB device mapping guidance

---

## Repository layout

```
Dockerfile              # Ubuntu 22.04 image with GNU Radio, gr-osmosdr, discord.py voice
stereo_fm.py            # Bot implementation (Discord + GNU Radio flowgraphs)
healthcheck.sh          # Container health check (process & USB bus visibility)
presets.json            # Optional JSON config copied to /opt/presets.json
presets_single_quoted_example.txt  # Example env payload using single-quoted JSON style
```

---

## Prerequisites

- **Hardware:** RTL‑SDR (e.g., RTLSDR Blog V4) connected to the host
- **Docker:** BuildKit enabled (recommended)
- **Discord:** A bot token with voice permissions in your server

---

## Build

```bash
# From the repo directory
docker build -t stereo-fm-bot --build-arg run_volk_profile=1 .
```

The image installs GNU Radio, gr‑osmosdr, PyNaCl/CFFI from OS packages (to avoid build issues), and `discord.py[voice]==1.7.2`.

---

## Configuration

You can provide configuration either as a **file** copied into the image or via an **environment variable**.

### Option A — JSON file (recommended)

Place `presets.json` next to your Dockerfile so it is copied to `/opt/presets.json` at build time.

```json
{
  "mode": "nfm",
  "default_squelch": 0.02,
  "default_gain": null,
  "nfm_deviation_hz": 2500,
  "presets": {
    "navfire": { "mhz": 154.1075 },
    "navmed":  { "mhz": 154.2350 },
    "fg1":     { "mhz": 155.4000 },
    "fg2":     { "mhz": 155.2950 },
    "so1":     { "mhz": 155.1000 }
  }
}
```

### Option B — Environment variable (single‑quoted JSON supported)

Set `PRESETS_JSON` when you run the container. The bot accepts your preferred single‑quoted style inside double quotes and converts it automatically.

```bash
docker run --rm \
  --name stereo-fm \
  --device /dev/bus/usb \
  -e PRESETS_JSON="{'mode':'nfm','default_squelch':0.02,'nfm_deviation_hz':2500,'presets':{'navfire':{'mhz':154.1075},'navmed':{'mhz':154.235},'fg1':{'mhz':155.4},'fg2':{'mhz':155.295},'so1':{'mhz':155.1}}}" \
  stereo-fm-bot '<YOUR_DISCORD_BOT_TOKEN>'
```

---

## Run

### With a baked `presets.json` file
```bash
docker run --rm \
  --name stereo-fm \
  --device /dev/bus/usb \
  stereo-fm-bot '<YOUR_DISCORD_BOT_TOKEN>'
```

### Using an environment variable for the token
```bash
docker run --rm \
  --name stereo-fm \
  --device /dev/bus/usb \
  -e DISCORD_TOKEN='<YOUR_DISCORD_BOT_TOKEN>' \
  stereo-fm-bot
```

> **USB access:** Map `/dev/bus/usb` into the container. For quick troubleshooting you can run `--privileged`, but mapping the device is preferred.

---

## Discord Commands

```
!join #<voice-channel>      # Join a voice channel
!listpresets                # Show all presets and defaults
!preset <name>              # Tune a preset (navfire, navmed, fg1, fg2, so1)
!navfire | !navmed | !fg1 | !fg2 | !so1   # Convenience commands
!fm <freqMHz>               # Tune arbitrary frequency (respects current mode)
!mode <nfm|wfm>             # Switch demodulation mode
!vol <0.0-2.0>              # Set output volume (PCM transformer)
!squelch <level>            # Set RMS squelch threshold (0.0 disables)
!gain <dB>                  # Set RTL tuner RF gain
!stop                       # Stop SDR and leave voice
```

On successful tune, the bot prints current mode and a simple audio RMS.

---

## Demodulation paths

- **WFM** (broadcast FM):
  - 2.4 MHz → 200 kHz (rational resampler) → `wfm_rcv` (audio_decim=4) → 50 kHz → 48 kHz → Discord
- **NFM** (public safety VHF):
  - 2.4 MHz → 200 kHz → **channel LPF** (≈6–12 kHz) → **quadrature demod** (deviation ≈2.5 kHz) → decimate ×4 → **audio LPF** (3.5 kHz) → 48 kHz → Discord

You can adjust NFM deviation via `nfm_deviation_hz` in JSON.

---

## Health check

The container includes `healthcheck.sh`:
- Verifies the bot process is running
- Ensures `/dev/bus/usb` exists
- Optionally probes the dongle briefly with `rtl_eeprom`

Dockerfile declares:
```dockerfile
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD /usr/local/bin/healthcheck.sh
```

---

## Troubleshooting

- **CFFI/PyNaCl build errors during `pip install`:** We use OS packages (`python3-cffi`, `python3-nacl`) to avoid source builds that can mismatch the ARM64 `_cffi_backend`.
- **`[R82XX] PLL not locked!`:** Common on RTL tuners during retunes. The flowgraph sets frequency, bandwidth, then sample rate with short delays, and does a small jitter retune. If lock issues persist:
  - Start at **1.8 MHz** sample rate, tune, then bump to **2.4 MHz**
  - Use a stable USB port (avoid bus‑powered hubs)
- **Silence while tuned:** Check `!squelch` (try `!squelch 0.0`), raise `!vol`, and confirm the bot prints a non‑zero audio RMS.
- **No SDR device:** Ensure `/dev/bus/usb` is mapped; consider `--privileged` for quick tests.

---

## Security & permissions

- The Discord bot needs permission to join voice channels and speak.
- Keep your bot token secure; prefer passing via `DISCORD_TOKEN` env and secrets management.

---

## Extending

- Add more presets in `presets.json` with optional per‑preset `squelch` and `gain`.
- Implement `!saveconfig` and `!setpreset` to persist runtime changes back to JSON.
- Add basic station metadata overlay or logging if you integrate RDS (for WFM sources that support it).

---

## License

This project combines GNU Radio flowgraphs and Discord bot scaffolding intended for operational use. Review dependencies for their respective licenses (GNU Radio, gr‑osmosdr, discord.py).
