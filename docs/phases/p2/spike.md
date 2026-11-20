> **Original working document, written in Spanish (rioplatense) for the owner and kept untouched below this line.** It is a planning draft: where it differs from `SPEC.md`, `DECISIONS.md` or the code, those are the authority (see the phase `README.md` for the differences).

# P2 — Resultado del spike local de solo lectura

> Sin commitear. Ejecutado el 2026-11-17 (partes A, B, C y D, aprobadas por vos). Los scripts viven en el
> scratchpad, fuera del repo. Fixtures y respuestas sanitizadas, para que las revises, en `plans/p2-spike/`
> (`embeddings/lab_signatures.json`, `embeddings/pairs.json`, `github/*.json`, 5 archivos). **No entran en
> ningún PR hasta que los revises.**

## Qué se hizo, y qué no

- **Parte A** (Windows 11, CPython 3.13.7): entorno descartable con `uv run --no-project --with fastembed`
  (fastembed 0.9.1, onnxruntime 1.32.0); no toca `pyproject.toml` ni `uv.lock`. Las 25 firmas del lab salieron de
  `analyze_jobs` (el pipeline del repo, sin modificarlo) y 11 variantes se fabricaron editando el log crudo del lab
  y volviendo a pasar por el mismo pipeline.
- **Parte B** (Linux): `docker run --rm python:3.13-slim` (CPython 3.13.18, 16 CPU del contenedor), con el script
  montado de solo lectura. También con `--memory 1g` y `--memory 512m`.
- **Parte C**: un contenedor `pgvector/pgvector:pg17` descartable en `127.0.0.1:55432` (contraseña aleatoria de la
  corrida, borrada al terminar), ya parado y borrado (`--rm`). **No toqué tu contenedor de Compose** ni ninguna base
  tuya: por eso **no leí la versión de pgvector de tu imagen local**; la que midió es la que baja hoy la etiqueta
  `pg17` (lo que verá CI).
- **Parte D**: solo `gh api -X GET` con tu sesión de `gh`, sobre el lab y este repo (108 runs). Nada con el JWT de
  la app ni con `.env`.
- Nada de esto escribió en GitHub, ni en el repo, ni leyó `.env`, ni imprimió un secreto. Una sola cosa de más:
  `docker manifest inspect` (lectura del registro) para ver si existen etiquetas fijas de pgvector.
- **No se pudo medir:** el `head_branch` nulo (ninguno de los 108 runs lo tiene), un éxito en el intento 2 de un
  run que había fallado (el lab no tiene ninguno), RDS, y fastembed bajo concurrencia con logs reales.

## Lo que cambia respecto del plan (resumen)

| # | Hallazgo | Efecto en el plan |
|---|---|---|
| 1 | La descarga en frío del modelo tardó **12 s en una corrida y 162–166 s en las otras tres**; la carga con el modelo ya en disco, **0,1 s** (el "7,5 s" de SPEC §14 era casi todo descarga) | **D-78 cambia:** el calentamiento va en **segundo plano**, no bloquea los consumidores; mientras el modelo no está, `embed` falla rápido y la falla se guarda sin embedding. Ver 1 y 2 abajo |
| 2 | `SIMILAR_MIN_SCORE = 0,85` **funciona** sobre la muestra del lab: recuperación 11/11, 1 falso positivo de 253 pares | **D-80:** se mantiene 0,85; la casilla 3 no se edita |
| 3 | A 20 000 filas por repo el HNSW **sin** iterative scan pierde recall (0,84) y devuelve menos de `k`; el escaneo exacto por repo tarda 5–13 ms y tiene recall 1 | **D-79 cambia la recomendación:** exacto por repo por defecto; HNSW + `relaxed_order` queda como camino para repos enormes |
| 4 | pgvector 0,8.7 en la etiqueta `pg17` actual (≥ 0,8.0: hay iterative scans); existe la etiqueta fija `0.8.7-pg17` | **D-79:** fijar `pgvector/pgvector:0.8.7-pg17` en Compose y en CI |
| 5 | Los vectores de Windows y de Linux son **idénticos bit a bit** (diferencia máxima 0,0) con las mismas versiones | **D-86:** el redondeo de scores queda como seguro contra cambios de versión, no como necesidad |
| 6 | Orden de runs: `id` crece con `created_at` en 108 runs (0 inversiones); `run_started_at` **no** sirve (cambia con cada intento) | **D-83** confirmado: orden `(run_id, run_attempt)` |
| 7 | 69 de 71 runs de este repo son `success` | **D-83:** casi todo run genera un `record_outcome`; hay que mantenerlo barato |
| 8 | RSS del proceso con el modelo: ~210–290 MB al cargar, **430–480 MB** estable; sobrevivió con un tope de 512 MB (482 MB de RSS) | **D-78, P5:** el worker necesita ≥ 1 GB solo por el modelo; con los logs de D-71, apuntar a 2 GB |

