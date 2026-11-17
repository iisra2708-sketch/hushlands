# Hushlands

Automated production pipeline for the **Hushlands** ambience YouTube channel — new quiet places every week.

Every soundscape is synthesized from scratch (no stock loops) and paired with a seamless animated artwork loop.

- `worlds/` — one YAML per video (scene, sound layers, visuals, metadata)
- `pipeline/` — audio synth, visual loop, thumbnail and metadata generators
- `.github/workflows/render.yml` — renders a video and publishes it as a GitHub Release

Run it: **Actions → Render video → Run workflow**.
