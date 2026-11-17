"""Procedural ambience synthesizer for Hushlands.

Every sound is generated from noise and oscillators, so the audio is 100% original
(no stock samples -> no third-party Content ID claims). Audio is rendered in chunks
and streamed to ffmpeg, so a 10-hour soundscape never needs to fit in memory and
never repeats.
"""
from __future__ import annotations

import math
import subprocess
from dataclasses import dataclass

import numpy as np
from scipy import signal

SR = 44100
CH = 2


# --------------------------------------------------------------------------- helpers
def db(x: float) -> float:
    return 10 ** (x / 20)


class Smooth:
    """Deterministic smooth random control signal (cosine-interpolated random points).

    Chunk-independent: the value at time t only depends on (seed, t), so chunks join
    seamlessly."""

    def __init__(self, seed: int, rate_hz: float, lo: float = 0.0, hi: float = 1.0):
        self.seed, self.rate, self.lo, self.hi = seed, rate_hz, lo, hi
        self._cache: dict[int, float] = {}

    def _pt(self, k: int) -> float:
        v = self._cache.get(k)
        if v is None:
            v = float(np.random.default_rng([self.seed, k]).random())
            self._cache[k] = v
            if len(self._cache) > 4096:
                self._cache.clear()
        return v

    def __call__(self, t: np.ndarray) -> np.ndarray:
        x = t * self.rate
        k0 = np.floor(x).astype(np.int64)
        frac = x - k0
        ks = np.arange(k0.min(), k0.max() + 2)
        pts = np.array([self._pt(int(k)) for k in ks])
        a = pts[k0 - ks[0]]
        b = pts[k0 - ks[0] + 1]
        w = (1 - np.cos(np.pi * frac)) / 2
        return self.lo + (self.hi - self.lo) * (a + (b - a) * w)


class Filt:
    """Stateful SOS filter for (2, n) stereo blocks."""

    def __init__(self, sos):
        self.sos = sos
        self.zi = np.zeros((sos.shape[0], CH, 2))

    def __call__(self, x: np.ndarray) -> np.ndarray:
        y, self.zi = signal.sosfilt(self.sos, x, axis=-1, zi=self.zi)
        return y


def bp(lo, hi, order=2):
    return Filt(signal.butter(order, [lo, hi], btype="band", fs=SR, output="sos"))


def lp(fc, order=2):
    return Filt(signal.butter(order, fc, btype="low", fs=SR, output="sos"))


def hp(fc, order=2):
    return Filt(signal.butter(order, fc, btype="high", fs=SR, output="sos"))


class Pink:
    """Paul Kellet pink noise filter (stateful)."""

    b = [0.049922035, -0.095993537, 0.050612699, -0.004408786]
    a = [1, -2.494956002, 2.017265875, -0.522189400]

    def __init__(self):
        self.zi = np.zeros((CH, 3))

    def __call__(self, w):
        y, self.zi = signal.lfilter(self.b, self.a, w, axis=-1, zi=self.zi)
        return y * 3.5


class Brown:
    def __init__(self):
        self.zi = np.zeros((CH, 1))

    def __call__(self, w):
        y, self.zi = signal.lfilter([0.02], [1, -0.998], w, axis=-1, zi=self.zi)
        return y * 3.0


def widen(x: np.ndarray, width: float) -> np.ndarray:
    mid = (x[0] + x[1]) / 2
    side = (x[0] - x[1]) / 2 * width
    return np.stack([mid + side, mid - side])


# --------------------------------------------------------------------------- layers
class Layer:
    def __init__(self, seed: int, gain_db: float = 0.0, width: float = 0.8, **p):
        self.rng = np.random.default_rng(seed)
        self.seed = seed
        self.gain = db(gain_db)
        self.width = width
        self.p = p
        self.setup()

    def setup(self):
        pass

    def noise(self, n):
        return self.rng.standard_normal((CH, n))

    def render(self, n: int, t: np.ndarray) -> np.ndarray:  # t = absolute time (s)
        raise NotImplementedError

    def __call__(self, n, t):
        return widen(self.render(n, t), self.width) * self.gain


