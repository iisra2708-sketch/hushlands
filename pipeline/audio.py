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
    """Running water. Defaults = mountain brook; with low `bubble` range and a quiet
    `base` it becomes a simmering pot / kettle."""

    def setup(self):
        p = self.p
        self.base = bp(*p.get("base", (1200, 5500)))
        self.base_gain = p.get("base_gain", 0.6)
        self.gurgle = Smooth(self.seed + 1, 9, 0.4, 1.0)
        self.rate = p.get("bubble_rate", 25)
        self.bub_gain = p.get("bubble_gain", 0.12)
        rng = self.rng
        kernels = []
        lo, hi = p.get("bubble", (350, 1100))
        for f0 in rng.uniform(lo, hi, 10):
            tt = np.arange(int(0.04 * SR)) / SR
            f = f0 * (1 + 2.5 * tt / 0.04)
            kernels.append(np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-tt / 0.01))
        self.bubbles = ImpulseBank(rng, kernels)

    def render(self, n, t):
        b = self.base(self.noise(n)) * self.gurgle(t) * self.base_gain
        return b + self.bubbles(n, self.rate * self.gurgle(t), 0.1, 1.0) * self.bub_gain


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


# --------------------------------------------------------------------------- event layers (s3)
def reverb_kernel(rng, seconds=3.0, decay=0.8, lp_hz=3000.0) -> np.ndarray:
    """Synthetic stereo room/hall impulse response (decorrelated noise tails)."""
    L = int(seconds * SR)
    tt = np.arange(L) / SR
    k = rng.standard_normal((CH, L)) * np.exp(-tt / decay)
    k = signal.sosfilt(signal.butter(2, lp_hz, fs=SR, output="sos"), k, axis=-1)
    k[:, : int(0.015 * SR)] = 0  # pre-delay
    return k / np.sqrt((k**2).sum(axis=-1, keepdims=True)) * 6


def place(dry: np.ndarray, pan: float, rev: np.ndarray | None = None, wet: float = 0.5) -> np.ndarray:
    """Mono kernel -> stereo with equal-power pan and optional reverb."""
    w = np.stack([dry * np.sqrt((1 - pan) / 2), dry * np.sqrt((1 + pan) / 2)])
    if rev is not None:
        tail = signal.oaconvolve(dry[None, :], rev, axes=-1)
        out = np.zeros((CH, tail.shape[1]))
        out[:, : w.shape[1]] += w * (1 - wet)
        out += tail * wet
        return out
    return w


class EventLayer(Layer):
    """Discrete scheduled sound events. Subclasses implement make() -> (wave(2,L), gap_s)."""

    def setup(self):
        self.events: list[list] = []
        self.pos = 0
        self.init()
        self.next_t = self.first()

    def init(self):
        pass

    def first(self) -> float:
        return float(self.rng.uniform(4, 20))

    def make(self):
        raise NotImplementedError

    def render(self, n, t):
        self.pos = int(round(t[0] * SR))
        end = self.pos + n
        while self.next_t * SR < end:
            w, gap = self.make()
            self.events.append([int(self.next_t * SR), w])
            self.next_t += max(gap, 0.05)
        out = np.zeros((CH, n))
        keep = []
        for s, w in self.events:
            a, b = max(s, self.pos), min(s + w.shape[1], end)
            if a < b:
                out[:, a - self.pos : b - self.pos] += w[:, a - s : b - s]
            if s + w.shape[1] > end:
                keep.append([s, w])
        self.events = keep
        return out


