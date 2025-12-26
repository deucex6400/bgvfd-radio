#!/usr/bin/env python3
import sys
import os
import time
import json
import numpy
import discord
from discord.ext import commands as discord_commands
from discord import app_commands

import gnuradio
import gnuradio.analog
import gnuradio.audio
import gnuradio.filter
import gnuradio.gr
from gnuradio.filter import firdes
from gnuradio.fft import window
import osmosdr

# -------------------- Config loading --------------------

def _load_config():
    cfg = None
    env = os.environ.get('PRESETS_JSON')
    if env:
        try:
            env_json = env.replace("'", '"') if env.count("'") > env.count('"') else env
            cfg = json.loads(env_json)
        except Exception:
            print('WARN: Failed to parse PRESETS_JSON; falling back to file or defaults')
    if cfg is None:
        path = '/opt/presets.json'
        if os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    cfg = json.load(f)
            except Exception as e:
                print('WARN: Failed to read /opt/presets.json:', e)
    if cfg is None:
        cfg = {
            'mode': 'nfm',
            'default_squelch': 0.02,
            'default_gain': None,
            'nfm_deviation_hz': 2500,
            'presets': {
                'navfire': {'mhz': 154.1075},
                'navmed':  {'mhz': 154.2350},
                'fg1':     {'mhz': 155.4000},
                'fg2':     {'mhz': 155.2950},
                'so1':     {'mhz': 155.1000}
            }
        }
    return cfg

CONFIG = _load_config()

# -------------------- GNU Radio helper blocks --------------------

def make_source(sample_rate, center_freq=88_500_000):
    src = osmosdr.source(args='rtl=0')
    src.set_freq_corr(0, 0)
    src.set_dc_offset_mode(0, 0)
    src.set_iq_balance_mode(0, 0)
    src.set_gain_mode(False, 0)
    src.set_if_gain(20, 0)
    src.set_bb_gain(20, 0)
    src.set_antenna("", 0)

    src.set_center_freq(center_freq)
    time.sleep(0.05)
    src.set_bandwidth(2_000_000, 0)
    time.sleep(0.05)
    src.set_sample_rate(sample_rate)

    default_gain = CONFIG.get('default_gain')
    if default_gain is not None:
        try:
            src.set_gain(float(default_gain))
        except Exception:
            pass
    else:
        src.set_gain(29.7)
    return src


def make_resampler_ccc(num, denom):
    return gnuradio.filter.rational_resampler_ccc(
        interpolation=num,
        decimation=denom,
        taps=[],
        fractional_bw=0.0,
    )


def make_resampler_fff(num, denom):
    return gnuradio.filter.rational_resampler_fff(
        interpolation=num,
        decimation=denom,
        taps=[],
        fractional_bw=0.0,
    )


def make_channel_lpf(sample_rate, cutoff_hz, trans_hz):
    taps = firdes.low_pass(1.0, sample_rate, cutoff_hz, trans_hz, window.WIN_HAMMING, 6.76)
    return gnuradio.filter.fir_filter_ccf(1, taps)


def make_audio_lpf(sample_rate, cutoff_hz=3500, trans_hz=1500):
    taps = firdes.low_pass(1.0, sample_rate, cutoff_hz, trans_hz, window.WIN_HAMMING, 6.76)
    return gnuradio.filter.fir_filter_fff(1, taps)


def make_wfm(input_rate, decim):
    return gnuradio.analog.wfm_rcv(quad_rate=input_rate, audio_decimation=decim)


def make_nfm_quadrature_demod(sample_rate, deviation_hz):
    g = float(sample_rate) / (2.0 * numpy.pi * float(deviation_hz))
    return gnuradio.analog.quadrature_demod_cf(g)