## A y B — fastembed

| Incógnita | Respuesta |
|---|---|
| A1/B1 ¿Instala y corre con Python 3.13? | **Sí, en Windows (3.13.7) y en Linux (3.13.18, `python:3.13-slim`).** fastembed 0.9.1, onnxruntime 1.32.0, numpy 2.5.5, tokenizers 0.23.3, huggingface-hub 1.33.0. Cierra el pendiente de §14 y la última casilla de P0 (a falta de CI, que corre en `ubuntu-24.04`: se ve en el PR (a)) |
| A1 Tamaño | Modelo `Qdrant/bge-small-en-v1.5-onnx-Q`, archivo `model_optimized.onnx`: **66 465 124 bytes**, SHA-256 `51f1bd0a…49f2431` (el mismo en Windows y en Linux); la carpeta de caché pesa 67,2 MB. fastembed y sus 33 dependencias ocupan **133 MB** instalados (203 MB de ruedas en la caché de `uv` en Linux). Total aproximado para la imagen: **+200 MB** |
| A2 Carga | `import fastembed`: 0,5–0,9 s. **Con el modelo en disco, 0,08–0,12 s** (Windows y Linux). **En frío, 12,2 s (Linux, la primera vez) y 162–166 s las otras tres veces** (Windows y dos Linux): Hugging Face avisa "unauthenticated requests" y las descargas se quedan esperando en bloque ("Fetching 5 files … 02:42"). Es variación del servidor, no del código |
| A2 Memoria | RSS tras importar: 82 MB. Tras cargar: 206–208 MB (Windows), 266–290 MB (Linux). Tras 100 lotes de 3: **430 MB (Windows), 460–482 MB (Linux)**. La primera corrida de `embed.py` llegó a 804–986 MB, pero ahí cargué **tres sesiones** del modelo y lotes de 10: no es el número de una instancia. Un contenedor con `--memory 512m` y `--memory-swap 512m` terminó bien con 482 MB (sin margen) |
| A3 Latencia (mediana / p95, 200 repeticiones, Windows) | sin límite de hilos: lote de 1: **4,4 / 4,8 ms**; de 3: **14,1 / 14,9 ms**; de 10: 79 / 83 ms. `threads=2`: 9,0 / 10,7; 28,8 / 29,7; 181 / 187 ms. `threads=1`: 16,5 / 17,0; 54,7 / 55,6; 353 / 362 ms. Linux (16 CPU): lote de 1: 4,5 / 5,9 ms; de 3: 13,0 / 17,1 ms; de 10: 69 / 83 ms. **Todo lo que usa el worker (1 a 3 firmas por intento) queda muy por debajo de 200 ms**, también con 1 hilo |
| A4 Determinismo | Idéntico bit a bit: en el mismo proceso (diferencia máxima 0,0), entre procesos (0,0, con y sin `threads=2`), una firma sola contra el lote (0,0) y **Windows contra Linux (0,0; coseno mínimo 1,0)**. Norma L2 entre 0,9999999 y 1,0000001 |
| A5 Caché por defecto | `tempfile.gettempdir()/fastembed_cache`, o la variable `FASTEMBED_CACHE_PATH`, o el parámetro `cache_dir` (confirmado leyendo `fastembed/common/utils.py`). En Windows el directorio temporal se puede limpiar: hace falta un directorio fijo (D-78) |
| B4 Tiempo de instalación | No lo medí con cronómetro en Linux (la línea quedó vacía: faltaba `bc`). Con la caché de `uv` fría tardó segundos en resolver e instalar 28 paquetes (el grueso es la descarga de ~200 MB). Se mide bien en CI en (a) |