class Bells(EventLayer):
    """Distant church bells: a peal of a few strikes every ~`every` seconds.
    freq: fundamental (Hz); ratios: one entry per bell in the ring; distance 0..1."""

    def init(self):
        p = self.p
        self.every = p.get("every", 300)
        self.dist = p.get("distance", 0.7)
        self.rev = reverb_kernel(self.rng, 5.0, 1.4, 2600)
        f0 = p.get("freq", 262)
        self.bells = [self._bell(f0 * r) for r in p.get("ratios", [1.0, 0.84, 0.75])]
        self.strikes = p.get("strikes", [5, 12])

    def first(self):  # after the 30 s loudness-calibration chunk
        return float(self.rng.uniform(35, 120))

    def _bell(self, f):
        tt = np.arange(int(7 * SR)) / SR
        parts = [(0.5, 0.9, 5.0), (1.0, 0.8, 3.5), (1.2, 0.5, 2.6), (1.5, 0.35, 2.2),
                 (2.0, 0.6, 1.8), (2.5, 0.25, 1.2), (3.0, 0.2, 0.9), (4.2, 0.12, 0.5)]
        k = sum(a * np.sin(2 * np.pi * f * r * tt + self.rng.uniform(0, 6.28)) * np.exp(-tt / d) for r, a, d in parts)
        k = k * np.minimum(1, tt / 0.003)
        cut = 6500 - 4500 * self.dist
        k = signal.sosfilt(signal.butter(2, cut, fs=SR, output="sos"), k)
        return k / np.abs(k).max()

    def make(self):
        rng = self.rng
        n = int(rng.integers(self.strikes[0], self.strikes[1] + 1))
        gap = rng.uniform(1.7, 2.5)
        L = int((n * gap + 7.5) * SR)
        dry = np.zeros(L)
        for i in range(n):
            b = self.bells[i % len(self.bells)]
            s = int((i * gap + rng.normal(0, 0.03)) * SR)
            s = max(s, 0)
            m = min(len(b), L - s)
            dry[s : s + m] += b[:m] * rng.uniform(0.75, 1.0)
        fade = np.minimum(1, np.arange(L) / (0.5 * SR))
        w = place(dry * fade, rng.uniform(-0.5, 0.5), self.rev, wet=0.35 + 0.5 * self.dist)
        return w * 0.25, self.every * rng.uniform(0.6, 1.4)


class Clock(EventLayer):
    """Pendulum clock tick-tock. period: seconds between ticks."""

    def init(self):
        self.period = self.p.get("period", 1.0)
        rng = self.rng
        rev = reverb_kernel(rng, 0.6, 0.12, 5000)
        self.k = []
        for f in (2300, 1850):
            tt = np.arange(int(0.08 * SR)) / SR
            k = 0.6 * damped_sine(f, 0.08, 0.006) + 0.5 * damped_sine(f * 0.27, 0.08, 0.02)
            k[: len(tt)] += noise_burst(rng, 3500, 0.0015, 0.08)[: len(tt)] * 0.4
            self.k.append(place(k, 0.15, rev, wet=0.3))
        self.i = 0

    def first(self):
        return 0.3

    def make(self):
        self.i ^= 1
        a = self.rng.uniform(0.85, 1.0)
        return self.k[self.i] * a * 0.5, self.period * (1 + self.rng.normal(0, 0.002))


class Chimes(EventLayer):
    """Music-box / glass chimes: random notes (and short runs) from a pentatonic scale.
    Not a melody — pitches are random, so nothing resembles an existing tune."""

    def init(self):
        p = self.p
        base = p.get("base", 587.33)
        steps = p.get("scale", [0, 2, 4, 7, 9])
        octs = p.get("octaves", 2)
        self.notes = [base * 2 ** ((o * 12 + s) / 12) for o in range(octs) for s in steps]
        self.per_min = p.get("per_minute", 8)
        self.rev = reverb_kernel(self.rng, 2.5, 0.6, 6000)
        self.cache: dict[int, np.ndarray] = {}

    def _tine(self, i):
        if i not in self.cache:
            f = self.notes[i]
            tt = np.arange(int(3.5 * SR)) / SR
            k = (np.sin(2 * np.pi * f * tt) * np.exp(-tt / 1.6)
                 + 0.12 * np.sin(2 * np.pi * f * 2 * tt) * np.exp(-tt / 0.5)
                 + 0.05 * np.sin(2 * np.pi * f * 5.4 * tt) * np.exp(-tt / 0.08))
            self.cache[i] = k * np.minimum(1, tt / 0.002)
        return self.cache[i]

    def make(self):
        rng = self.rng
        run = 1 if rng.random() < 0.6 else int(rng.integers(2, 5))
        i = int(rng.integers(len(self.notes)))
        L = int((run * 0.35 + 3.6) * SR)
        dry = np.zeros(L)
        for r in range(run):
            k = self._tine(i)
            s = int(r * rng.uniform(0.22, 0.4) * SR)
            dry[s : s + len(k)] += k[: L - s] * rng.uniform(0.5, 1.0)
            i = int(np.clip(i + rng.choice([-2, -1, 1, 2]), 0, len(self.notes) - 1))
        w = place(dry, rng.uniform(-0.6, 0.6), self.rev, wet=0.35)
        return w * 0.12, rng.exponential(60 / self.per_min)


