
#!/usr/bin/env python3
# Stereo/VHF Radio Discord Bot — prefix + slash commands
# Patched for RTL-SDR Blog V4 / R828D: vmcircbuf env, preset fix, robust tuning, V4-friendly rates
# + prefix utilities: !mode, !bw, !rfinfo

import sys
import os

# --- GNU Radio runtime: force a stable, portable circular buffer and reduce log noise ---
os.environ.setdefault('GR_VMCIRCBUF_IMPLEMENTATION', 'malloc')
os.environ.setdefault('GR_CONSOLE_LOG_ENABLE', '0')

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
    """Load presets/config from env PRESETS_JSON or /opt/presets.json.
    Supports standard JSON or single-quoted JSON-like strings inside double quotes.
    """
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
            # Optional: add "ppm": 2 if you want a tiny correction from rtl_test -p
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
    # If you measured ppm with `rtl_test -p`, you can set it in presets.json as "ppm"
    ppm = int(CONFIG.get('ppm', 0))
    try:
        if ppm:
            src.set_freq_corr(ppm, 0)
    except Exception:
        pass

    src.set_freq_corr(0, 0)
    src.set_dc_offset_mode(0, 0)
    src.set_iq_balance_mode(0, 0)
    src.set_gain_mode(False, 0)
    src.set_if_gain(20, 0)
    src.set_bb_gain(20, 0)
    src.set_antenna("", 0)

    # Gentle bring-up for R82xx/R828D
    try:
        src.set_center_freq(center_freq)
        time.sleep(0.05)
        # Friendlier default bandwidth for VHF public safety
        src.set_bandwidth(1_200_000, 0)
        time.sleep(0.05)
    except Exception:
        pass

    src.set_sample_rate(sample_rate)

    default_gain = CONFIG.get('default_gain')
    if default_gain is not None:
        try:
            src.set_gain(float(default_gain))
        except Exception:
            pass
    else:
        # Start moderate; user can adjust via commands
        src.set_gain(29.7)
    return src

def make_resampler_ccc(num, denom):
    return gnuradio.filter.rational_resampler_ccc(
        interpolation=num,
        decimation=denom,
        taps=[],  # explicit, not None
        fractional_bw=0.0,
    )

