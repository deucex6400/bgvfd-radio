#!/usr/bin/env python3
import sys, os, time, json, numpy, discord
from discord.ext import commands as discord_commands
from discord import app_commands
import gnuradio, gnuradio.analog, gnuradio.audio, gnuradio.filter, gnuradio.gr
from gnuradio.filter import firdes
from gnuradio.fft import window
import osmosdr

os.environ.setdefault('GR_VMCIRCBUF_IMPLEMENTATION', 'malloc')
os.environ.setdefault('GR_CONSOLE_LOG_ENABLE', '0')

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
        try:
            with open('/opt/presets.json','r') as f:
                cfg = json.load(f)
        except Exception:
            cfg = None
    if cfg is None:
        cfg = {
            'mode':'wx',
            'default_squelch':0.15,
            'nfm_deviation_hz':5000,
            'presets':{'wx6':{'mhz':162.5250}}
        }
    return cfg
CONFIG = _load_config()

# --- helpers ---

def make_source(sample_rate, center_freq=88_500_000):
    s = osmosdr.source(args='rtl=0')
    s.set_freq_corr(int(CONFIG.get('ppm',0)),0)
    s.set_dc_offset_mode(0,0); s.set_iq_balance_mode(0,0); s.set_gain_mode(False,0)
    s.set_if_gain(20,0); s.set_bb_gain(20,0); s.set_antenna("",0)
    try:
        s.set_center_freq(center_freq); time.sleep(0.05); s.set_bandwidth(1_200_000,0); time.sleep(0.05)
    except Exception: pass
    s.set_sample_rate(sample_rate)
    try:
        s.set_gain(float(CONFIG.get('default_gain',18.0)))
    except Exception: pass
    return s

class CaptureBlock(gnuradio.gr.sync_block, discord.AudioSource):
    def __init__(self, squelch):
        gnuradio.gr.sync_block.__init__(self, name='Capture Block', in_sig=[numpy.float32], out_sig=[])
        self.buffer=[]; self.buffer_len=0; self.playback_started=False
        self.min_buffer=int(48000*2*2*0.06); self.playback_length=int(48000*2*2*0.02)
        self.dtype=numpy.dtype('int16'); self.dtype_i=numpy.iinfo(self.dtype); self.dtype_abs_max=2**(self.dtype_i.bits-1)
        self.last_rms=0.0; self.squelch_threshold=float(squelch); self._last_rms_log=0.0
    def work(self, input_items, output_items):
        f=input_items[0]
        if f.size:
            self.last_rms=float(numpy.sqrt(numpy.mean(numpy.clip(f,-1.0,1.0)**2)))
            now=time.monotonic();
            if now-self._last_rms_log>=0.5:
                print(f"[AUDIO] RMS={self.last_rms:.4f} (squelch={self.squelch_threshold:.3f})"); self._last_rms_log=now
            buf=self._convert(f); self.buffer_len+=len(buf); self.buffer.append(buf); self.playback_started=self.buffer_len>self.min_buffer
        return len(f)
    def _convert(self,f):
        f=numpy.asarray(f)*self.dtype_abs_max
        f=f.clip(self.dtype_i.min,self.dtype_i.max).astype(self.dtype)
        f=f.repeat(2); return f.tobytes()
    def read(self):
        if not self.playback_started: return bytes(self.playback_length)
        if self.squelch_threshold>0.0 and self.last_rms<self.squelch_threshold: return bytes(self.playback_length)
        buf=bytearray(self.playback_length); i=0
        while i<self.playback_length and self.buffer:
            nb=self.buffer.pop(0); nl=len(nb); self.buffer_len-=nl
            if i+nl>self.playback_length:
                pb=nl-(self.playback_length-i); put=nb[-pb:]; self.buffer.insert(0,put); self.buffer_len+=pb; nb=nb[:-pb]; nl=len(nb)
            buf[i:i+nl]=nb; i+=nl
        return buf