class Whistle(EventLayer):
    """Distant steam-train whistle: a soft chord blast, sometimes long-short."""

    def init(self):
        self.every = self.p.get("every", 360)
        self.f = self.p.get("freq", 440)
        self.dist = self.p.get("distance", 0.8)
        self.rev = reverb_kernel(self.rng, 4.0, 1.1, 2000)

    def first(self):
        return float(self.rng.uniform(40, 120))

    def _blast(self, dur):
        tt = np.arange(int(dur * SR)) / SR
        bend = 1 - 0.015 * tt / dur
        x = np.zeros_like(tt)
        for r in (1.0, 1.19, 1.5):
            ph = 2 * np.pi * self.f * r * np.cumsum(bend) / SR
            for h in range(1, 7):
                x += np.sin(h * ph) / h * (0.7 if h % 2 == 0 else 1.0)
        x += 0.3 * self.rng.standard_normal(len(tt))  # steam breath
        env = np.minimum(1, tt / 0.18) * np.minimum(1, (dur - tt) / 0.5)
        cut = 2200 - 1500 * self.dist
        return signal.sosfilt(signal.butter(2, cut, fs=SR, output="sos"), x * env)

    def make(self):
        rng = self.rng
        parts = [self._blast(rng.uniform(1.4, 2.6))]
        if rng.random() < 0.5:
            parts += [np.zeros(int(0.35 * SR)), self._blast(rng.uniform(0.5, 0.8))]
        dry = np.concatenate(parts)
        dry /= np.abs(dry).max() + 1e-9
        w = place(dry, rng.uniform(-0.7, 0.7), self.rev, wet=0.4 + 0.4 * self.dist)
        return w * 0.12, self.every * rng.uniform(0.6, 1.5)


class Steps(EventLayer):
    """Someone walking past through fresh snow (crunching footsteps, panned across)."""

    def init(self):
        self.every = self.p.get("every", 150)
        rng = self.rng
        self.k = []
        for _ in range(8):
            dur = 0.22
            L = int(dur * SR)
            tt = np.arange(L) / SR
            g = np.zeros(L)
            dens = 9000 * np.exp(-tt / 0.06)
            hits = np.nonzero(rng.random(L) < dens / SR)[0]
            g[hits] = rng.uniform(-1, 1, len(hits))
            g = signal.sosfilt(signal.butter(2, [900, 5500], btype="band", fs=SR, output="sos"), g)
            g += 0.8 * damped_sine(rng.uniform(90, 140), dur, 0.02)[:L]
            self.k.append(g * np.minimum(1, tt / 0.01))
        self.f = lp(6000)

    def first(self):
        return float(self.rng.uniform(35, 100))

    def make(self):
        rng = self.rng
        n = int(rng.integers(10, 20))
        step = rng.uniform(0.5, 0.65)
        L = int((n * step + 0.5) * SR)
        w = np.zeros((CH, L))
        p0 = rng.choice([-0.9, 0.9])
        for i in range(n):
            x = i / (n - 1)
            pan = p0 * (1 - 2 * x)
            amp = np.sin(np.pi * (0.1 + 0.8 * x)) * rng.uniform(0.7, 1.0)
            k = self.k[rng.integers(len(self.k))] * amp
            s = int((i * step + rng.normal(0, 0.02)) * SR)
            s = max(s, 0)
            w[0, s : s + len(k)] += k[: L - s] * np.sqrt((1 - pan) / 2)
            w[1, s : s + len(k)] += k[: L - s] * np.sqrt((1 + pan) / 2)
        return w * 2.0, self.every * rng.uniform(0.5, 1.6)