### Calibración de `SIMILAR_MIN_SCORE` (D-80)

Muestra: 23 firmas distintas del lab (25 casos, 2 pares gemelos con hash idéntico: coseno 1,0 en los dos), 11
pares **positivos** (mismo fallo, otra firma) y 253 pares **negativos** (todas las combinaciones de dos fallas
distintas del lab, 28 de la misma categoría y 225 de categorías distintas).

| Grupo | n | mín | mediana | p99 | máx |
|---|---|---|---|---|---|
| positivos | 11 | **0,905** | 0,974 | — | 0,999 |
| negativos, misma categoría, otra causa | 28 | 0,579 | 0,694 | 0,838 | **0,842** |
| negativos, otra categoría | 225 | 0,457 | 0,633 | 0,785 | **0,873** |

Positivos, de menor a mayor: `modnf-yaml` 0,905, `modnf-httpx` 0,927, `assert-other-test` 0,955, `input-other-name`
0,974, `base-other-image` 0,976, `dns-other-host` 0,977, `mypy-other-type` 0,979, `copy-other-file` 0,987,
`nodist-other-package` 0,988, `modnf-other-workflow-name` (mismo error en `pr-py-modnf` que en `py-modnf`) 0,996,
`flaky-timing-other-test` 0,999. Los negativos más altos: `tf-init ~ tf-validate` 0,873 (los dos son Terraform),
`flaky-random ~ flaky-timing` 0,842, `flaky-timing ~ py-assert` 0,836, `job-timeout ~ step-timeout` 0,826.

| Umbral | recuperación | falsos positivos | precisión |
|---|---|---|---|
| ≥ 0,75 | 1,00 | 10 | 0,52 |
| ≥ 0,80 | 1,00 | 4 | 0,73 |
| **≥ 0,85** | **1,00** | **1** (`tf-init ~ tf-validate`) | **0,92** |
| ≥ 0,88 | 1,00 | 0 | 1,00 |
| ≥ 0,90 | 1,00 | 0 | 1,00 |
| ≥ 0,92 | 0,91 | 0 | 1,00 |

- **La hipótesis del plan ("bge-small deja un piso alto entre textos no relacionados") no se cumplió:** la mediana
  de los negativos es 0,63 a 0,69. Hay un hueco entre el negativo más alto de la misma categoría (0,842) y el
  positivo más bajo (0,905).
- **Recomendación: dejar 0,85.** Es el valor de la casilla, tiene recuperación 1,00 y un único falso positivo, que
  además es defendible (dos fallas de Terraform son "parecidas"). 0,88 separaría perfecto la muestra, pero son 11
  positivos fabricados a mano editando logs reales: ajustar el umbral a 11 puntos sería sobreajustar. Se
  re-calibra con los casos públicos de P4. Sigue siendo configuración (`SIMILAR_MIN_SCORE`).
