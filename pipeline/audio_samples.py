"""`sample` layer: endless, non-repeating ambience built from a pool of generated clips.

World usage (worlds/<id>.yaml, section `audio`):

    - {type: sample, pool: rain-tin, level: 0.12, gain_db: 0,
       fallback: {type: rain, surface: tin, intensity: 0.7}}

`pool` is the id prefix of assets/sounds/<pool>-NN.mp3 (see sounds/library.yaml). If the pool has
no clips yet, the optional `fallback` synth layer is used, so a world never breaks.

How it avoids audible loops: segments of 12-28 s are cut from random clips at random offsets,
with a small random playback-rate change (+-3 %), a small random gain (+-1.2 dB) and 4 s
equal-power crossfades, plus a very slow +-`wobble_db` level drift. A pool of N clips of 30 s
therefore never plays the same sequence twice in a 3 h video.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import numpy as np
from scipy import signal

from .audio import CH, LAYERS, SR, EventLayer, Layer, Smooth, db

SOUNDS = Path(os.environ.get("HUSHLANDS_SOUNDS", Path(__file__).resolve().parent.parent / "assets" / "sounds"))
_KW = signal.butter(2, 120, btype="high", fs=SR, output="sos")


def _decode(path: Path) -> np.ndarray:
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", str(CH), "-ar", str(SR), "-"],
        capture_output=True, check=True,
    )
    x = np.frombuffer(r.stdout, dtype=np.float32).astype(np.float64).reshape(-1, CH).T
    return np.ascontiguousarray(x)


def _high_shelf(fc: float, gain_db: float, fs: int = SR, q: float = 0.707) -> np.ndarray:
    """RBJ high-shelf biquad as SOS."""
    A = 10 ** (gain_db / 40)
    w0 = 2 * np.pi * fc / fs
    cw, sw = np.cos(w0), np.sin(w0)
    al = sw / (2 * q)
    b0 = A * ((A + 1) + (A - 1) * cw + 2 * np.sqrt(A) * al)
    b1 = -2 * A * ((A - 1) + (A + 1) * cw)
    b2 = A * ((A + 1) + (A - 1) * cw - 2 * np.sqrt(A) * al)
    a0 = (A + 1) - (A - 1) * cw + 2 * np.sqrt(A) * al
    a1 = 2 * ((A - 1) - (A + 1) * cw)
    a2 = (A + 1) - (A - 1) * cw - 2 * np.sqrt(A) * al
    return signal.tf2sos([b0 / a0, b1 / a0, b2 / a0], [1, a1 / a0, a2 / a0])


def _prefilter(x: np.ndarray, hp: float | None, lp: float | None, shelf_db: float | None,
               notch: list | None = None) -> np.ndarray:
    """Zero-phase clean-up of a decoded clip: sub-bass removal, optional treble roll-off / shelf."""
    if hp:
        x = signal.sosfiltfilt(signal.butter(4, hp, "high", fs=SR, output="sos"), x, axis=-1)
    if lp:
        x = signal.sosfiltfilt(signal.butter(4, lp, "low", fs=SR, output="sos"), x, axis=-1)
    if shelf_db:
        x = signal.sosfiltfilt(_high_shelf(3500, shelf_db), x, axis=-1)
    for fc, q in (notch or []):  # kill steady whistles / hums: [[freq_hz, Q], ...]
        b, a = signal.iirnotch(float(fc), float(q), fs=SR)
        x = signal.filtfilt(b, a, x, axis=-1)
    return np.ascontiguousarray(x)


def _krms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(signal.sosfilt(_KW, x, axis=-1) ** 2)) + 1e-12)


class SampleLayer(Layer):
    def setup(self):
        pool = self.p["pool"]
        self.fade = int(float(self.p.get("fade", 4.0)) * SR)
        self.level = float(self.p.get("level", 0.1))  # K-weighted rms of the layer (synth rain ~0.2, fire ~0.09)
        cap = float(self.p.get("crest_cap", 12.0))  # max peak/level ratio (12 = 21.6 dB): keeps crackles from clipping
        raw = []
        files = sorted(SOUNDS.glob(f"{pool}-*.mp3"))
        if self.p.get("clips"):  # 1-based subset of the pool, e.g. clips: [2]
            files = [files[i - 1] for i in self.p["clips"] if 0 < i <= len(files)]
        for f in files:
            x = _decode(f)
            if x.shape[1] < 2 * self.fade + SR:  # too short to crossfade: tile it
                x = np.tile(x, (1, int(np.ceil((2 * self.fade + SR) / x.shape[1]))))
            raw.append(_prefilter(x, self.p.get("highpass", 70), self.p.get("lowpass"), self.p.get("shelf_db"), self.p.get("notch")))
        self.clips = []
        if raw:
            refs = [max(_krms(x), float(np.abs(x).max()) / cap) for x in raw]
            if self.p.get("per_clip", False):
                # stationary textures (rain, wind): equalise every clip so segment changes are inaudible
                self.clips = [x / r for x, r in zip(raw, refs)]
            else:
                # crackly material (fire): one common scale for the pool keeps natural variation
                self.clips = [x / float(np.median(refs)) for x in raw]
        self.fb = None
        if not self.clips:
            fb = self.p.get("fallback")
            if not fb:
                raise RuntimeError(f"sample pool '{pool}' is empty and no fallback was given")
            fb = dict(fb)
            self.fb = LAYERS[fb.pop("type")](seed=self.seed, **fb)
            return
        self.buf = np.zeros((CH, 0))
        self.last = -1
        self.wobble = float(self.p.get("wobble_db", 1.0))
        self.drift = Smooth(self.seed + 3, 1 / 25, -self.wobble, self.wobble)
        ramp = np.linspace(0, np.pi / 2, self.fade)
        self.fi, self.fo = np.sin(ramp), np.cos(ramp)

    def _segment(self) -> np.ndarray:
        rng = self.rng
        n = len(self.clips)
        k = int(rng.integers(0, n))
        if n > 1 and k == self.last:
            k = (k + 1 + int(rng.integers(0, n - 1))) % n
        self.last = k
        c = self.clips[k]
        if self.p.get("whole", False):
            # rhythmic seamless loops (train wheels): play the clip whole, no tempo change -> steady rhythm
            return c
        length = min(int(rng.uniform(12, 28) * SR), c.shape[1])
        start = int(rng.integers(0, c.shape[1] - length + 1))
        seg = c[:, start:start + length]
        j = float(self.p.get("jitter", 0.03))
        rate = rng.uniform(1 - j, 1 + j)
        if abs(rate - 1) > 1e-3:
            seg = signal.resample_poly(seg, 1000, int(round(1000 * rate)), axis=1)
        return seg * db(rng.uniform(-1.2, 1.2))

    def _fill(self, n: int):
        F = self.fade
        while self.buf.shape[1] - F < n:
            seg = self._segment()
            if self.buf.shape[1] == 0:
                seg = seg.copy()
                seg[:, :F] *= self.fi  # fade in the very first segment
                self.buf = seg
            else:
                self.buf[:, -F:] = self.buf[:, -F:] * self.fo + seg[:, :F] * self.fi
                self.buf = np.concatenate([self.buf, seg[:, F:]], axis=1)

    def render(self, n, t):
        self._fill(n)
        out, self.buf = self.buf[:, :n], self.buf[:, n:]
        return out * self.level * db(self.drift(t))[None, :]

    def __call__(self, n, t):
        if self.fb is not None:
            return self.fb(n, t) * self.gain
        return super().__call__(n, t)


class TickSeq(EventLayer):
    """Wall/pendulum clock rebuilt from real ticks: individual tick events are cut out of generated
    clips and re-scheduled at a perfectly regular period (tick / tock alternate), because generated
    clock audio is rhythmically irregular and cannot be crossfaded or looped without phasing.

    {type: tickseq, pool: clock, period: 1.0, peak: 0.3, fallback: {type: clock, period: 1.0}}"""

    def init(self):
        pool = self.p["pool"]
        self.period = float(self.p.get("period", 1.0))
        peak = float(self.p.get("peak", 0.3))
        evs = []
        for f in sorted(SOUNDS.glob(f"{pool}-*.mp3")):
            x = _prefilter(_decode(f), self.p.get("highpass", 150), self.p.get("lowpass"), None)
            m = x.mean(0)
            hf = signal.sosfilt(signal.butter(4, 1500, "high", fs=SR, output="sos"), m)
            env = np.abs(signal.hilbert(hf))
            w = int(0.004 * SR)
            env = np.convolve(env, np.ones(w) / w, "same")
            pk, _ = signal.find_peaks(env, height=env.max() * 0.35, distance=int(0.2 * SR))
            for p in pk:
                a, b = max(p - int(0.008 * SR), 0), min(p + int(0.22 * SR), x.shape[1])
                e = x[:, a:b].copy()
                if e.shape[1] < int(0.2 * SR):
                    continue
                n = int(0.04 * SR)
                e[:, -n:] *= np.linspace(1, 0, n)
                f_, pw = signal.welch(e.mean(0), SR, nperseg=min(1024, e.shape[1]))
                evs.append((float((f_ * pw).sum() / (pw.sum() + 1e-18)), float(np.abs(e).max()), e))
        self.fb = None
        if len(evs) < 6:
            fb = self.p.get("fallback")
            if not fb:
                raise RuntimeError(f"tickseq pool '{pool}' has no usable ticks")
            fb = dict(fb)
            self.fb = LAYERS[fb.pop("type")](seed=self.seed, **fb)
            self.evs = [[], []]
            return
        med = float(np.median([e[1] for e in evs]))
        evs = [e for e in evs if e[1] >= 0.4 * med]          # drop weak / ghost detections
        evs.sort(key=lambda e: e[0])
        half = len(evs) // 2
        self.evs = [[e[2] / med * peak for e in evs[:half]], [e[2] / med * peak for e in evs[half:]]]  # tock (dull) / tick (bright)
        self.i = 0

    def first(self):
        return 0.3

    def make(self):
        self.i ^= 1
        pool = self.evs[self.i]
        e = pool[int(self.rng.integers(0, len(pool)))] * self.rng.uniform(0.88, 1.0)
        return e, self.period * (1 + self.rng.normal(0, 0.0015))

    def __call__(self, n, t):
        if self.fb is not None:
            return self.fb(n, t) * self.gain
        return super().__call__(n, t)


class ThunderSeq(EventLayer):
    """Rare single distant thunder rolls taken from generated clips (pool `thunder-distant`).
    One clip = one event (no overlapping, no repeats in a row), faded in/out, low-passed so there is no
    sharp crack, spaced `gap` seconds apart (min, max). `level` = rms of the event (rain sample layer ~0.1).

    {type: thunderseq, pool: thunder-distant, level: 0.05, gap: [90, 240],
     fallback: {type: thunder, per_minute: 0.5, distance: 0.6, gain_db: -9}}"""

    def init(self):
        pool = self.p["pool"]
        lo, hi = self.p.get("gap", [90, 240])
        self.gap = (float(lo), float(hi))
        level = float(self.p.get("level", 0.05))
        self.clips = []
        files = sorted(SOUNDS.glob(f"{pool}-*.mp3"))
        if self.p.get("clips"):  # 1-based subset of the pool
            files = [files[i - 1] for i in self.p["clips"] if 0 < i <= len(files)]
        for f in files:
            x = _prefilter(_decode(f), self.p.get("highpass", 35), self.p.get("lowpass", 1200), None)
            x = x / (float(np.sqrt(np.mean(x ** 2))) + 1e-9) * level
            n_in, n_out = int(1.5 * SR), min(int(2.5 * SR), x.shape[1] // 2)
            x[:, :n_in] *= np.sin(np.linspace(0, np.pi / 2, n_in))
            x[:, -n_out:] *= np.cos(np.linspace(0, np.pi / 2, n_out))
            self.clips.append(x)
        self.fb = None
        self.last = -1
        if not self.clips:
            fb = self.p.get("fallback")
            if not fb:
                raise RuntimeError(f"thunderseq pool '{pool}' is empty and no fallback was given")
            fb = dict(fb)
            self.fb = LAYERS[fb.pop("type")](seed=self.seed, **fb)

    def first(self):
        return float(self.rng.uniform(15, 45))

    def make(self):
        n = len(self.clips)
        k = int(self.rng.integers(0, n))
        if n > 1 and k == self.last:
            k = (k + 1 + int(self.rng.integers(0, n - 1))) % n
        self.last = k
        x = self.clips[k] * db(self.rng.uniform(-3, 0))
        pan = float(self.rng.uniform(-0.3, 0.3))
        x = np.stack([x[0] * np.sqrt((1 - pan) / 2), x[1] * np.sqrt((1 + pan) / 2)]) * np.sqrt(2)
        gap = float(self.rng.uniform(*self.gap)) + x.shape[1] / SR  # gap counts from the end of this event
        return x, gap

    def __call__(self, n, t):
        if self.fb is not None:
            return self.fb(n, t) * self.gain
        return super().__call__(n, t)


LAYERS["sample"] = SampleLayer
LAYERS["tickseq"] = TickSeq
LAYERS["thunderseq"] = ThunderSeq