class Sleigh(Layer):
    """Horse-drawn sleigh: muffled hooves in snow + sleigh-bell shakes on each step,
    runners hissing over the snow. The horse slows, speeds up and sometimes rests."""

    def setup(self):
        rng = self.rng
        self.tempo = Smooth(self.seed + 1, 1 / 50, 0.36, 0.5)
        self.gate = Smooth(self.seed + 2, 1 / 110)
        self.inten = Smooth(self.seed + 3, 1 / 35, 0.5, 1.0)
        self.jingles = []
        for _ in range(12):
            L = int(0.35 * SR)
            j = np.zeros(L)
            for _ in range(rng.integers(6, 13)):
                f = rng.uniform(2600, 5200)
                tt = np.arange(int(0.25 * SR)) / SR
                b = (np.sin(2 * np.pi * f * tt) + 0.5 * np.sin(2 * np.pi * f * 2.32 * tt)
                     + 0.25 * np.sin(2 * np.pi * f * 4.25 * tt)) * np.exp(-tt / rng.uniform(0.03, 0.09))
                s = int(rng.uniform(0, 0.06) * SR)
                j[s : s + len(b)] += b[: L - s] * rng.uniform(0.3, 1.0)
            self.jingles.append(j / np.abs(j).max())
        self.hooves = []
        for _ in range(6):
            k = damped_sine(rng.uniform(70, 120), 0.2, 0.03) + noise_burst(rng, 400, 0.02, 0.2) * 0.6
            self.hooves.append(k)
        self.L = int(0.4 * SR)
        self.tail = np.zeros((CH, self.L))
        self.next_t = 0.5
        self.beat = 0
        self.pink = Pink()
        self.run = bp(700, 2600)
        self.jlp = lp(7000)

    def render(self, n, t):
        out = np.zeros((CH, n + self.L))
        out[:, : self.L] += self.tail
        t0 = t[0]
        while self.next_t < t0 + n / SR:
            tt = np.array([self.next_t])
            moving = self.gate(tt)[0] > 0.15
            if moving:
                i = int((self.next_t - t0) * SR)
                if 0 <= i < n:
                    a = self.inten(tt)[0] * self.rng.uniform(0.75, 1.0)
                    hv = self.hooves[self.rng.integers(len(self.hooves))] * (1.0 if self.beat % 2 == 0 else 0.7)
                    out[:, i : i + len(hv)] += hv * a * 0.5
                    j = self.jingles[self.rng.integers(len(self.jingles))] * a * (1.0 if self.beat % 2 == 0 else 0.6)
                    pan = self.rng.uniform(-0.2, 0.2)
                    out[0, i : i + len(j)] += j * 0.22 * np.sqrt((1 - pan) / 2)
                    out[1, i : i + len(j)] += j * 0.22 * np.sqrt((1 + pan) / 2)
                self.beat += 1
            self.next_t += self.tempo(tt)[0] * self.rng.uniform(0.96, 1.04)
        self.tail = out[:, n:].copy()
        moving = (self.gate(t) > 0.15).astype(float)
        moving = np.convolve(moving, np.ones(2048) / 2048, mode="same")
        runners = self.run(self.pink(self.noise(n))) * 0.12 * self.inten(t) * moving
        return self.jlp(out[:, :n]) + runners


class Pad(Layer):
    """Soft evolving tone cloud (aurora / starlight): each note swells in and out
    on its own slow cycle; pitches from a consonant chord."""

    def setup(self):
        self.notes = self.p.get("notes", [110, 164.81, 220, 261.63, 329.63, 392.0, 493.88])
        rng = self.rng
        self.env = [Smooth(self.seed + 10 + i, 1 / rng.uniform(20, 45)) for i in range(len(self.notes))]
        self.det = rng.uniform(0.1, 0.35, len(self.notes))
        self.ph = rng.uniform(0, 2 * np.pi, (len(self.notes), CH))

    def render(self, n, t):
        out = np.zeros((CH, n))
        for i, f in enumerate(self.notes):
            e = self.env[i](t) ** 3
            for c, sgn in ((0, -1), (1, 1)):
                fc = f + sgn * self.det[i]
                out[c] += (np.sin(2 * np.pi * fc * t + self.ph[i, c])
                           + 0.2 * np.sin(4 * np.pi * fc * t + self.ph[i, c])) * e
        return out * 0.08


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
    "bells": Bells,
    "clock": Clock,
    "chimes": Chimes,
    "whistle": Whistle,
    "steps": Steps,
    "sleigh": Sleigh,
    "pad": Pad,
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

# sample layer (generated clips) registers itself in LAYERS
from . import audio_samples  # noqa: E402,F401