def make_resampler_fff(num, denom):
    return gnuradio.filter.rational_resampler_fff(
        interpolation=num,
        decimation=denom,
        taps=[],  # explicit, not None
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
        self.min_buffer = int(48000 * 2 * 2 * 0.06)    # ~60ms priming
        self.playback_length = int(48000 * 2 * 2 * 0.02)  # ~20ms chunks
        self.dtype = numpy.dtype('int16')
        self.dtype_i = numpy.iinfo(self.dtype)
        self.dtype_abs_max = 2 ** (self.dtype_i.bits - 1)
        self.last_rms = 0.0
        self.squelch_threshold = float(CONFIG.get('default_squelch', 0.0))
        # Light-rate audio RMS debug printing
        self._last_rms_log = 0.0

    def work(self, input_items, output_items):
        f = input_items[0]
        if f.size:
            self.last_rms = float(numpy.sqrt(numpy.mean(numpy.clip(f, -1.0, 1.0) ** 2)))
            # Debug: print RMS about twice per second while audio is flowing
            try:
                now = time.monotonic()
                if now - self._last_rms_log >= 0.5:
                    print(f"[AUDIO] RMS={self.last_rms:.4f} (squelch={self.squelch_threshold:.3f})")
                    self._last_rms_log = now
            except Exception:
                pass
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
        f = f.repeat(2)  # mono -> stereo
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
        # Rates optimized for RTL‑SDR Blog V4 / R828D on Pi 5
        self.source_sample_rate = 2_048_000
        self.audio_sample_rate  = 48_000
        self.wfm_sample_rate    = 256_000
        self.wfm_output_rate    = self.wfm_sample_rate // 4  # 64 kHz
        self.nfm_deviation_hz   = int(CONFIG.get('nfm_deviation_hz', 2500))

        # Blocks
        self.source = make_source(self.source_sample_rate)
        self.capture_block = CaptureBlock()
        self.mode = str(CONFIG.get('mode', 'nfm')).lower()

        # Build initial chain per mode
        self._build_chain()

    def _disconnect_all(self):
        try:
            self.disconnect_all()
        except Exception:
            pass

    def _build_chain(self):
        self._disconnect_all()

        # 2.048 MS/s -> 256 kS/s (complex) via integer decimation 8
        self.resamp1 = make_resampler_ccc(1, 8)

        if self.mode == 'wfm':
            # WFM: demod at 256 k -> 64 k audio -> resample to 48 k
            self.wfm = make_wfm(self.wfm_sample_rate, 4)
            self.resamp2 = make_resampler_fff(3, 4)  # 64k -> 48k
            self.connect((self.source, 0), (self.resamp1, 0))
            self.connect((self.resamp1, 0), (self.wfm, 0))
            self.connect((self.wfm, 0), (self.resamp2, 0))
            self.connect((self.resamp2, 0), (self.capture_block, 0))
        else:
            # NFM chain: channel LPF -> quadrature demod -> decimate x4 -> audio LPF -> resample
            self.chan_lpf   = make_channel_lpf(self.wfm_sample_rate, cutoff_hz=6_000,  trans_hz=6_000)
            self.quad_demod = make_nfm_quadrature_demod(self.wfm_sample_rate, self.nfm_deviation_hz)
            self.decim4     = make_resampler_fff(1, 4)       # 256k -> 64k
            self.audio_lpf  = make_audio_lpf(self.wfm_output_rate, cutoff_hz=3500, trans_hz=1500)
            self.resamp2    = make_resampler_fff(3, 4)       # 64k -> 48k

            self.connect((self.source,   0), (self.resamp1,   0))
            self.connect((self.resamp1,  0), (self.chan_lpf,  0))
            self.connect((self.chan_lpf, 0), (self.quad_demod,0))
            self.connect((self.quad_demod,0), (self.decim4,   0))
            self.connect((self.decim4,   0), (self.audio_lpf, 0))
            self.connect((self.audio_lpf,0), (self.resamp2,   0))
            self.connect((self.resamp2,  0), (self.capture_block, 0))

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

    def tune(self, freq_hz: int):
        target = int(freq_hz)
        try:
            print(f"[RADIO] Tuning to {target/1_000_000:.6f} MHz")
        except Exception:
            pass

        # Temporarily relax bandwidth during lock (auto)
        try:
            self.source.set_bandwidth(0, 0)
        except Exception:
            pass

        # Wider multi-step with longer settle times: f -> f+50k -> f-25k -> f
        try:
            self.source.set_center_freq(target);             time.sleep(0.06)
            self.source.set_center_freq(target + 50_000);    time.sleep(0.06)
            self.source.set_center_freq(target - 25_000);    time.sleep(0.06)
            self.source.set_center_freq(target);             time.sleep(0.12)
        except Exception:
            pass

        # Retry: read back center freq and nudge if needed
        for _ in range(3):
            try:
                tuned = int(self.source.get_center_freq())
            except Exception:
                tuned = -1
            if tuned > 0 and abs(tuned - target) <= 3_000:  # within ±3 kHz
                break
            try:
                self.source.set_center_freq(target); time.sleep(0.08)
            except Exception:
                pass

        # Restore a modest bandwidth for VHF public safety
        try:
            self.source.set_bandwidth(1_200_000, 0)
        except Exception:
            pass

    def get_center_mhz(self) -> float:
        try:
            return float(self.source.get_center_freq()) / 1_000_000.0
        except Exception:
            return -1.0

# -------------------- Discord bot (prefix + slash) --------------------
intents = discord.Intents.default()
intents.message_content = True  # needed for prefix commands

bot = discord_commands.Bot(
    command_prefix=discord_commands.when_mentioned_or('!'),
    description='BGVFD Radio Bot',
    intents=intents,
    help_command=None,
)

GUILD_ID = os.environ.get('GUILD_ID')
GUILD_OBJ = discord.Object(id=int(GUILD_ID)) if GUILD_ID and GUILD_ID.isdigit() else None

@bot.event
async def on_ready():
    print(f"Logged on as {bot.user} (latency ~{bot.latency*1000:.1f} ms) "
          f"mode={CONFIG.get('mode','nfm')} dev={CONFIG.get('nfm_deviation_hz',2500)} Hz")

# -------- Prefix Cog --------
class BotCommands(discord_commands.Cog):
    def __init__(self, bot, radio):
        self.bot = bot
        self.radio = radio
        # FIX: assign presets to self
        self.PRESETS = {
            k: {
                'mhz':     float(v.get('mhz')),
                'squelch': float(v.get('squelch', CONFIG.get('default_squelch', 0.0))),
                'gain':    v.get('gain',    CONFIG.get('default_gain', None)),
            } for k, v in CONFIG.get('presets', {}).items()
        }

    async def _ensure_playing(self, ctx):
        vc = ctx.voice_client
        if vc is None and ctx.author.voice:
            vc = await ctx.author.voice.channel.connect()
        if vc and not vc.is_playing():
            src = discord.PCMVolumeTransformer(self.radio.capture_block)
            vc.play(src)
            self.radio.start()

    async def _tune_and_play_ctx(self, ctx, freq_mhz: float, squelch=None, gain=None):
        freq_hz = int(float(freq_mhz) * 1_000_000)
        if gain is not None:
            try:
                self.radio.source.set_gain(float(gain))
            except Exception:
                pass
        self.radio.tune(freq_hz)
        if squelch is not None:
            self.radio.capture_block.squelch_threshold = float(squelch)
        await self._ensure_playing(ctx)
        await ctx.send(
            f"Preset tuned: {float(freq_mhz):.4f} MHz (mode={self.radio.mode.upper()}) "
            f"→ radio reports {self.radio.get_center_mhz():.6f} MHz"
        )

    # --- Prefix: core ---
    @discord_commands.command()
    async def join(self, ctx, *, channel: discord.VoiceChannel):
        if ctx.voice_client is not None:
            return await ctx.voice_client.move_to(channel)
        await channel.connect()

    @discord_commands.command()
    async def fm(self, ctx, *, freq):
        freq_mhz = float(freq)
        await self._tune_and_play_ctx(ctx, freq_mhz)

    @discord_commands.command()
    async def stop(self, ctx):
        try:
            self.radio.stop()
        finally:
            if ctx.voice_client:
                await ctx.voice_client.disconnect()

    # --- Prefix: presets ---
    @discord_commands.command(aliases=['nf'])
    async def navfire(self, ctx):
        cfg = self.PRESETS.get('navfire')
        if not cfg: return await ctx.send("Preset 'navfire' not found")
        await self._tune_and_play_ctx(ctx, cfg['mhz'], cfg['squelch'], cfg['gain'])

    @discord_commands.command(aliases=['nm'])
    async def navmed(self, ctx):
        cfg = self.PRESETS.get('navmed')
        if not cfg: return await ctx.send("Preset 'navmed' not found")
        await self._tune_and_play_ctx(ctx, cfg['mhz'], cfg['squelch'], cfg['gain'])

    @discord_commands.command()
    async def fg1(self, ctx):
        cfg = self.PRESETS.get('fg1')
        if not cfg: return await ctx.send("Preset 'fg1' not found")
        await self._tune_and_play_ctx(ctx, cfg['mhz'], cfg['squelch'], cfg['gain'])

    @discord_commands.command()
    async def fg2(self, ctx):
        cfg = self.PRESETS.get('fg2')
        if not cfg: return await ctx.send("Preset 'fg2' not found")
        await self._tune_and_play_ctx(ctx, cfg['mhz'], cfg['squelch'], cfg['gain'])

    @discord_commands.command()
    async def so1(self, ctx):
        cfg = self.PRESETS.get('so1')
        if not cfg: return await ctx.send("Preset 'so1' not found")
        await self._tune_and_play_ctx(ctx, cfg['mhz'], cfg['squelch'], cfg['gain'])

    # --- Prefix: utilities ---
    @discord_commands.command()
    async def mode(self, ctx, name: str):
        """
        Switch demodulation mode via prefix. Usage:
          !mode nfm   or   !mode wfm
        """
        name = str(name).strip().lower()
        if name not in ('nfm', 'wfm'):
            return await ctx.send("Mode must be 'nfm' or 'wfm'")
        ok = self.radio.set_mode(name)
        if ok:
            await ctx.send(f"Mode switched to {name.upper()}")
        else:
            await ctx.send("Failed to switch mode")

    @discord_commands.command()
    async def bw(self, ctx, hz: int):
        """
        Set tuner front-end bandwidth (Hz). Example: !bw 1200000
        Useful to tighten or relax RF front-end filtering.
        """
        try:
            hz = int(hz)
            self.radio.source.set_bandwidth(hz, 0)
            await ctx.send(f"RF bandwidth set to {hz} Hz")
        except Exception as e:
            await ctx.send(f"Failed to set bandwidth: {e}")

    @discord_commands.command()
    async def rfinfo(self, ctx):
        """
        Show current RF center frequency, gain, and squelch.
        """
        try:
            f = float(self.radio.source.get_center_freq())/1_000_000.0
            g = float(self.radio.source.get_gain())
            await ctx.send(f"RF Info: center={f:.6f} MHz, gain={g:.1f} dB, squelch={self.radio.capture_block.squelch_threshold:.3f}")
        except Exception as e:
            await ctx.send(f"RF Info failed: {e}")

    @discord_commands.command()
    async def vol(self, ctx, level: float):
        vc = ctx.voice_client
        if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = max(0.0, min(2.0, float(level)))
            await ctx.send(f"Volume set to {vc.source.volume:.2f}")
        else:
            await ctx.send("Voice not playing yet. Start a stream first.")

    @discord_commands.command()
    async def squelch(self, ctx, level: float):
        self.radio.capture_block.squelch_threshold = max(0.0, float(level))
        await ctx.send(f"Squelch set to {self.radio.capture_block.squelch_threshold:.3f}")

    @discord_commands.command()
    async def gain(self, ctx, db: float):
        try:
            self.radio.source.set_gain(float(db))
            await ctx.send(f"RF gain set to {float(db):.1f} dB")
        except Exception as e:
            await ctx.send(f"Failed to set gain: {e}")

    @discord_commands.command()
    async def listpresets(self, ctx):
        lines = []
        for name, cfg in self.PRESETS.items():
            g = cfg['gain'] if cfg['gain'] is not None else 'current'
            s = cfg['squelch'] if cfg['squelch'] is not None else 'off'
            lines.append(f"- {name}: {cfg['mhz']:.4f} MHz (squelch={s}, gain={g})")
        await ctx.send("Presets:\n" + "\n".join(lines))

    @discord_commands.command(name='help')
    async def help_cmd(self, ctx):
        await ctx.send(self._build_help_text(prefix=True))

    def _build_help_text(self, prefix=False):
        prefix_cmds = [
            "!join #channel",
            "!fm <MHz>",
            "!navfire  !navmed  !fg1  !fg2  !so1",
            "!vol <0.0-2.0>",
            "!squelch <level>",
            "!gain <dB>",
            "!listpresets",
            "!mode <nfm|wfm>",
            "!bw <Hz>",
            "!rfinfo",
            "!stop",
        ]
        slash_cmds = [
            "/join [channel]",
            "/fm freq_mhz:<number>",
            "/preset name:<navfire navmed fg1 fg2 so1>",
            "/vol level:<0.0-2.0>",
            "/squelch level:<0.0-0.5>",
            "/gain db:<number>",
            "/mode name:<nfm wfm>",
            "/stop",
        ]
        s = ["**BGVFD Radio Bot Help**"]
        if prefix:
            s.append("\n**Prefix commands**\n" + "\n".join(prefix_cmds))
            s.append("\n**Slash commands**\n" + "\n".join(slash_cmds))
        return "\n".join(s)

# -------- Slash commands (still present; may appear after global sync delay) --------
PRESET_CHOICES = [app_commands.Choice(name=k, value=k) for k in CONFIG.get('presets', {}).keys()]

@bot.tree.command(name="join", description="Join a voice channel")
@app_commands.describe(channel="Voice channel to join; if omitted, I'll join your current voice channel")
async def join_slash(interaction: discord.Interaction, channel: discord.VoiceChannel | None = None):
    vc = interaction.guild.voice_client
    target = channel
    if target is None and interaction.user and getattr(interaction.user, 'voice', None):
        target = interaction.user.voice.channel
    if target is None:
        await interaction.response.send_message("Specify a voice channel or join one.", ephemeral=True)
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
    await interaction.response.send_message(
        f"Tuning {float(freq_mhz):.3f} MHz (mode={interaction.client.radio.mode.upper()}) "
        f"→ {interaction.client.radio.get_center_mhz():.6f} MHz"
    )

@bot.tree.command(name="preset", description="Tune to a named preset")
@app_commands.describe(name="Preset name")
@app_commands.choices(name=PRESET_CHOICES)
async def preset_slash(interaction: discord.Interaction, name: app_commands.Choice[str]):
    key = name.value
    cfg = {
        k: {
            'mhz':     float(v.get('mhz')),
            'squelch': float(v.get('squelch', CONFIG.get('default_squelch', 0.0))),
            'gain':    v.get('gain',    CONFIG.get('default_gain', None)),
        } for k, v in CONFIG.get('presets', {}).items()
    }
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
    await interaction.response.send_message(
        f"Preset '{key}' tuned to {sel['mhz']:.4f} MHz (squelch={sel['squelch']}, gain={sel['gain']}) "
        f"→ {interaction.client.radio.get_center_mhz():.6f} MHz"
    )

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
@app_commands.choices(name=[app_commands.Choice(name='nfm', value='nfm'),
                            app_commands.Choice(name='wfm', value='wfm')])
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

@bot.tree.command(name="help", description="Show all available bot commands")
async def help_slash(interaction: discord.Interaction):
    cog = None
    for c in bot.cogs.values():
        if isinstance(c, BotCommands):
            cog = c
            break
    text = "**BGVFD Radio Bot Help**\n"
    if cog:
        text = cog._build_help_text(prefix=False)
    await interaction.response.send_message(text, ephemeral=True)

# --- Basic error handler so prefix issues surface in-channel and logs ---
@bot.event
async def on_command_error(ctx, error):
    msg = f"Command error: {error}"
    try:
        await ctx.send(msg)
    except Exception:
        pass
    print(msg)

# --- setup_hook: attach radio, add cog (await), sync slash ---
@bot.event
async def setup_hook():
    bot.radio = RadioBlock()
    await bot.add_cog(BotCommands(bot, bot.radio))
    if GUILD_ID and GUILD_OBJ:
        await bot.tree.sync(guild=GUILD_OBJ)
        print(f"Slash commands synced to guild {GUILD_OBJ.id}")
    else:
        await bot.tree.sync()
        print("Slash commands synced globally (may take up to an hour to appear)")

# -------------------- Main --------------------
if __name__ == '__main__':
    token = None
    if len(sys.argv) >= 2:
        token = sys.argv[1]
    else:
        token = os.environ.get('DISCORD_TOKEN')
    if not token:
        print('Usage: stereo_fm.py <DISCORD_BOT_TOKEN> (or set DISCORD_TOKEN env)')
        sys.exit(2)
    bot.run(token)