class CaptureBlock(gnuradio.gr.sync_block, discord.AudioSource):
    def __init__(self):
        gnuradio.gr.sync_block.__init__(self,
            name='Capture Block', in_sig=[numpy.float32], out_sig=[])
        self.buffer = []
        self.buffer_len = 0
        self.playback_started = False
        self.min_buffer = int(48000 * 2 * 2 * 0.06)
        self.playback_length = int(48000 * 2 * 2 * 0.02)
        self.dtype = numpy.dtype('int16')
        self.dtype_i = numpy.iinfo(self.dtype)
        self.dtype_abs_max = 2 ** (self.dtype_i.bits - 1)
        self.last_rms = 0.0
        self.squelch_threshold = float(CONFIG.get('default_squelch', 0.0))

    def work(self, input_items, output_items):
        f = input_items[0]
        if f.size:
            self.last_rms = float(numpy.sqrt(numpy.mean(numpy.clip(f, -1.0, 1.0) ** 2)))
        buf = self._convert(f)
        self.buffer_len += len(buf)
        self.buffer.append(buf)
        self.playback_started = self.buffer_len > self.min_buffer
        return len(f)

    def _convert(self, f):
        f = numpy.asarray(f)
        f = f * self.dtype_abs_max
        f = f.clip(self.dtype_i.min, self.dtype_i.max)
        f = f.astype(self.dtype)
        f = f.repeat(2)
        return f.tobytes()

    def read(self):
        if not self.playback_started:
            return bytes(self.playback_length)
        if self.squelch_threshold > 0.0 and self.last_rms < self.squelch_threshold:
            return bytes(self.playback_length)
        buf = bytearray(self.playback_length)
        i = 0
        while i < self.playback_length and self.buffer:
            next_buf = self.buffer.pop(0)
            next_len = len(next_buf)
            self.buffer_len -= next_len
            if i + next_len > self.playback_length:
                putback_len = next_len - (self.playback_length - i)
                putback = next_buf[-putback_len:]
                self.buffer.insert(0, putback)
                self.buffer_len += putback_len
                next_buf = next_buf[:-putback_len]
                next_len = len(next_buf)
            buf[i:i + next_len] = next_buf
            i += next_len
        return buf


