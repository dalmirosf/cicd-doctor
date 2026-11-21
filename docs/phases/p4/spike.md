> **Original working document, written in Spanish (rioplatense) for the owner and kept untouched below this line.** It is a planning draft: where it differs from `SPEC.md`, `DECISIONS.md` or the code, those are the authority (see the phase `README.md` for the differences).

# P4 — Informe del spike (parcial: partes locales C4, C5, C7)

> Escrito el 2026-11-20 sobre `main` en `5a34428`. Solo lectura y local: no se llamó a OpenAI, no se tocó `.env`,
> no se usó la red salvo `gh run list` de este repo. Falta la parte B (repos ajenos): espera el veto de repos.

| # | Pregunta | Respuesta medida | Consecuencia |
|---|---|---|---|
| C5 | ¿Cuánto tarda el runner offline? | `rules` sobre 55 casos (lab + inyección): **1,5 s** en total con `uv run`. Las 4 variantes sobre los mismos 55 casos con caché vacía (todo cae a reglas) y fastembed cargado: **2,8 s**. tiktoken carga en 0,33 s. | El costo del eval offline es despreciable; el tiempo de `evals.yml` lo domina `uv sync` y las cachés, no el runner. Con 85 casos, `llm+hints+similar` **entra** en el gate en tiempo (D-105 puede decidir por otra razón). |
| C5b | ¿Cuánto dura el CI actual? | `ci.yml` (3 jobs en paralelo): runs de hoy entre **~2 y 2,5 min** (ej. 22:31:31 a 22:33:30). | El objetivo de < 5 min de `evals.yml` (sin Postgres) es holgado; `timeout-minutes: 8` es razonable. |
| C5c | Hallazgo | Con la caché vacía el runner imprime **una línea WARNING por llamada fallida** (cientos de líneas). | En (g) el `check` debe agrupar los fallos por falta de respuesta en una sola línea (código 3), no imprimir una por llamada. |
| C4 | ¿Cuántos tokens tiene un prompt de `diag-v3` con casos de lab? | 25 casos: prompt completo **media 2 225, máx 3 259** tokens (excerpt media 1 000, máx 2 034). El `SYSTEM_PROMPT` solo pesa 951. | D-88 midió ≈ 1 290 de entrada con `diag-v2`: `diag-v3` es ≈ 70 % más grande. Con el tope de ≈ 6 000 del excerpt, un log público grande llega a ≈ 7 500 de entrada. El costo en USD espera los precios (pregunta 8). |
| C7 | ¿La huella del prompt cambia con una palabra y es estable? | sha256(prompt + esquema estricto): `749f746a…`; con un espacio y una `x` más: `632afe5c…`; dos cálculos iguales dan lo mismo. | D-100 es viable; la sesión de (a) implementa la función. |

**Pendiente (parte B y C1 a C3, C6):** necesita repos públicos ajenos; se corre cuando confirmes la lista de candidatos.

---

# Parte B y C1 a C3 (repos ajenos, solo `GET`) — corrida del 2026-11-20

Repos: `python-poetry/poetry`, `pypa/pip`, `docker/compose`, `cli/cli`, `vitejs/vite`, `prettier/prettier` (los 6 con licencia
MIT o Apache-2.0 confirmada por la API). Consumo de la API: **238 + 28 + 6 ≈ 272 llamadas** (de 5 000/h; sobran 4 671). Los logs bajados
(6, ≈ 370 KB) se **borraron** del scratchpad; quedan solo los metadatos (`plans/p4-spike/stage1.json`, `stage2.json`).

