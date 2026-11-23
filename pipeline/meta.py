"""Title, description (with chapters), tags for a world."""
from __future__ import annotations


def _ts(sec: int) -> str:
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def build(world: dict, hours: float) -> dict:
    h = int(hours) if float(hours).is_integer() else hours
    title = f"{world['title']} | {world['primary']} for Deep Sleep | {h} Hours"
    if len(title) > 100:
        title = f"{world['title']} | {world['primary']} | {h} Hours"
    title = title[:100]

    names = world.get("chapters") or []
    total = int(hours * 3600)
    step = 3600 if hours >= 2 else 900
    chapters = []
    for i, s in enumerate(range(0, total, step)):
        label = names[i] if i < len(names) else (f"Hour {i + 1}" if step == 3600 else f"Part {i + 1}")
        chapters.append(f"{_ts(s)} {label}")
    if len(chapters) < 3:  # YouTube needs >= 3 chapters
        chapters = [f"{_ts(int(total * k / 3))} Part {k + 1}" for k in range(3)]

    sounds = "\n".join(f"• {s}" for s in world["sounds"])
    tags = world.get("tags", []) + [
        "sleep sounds", "ambience", "relaxing sounds", "white noise", "sleep aid",
        "study ambience", "insomnia", "deep sleep", "hushlands",
    ]
    hashtags = " ".join("#" + t for t in world.get("hashtags", ["sleepsounds", "ambience", "relaxing"]))
    desc = f"""{world['hook']}

🌙 What you'll hear
{sounds}

Use it for deep sleep, insomnia relief, studying, reading, meditation, focus or masking tinnitus. Turn the volume down low, close your eyes, and let this place hold you for the next {h} hours.

Every Hushlands soundscape is designed layer by layer from AI-generated and synthesized sounds — no recycled stock loops. Artwork is AI-assisted.

⏱ Chapters
{chr(10).join(chapters)}

🔔 Subscribe to Hushlands — new quiet places every week.

{hashtags}"""
    return {
        "title": title,
        "description": desc,
        "tags": list(dict.fromkeys(tags))[:30],
        "category": "Music" if world.get("category") == "music" else "People & Blogs",
        "made_for_kids": False,
        "altered_content": True,
    }