- **Límite honesto:** los positivos son los "fáciles" (cambia una palabra); el caso difícil ("el mismo error con otro
  mensaje de la herramienta") no está en la muestra.

## C — pgvector

| Incógnita | Respuesta |
|---|---|
| C1 Versión | `pgvector/pgvector:pg17` → **PostgreSQL 17.11, pgvector 0.8.7**. Existen `hnsw.iterative_scan` (por defecto `off`), `hnsw.ef_search` (40) y `hnsw.max_scan_tuples` (20 000). **Existe la etiqueta fija `0.8.7-pg17`** (comprobado en el registro) |
| C2 Datos | 110 360 filas (100 repos × 900, uno de 360 y uno de ~20 000; 20 % en clústeres de 10 casi duplicados), `vector(384)`; la tabla ocupa 175 MB |
| C3 Construcción del HNSW (parámetros por defecto) | con `maintenance_work_mem` en el valor de la imagen (**64 MB**): **42,8 s**; con **256 MB**: **12,1 s**. Tamaño del índice: **216 MB** (la tabla, 172 MB). **Insertar una fila con el índice: 0,38 ms**. Un índice vacío (como en `0001`) que crece con el tráfico no necesita ninguna reconstrucción |
| C4 Planes | Con el índice por `repository_id` presente, **un repo de 900 filas va por el índice B-tree + ordenamiento top-N (exacto): 0,2–0,9 ms**. Un repo de 20 000 filas (≈ 18 % de la tabla) va por el HNSW (2,5 ms). **Forzando el HNSW en el repo de 900 filas con `LIMIT 12` devolvió 10 filas** ("Rows Removed by Filter: 30"): el filtro posterior del `WHERE repository_id` deja al índice sin candidatos suficientes |
| C5 Recall (200 consultas por caso, top 3 con score ≥ 0,85) | **Repo de 20 000 filas:** HNSW sin iterative scan **0,848** (y 10,6 filas de 12); el plan por defecto **0,842**; `relaxed_order` con `ef_search = 40`: **0,908** (12 filas, 1,2 ms); con `ef_search = 100`: **0,935** (2,0 ms). **Repos de 360 y de 900 filas: 1,000** en todas las variantes. **Escaneo exacto del repo de 20 000 filas** (sin HNSW, B-tree + top-N): **4,7 a 12,6 ms y recall 1,0** |
| C6 ¿Qué elige el planificador? | Por tamaño del repo: chico → exacto por B-tree; grande (≥ ~18 % de la tabla) → HNSW, que es justo donde el recall cae. En producción hay un índice con `repository_id` al frente (`failures_sig_idx`), así que se cumple el mismo patrón. **Esto no está medido sobre `failures` real**: solo sobre una tabla con la forma de `failures` |
| C7 `SET LOCAL` | Dentro de `BEGIN … COMMIT`, `SET LOCAL hnsw.iterative_scan = relaxed_order` vale durante la transacción y **no se filtra** a la siguiente sentencia de la misma sesión (el `SHOW` posterior no devolvió `relaxed_order`) |

**Consecuencia para D-79 (la recomendación cambia).** A la escala de NFR-11 (≈ 900 filas por repo en 90 días) el
plan ya es exacto, y para un repo de 20 000 filas el escaneo exacto cuesta 5–13 ms con recall perfecto, mientras que
el HNSW cuesta 1–2 ms con recall de 0,84 a 0,94. Recomiendo: **búsqueda exacta por repositorio por defecto** (que
el interior de la consulta no pueda usar el HNSW, por ejemplo ordenando por `(embedding <=> q) + 0`, o con
`SET LOCAL enable_indexscan = off` solo sobre el HNSW; **ninguna de las dos formas está probada todavía**: el
test de plan de (b) las comprueba con `EXPLAIN`), y dejar el índice HNSW y los iterative scans como el camino para
un repo de más de ~50 000 filas, activable por configuración. El índice de `0001` se conserva. Esto además elimina
la dependencia de la versión de pgvector para el camino por defecto (0,8.0 sigue siendo necesario para el
alternativo).

**Consecuencias de la etiqueta:** fijar `pgvector/pgvector:0.8.7-pg17` en `docker/compose.yaml` y en `ci.yml`.
RDS no se midió: se comprueba en P5 qué versión ofrece (no verificado).

## D — GitHub (solo `GET`)

| Pregunta | Respuesta |
|---|---|
| D1 Orden de runs | 108 runs (37 del lab, 71 de este repo): **0 inversiones** de `id` contra `created_at`; **1 inversión** contra `run_started_at`, y es una re-ejecución: `run_started_at` cambia con cada intento (el intento 2 del run `43966010866` empezó a las 22:26:43, el run se creó a las 22:02:00). **`run_started_at` no sirve de clave de orden; `(run_id, run_attempt)` sí** (D-83, D-69). 108 runs no prueban el caso general |
| D2 Forma del éxito | Un run exitoso (`pull_request`, `push` o `workflow_dispatch`) trae los **mismos campos** que uno fallido: `id`, `name`, `workflow_id`, `head_branch`, `head_sha`, `head_repository`, `run_attempt`, `event`, `conclusion`, `pull_requests`, `updated_at`: el router y `_WorkflowRunEvent` ya los exigen. Los dos runs `pull_request` exitosos de este repo leídos ahora traen `pull_requests: []` (el PR ya está mergeado: no sabemos qué traía el webhook en el momento; el camino de éxito no usa esa lista, usa el PR guardado de la falla). `GET …/attempts/1` y `…/attempts/2` devuelven cada intento con su `conclusion`, y el PR con `base.repo.id` |
| D3 `head_branch` nulo | **0 de 108.** No observado (el caso de tags, `schedule` o `merge_group` no está en estos repos): la regla de D-83 ("sin rama, solo flaky") sigue siendo defensiva y se prueba con un fixture sintético |
| D4 Proporción | Este repo: **69 `success` y 2 `failure`** de 71. Lab: 4 `success`, 30 `failure`, 3 `cancelled` de 37 (es un lab hecho para fallar). En un repo normal, casi **todo run** genera un `record_outcome`; las consultas del camino sin fallas abiertas tienen que ser baratas (D-83) |
| Éxito en el intento 2 | **No hay ningún caso** en el lab (los flaky re-ejecutados siguieron fallando): se mide en vivo en (h) con `pr-flaky` |

## Fixtures preparados (para tu revisión)

- `plans/p2-spike/embeddings/lab_signatures.json`: 36 firmas (25 casos del lab + 11 variantes), con su texto (el
  que produce el pipeline, ya redactado), su vector de 384 floats redondeado a 8 decimales, el SHA-256 del `.onnx`
  y las versiones. Pasa a `tests/fixtures/embeddings/` en (a).
- `plans/p2-spike/embeddings/pairs.json`: 264 pares (11 `same`, 253 `different`, estos con `same_category`), con el
  coseno de Windows y el de Linux (idénticos).
- `plans/p2-spike/github/` (5 archivos, sanitizados): `workflow_run_success_{pull_request,push,dispatch}.json` y
  `workflow_run_failure_pr_attempt{1,2}.json`. Dueño → `acme`, ids, SHAs y `node_id` reemplazados de forma
  consistente, emails y nombres de autor reemplazados, avatares sin dominio real. Escaneo posterior: 0 coincidencias
  de login, de email, de SHA ni de id de los originales, y 0 `ghp_`/`github_pat_`/PEM. Pasan a `tests/fixtures/github/` en
  (e2), solo los que apruebes.

## Qué queda por medir (y dónde)

- fastembed bajo `WORKER_CONCURRENCY = 4` con logs reales (un test corto en (a)).
- El plan y el recall sobre `failures` real con el índice de producción (el test de plan de (b)).
- La versión de pgvector de tu Compose local (no la toqué): `docker compose pull` + `SELECT extversion …` cuando
  fijes la etiqueta.
- Un éxito en el intento 2 y un `head_branch` nulo reales: (h).
- El tiempo de `uv sync` con fastembed en CI y que `actions/cache` evite la descarga: (a).