class ImpulseBank:
    """Sparse random events convolved with a bank of short kernels; carries tails
    between chunks so events never get cut."""

    def __init__(self, rng, kernels: list[np.ndarray]):
        self.rng = rng
        self.kernels = kernels
        self.L = max(len(k) for k in kernels)
        self.tail = np.zeros((CH, self.L))

    def __call__(self, n: int, rate: np.ndarray | float, amp_lo=0.3, amp_hi=1.0, pan=0.9):
        """rate: events/s (scalar or per-sample array)."""
        rate_arr = np.broadcast_to(np.asarray(rate, dtype=float), (n,))
        prob = rate_arr / SR
        hits = np.nonzero(self.rng.random(n) < prob)[0]
        out = np.zeros((CH, n + self.L))
        out[:, : self.L] += self.tail
        if len(hits):
            kidx = self.rng.integers(0, len(self.kernels), len(hits))
            amps = self.rng.uniform(amp_lo, amp_hi, len(hits)) ** 2
            pans = self.rng.uniform(-pan, pan, len(hits))
            for ki, k in enumerate(self.kernels):
                sel = kidx == ki
                if not sel.any():
                    continue
                imp = np.zeros((CH, n))
                gl = amps[sel] * np.sqrt((1 - pans[sel]) / 2)
                gr = amps[sel] * np.sqrt((1 + pans[sel]) / 2)
                np.add.at(imp[0], hits[sel], gl)
                np.add.at(imp[1], hits[sel], gr)
                conv = signal.oaconvolve(imp, k[None, :], axes=-1)
                out[:, : conv.shape[1]] += conv
        self.tail = out[:, n : n + self.L].copy()
        return out[:, :n]


def damped_sine(f, dur, decay, rng=None, noise=0.0):
    t = np.arange(int(dur * SR)) / SR
    k = np.sin(2 * np.pi * f * t) * np.exp(-t / decay)
    if noise and rng is not None:
        k += noise * rng.standard_normal(len(t)) * np.exp(-t / (decay * 0.3))
    return k


def noise_burst(rng, fc, decay, dur=0.05):
    tt = np.arange(int(dur * SR)) / SR
    k = rng.standard_normal(len(tt)) * np.exp(-tt / decay)
    lo, hi = fc / 1.8, min(fc * 1.8, SR / 2 - 100)
    return signal.sosfilt(signal.butter(2, [lo, hi], btype="band", fs=SR, output="sos"), k) * 2.5


class NoiseBed(Layer):
    """color: white | pink | brown, optional lp/hp."""

    def setup(self):
        self.color = self.p.get("color", "brown")
        self.pink, self.brown = Pink(), Brown()
        self.lp = lp(self.p["lowpass"]) if "lowpass" in self.p else None
        self.hp = hp(self.p["highpass"]) if "highpass" in self.p else None
        self.mod = Smooth(self.seed + 1, 1 / 30, 0.85, 1.0)

    def render(self, n, t):
        w = self.noise(n)
        x = {"white": lambda: w * 0.3, "pink": lambda: self.pink(w), "brown": lambda: self.brown(w)}[self.color]()
        if self.lp:
            x = self.lp(x)
        if self.hp:
            x = self.hp(x)
        return x * self.mod(t)


class Rain(Layer):
    """surface: open | window | tin | tent | leaves ; intensity 0..1"""

    def setup(self):
        s = self.p.get("surface", "open")
        self.surface = s
        inten = self.p.get("intensity", 0.7)
        self.pink = Pink()
        muffle = {"open": 9000, "leaves": 7000, "window": 4200, "tin": 8000, "tent": 3000}[s]
        self.f_hp, self.f_lp = hp(250), lp(muffle)
        self.f_air = bp(2500, 7000)
        self.inten = Smooth(self.seed + 1, 1 / 45, 0.7 + 0.2 * inten, 1.0)
        rng = self.rng
        if s == "tin":
            kernels = [
                damped_sine(f, 0.12, rng.uniform(0.015, 0.04), rng, 0.4)
                + 0.5 * damped_sine(f * 2.7, 0.12, 0.012)
                for f in rng.uniform(1400, 3800, 32)
            ]
            self.drop_rate, self.drop_amp = 60 + 140 * inten, 0.10
        elif s == "tent":
            kernels = [damped_sine(f, 0.1, rng.uniform(0.012, 0.03), rng, 0.6) for f in rng.uniform(140, 420, 32)]
            self.drop_rate, self.drop_amp = 80 + 200 * inten, 0.22
        elif s == "window":
            kernels = [noise_burst(rng, f, rng.uniform(0.003, 0.01)) for f in rng.uniform(800, 2800, 40)]
            self.drop_rate, self.drop_amp = 40 + 120 * inten, 0.07
        else:  # open / leaves: noise-like splashes (no tonal ringing)
            kernels = [noise_burst(rng, f, rng.uniform(0.002, 0.007)) for f in rng.uniform(1500, 7000, 40)]
            self.drop_rate, self.drop_amp = 100 + 250 * inten, 0.05
        self.drops = ImpulseBank(rng, kernels)
        self.drop_lp = lp(muffle * 1.2)
        self.bed_gain = 0.35 + 0.65 * inten

    def render(self, n, t):
        bed = self.f_lp(self.f_hp(self.pink(self.noise(n))))
        air = self.f_air(self.noise(n)) * 0.25
        m = self.inten(t)
        drops = self.drop_lp(self.drops(n, self.drop_rate * m, 0.2, 1.0)) * self.drop_amp * 8
        return (bed * self.bed_gain + air) * m + drops


