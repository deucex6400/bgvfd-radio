#!/usr/bin/env python3
# Stereo/VHF Radio Discord Bot — prefix + slash commands
# WX Patch: adds a dedicated 'wx' mode optimized for NOAA Weather Radio (162 MHz band)
#  - RF channel LPF ~12 kHz cutoff (16 kHz occupied BW typical)
#  - NFM deviation default 5000 Hz
#  - Audio LPF ~5 kHz
#  - Preset for WX6 (162.525 MHz)

import sys
import os
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
            'default_squelch': 0.10,
            'default_gain': None,
            'nfm_deviation_hz': 5000,
            'presets': {
                'wx6':     {'mhz': 162.5250, 'squelch': 0.12},
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
    try:
        src.set_center_freq(center_freq)
        time.sleep(0.05)
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
        src.set_gain(18.0)
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


def make_audio_lpf(sample_rate, cutoff_hz=5000, trans_hz=2000):
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
        self._last_rms_log = 0.0

    def work(self, input_items, output_items):
        f = input_items[0]
        if f.size:
            self.last_rms = float(numpy.sqrt(numpy.mean(numpy.clip(f, -1.0, 1.0) ** 2)))
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
        self._running = False
        self.source_sample_rate = 2_048_000
        self.audio_sample_rate = 48_000
        self.wfm_sample_rate = 256_000
        self.wfm_output_rate = self.wfm_sample_rate // 4
        self.nfm_deviation_hz = int(CONFIG.get('nfm_deviation_hz', 5000))
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
        self.resamp1 = make_resampler_ccc(1, 8)
        if self.mode == 'wfm':
            self.wfm = make_wfm(self.wfm_sample_rate, 4)
            self.resamp2 = make_resampler_fff(3, 4)
            self.connect((self.source, 0), (self.resamp1, 0))
            self.connect((self.resamp1, 0), (self.wfm, 0))
            self.connect((self.wfm, 0), (self.resamp2, 0))
            self.connect((self.resamp2, 0), (self.capture_block, 0))
        elif self.mode == 'wx':
            # NOAA WX: slightly wider RF channel and audio
            self.chan_lpf = make_channel_lpf(self.wfm_sample_rate, cutoff_hz=12_000, trans_hz=8_000)
            self.quad_demod = make_nfm_quadrature_demod(self.wfm_sample_rate, self.nfm_deviation_hz)
            self.decim4 = make_resampler_fff(1, 4)
            self.audio_lpf = make_audio_lpf(self.wfm_output_rate, cutoff_hz=5000, trans_hz=2000)
            self.resamp2 = make_resampler_fff(3, 4)
            self.connect((self.source, 0), (self.resamp1, 0))
            self.connect((self.resamp1, 0), (self.chan_lpf, 0))
            self.connect((self.chan_lpf, 0), (self.quad_demod,0))
            self.connect((self.quad_demod,0), (self.decim4, 0))
            self.connect((self.decim4, 0), (self.audio_lpf, 0))
            self.connect((self.audio_lpf,0), (self.resamp2, 0))
            self.connect((self.resamp2, 0), (self.capture_block, 0))
        else:
            # NFM voice
            self.chan_lpf = make_channel_lpf(self.wfm_sample_rate, cutoff_hz=5_000, trans_hz=3_000)
            self.quad_demod = make_nfm_quadrature_demod(self.wfm_sample_rate, self.nfm_deviation_hz)
            self.decim4 = make_resampler_fff(1, 4)
            self.audio_lpf = make_audio_lpf(self.wfm_output_rate, cutoff_hz=3500, trans_hz=1500)
            self.resamp2 = make_resampler_fff(3, 4)
            self.connect((self.source, 0), (self.resamp1, 0))
            self.connect((self.resamp1, 0), (self.chan_lpf, 0))
            self.connect((self.chan_lpf, 0), (self.quad_demod,0))
            self.connect((self.quad_demod,0), (self.decim4, 0))
            self.connect((self.decim4, 0), (self.audio_lpf, 0))
            self.connect((self.audio_lpf,0), (self.resamp2, 0))
            self.connect((self.resamp2, 0), (self.capture_block, 0))

    def start(self):
        try:
            super(RadioBlock, self).start()
        finally:
            self._running = True

    def stop(self):
        try:
            super(RadioBlock, self).stop()
        finally:
            self._running = False

    def set_mode(self, mode: str):
        m = str(mode).lower()
        if m not in ('nfm', 'wfm', 'wx'):
            return False
        was_running = bool(getattr(self, '_running', False))
        if was_running:
            self.stop(); self.wait()
        self.mode = m
        self._build_chain()
        if was_running:
            self.start()
        return True

    def tune(self, freq_hz: int):
        target = int(freq_hz)
        print(f"[RADIO] Tuning to {target/1_000_000:.6f} MHz")
        try:
            self.source.set_bandwidth(0, 0)
        except Exception:
            pass
        try:
            self.source.set_center_freq(target); time.sleep(0.06)
            self.source.set_center_freq(target + 50_000); time.sleep(0.06)
            self.source.set_center_freq(target - 25_000); time.sleep(0.06)
            self.source.set_center_freq(target); time.sleep(0.12)
        except Exception:
            pass
        for _ in range(3):
            try:
                tuned = int(self.source.get_center_freq())
            except Exception:
                tuned = -1
            if tuned > 0 and abs(tuned - target) <= 3_000:
                break
            try:
                self.source.set_center_freq(target); time.sleep(0.08)
            except Exception:
                pass
        try:
            self.source.set_bandwidth(1_200_000, 0)
        except Exception:
            pass

    def is_running(self) -> bool:
        return bool(self._running)

    def get_center_mhz(self) -> float:
        try:
            return float(self.source.get_center_freq()) / 1_000_000.0
        except Exception:
            return -1.0

# -------------------- Discord bot (prefix + slash) --------------------
intents = discord.Intents.default()
intents.message_content = True
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
          f"mode={CONFIG.get('mode','nfm')} dev={CONFIG.get('nfm_deviation_hz',5000)} Hz")