| # | Pregunta | Respuesta medida | Consecuencia |
|---|---|---|---|
| B1 | ¿Hay suficientes runs fallados? | Sí: 92 (poetry), 153 (pip) y **300 o más** (el tope de 3 páginas) en los otros 4, en 90 días. Casi todos son `pull_request` (poetry 82/92, vite 202/300, prettier 295/300). Solo 5 a 20 % en la rama por defecto. | Alcanza para elegir; los casos serán sobre todo fallas de PR. |
| B1b | ¿Cuántos son fallas "reales de CI"? | **Muchos son workflows de compuerta o bots**, no builds: pip 107 de 153 (un workflow de checklist de PR); compose 119 y 33 de 300 (un workflow de revisión de PR y una compuerta); cli 72 y 12 (escaneo de código y triage de issues); poetry una vista previa de documentación y updates de dependabot. [Redactado al archivar: los nombres de workflows de repos ajenos no se guardan en git, ver CLAUDE.md "Rules".] | (c) necesita una **lista de exclusión de workflows** por repo en el manifiesto (la regex del spike excluyó `checklist|triage|review|preview|scanning|zizmor|disclosure|merge|publish|release|semantic|documentation|autofix|size|dependabot`). |
| B2 | ¿Cuántos pares salen? (muestra: 1 run por workflow+rama, ≤ 25 por repo, 125 en total) | Emparejados (H1, otro `head_sha`): poetry 15/25, pip 8/25 (+6 con el mismo sha), compose 13/25, vite 7/25, prettier 10/25, **cli 0/25** (sus PRs vienen de forks: `branch=` no los encuentra). Pasan el filtro mecánico (≤ 3 commits, 1 a 10 archivos, ≤ 7 días): poetry 7, pip 6, compose 1, vite 6, prettier 10. | El emparejado por `branch` **no sirve con PRs de forks** (cli/cli). Se puede cambiar a emparejar por `head_sha` de la rama del PR vía `pull_requests`/`compare`, o descartar repos de forks. Compose da pares de 10 a 18 commits (merges de ramas largas): casi todo fuera del filtro. |
| B2b | Calidad de los pares | Varios pares son "1 commit, 0 archivos, 0 días": commits vacíos o re-disparos, no arreglos. Otros son 1 commit, 1 archivo. | Filtro: **≥ 1 archivo cambiado** (ya aplicado arriba). Nada es caso hasta que lo etiquetás (D-102). |
| B3 | ¿Cuántos tienen **un solo** job fallado? | De 21 candidatos con filtro y sin compuertas: **7** tienen un único job fallado; el resto son matrices (2 a 16 jobs fallados: poetry/pip/prettier/vite). | D-101 dice "un solo job fallado": con esa regla se pierde ~2/3. **Cambio propuesto:** aceptar una matriz si todos los jobs fallados fallan en el **mismo paso** y se toma el de menor id; los de pasos distintos se descartan. |
| B4 | ¿Se bajan los logs? Tamaño | 7 intentos: **6 ok, 1 × 404** (un job que falló en `Set up job`: sin log). Tamaño del log del job: **21 a 135 KB**, mediana 56 KB; 246 a 653 líneas; 0,2 a 4 s por log. | Los logs de estos repos entran sobrados en el tope de 150 KB / 1 500 líneas; el recorte casi no hace falta. Un job que no arrancó (`Set up job`) se descarta (404 es normal). Falta ver logs de repos con builds largos (compose, pip matrix). |
| B5 | Límite de API | 4 935 → 4 699 tras la etapa 1 (238 llamadas); 4 671 al final. | El captador cabe holgado; con 10 repos serían < 1 000 llamadas. |
| B6 | Licencias | Las 6 con `license.spdx_id` MIT o Apache-2.0. | La lista permitida de D-101 sirve. |
| H2 | Re-runs verdes (candidatos a flaky) | De 200 runs verdes por repo (los más recientes): intento > 1 en poetry 3, pip 3, **compose 13**, vite 4, prettier 0, cli 0. | Hay ≈ 23 candidatos H2 por ≈ 1 000 verdes: alcanza para 3 a 6 casos `flaky`, **con** la etiqueta tuya. |
| C1 | ¿El redactor y un escáner independiente encuentran residuos? | `redact_text` encontró **0** secretos en los 6 logs (CI público de JS: esperable). El escáner independiente dio falsos positivos: `nombre@1.2.3` parece email; cadenas de 32+ caracteres con entropía alta son hashes de paquetes y rutas. **No se vio ningún email ni credencial reales.** | El escáner de (c) tiene que ser **más específico** (emails con TLD y dominio real, patrones de token por prefijo, `Authorization`), y la regla es: cero hallazgos o revisión tuya. 0 redacciones en 6 logs no prueba nada sobre repos con secretos: el corpus de redacción sigue siendo la prueba. |
| C2 | Equivalencia del recorte | Los 6 logs (≤ 653 líneas, ≤ 135 KB) caben enteros: el recorte no se ejercita con esta muestra. | La regla de (c) ("excerpt idéntico o se amplía/descarta") queda como está, pero **su prueba real necesita un log grande**: se incluye un repo con builds largos o se genera un caso sintético grande en los tests. |
| C3 | ¿Qué dicen las reglas? | **6 de 6 → `unknown`**, ninguna regla dispara (todos los logs son de Node/TS: vite, prettier). | Confirma el sesgo esperado: fuera de Python/Docker/Terraform las reglas no saben nada, así que ahí se ve la brecha reglas contra LLM. La mitad "fuera de reglas" del lote sale casi gratis; la mitad "dentro de reglas" hay que buscarla en poetry y pip. |
| C4b | Tokens de los excerpts | 442 a 3 750 tokens (media ≈ 1 800; solo 2 pasan de 3 000). Con el `SYSTEM_PROMPT` (951) y las pistas, el prompt ronda **1 500 a 5 000** tokens. | Un log público típico pesa ≈ 1,2 a 2× un caso de lab. El caso extremo de 6 000 tokens de excerpt no apareció. Costo en USD: espera los precios. |

## Conclusiones del spike para el plan

1. **El lote de 30 es viable**, pero el rendimiento mecánico es bajo (≈ 5 % de los emparejados salen con un solo job fallado, ≈ 15 %
   con la regla de matriz). Con 300 fallas por repo (el tope de lo que leí) y 5 a 6 repos salen cientos de candidatos: hace falta
   `discover` con **lista de exclusión de workflows** y la regla de matriz. Estimo ≈ 80 a 120 candidatos mecánicos para llegar a 30 buenos.
2. **Cambios al plan (a incorporar en p4.md v1.1):** (i) regla de matriz en D-101; (ii) lista de exclusión de workflows por repo en el
   manifiesto; (iii) emparejado por rama no sirve con PRs de forks: `cli/cli` se descarta como fuente (o se empareja por `compare` del PR);
   (iv) el escáner independiente de (c) se afina (los falsos positivos de este spike son la prueba de que uno ingenuo no sirve);
   (v) el CI de evals no necesita recortar nada con estos repos, pero (c) debe probar el recorte con un log grande sintético.
3. **Mezcla de ecosistemas:** las reglas dan `unknown` en Node/TS, así que prettier y vite aportan casos "fuera de reglas";
   poetry y pip aportan "Python dentro de reglas"; compose (Go/Docker) tiene pares difíciles (ramas largas). Reco: poetry, pip, vite, prettier + 1 a 2 más a
   elegir por el mismo criterio (un repo de Terraform/Docker con CI chico).
4. **Nada de esto se verificó con etiquetas**: cuántos de los pares mecánicos son arreglos reales lo dice solo tu revisión (D-102).

**Lo que el spike no pudo medir:** logs de builds grandes; la tasa real de "arreglo verdadero" entre los pares; la distribución por categoría de una
muestra etiquetada; el costo en USD (faltan los precios); matrices (se midió solo el recuento de jobs, no se bajó ningún log de matriz).