class Wind(Layer):
    def setup(self):
        self.pink = Pink()
        self.low, self.high = bp(120, 500), bp(400, 1400)
        self.mix = Smooth(self.seed + 1, 1 / 9)
        self.gust = Smooth(self.seed + 2, 1 / 13, 0.25, 1.0)
        self.strength = self.p.get("strength", 0.6)

    def render(self, n, t):
        x = self.pink(self.noise(n))
        m = self.mix(t)
        g = self.gust(t) ** (1.5 - self.strength)
        return (self.low(x) * (1 - m) + self.high(x) * m * 0.7) * g * 2.2


class Fire(Layer):
    def setup(self):
        self.brown = Brown()
        self.rumble = lp(260)
        self.flick = Smooth(self.seed + 1, 3, 0.5, 1.0)
        rng = self.rng
        crk = []
        for _ in range(30):
            d = rng.uniform(0.002, 0.012)
            tt = np.arange(int(0.03 * SR)) / SR
            k = rng.standard_normal(len(tt)) * np.exp(-tt / d)
            crk.append(signal.sosfilt(signal.butter(2, rng.uniform(900, 2500), "high", fs=SR, output="sos"), k))
        pops = [damped_sine(f, 0.08, 0.01, rng, 1.5) for f in rng.uniform(250, 900, 6)]
        self.crackle = ImpulseBank(rng, crk)
        self.pops = ImpulseBank(rng, pops)
        self.burst = Smooth(self.seed + 3, 0.6, 0.0, 1.0)
        self.hiss = bp(1800, 6000)

    def render(self, n, t):
        r = self.rumble(self.brown(self.noise(n))) * self.flick(t) * 0.35
        h = self.hiss(self.noise(n)) * 0.05 * self.flick(t)
        b = self.burst(t) ** 2
        c = self.crackle(n, 6 + 35 * b, 0.1, 1.0, pan=0.4) * 0.9
        p = self.pops(n, 0.5 + 1.5 * b, 0.3, 1.0, pan=0.4) * 0.5
        return r + h + c + p


class Thunder(Layer):
    """Rare rolling thunder events; distance 0 (close) .. 1 (far)."""

    def setup(self):
        self.per_min = self.p.get("per_minute", 0.6)
        self.dist = self.p.get("distance", 0.7)
        self.events: list[list] = []  # [start_sample_abs, wave(2,L)]
        self.pos = 0

    def _make(self):
        rng = self.rng
        dur = rng.uniform(6, 16)
        L = int(dur * SR)
        tt = np.arange(L) / SR
        env = np.zeros(L)
        for _ in range(rng.integers(2, 6)):
            c = rng.uniform(0, dur * 0.5)
            env += rng.uniform(0.3, 1.0) * np.exp(-np.maximum(tt - c, 0) / rng.uniform(0.8, 3.0)) * (tt >= c)
        env = signal.sosfiltfilt(signal.butter(1, 6, fs=SR, output="sos"), env)
        cutoff = 90 + 260 * (1 - self.dist)
        w = rng.standard_normal((CH, L))
        w = np.cumsum(w, axis=-1)
        w -= signal.sosfiltfilt(signal.butter(1, 15, fs=SR, output="sos"), w, axis=-1)
        w = signal.sosfilt(signal.butter(3, cutoff, fs=SR, output="sos"), w, axis=-1)
        w /= np.abs(w).max() + 1e-9
        loud = rng.uniform(0.5, 1.0) * (1.2 - 0.6 * self.dist)
        fade = np.minimum(1, tt / 0.4) * np.minimum(1, (dur - tt) / 1.5)
        return w * env * fade * loud

    def render(self, n, t):
        prob = self.per_min / 60 / SR * n
        if self.rng.random() < prob * 1.0:
            self.events.append([self.pos + int(self.rng.integers(0, n)), self._make()])
        out = np.zeros((CH, n))
        keep = []
        for ev in self.events:
            s, w = ev
            a = max(s, self.pos)
            b = min(s + w.shape[1], self.pos + n)
            if a < b:
                out[:, a - self.pos : b - self.pos] += w[:, a - s : b - s]
            if s + w.shape[1] > self.pos + n:
                keep.append(ev)
        self.events = keep
        self.pos += n
        return out * 1.5


