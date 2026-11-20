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

from .audio import CH, LAYERS, SR, Layer, Smooth, db

SOUNDS = Path(os.environ.get("HUSHLANDS_SOUNDS", Path(__file__).resolve().parent.parent / "assets" / "sounds"))
_KW = signal.butter(2, 120, btype="high", fs=SR, output="sos")


def _decode(path: Path) -> np.ndarray:
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", str(CH), "-ar", str(SR), "-"],
        capture_output=True, check=True,
    )
    x = np.frombuffer(r.stdout, dtype=np.float32).astype(np.float64).reshape(-1, CH).T
    return np.ascontiguousarray(x)


def _krms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(signal.sosfilt(_KW, x, axis=-1) ** 2)) + 1e-12)


class SampleLayer(Layer):
    def setup(self):
        pool = self.p["pool"]
        self.fade = int(float(self.p.get("fade", 4.0)) * SR)
        self.level = float(self.p.get("level", 0.1))  # K-weighted rms of the layer (synth rain ~0.2, fire ~0.09)
        self.clips = []
        for f in sorted(SOUNDS.glob(f"{pool}-*.mp3")):
            x = _decode(f)
            if x.shape[1] < 2 * self.fade + SR:  # too short to crossfade: tile it
                x = np.tile(x, (1, int(np.ceil((2 * self.fade + SR) / x.shape[1]))))
            self.clips.append(x / _krms(x))
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
        length = min(int(rng.uniform(12, 28) * SR), c.shape[1])
        start = int(rng.integers(0, c.shape[1] - length + 1))
        seg = c[:, start:start + length]
        rate = rng.uniform(0.97, 1.03)
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


LAYERS["sample"] = SampleLayer
