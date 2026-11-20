# Hushlands — instrucciones para Claude

Canal de YouTube automatizado de paisajes sonoros para dormir/concentrarse ("cada vídeo es un lugar").
Propietario: Israel (no programador; quiere intervenir lo mínimo). Responder siempre en español.

## Al empezar cada sesión
1. Leer el doc del Proyecto Claude `claude/00-ESTADO-Y-DECISIONES.md` (fuente de verdad del estado).
2. Hacer el trabajo pendiente que indique ese doc.
3. Al terminar: actualizar ese doc (decisiones, pendiente, KPIs) y hacer commit+push aquí.

## Cómo funciona el pipeline
- `worlds/<id>.yaml`: definición de cada vídeo (título, hook, sonidos, capas de audio, efectos visuales, prompt de imagen, `order`).
- `assets/images/<id>.jpg`: arte de fondo 16:9. **Sin imagen, el mundo no se renderiza** (el cron lo salta).
- `pipeline/audio.py`: sintetizador procedural (rain/wind/fire/thunder/waves/brook/drone/train/ping/noise + s3: bells/clock/chimes/whistle/steps/sleigh/pad). Sonido 100 % propio. Nunca melodías reconocibles (villancicos incluidos).
- `sounds/library.yaml` + `pipeline/gen_sounds.py` + `.github/workflows/sounds.yml` (manual): genera clips con **ElevenLabs Sound Effects** (secreto de repo `ELEVENLABS_API_KEY`, plan Starter con licencia comercial) en `assets/sounds/<id>-NN.mp3`. Tras la prueba de escucha del 20 nov, el sintetizador de lluvia/fuego/reloj NO convence (suena a ruido blanco / palomitas / sala vacía); los clips de ElevenLabs sí.
- `pipeline/audio_samples.py`: capa `sample` = ambiente sin repetición a partir de un pool de clips (segmentos aleatorios + ±3 % tono + crossfade 4 s). Uso: `{type: sample, pool: rain-tin, level: 0.12, fallback: {type: rain, surface: tin, intensity: 0.7}}`. Sin clips en el pool usa el `fallback` sintético. `level` = rms ponderado K (lluvia sintética ~0.2, fuego ~0.09, viento ~0.05).
- `pipeline/visual.py`: bucle animado sin costuras (lluvia, nieve con `region` opcional, brillos con `cycles`, `twinkle` de luces) + `-stream_loop` para alargar a horas; miniatura.
- `pipeline/meta.py`: título ≤100, descripción con capítulos, tags.
- `.github/workflows/render.yml`: manual (workflow_dispatch) o cron cada 3 h (siguiente mundo por `order`) → publica en **Releases** (`<id>-YYYYMMDD`): mp4 + miniatura + metadata.json.
- Prueba local: `python -m pipeline.render --world <id> --minutes 1 --out /tmp/out --allow-placeholder`.

## Reglas
- Variedad real entre vídeos (política "inauthentic content" de YouTube): cambiar escena, mezcla y efectos; nada de clones.
- Archivo final < 1,9 GB (límite de assets en Releases); render.py ya limita el bitrate.
- Al subir a YouTube: marcar "contenido alterado o sintético" = sí, "hecho para niños" = no.
- Nunca usar audio o imágenes de terceros con copyright (los clips de ElevenLabs con plan de pago son propios/royalty-free; el plan gratuito NO tiene licencia comercial: no usar su salida en vídeos publicados).
- Presupuesto de créditos ElevenLabs: 30 k/mes (Starter). Usar `--max-credits` y no regenerar lo que ya existe.
- Las subidas por API quedan bloqueadas en privado sin auditoría de Google → subir vía YouTube Studio (Chrome).