class Waves(Layer):
    def setup(self):
        self.pink = Pink()
        self.soft, self.bright = lp(700), bp(500, 4000)
        self.env_lp = Filt(signal.butter(1, 3, fs=SR, output="sos"))
        self.next_start = 0.0
        self.waves: list[tuple[float, float, float]] = []
        self.calm = self.p.get("calm", 0.5)

    def _env(self, t):
        while self.next_start < t[-1] + 20:
            per = self.rng.uniform(7, 13)
            self.waves.append((self.next_start, per, self.rng.uniform(0.5, 1.0)))
            self.next_start += per
        e = np.zeros_like(t)
        for s, per, pk in self.waves:
            m = (t >= s) & (t < s + per)
            if m.any():
                ph = (t[m] - s) / per
                e[m] = pk * np.where(ph < 0.55, (ph / 0.55) ** 2, np.exp(-(ph - 0.55) * 5))
        self.waves = [w for w in self.waves if w[0] + w[1] > t[0]]
        return self.env_lp(np.stack([e, e]))

    def render(self, n, t):
        x = self.pink(self.noise(n))
        e = self._env(t)
        return self.soft(x) * (0.25 + 0.75 * e) + self.bright(x) * e**2 * (1.2 - self.calm)


class Brook(Layer):
    def setup(self):
        self.base = bp(1200, 5500)
        self.gurgle = Smooth(self.seed + 1, 9, 0.4, 1.0)
        rng = self.rng
        kernels = []
        for f0 in rng.uniform(350, 1100, 10):
            tt = np.arange(int(0.04 * SR)) / SR
            f = f0 * (1 + 2.5 * tt / 0.04)
            kernels.append(np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-tt / 0.01))
        self.bubbles = ImpulseBank(rng, kernels)

    def render(self, n, t):
        b = self.base(self.noise(n)) * self.gurgle(t) * 0.6
        return b + self.bubbles(n, 25, 0.1, 1.0) * 0.12


class Drone(Layer):
    """Low engine / ship hum. tones: list of Hz."""

    def setup(self):
        self.tones = self.p.get("tones", [55, 110, 165])
        self.brown = Brown()
        self.lp = lp(140)
        self.am = Smooth(self.seed + 1, 1 / 7, 0.7, 1.0)
        self.det = self.rng.uniform(-0.15, 0.15, (len(self.tones), CH))
        self.ph = self.rng.uniform(0, 2 * np.pi, (len(self.tones), CH))

    def render(self, n, t):
        out = self.lp(self.brown(self.noise(n))) * 0.8
        for i, f in enumerate(self.tones):
            for c in range(CH):
                out[c] += np.sin(2 * np.pi * (f + self.det[i, c]) * t + self.ph[i, c]) * 0.12 / (i + 1)
        return out * self.am(t)


class TrackClack(Layer):
    """Night-train wheels: double clacks every ~period seconds."""

    def setup(self):
        self.period = self.p.get("period", 1.15)
        rng = self.rng
        self.k = [damped_sine(f, 0.15, 0.03, rng, 0.8) + 0.3 * damped_sine(f * 9, 0.15, 0.006) for f in rng.uniform(90, 160, 4)]
        self.next_t = 0.5
        self.tail = np.zeros((CH, max(len(k) for k in self.k)))
        self.lp = lp(2500)

    def render(self, n, t):
        L = self.tail.shape[1]
        out = np.zeros((CH, n + L))
        out[:, :L] += self.tail
        t0 = t[0]
        while self.next_t < t0 + n / SR:
            for off, amp in ((0.0, 1.0), (0.13, 0.8)):
                tt = self.next_t + off + self.rng.normal(0, 0.004)
                i = int((tt - t0) * SR)
                if 0 <= i < n:
                    k = self.k[self.rng.integers(len(self.k))] * amp * self.rng.uniform(0.7, 1.0)
                    out[:, i : i + len(k)] += k
            self.next_t += self.period * self.rng.uniform(0.97, 1.03)
        self.tail = out[:, n:].copy()
        return self.lp(out[:, :n]) * 0.6