class RadioBlock(gnuradio.gr.top_block):
    def __init__(self):
        gnuradio.gr.top_block.__init__(self, 'Discord Radio')
        self._running=False
        self.source_sample_rate=2_048_000
        self.audio_sample_rate=48_000
        self.mid_rate=256_000
        self.out_rate=self.mid_rate//4 # 64k
        self.nfm_dev=int(CONFIG.get('nfm_deviation_hz',5000))
        self.source=make_source(self.source_sample_rate)
        self.capture=CaptureBlock(CONFIG.get('default_squelch',0.15))
        self.mode=str(CONFIG.get('mode','wx')).lower()
        self._build()
    def _disconnect_all(self):
        try: self.disconnect_all()
        except Exception: pass
    def _build(self):
        self._disconnect_all()
        # 2.048 MS/s -> 256 kS/s
        self.resamp1 = gnuradio.filter.rational_resampler_ccc(interpolation=1, decimation=8, taps=[], fractional_bw=0.0)
        self.connect((self.source,0),(self.resamp1,0))
        # AGC on complex before filtering; tuner DC avoidance via xlating FIR
        self.agc_c = gnuradio.analog.agc2_cc(attack_rate=1e-3, decay_rate=1e-2, reference=1.0, gain=1.0)
        self.connect((self.resamp1,0),(self.agc_c,0))
        # Placeholders; configured in tune()
        self.chan = None
        self.quad = gnuradio.analog.quadrature_demod_cf(float(self.mid_rate)/(2.0*numpy.pi*float(self.nfm_dev)))
        self.decim4 = gnuradio.filter.rational_resampler_fff(interpolation=1, decimation=4, taps=[], fractional_bw=0.0)
        self.dc_block = gnuradio.analog.dc_blocker_ff(32, True)
        self.agc_f = gnuradio.analog.agc2_ff(attack_rate=1e-3, decay_rate=1e-2, reference=0.5, gain=1.0)
        self.audio_lpf = gnuradio.filter.fir_filter_fff(1, firdes.low_pass(1.0, self.out_rate, 5000, 2000, window.WIN_HAMMING, 6.76))
        self.resamp2 = gnuradio.filter.rational_resampler_fff(interpolation=3, decimation=4, taps=[], fractional_bw=0.0)
        # Wire post-demod chain
        self.connect((self.agc_c,0),(self.quad,0))
        self.connect((self.quad,0),(self.decim4,0))
        self.connect((self.decim4,0),(self.dc_block,0))
        self.connect((self.dc_block,0),(self.agc_f,0))
        self.connect((self.agc_f,0),(self.audio_lpf,0))
        self.connect((self.audio_lpf,0),(self.resamp2,0))
        self.connect((self.resamp2,0),(self.capture,0))
    def start(self):
        try: super(RadioBlock,self).start()
        finally: self._running=True
    def stop(self):
        try: super(RadioBlock,self).stop()
        finally: self._running=False
    def set_mode(self,m):
        m=str(m).lower()
        if m not in ('wx','nfm','wfm'): return False
        was=bool(self._running)
        if was: self.stop(); self.wait()
        self.mode=m; self._build()
        if was: self.start()
        return True
    def _install_xlating(self, shift_hz, cutoff_hz, trans_hz):
        # Remove existing channel if present
        if self.chan is not None:
            try: self.disconnect((self.agc_c,0),(self.chan,0))
            except Exception: pass
        taps = firdes.low_pass(1.0, self.mid_rate, cutoff_hz, trans_hz, window.WIN_HAMMING, 6.76)
        self.chan = gnuradio.filter.freq_xlating_fir_filter_ccf(1, taps, shift_hz, self.mid_rate)
        self.connect((self.agc_c,0),(self.chan,0))
        # rewire into demod
        try:
            self.disconnect((self.agc_c,0),(self.quad,0))
        except Exception: pass
        self.connect((self.chan,0),(self.quad,0))
    def tune(self,freq_hz:int):
        target=int(freq_hz)
        print(f"[RADIO] Tuning WX -> {target/1_000_000:.6f} MHz")
        # Offset the tuner by +250 kHz to avoid DC spike at 0 Hz
        offset=250_000
        tuned_center=target+offset
        try:
            self.source.set_bandwidth(0,0)
        except Exception: pass
        try:
            self.source.set_center_freq(tuned_center); time.sleep(0.10)
        except Exception: pass
        try:
            self.source.set_bandwidth(1_200_000,0)
        except Exception: pass
        # Frequency translating filter to bring target back to baseband
        if self.mode=='wx':
            self._install_xlating(shift_hz=-offset, cutoff_hz=12_000, trans_hz=8_000)
        else:
            self._install_xlating(shift_hz=-offset, cutoff_hz=5_000, trans_hz=3_000)
    def get_center_mhz(self):
        try: return float(self.source.get_center_freq())/1_000_000.0
        except Exception: return -1.0

