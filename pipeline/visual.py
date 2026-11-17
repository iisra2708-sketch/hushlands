"""Seamless animated loop over a still artwork, then stretched to hours via stream copy.

Every effect is periodic in the loop length T (integer cycles), so the loop has no seam.
Only the short loop is encoded; the final video repeats it with `-stream_loop` + `-c copy`,
which takes seconds even for a 10-hour video.
"""
from __future__ import annotations

import subprocess

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1920, 1080
FPS = 24


def load_base(path: str | None, world_id: str, palette=((12, 18, 38), (40, 52, 84))) -> tuple[Image.Image, bool]:
    """Return (1920x1080 RGB image, is_placeholder)."""
    if path:
        im = Image.open(path).convert("RGB")
        r = max(W / im.width, H / im.height)
        im = im.resize((round(im.width * r), round(im.height * r)), Image.LANCZOS)
        l, t = (im.width - W) // 2, (im.height - H) // 2
        return im.crop((l, t, l + W, t + H)), False
    # placeholder: night gradient + stars (for tests only — never upload)
    y = np.linspace(0, 1, H)[:, None, None]
    a, b = np.array(palette[0]), np.array(palette[1])
    arr = (a * (1 - y) + b * y) * np.ones((1, W, 1))
    rng = np.random.default_rng(abs(hash(world_id)) % 2**32)
    for _ in range(400):
        x, yy = rng.integers(0, W), rng.integers(0, H // 2)
        arr[yy, x] = 255 * rng.uniform(0.4, 1)
    im = Image.fromarray(arr.clip(0, 255).astype(np.uint8))
    d = ImageDraw.Draw(im)
    d.text((60, H - 120), f"PLACEHOLDER — {world_id}", fill=(255, 80, 80), font=_font(48))
    return im, True


def _font(size: int, bold: bool = True):
    for f in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ):
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _vignette(strength=0.35):
    yy, xx = np.mgrid[0:H, 0:W]
    d = np.sqrt(((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2)
    return (1 - strength * np.clip(d - 0.4, 0, 1) ** 1.5)[..., None].astype(np.float32)


class Effects:
    def __init__(self, cfg: dict, T: float, seed: int):
        self.cfg, self.T = cfg, T
        rng = np.random.default_rng(seed)
        self.rng = rng
        r = cfg.get("rain")
        if r:
            n = int(r.get("density", 350))
            span = H + 200
            self.rain = dict(
                x0=rng.uniform(-300, W + 300, n),
                y0=rng.uniform(0, span, n),
                k=rng.integers(int(r.get("speed_min", 8)), int(r.get("speed_max", 13)) + 1, n),  # cycles per loop
                ln=rng.uniform(18, 45, n),
                a=rng.uniform(40, 105, n) * r.get("opacity", 1.0),
                slant=np.tan(np.radians(r.get("angle", 12))),
                span=span,
            )
        s = cfg.get("snow")
        if s:
            n = int(s.get("density", 260))
            span = H + 40
            self.snow = dict(
                x0=rng.uniform(0, W, n), y0=rng.uniform(0, span, n),
                k=rng.integers(1, 3, n), sw=rng.uniform(10, 40, n), m=rng.integers(1, 4, n),
                ph=rng.uniform(0, 2 * np.pi, n), rad=rng.uniform(1.2, 3.4, n), span=span,
                drift=s.get("drift", 1),
            )
        self.glows = []
        for g in cfg.get("glow", []):  # flickering warm light (fire, lamp, candle, window)
            yy, xx = np.mgrid[0:H, 0:W]
            cx, cy, rad = g["x"] * W, g["y"] * H, g.get("radius", 0.25) * W
            mask = np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * rad**2))).astype(np.float32)
            harm = [(int(nn), rng.uniform(0, 2 * np.pi), rng.uniform(0.3, 1)) for nn in rng.integers(5, 40, 6)]
            col = np.array(g.get("color", [255, 150, 60]), np.float32) / 255
            self.glows.append((mask[..., None], harm, col, g.get("strength", 0.25)))
        self.breathe = cfg.get("breathe", 0.03)
        self.vig = _vignette(cfg.get("vignette", 0.35))

    def frame(self, base: np.ndarray, t: float) -> np.ndarray:
        T = self.T
        img = base * (1 + self.breathe * np.sin(2 * np.pi * t / T))
        for mask, harm, col, st in self.glows:
            f = sum(a * np.sin(2 * np.pi * n * t / T + p) for n, p, a in harm) / len(harm)
            img = img + mask * col * (st * (0.6 + 0.4 * f)) * 255 * 0.5
        img = img * self.vig
        im = Image.fromarray(img.clip(0, 255).astype(np.uint8))
        if hasattr(self, "rain") or hasattr(self, "snow"):
            ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(ov)
            if hasattr(self, "rain"):
                r = self.rain
                y = (r["y0"] + r["k"] * r["span"] * t / T) % r["span"] - 100
                x = r["x0"] + y * r["slant"]
                dx, dy = r["ln"] * r["slant"], r["ln"]
                for i in range(len(y)):
                    d.line([(x[i], y[i]), (x[i] + dx[i], y[i] + dy[i])], fill=(200, 215, 235, int(r["a"][i])), width=1)
            if hasattr(self, "snow"):
                s = self.snow
                y = (s["y0"] + s["k"] * s["span"] * t / T) % s["span"] - 20
                x = (s["x0"] + s["sw"] * np.sin(2 * np.pi * s["m"] * t / T + s["ph"]) + s["drift"] * s["k"] * 0) % W
                for i in range(len(y)):
                    rr = s["rad"][i]
                    d.ellipse([x[i] - rr, y[i] - rr, x[i] + rr, y[i] + rr], fill=(240, 245, 255, 190))
            ov = ov.filter(ImageFilter.GaussianBlur(0.6))
            im = Image.alpha_composite(im.convert("RGBA"), ov).convert("RGB")
        return np.asarray(im)