class BotCommands(discord_commands.Cog):
    def __init__(self, bot, radio):
        self.bot = bot
        self.radio = radio
        self.PRESETS = {
            k: {
                'mhz': float(v.get('mhz')),
                'squelch': float(v.get('squelch', CONFIG.get('default_squelch', 0.0))),
                'gain': v.get('gain', CONFIG.get('default_gain', None)),
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
            f"Tuned {float(freq_mhz):.4f} MHz (mode={self.radio.mode.upper()}) "
            f"→ {self.radio.get_center_mhz():.6f} MHz"
        )

    @discord_commands.command()
    async def wx(self, ctx):
        """Switch to WX mode and tune NOAA WX6 (162.525 MHz)."""
        self.radio.set_mode('wx')
        cfg = self.PRESETS.get('wx6', {'mhz': 162.5250, 'squelch': CONFIG.get('default_squelch', 0.12)})
        await self._tune_and_play_ctx(ctx, cfg['mhz'], cfg.get('squelch'))

    @discord_commands.command()
    async def fm(self, ctx, *, freq):
        freq_mhz = float(freq)
        await self._tune_and_play_ctx(ctx, freq_mhz)

    @discord_commands.command()
    async def mode(self, ctx, name: str):
        name = str(name).strip().lower()
        if name not in ('nfm', 'wfm', 'wx'):
            return await ctx.send("Mode must be 'nfm', 'wfm', or 'wx'")
        ok = self.radio.set_mode(name)
        await ctx.send(f"Mode switched to {name.upper()}" if ok else "Failed to switch mode")

    @discord_commands.command()
    async def bw(self, ctx, hz: int):
        try:
            hz = int(hz)
            self.radio.source.set_bandwidth(hz, 0)
            await ctx.send(f"RF bandwidth set to {hz} Hz")
        except Exception as e:
            await ctx.send(f"Failed to set bandwidth: {e}")

    @discord_commands.command()
    async def rfinfo(self, ctx):
        try:
            f = float(self.radio.source.get_center_freq())/1_000_000.0
            g = float(self.radio.source.get_gain())
            await ctx.send(f"RF Info: center={f:.6f} MHz, gain={g:.1f} dB, squelch={self.radio.capture_block.squelch_threshold:.3f}")
        except Exception as e:
            await ctx.send(f"RF Info failed: {e}")

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
    async def stop(self, ctx):
        try:
            self.radio.stop()
        finally:
            if ctx.voice_client:
                await ctx.voice_client.disconnect()

@bot.event
async def on_command_error(ctx, error):
    msg = f"Command error: {error}"
    try:
        await ctx.send(msg)
    except Exception:
        pass
    print(msg)

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

if __name__ == '__main__':
    token = None
    if len(sys.argv) >= 2:
        token = sys.argv[1]
    else:
        token = os.environ.get('DISCORD_TOKEN')
    if not token:
        print('Usage: stereo_fm.wxpatched.py <DISCORD_BOT_TOKEN> (or set DISCORD_TOKEN env)')
        sys.exit(2)
    bot.run(token)
