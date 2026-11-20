"""Generate the Hushlands sound library with ElevenLabs Sound Effects.

    python -m pipeline.gen_sounds --phase 1                # all missing clips of phase 1
    python -m pipeline.gen_sounds --only rain-tin,fire     # specific ids
    python -m pipeline.gen_sounds --phase 1 --dry-run      # list what would be generated

Reads sounds/library.yaml, writes assets/sounds/<id>-NN.mp3 (skips files that exist, so it is
safe to re-run). The API key comes from the ELEVENLABS_API_KEY environment variable (a GitHub
secret in Actions) and is never printed. Stops cleanly when the credit budget is reached.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "sounds" / "library.yaml"
OUT = ROOT / "assets" / "sounds"
API = os.environ.get("ELEVENLABS_API_BASE", "https://api.elevenlabs.io/v1")
CREDITS_PER_SECOND = 40  # conservative: docs say 20-40 credits per second for a fixed duration


def _req(path: str, key: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        API + path, data=data, method="POST" if body is not None else "GET",
        headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "*/*"},
    )
    return urllib.request.urlopen(r, timeout=180)


def credits_left(key: str) -> int | None:
    try:
        s = json.load(_req("/user/subscription", key))
        return int(s["character_limit"]) - int(s["character_count"])
    except Exception:
        return None  # key without user_read scope: fall back to our own estimate


def generate(key: str, text: str, duration: float, loop: bool, influence: float) -> bytes:
    body = {"text": text, "duration_seconds": duration, "loop": loop, "prompt_influence": influence,
            "model_id": "eleven_text_to_sound_v2"}
    for attempt in range(4):
        try:
            return _req("/sound-generation?output_format=mp3_44100_128", key, body).read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            detail = e.read()[:300].decode("utf-8", "replace")
            raise SystemExit(f"ElevenLabs HTTP {e.code}: {detail}")
    raise SystemExit("unreachable")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", default="all", help="1, 2, ... or all")
    ap.add_argument("--only", default="", help="comma-separated sound ids")
    ap.add_argument("--max-credits", type=int, default=28000, help="stop before spending more than this")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    lib = yaml.safe_load(LIB.read_text())
    d = lib.get("defaults", {})
    only = {s.strip() for s in a.only.split(",") if s.strip()}
    todo = []
    for s in lib["sounds"]:
        if only and s["id"] not in only:
            continue
        if not only and a.phase != "all" and str(s.get("phase", 1)) != str(a.phase):
            continue
        for k in range(1, int(s.get("count", 1)) + 1):
            f = OUT / f"{s['id']}-{k:02d}.mp3"
            if not f.exists():
                dur = float(s.get("duration", d.get("duration", 30)))
                todo.append((f, s, dur))
    est = sum(dur * CREDITS_PER_SECOND for _, _, dur in todo)
    print(f"{len(todo)} clips to generate, worst-case estimate {est:,.0f} credits "
          f"(budget {a.max_credits:,})")
    for f, s, dur in todo:
        print(f"  - {f.name}  {dur:.0f}s  loop={s.get('loop', d.get('loop', True))}")
    if a.dry_run or not todo:
        return

    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not key:
        raise SystemExit("ELEVENLABS_API_KEY is not set")
    OUT.mkdir(parents=True, exist_ok=True)
    start_left = credits_left(key)
    if start_left is not None:
        print(f"credits available on the account: {start_left:,}")
    spent_est = 0
    made = 0
    for f, s, dur in todo:
        worst = dur * CREDITS_PER_SECOND
        if spent_est + worst > a.max_credits:
            print(f"budget reached ({spent_est:,.0f}/{a.max_credits:,}); stopping, re-run later for the rest")
            break
        if start_left is not None and worst > start_left - spent_est:
            print("account credits exhausted; stopping")
            break
        audio = generate(key, s["prompt"], dur, bool(s.get("loop", d.get("loop", True))),
                         float(s.get("influence", d.get("influence", 0.4))))
        f.write_bytes(audio)
        made += 1
        left = credits_left(key)
        if start_left is not None and left is not None:
            spent_est = start_left - left      # real consumption
        else:
            spent_est += worst                 # pessimistic
        print(f"  ok {f.name}  {len(audio) / 1024:.0f} KB   spent so far ~{spent_est:,.0f} credits")
    print(f"done: {made} new clips, ~{spent_est:,.0f} credits")


if __name__ == "__main__":
    sys.exit(main())