intents=discord.Intents.default(); intents.message_content=True
bot = discord_commands.Bot(command_prefix=discord_commands.when_mentioned_or('!'), description='BGVFD Radio Bot', intents=intents, help_command=None)
GUILD_ID=os.environ.get('GUILD_ID'); GUILD_OBJ=discord.Object(id=int(GUILD_ID)) if GUILD_ID and GUILD_ID.isdigit() else None

@bot.event
async def setup_hook():
    bot.radio=RadioBlock()
    await bot.add_cog(BotCommands(bot, bot.radio))
    if GUILD_OBJ: await bot.tree.sync(guild=GUILD_OBJ)
    else: await bot.tree.sync()

@bot.event
async def on_ready():
    print(f"Logged on as {bot.user} (latency ~{bot.latency*1000:.1f} ms) mode={CONFIG.get('mode','wx')} dev={CONFIG.get('nfm_deviation_hz',5000)} Hz")

class BotCommands(discord_commands.Cog):
    def __init__(self, bot, radio):
        self.bot=bot; self.radio=radio
        self.PRESETS={k:{'mhz':float(v.get('mhz')), 'squelch': float(v.get('squelch', CONFIG.get('default_squelch',0.15)))} for k,v in CONFIG.get('presets',{}).items()}
    async def _ensure(self, ctx):
        vc=ctx.voice_client
        if vc is None and ctx.author.voice:
            vc=await ctx.author.voice.channel.connect()
        if vc and not vc.is_playing():
            vc.play(discord.PCMVolumeTransformer(self.radio.capture))
            self.radio.start()
    async def _go(self, ctx, mhz:float, squelch=None):
        self.radio.tune(int(mhz*1_000_000))
        if squelch is not None: self.radio.capture.squelch_threshold=float(squelch)
        await self._ensure(ctx)
        await ctx.send(f"Tuned {mhz:.4f} MHz (mode={self.radio.mode.upper()}) → RF {self.radio.get_center_mhz():.6f} MHz")
    @discord_commands.command()
    async def wx(self, ctx):
        self.radio.set_mode('wx')
        sel=self.PRESETS.get('wx6', {'mhz':162.5250,'squelch':0.15})
        await self._go(ctx, sel['mhz'], sel['squelch'])
    @discord_commands.command()
    async def fm(self, ctx, *, freq):
        self.radio.set_mode('nfm')
        await self._go(ctx, float(freq))
    @discord_commands.command()
    async def gain(self, ctx, db: float):
        try:
            bot.radio.source.set_gain(float(db)); await ctx.send(f"RF gain set to {float(db):.1f} dB")
        except Exception as e:
            await ctx.send(f"Failed to set gain: {e}")
    @discord_commands.command()
    async def squelch(self, ctx, level: float):
        bot.radio.capture.squelch_threshold=max(0.0,float(level)); await ctx.send(f"Squelch set to {bot.radio.capture.squelch_threshold:.3f}")
    @discord_commands.command()
    async def bw(self, ctx, hz:int):
        try:
            bot.radio.source.set_bandwidth(int(hz),0); await ctx.send(f"RF bandwidth set to {int(hz)} Hz")
        except Exception as e:
            await ctx.send(f"Failed to set bandwidth: {e}")
    @discord_commands.command()
    async def rfinfo(self, ctx):
        try:
            f=float(bot.radio.source.get_center_freq())/1_000_000.0; g=float(bot.radio.source.get_gain())
            await ctx.send(f"RF Info: center={f:.6f} MHz, gain={g:.1f} dB, squelch={bot.radio.capture.squelch_threshold:.3f}")
        except Exception as e:
            await ctx.send(f"RF Info failed: {e}")
    @discord_commands.command()
    async def stop(self, ctx):
        try: bot.radio.stop()
        finally:
            if ctx.voice_client: await ctx.voice_client.disconnect()

@bot.event
async def on_command_error(ctx,error):
    try: await ctx.send(f"Command error: {error}")
    except Exception: pass
    print(f"Command error: {error}")

if __name__=='__main__':
    token=sys.argv[1] if len(sys.argv)>=2 else os.environ.get('DISCORD_TOKEN')
    if not token:
        print('Usage: stereo_fm.wx2.py <DISCORD_BOT_TOKEN> (or set DISCORD_TOKEN env)'); sys.exit(2)
    bot.run(token)
