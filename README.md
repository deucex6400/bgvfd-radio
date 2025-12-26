# Stereo FM / VHF Radio Discord Bot

A Dockerized GNU Radio + RTL‑SDR bot that streams radio audio into a Discord voice channel. Supports **WFM** (broadcast) and **NFM** (public safety VHF), **slash commands** and **prefix commands**, JSON presets, and a container health check.

### Highlights
- Slash commands via `discord.app_commands` (discord.py ≥ 2.x) — `/join`, `/fm`, `/preset`, `/vol`, `/squelch`, `/gain`, `/mode`, `/stop`.
- Backward‑compatible `!` prefix commands.
- JSON config file `/opt/presets.json` or env `PRESETS_JSON` (single‑quoted JSON accepted).
- Uses OS packages for PyNaCl/CFFI to avoid CFFI mismatches on ARM64.

> **References:** Discord Application Commands & sync model, and discord.py v2 app‑commands framework. See:
> - Discord Developer Portal — *Application Commands* [docs](https://discord.com/developers/docs/interactions/application-commands)
> - Discord.py v2 app‑commands & CommandTree guides [guide 1](https://www.pythondiscord.com/pages/guides/python-guides/app-commands/) • [guide 2](https://fallendeity.github.io/discord.py-masterclass/slash-commands/)

---

## Files
```
Dockerfile              # Ubuntu 22.04, GNU Radio, gr-osmosdr, discord.py ≥ 2.x
stereo_fm.py            # Bot (GNU Radio + Discord prefix + slash commands)
healthcheck.sh          # Health check script
presets.json            # Default JSON config copied to /opt/presets.json
README.md               # This file
```

---

## Build
```bash
docker build --no-cache -t bgvfd-radio --build-arg run_volk_profile=1 .
```
This image installs `python3-nacl` and `python3-cffi` from Ubuntu and **does not** use `discord.py[voice]` to avoid the CFFI ABI mismatch. discord.py ≥ 2.6 is installed from PyPI. See PyPI notes on voice support and OS prerequisites. 

---

## Run

**Token as env, guild‑scoped slash sync for fast visibility:**
```bash
docker run --rm \
  --name stereo-fm \
  --device /dev/bus/usb \
  -e DISCORD_TOKEN='<YOUR_DISCORD_BOT_TOKEN>' \
  -e GUILD_ID='<YOUR_GUILD_ID>' \
  bgvfd-radio
```
> If you omit `GUILD_ID`, commands sync **globally** which can take minutes to an hour.

**Using JSON config via file:** place `presets.json` next to Dockerfile (it’s copied to `/opt/presets.json`).

**Using JSON via env (single‑quoted style accepted):**
```bash
docker run --rm \
  --name stereo-fm \
  --device /dev/bus/usb \
  -e DISCORD_TOKEN='<TOKEN>' \
  -e PRESETS_JSON="{'mode':'nfm','default_squelch':0.02,'presets':{'navfire':{'mhz':154.1075}}}" \
  bgvfd-radio
```

---

## Slash Commands (examples)
- `/join [channel]` — joins a voice channel (or your current one).
- `/fm freq_mhz:154.235` — tunes to the frequency and starts streaming.
- `/preset name:<navfire|navmed|fg1|fg2|so1>` — tunes to a named preset.
- `/vol level:1.25` — set playback volume.
- `/squelch level:0.02` — set RMS squelch threshold (`0.0` disables).
- `/gain db:30` — set RTL‑SDR RF gain.
- `/mode name:nfm|wfm` — switch demodulation mode.
- `/stop` — stop SDR & disconnect.

**Prefix commands**: `!join`, `!fm 154.235`, `!preset fg2`, `!stop`.

---

## Troubleshooting
- **CFFI mismatch / PyNaCl build errors**: we use OS packages (`python3-nacl`, `python3-cffi`) instead of `pip install ...[voice]` to keep `_cffi_backend` aligned on ARM64.
- **R82XX PLL not locked**: common retune warnings; the code orders center‑freq → bandwidth → sample rate with short delays + a jitter retune.
- **No audio**: check `/vol`, `/squelch` (try `0.0`), and that the bot prints a non‑zero RMS.

---

## Permissions / Invite
Re‑invite your bot with scopes **`bot`** and **`applications.commands`** and voice permissions (Connect, Speak) so slash commands register and are visible.

---

## License
Review dependency licenses (GNU Radio, gr‑osmosdr, discord.py). This repo content is provided as-is.