def render_loop(base: Image.Image, cfg: dict, out_path: str, T: float = 12.0, seed: int = 1, crf: int = 27,
                maxrate_k: int = 1500, log=print):
    fx = Effects(cfg, T, seed)
    b = np.asarray(base, dtype=np.float32)
    n = int(T * FPS)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
        "-c:v", "libx264", "-preset", "slow", "-crf", str(crf), "-maxrate", f"{maxrate_k}k",
        "-bufsize", f"{maxrate_k * 2}k", "-pix_fmt", "yuv420p",
        "-g", str(n), "-keyint_min", str(n), "-movflags", "+faststart", out_path,
    ]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for i in range(n):
        p.stdin.write(fx.frame(b, i / FPS).tobytes())
        if i % 72 == 0:
            log(f"  loop frame {i}/{n}")
    p.stdin.close()
    if p.wait() != 0:
        raise RuntimeError("ffmpeg loop encode failed")


def mux(loop_path: str, audio_path: str, seconds: float, out_path: str):
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-stream_loop", "-1", "-i", loop_path, "-i", audio_path,
        "-map", "0:v", "-map", "1:a", "-c", "copy", "-t", f"{seconds:.3f}",
        "-movflags", "+faststart", out_path,
    ]
    subprocess.run(cmd, check=True)


def thumbnail(base: Image.Image, lines: list[str], badge: str, out_path: str, accent=(255, 196, 120)):
    im = base.resize((1280, 720), Image.LANCZOS).convert("RGB")
    arr = np.asarray(im, dtype=np.float32)
    yy, xx = np.mgrid[0:720, 0:1280]
    shade = np.clip(1 - (xx / 1280) * 1.2, 0, 1) * 0.65  # darken left side for text
    arr = arr * (1 - shade[..., None])
    im = Image.fromarray(arr.clip(0, 255).astype(np.uint8))
    d = ImageDraw.Draw(im)
    y = 200
    for i, line in enumerate(lines[:3]):
        size = 92 if i == 0 else (64 if i == 1 else 48)
        f = _font(size)
        d.text((64 + 3, y + 3), line, font=f, fill=(0, 0, 0))
        d.text((64, y), line, font=f, fill=(255, 255, 255) if i == 0 else accent)
        y += size + 18
    fb = _font(44)
    tw = d.textlength(badge, font=fb)
    d.rounded_rectangle([64, 600, 64 + tw + 40, 664], radius=14, fill=(0, 0, 0, 200))
    d.text((84, 606), badge, font=fb, fill=accent)
    im.save(out_path, quality=92)
