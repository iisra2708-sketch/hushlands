# Previews A/B de la sesión 9 (23 nov) — migración T3/T4 a clips

Mezclas enviadas a Israel para escuchar. Cuando elija A o B, copiar `layers` y `target_rms_db`
de la elegida al `worlds/<id>.yaml` correspondiente (y ajustar `sounds:` si cambia lo que se oye).

| fichero | mundo |
|---|---|
| market | christmas-market-night |
| santa | santa-workshop |
| sleigh | sleigh-ride-forest |
| train | christmas-train |
| kitchen | christmas-cottage-kitchen |
| aurora | northern-lights |
| tent | forest-tent |
| nye | new-years-eve-city |
| cliff | cliff-ocean (ahora con 3 clips `waves-cliff`) |
| stream | mountain-stream |
| ryokan | ryokan-rain |

Submarino, nave y observatorio siguen sintéticos (`target_rms_db: -33` ya en sus yaml).
`prev.py <mix.yaml> <nombre> [segundos] [semilla]` renderiza un preview (ruta absoluta al yaml).