class RadioBlock(gnuradio.gr.top_block):
    def __init__(self):
        gnuradio.gr.top_block.__init__(self, "Discord Radio")
        self.source_sample_rate = 2_400_000
        self.audio_sample_rate = 48_000
        self.wfm_sample_rate = 200_000
        self.wfm_output_rate = self.wfm_sample_rate // 4
        self.nfm_deviation_hz = int(CONFIG.get('nfm_deviation_hz', 2500))

        self.source = make_source(self.source_sample_rate)
        self.capture_block = CaptureBlock()
        self.mode = str(CONFIG.get('mode', 'nfm')).lower()
        self._build_chain()

    def _disconnect_all(self):
        try:
            self.disconnect_all()
        except Exception:
            pass

    def _build_chain(self):
        self._disconnect_all()
        self.resamp1 = make_resampler_ccc(1, self.source_sample_rate // self.wfm_sample_rate)

        if self.mode == 'wfm':
            self.wfm = make_wfm(self.wfm_sample_rate, 4)
            self.resamp2 = make_resampler_fff(48, 50)
            self.connect((self.source, 0), (self.resamp1, 0))
            self.connect((self.resamp1, 0), (self.wfm, 0))
            self.connect((self.wfm, 0), (self.resamp2, 0))
            self.connect((self.resamp2, 0), (self.capture_block, 0))
        else:
            self.chan_lpf = make_channel_lpf(self.wfm_sample_rate, cutoff_hz=6_000, trans_hz=6_000)
            self.quad_demod = make_nfm_quadrature_demod(self.wfm_sample_rate, self.nfm_deviation_hz)
            self.decim4 = make_resampler_fff(1, 4)
            self.audio_lpf = make_audio_lpf(self.wfm_output_rate, cutoff_hz=3500, trans_hz=1500)
            self.resamp2 = make_resampler_fff(48, 50)
            self.connect((self.source, 0), (self.resamp1, 0))
            self.connect((self.resamp1, 0), (self.chan_lpf, 0))
            self.connect((self.chan_lpf, 0), (self.quad_demod, 0))
            self.connect((self.quad_demod, 0), (self.decim4, 0))
            self.connect((self.decim4, 0), (self.audio_lpf, 0))
            self.connect((self.audio_lpf, 0), (self.resamp2, 0))
            self.connect((self.resamp2, 0), (self.capture_block, 0))

    def set_mode(self, mode: str):
        m = str(mode).lower()
        if m not in ('nfm', 'wfm'):
            return False
        was_running = self.is_running()
        if was_running:
            self.stop(); self.wait()
        self.mode = m
        self._build_chain()
        if was_running:
            self.start()
        return True

    def tune(self, freq_hz):
        self.source.set_center_freq(freq_hz)
        time.sleep(0.03)
        self.source.set_center_freq(freq_hz + 25_000)
        time.sleep(0.02)
        self.source.set_center_freq(freq_hz)


# -------------------- Discord bot (prefix + slash) --------------------

intents = discord.Intents.default()
# enable message content only if you need prefix commands
intents.message_content = True

bot = discord_commands.Bot(
    command_prefix=discord_commands.when_mentioned_or('!'),
    description='Radio bot with slash commands',
    intents=intents,
)

GUILD_ID = os.environ.get('GUILD_ID')
GUILD_OBJ = discord.Object(id=int(GUILD_ID)) if GUILD_ID and GUILD_ID.isdigit() else None

@bot.event
async def on_ready():
    print(f"Logged on as {bot.user} (latency ~{bot.latency*1000:.1f} ms) mode={CONFIG.get('mode','nfm')} dev={CONFIG.get('nfm_deviation_hz',2500)} Hz")

# ---------- Prefix commands (kept for backward-compat) ----------
class BotCommands(discord_commands.Cog):
    def __init__(self, bot, radio):
        self.bot = bot
        self.radio = radio

    PRESETS = {k: {
        'mhz': float(v.get('mhz')),
        'squelch': float(v.get('squelch', CONFIG.get('default_squelch', 0.0))),
        'gain': v.get('gain', CONFIG.get('default_gain', None)),
    } for k, v in CONFIG.get('presets', {}).items()}

    async def _ensure_playing(self, ctx):
        vc = ctx.voice_client
        if vc is None and ctx.author.voice:
            vc = await ctx.author.voice.channel.connect()
        if vc and not vc.is_playing():
            src = discord.PCMVolumeTransformer(self.radio.capture_block)
            vc.play(src)
            self.radio.start()

    @discord_commands.command()
    async def join(self, ctx, *, channel: discord.VoiceChannel):
        if ctx.voice_client is not None:
            return await ctx.voice_client.move_to(channel)
        await channel.connect()

    @discord_commands.command()
    async def fm(self, ctx, *, freq):
        freq_mhz = float(freq)
        freq_hz = int(freq_mhz * 1_000_000)
        self.radio.tune(freq_hz)
        await self._ensure_playing(ctx)
        await ctx.send(f"Tuning {freq_mhz:.3f} MHz (mode={self.radio.mode.upper()})")
        await ctx.send(f"Audio RMS ~{self.radio.capture_block.last_rms:.3f}")

    @discord_commands.command()
    async def stop(self, ctx):
        try:
            self.radio.stop()
        finally:
            if ctx.voice_client:
                await ctx.voice_client.disconnect()

# ---------- Slash commands ----------
PRESET_CHOICES = [app_commands.Choice(name=k, value=k) for k in CONFIG.get('presets', {}).keys()]

@bot.tree.command(name="join", description="Join a voice channel")
@app_commands.describe(channel="Voice channel to join; if omitted, I'll join your current voice channel")
async def join_slash(interaction: discord.Interaction, channel: discord.VoiceChannel | None = None):
    vc = interaction.guild.voice_client
    target = channel
    if target is None and interaction.user and getattr(interaction.user, 'voice', None):
        target = interaction.user.voice.channel
    if target is None:
        await interaction.response.send_message("You must specify a voice channel or be connected to one.", ephemeral=True)
        return
    if vc is not None and vc.channel != target:
        await vc.move_to(target)
    elif vc is None:
        await target.connect()
    await interaction.response.send_message(f"Joined {target.mention}")

@bot.tree.command(name="fm", description="Tune to a frequency in MHz and start streaming")
@app_commands.describe(freq_mhz="Frequency in MHz, e.g., 154.235")
async def fm_slash(interaction: discord.Interaction, freq_mhz: float):
    freq_hz = int(float(freq_mhz) * 1_000_000)
    interaction.client.radio.tune(freq_hz)
    vc = interaction.guild.voice_client
    if vc is None and interaction.user and getattr(interaction.user, 'voice', None):
        await interaction.user.voice.channel.connect()
        vc = interaction.guild.voice_client
    if vc and not vc.is_playing():
        vc.play(discord.PCMVolumeTransformer(interaction.client.radio.capture_block))
        interaction.client.radio.start()
    await interaction.response.send_message(f"Tuning {freq_mhz:.3f} MHz (mode={interaction.client.radio.mode.upper()})")

@bot.tree.command(name="preset", description="Tune to a named preset")
@app_commands.describe(name="Preset name")
@app_commands.choices(name=PRESET_CHOICES)
async def preset_slash(interaction: discord.Interaction, name: app_commands.Choice[str]):
    key = name.value
    cfg = {k: {
        'mhz': float(v.get('mhz')),
        'squelch': float(v.get('squelch', CONFIG.get('default_squelch', 0.0))),
        'gain': v.get('gain', CONFIG.get('default_gain', None)),
    } for k, v in CONFIG.get('presets', {}).items()}
    if key not in cfg:
        await interaction.response.send_message("Unknown preset.", ephemeral=True)
        return
    sel = cfg[key]
    interaction.client.radio.capture_block.squelch_threshold = float(sel['squelch'])
    if sel['gain'] is not None:
        try:
            interaction.client.radio.source.set_gain(float(sel['gain']))
        except Exception:
            pass
    freq_hz = int(sel['mhz'] * 1_000_000)
    interaction.client.radio.tune(freq_hz)
    vc = interaction.guild.voice_client
    if vc is None and interaction.user and getattr(interaction.user, 'voice', None):
        await interaction.user.voice.channel.connect()
        vc = interaction.guild.voice_client
    if vc and not vc.is_playing():
        vc.play(discord.PCMVolumeTransformer(interaction.client.radio.capture_block))
        interaction.client.radio.start()
    await interaction.response.send_message(f"Preset '{key}' tuned to {sel['mhz']:.4f} MHz (squelch={sel['squelch']}, gain={sel['gain']})")

@bot.tree.command(name="vol", description="Set output volume (0.0 to 2.0)")
@app_commands.describe(level="Volume level")
async def vol_slash(interaction: discord.Interaction, level: app_commands.Range[float, 0.0, 2.0]):
    vc = interaction.guild.voice_client
    if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = float(level)
        await interaction.response.send_message(f"Volume set to {float(level):.2f}", ephemeral=True)
    else:
        await interaction.response.send_message("Voice not playing yet. Start a stream first.", ephemeral=True)

@bot.tree.command(name="squelch", description="Set RMS squelch threshold (0.0 disables)")
@app_commands.describe(level="Squelch level")
async def squelch_slash(interaction: discord.Interaction, level: app_commands.Range[float, 0.0, 0.5]):
    interaction.client.radio.capture_block.squelch_threshold = float(level)
    await interaction.response.send_message(f"Squelch set to {float(level):.3f}", ephemeral=True)

@bot.tree.command(name="gain", description="Set RTL tuner RF gain (dB)")
@app_commands.describe(db="Gain in dB")
async def gain_slash(interaction: discord.Interaction, db: float):
    try:
        interaction.client.radio.source.set_gain(float(db))
        await interaction.response.send_message(f"RF gain set to {float(db):.1f} dB", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Failed to set gain: {e}", ephemeral=True)

@bot.tree.command(name="mode", description="Switch demodulation mode")
@app_commands.describe(name="Mode: nfm or wfm")
@app_commands.choices(name=[app_commands.Choice(name='nfm', value='nfm'), app_commands.Choice(name='wfm', value='wfm')])
async def mode_slash(interaction: discord.Interaction, name: app_commands.Choice[str]):
    ok = interaction.client.radio.set_mode(name.value)
    if ok:
        await interaction.response.send_message(f"Mode switched to {name.value.upper()}")
    else:
        await interaction.response.send_message("Failed to switch mode", ephemeral=True)

@bot.tree.command(name="stop", description="Stop SDR and disconnect")
async def stop_slash(interaction: discord.Interaction):
    try:
        interaction.client.radio.stop()
    finally:
        vc = interaction.guild.voice_client
        if vc:
            await vc.disconnect()
    await interaction.response.send_message("Radio stopped and disconnected.", ephemeral=True)

# --- setup_hook: attach radio, add cog (await), sync slash ---
@bot.event
async def setup_hook():
    # create radio and attach
    bot.radio = RadioBlock()
    # await add_cog (discord.py v2+)
    await bot.add_cog(BotCommands(bot, bot.radio))
    # slash sync
    if GUILD_ID and GUILD_OBJ:
        await bot.tree.sync(guild=GUILD_OBJ)
        print(f"Slash commands synced to guild {GUILD_OBJ.id}")
    else:
        await bot.tree.sync()
        print("Slash commands synced globally (may take up to an hour to appear)")

if __name__ == '__main__':
    token = None
    if len(sys.argv) >= 2:
        token = sys.argv[1]
    else:
        token = os.environ.get('DISCORD_TOKEN')

    if not token:
        print('Usage: stereo_fm.py <DISCORD_BOT_TOKEN>  (or set DISCORD_TOKEN env)')
        sys.exit(2)

    # setup_hook will create RadioBlock and add the cog
    bot.run(token)
