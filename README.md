# BGVFD Radio Bot (GNU Radio + Discord)

This image packages GNU Radio + gr-osmosdr with a Discord.py (2.x) bot that streams SDR audio to a voice channel.

## Build
```bash
docker build --no-cache -t bgvfd-radio --build-arg run_volk_profile=1 .
```

## Run
```bash
docker run --rm \
  --name stereo-fm \
  --device /dev/bus/usb \
  -e DISCORD_TOKEN='<YOUR_DISCORD_BOT_TOKEN>' \
  -e GUILD_ID='<YOUR_GUILD_ID>' \  # optional: guild-scoped slash sync
  bgvfd-radio
```

- Copy `stereo_fm.py` and `presets.json` alongside the Dockerfile before building.
- Slash commands require `applications.commands` scope on the bot invite; prefix commands require **Message Content** intent toggled in the Developer Portal and set in code.

## Healthcheck
The container calls `/usr/local/bin/healthcheck.sh` every 30s to verify bot process and USB bus visibility.
