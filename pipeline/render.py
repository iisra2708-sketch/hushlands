"""Render one Hushlands video.

    python -m pipeline.render --world lighthouse-storm --hours 3 --out out/
    python -m pipeline.render --next --exclude rendered.txt --out out/   # next world with artwork
    python -m pipeline.render --world X --minutes 1 --out out/            # quick preview
"""
from __future__ import annotations

import argparse
import json
import os
import time
import zlib
from pathlib import Path

import yaml

from . import meta
from .audio import AudioSpec, render_audio
from .visual import load_base, mux, render_loop, thumbnail

ROOT = Path(__file__).resolve().parent.parent
WORLDS = ROOT / "worlds"


def load_world(wid: str) -> dict:
    w = yaml.safe_load((WORLDS / f"{wid}.yaml").read_text())
    w["id"] = wid
    return w


def image_for(w: dict) -> str | None:
    for ext in ("jpg", "jpeg", "png", "webp"):
        p = ROOT / "assets" / "images" / f"{w['id']}.{ext}"
        if p.exists():
            return str(p)
    return None


def pick_next(exclude: set[str]) -> str | None:
    ws = []
    for f in sorted(WORLDS.glob("*.yaml")):
        w = load_world(f.stem)
        if f.stem not in exclude and image_for(w) and not w.get("disabled"):
            ws.append((w.get("order", 999), f.stem))
    return sorted(ws)[0][1] if ws else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--world")
    ap.add_argument("--next", action="store_true")
    ap.add_argument("--exclude", help="file with already-rendered world ids, one per line")
    ap.add_argument("--hours", type=float)
    ap.add_argument("--minutes", type=float, help="override duration (previews)")
    ap.add_argument("--out", default="out")
    ap.add_argument("--allow-placeholder", action="store_true")
    a = ap.parse_args()

    wid = a.world
    if a.next:
        ex = set()
        if a.exclude and os.path.exists(a.exclude):
            ex = {l.strip() for l in open(a.exclude) if l.strip()}
        wid = pick_next(ex)
        if not wid:
            print("NOTHING_TO_RENDER")
            Path(a.out).mkdir(parents=True, exist_ok=True)
            (Path(a.out) / "world.txt").write_text("")
            return
    w = load_world(wid)
    hours = a.minutes / 60 if a.minutes else (a.hours or w.get("hours", 3))
    seconds = hours * 3600
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    seed = zlib.crc32(wid.encode()) % 100000

    t0 = time.time()
    print(f"[{wid}] {hours:.2f} h")
    img = image_for(w)
    if not img and not a.allow_placeholder:
        raise SystemExit(f"No artwork in assets/images/ for {wid}")
    base, placeholder = load_base(img, wid)

    print("1/4 audio")
    render_audio(AudioSpec(layers=w["audio"], seconds=seconds, seed=seed), str(out / "audio.m4a"))
    print("2/4 visual loop")
    # GitHub Release assets max 2 GiB -> cap video bitrate so the file stays < ~1.9 GB
    maxrate = int(max(300, min(1500, 1.85e9 * 8 / max(seconds, 1) / 1000 - 170)))
    print(f"   video maxrate {maxrate} kbps")
    render_loop(base, w.get("visual", {}), str(out / "loop.mp4"), T=w.get("loop_seconds", 12), seed=seed, maxrate_k=maxrate)
    print("3/4 mux")
    mux(str(out / "loop.mp4"), str(out / "audio.m4a"), seconds, str(out / f"{wid}.mp4"))
    print("4/4 thumbnail + metadata")
    h = int(hours) if float(hours).is_integer() else round(hours, 2)
    thumbnail(base, w["thumb_lines"], f"{h} HOURS" if hours >= 1 else "PREVIEW", str(out / f"{wid}-thumb.jpg"))
    m = meta.build(w, hours)
    m.update(world=wid, hours=hours, placeholder=placeholder)
    (out / f"{wid}-metadata.json").write_text(json.dumps(m, indent=2, ensure_ascii=False))
    (out / f"{wid}-upload.md").write_text(
        f"# {m['title']}\n\n## Description\n\n{m['description']}\n\n## Tags\n\n{', '.join(m['tags'])}\n"
    )
    for f in ("audio.m4a", "loop.mp4"):
        (out / f).unlink(missing_ok=True)
    (out / "world.txt").write_text(wid)
    size = (out / f"{wid}.mp4").stat().st_size / 1e6
    print(f"DONE {wid}: {size:.0f} MB in {(time.time() - t0) / 60:.1f} min" + ("  [PLACEHOLDER ART]" if placeholder else ""))


if __name__ == "__main__":
    main()