class Ping(Layer):
    """Distant sonar ping (submarine) with simple echo tail."""

    def setup(self):
        self.every = self.p.get("every", 45)
        tt = np.arange(int(4 * SR)) / SR
        base = np.sin(2 * np.pi * self.p.get("freq", 1150) * tt) * np.exp(-tt / 0.9) * np.minimum(1, tt / 0.01)
        k = base.copy()
        for d, g in ((0.31, 0.35), (0.67, 0.2), (1.1, 0.1)):
            s = int(d * SR)
            k[s:] += base[: len(k) - s] * g
        self.k = k
        self.next_t = self.rng.uniform(5, 15)
        self.tail = np.zeros((CH, len(k)))

    def render(self, n, t):
        L = len(self.k)
        out = np.zeros((CH, n + L))
        out[:, :L] += self.tail
        while self.next_t < t[0] + n / SR:
            i = int((self.next_t - t[0]) * SR)
            if 0 <= i < n:
                out[0, i : i + L] += self.k * 0.8
                out[1, i : i + L] += self.k * 0.6
            self.next_t += self.every * self.rng.uniform(0.7, 1.4)
        self.tail = out[:, n:].copy()
        return out[:, :n] * 0.3


LAYERS = {
    "noise": NoiseBed,
    "rain": Rain,
    "wind": Wind,
    "fire": Fire,
    "thunder": Thunder,
    "waves": Waves,
    "brook": Brook,
    "drone": Drone,
    "train": TrackClack,
    "ping": Ping,
}


# --------------------------------------------------------------------------- render
@dataclass
class AudioSpec:
    layers: list[dict]
    seconds: float
    seed: int = 1
    target_rms_db: float = -24.0
    chunk_s: float = 30.0


_KW = signal.butter(2, 120, btype="high", fs=SR, output="sos")


def build_layers(spec: AudioSpec):
    out = []
    for i, ld in enumerate(spec.layers):
        ld = dict(ld)
        kind = ld.pop("type")
        out.append(LAYERS[kind](seed=spec.seed * 1000 + i * 17, **ld))
    return out


def stream_audio(spec: AudioSpec):
    """Yield float32 (n, 2) interleaved chunks."""
    layers = build_layers(spec)
    total = int(spec.seconds * SR)
    n_chunk = int(spec.chunk_s * SR)
    pos, gain = 0, None
    fade_in, fade_out = int(4 * SR), int(10 * SR)
    while pos < total:
        n = min(n_chunk, total - pos)
        t = (pos + np.arange(n)) / SR
        mix = sum(l(n, t) for l in layers)
        if gain is None:  # calibrate loudness on the first chunk (roughly K-weighted)
            meas = signal.sosfilt(_KW, mix, axis=-1)
            rms = np.sqrt(np.mean(meas**2)) + 1e-12
            gain = db(spec.target_rms_db) / rms
        y = mix * gain
        idx = pos + np.arange(n)
        env = np.minimum(1, idx / fade_in) * np.minimum(1, (total - idx) / fade_out)
        y = np.tanh(y * env * 1.1) / 1.1  # soft limiter
        yield y.T.astype(np.float32)
        pos += n


def render_audio(spec: AudioSpec, out_path: str, bitrate: str = "160k", log=print):
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "f32le", "-ar", str(SR), "-ac", str(CH), "-i", "-",
        "-c:a", "aac", "-b:a", bitrate, out_path,
    ]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    done = 0
    for i, chunk in enumerate(stream_audio(spec)):
        p.stdin.write(chunk.tobytes())
        done += len(chunk)
        if i % 20 == 0:
            log(f"  audio {done / SR / 60:6.1f} / {spec.seconds / 60:.1f} min")
    p.stdin.close()
    if p.wait() != 0:
        raise RuntimeError("ffmpeg audio encode failed")
