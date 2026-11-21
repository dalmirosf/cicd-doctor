> **Original working document, written in Spanish (rioplatense) for the owner and kept untouched below this line.** It is a planning draft: where it differs from `SPEC.md`, `DECISIONS.md` or the code, those are the authority (see the phase `README.md` for the differences).

# P4 — Evals a escala: plan (v1)

> Solo plan, sin código. No commiteado. Escrito el 2026-11-20 sobre `main` en `5a34428`.
> Base: SPEC §1, §9.1, §9.2, §11, §12, §13 (P4 y P5), §14, FR-22, NFR-03, NFR-07, y DECISIONS D-07, D-14, D-15, D-33,
> D-59, D-61 a D-66, D-73, D-86, D-88, D-99. Modelo de formato: `plans/p3.md`.
> Los números D-100 a D-110 son provisionales: se asignan en el orden de merge.
> Nada de esto empieza hasta que apruebes el plan; el spike (§1) espera tu "andá", parte por parte.

## Estado (2026-11-20, v1.1)

- **Respuestas del dueño:** "tu recomendación" a las 13 preguntas de §7; spike aprobado. Precios de los modelos: pendientes.
- **Sesiones lanzadas:** (a) `p4-answers-cache` (PR #47 abierto) y (e) `p4-judge` (trabajando).
- **Spike corrido (partes B y C):** informe en `plans/p4-spike.md`. **Cambios al plan por lo medido:** (i) D-101: un
  caso con matriz se acepta si todos los jobs fallados fallan en el **mismo paso** (se toma el de menor id); pasos
  distintos se descartan; (ii) el manifiesto lleva una **lista de exclusión de workflows** por repo (checklist, triage,
  review, preview, scanning, zizmor, disclosure, merge, publish, release, semantic, documentation, autofix, size,
  dependabot); (iii) el emparejado por `branch` no encuentra PRs de forks (`cli/cli` dio 0 de 25): `cli/cli` queda
  fuera; (iv) un par necesita **≥ 1 archivo cambiado** (hay pares de 0 archivos); (v) el escáner independiente de (c)
  debe ser específico (`nombre@1.2.3` y los hashes de paquetes lo disparan); (vi) el recorte se prueba con un log
  grande **sintético** (los 6 logs reales pesan ≤ 135 KB); (vii) un job que no arrancó (`Set up job`) da 404 y se
  descarta; (viii) el runner offline tarda segundos: `llm+hints+similar` entra en el gate por tiempo; (ix) con la
  caché vacía el runner imprime una línea por llamada fallida: `check` las agrupa (g). Repos para el lote: `poetry`,
  `pip`, `vite`, `prettier` + 1 o 2 con builds largos.

## Punto de partida (lo que encontré leyendo)

**Lo que ya existe y P4 solo tiene que completar:**

- El runner (`evals/runner/`): `cases.py` (carga `case.yaml` + `log.txt` + `jobs.json`, y opcionalmente
  `annotations.json` y `diff.json`), `variants.py` (`rules` y `run_llm_case`, que pasa por el `DiagnoseJob` de
  producción), `run.py`, `metrics.py` (accuracy, P/R/F1 por categoría, macro-F1, matriz de confusión, evidencia,
  abstención, tabla de calibración por confianza, latencia, costo, fallbacks, ataques ganados), `report.py`
  (JSON + Markdown), `capture.py` (solo del lab), `history.py` (la variante `llm+hints+similar`), `injection.py`,
  `markdown_threats.py`, `live.py`, `cli.py`.
- 25 casos de lab (23 workflows; `py-assert` y `py-modnf` capturados dos veces) con **todas** las categorías con ≥ 2
  casos (broken_test 4, dependency 6, network 2, config_or_secrets 5, timeout 2, infra_or_deploy 2, flaky 2,
  unknown 2) y 30 casos de inyección. O sea: **la primera casilla de §13 P4 ya se cumple en sus partes de lab e
  inyección**; solo faltan los ≥ 30 públicos.
- `CaseFile` ya acepta `source: public`, `fix: {commit, summary}` y `flaky`. `Case.run_ref()` y `load_cases` no
  distinguen fuentes: un caso público entra por el mismo camino.

**Lo que encontré y cambia el diseño (todo medido leyendo código y archivos, nada corrido):**

1. **La caché de respuestas no está commiteada y además está vencida.** `evals/.cache/` aparece sin seguimiento en
   `git status` (el `.gitignore` solo ignora `.cache/fastembed/`). Tiene 106 archivos (≈ 105 KB) y **todos** son de
   `prompt_version = diag-v2` (65 de `gpt-6-luna`, 41 de `gpt-5.4-mini`); el código hoy es `diag-v3` (P3), así que
   ninguna entrada sirve: un `evals run --variant llm+hints` offline hoy da 100 % de `llm_error`. SPEC §11.3 dice
   "CI corre desde la caché commiteada": hoy es falso en las dos cosas. (D-100)
2. **La clave de la caché no incluye el texto del prompt.** `CachedLLMClient.key` hashea `(model, prompt_version,
   request)`; el `request` es el trabajo, el excerpt, las pistas, lo parecido, el diff y el mensaje de reparación,
   pero **no** `SYSTEM_PROMPT` ni el esquema JSON. Si alguien empeora el prompt sin cambiar la etiqueta
   `PROMPT_VERSION`, el CI sigue sirviendo las respuestas viejas y **no ve nada**. La casilla "verificado commiteando
   un prompt deliberadamente malo en una rama" no se puede cumplir con la clave actual. (D-100, D-105)
3. **Las latencias y el costo offline están mal.** `latency_ms` de `run_llm_case` se mide con `perf_counter` alrededor
   de `pipeline.run`: con la caché eso mide leer un archivo (el reporte del 2026-11-18 muestra "0 / 1 ms" para
   `llm+hints`). La latencia real de la llamada está guardada en cada entrada (`LLMResult.latency_ms`) y no se usa.
   El costo sin `--price` da 0. (D-109)
4. **"Evidence validity" no se puede medir como está escrita.** §11.2 pide "share of published evidence items that
   pass verification", pero el pipeline **descarta** la evidencia que no verifica antes de publicar: lo publicado
   siempre pasa. Lo que mide algo es la evidencia **cruda** del modelo (cuántos ítems inventó). Hoy no se guarda.
   (D-109)
5. **El runner no tiene tope de gasto.** El libro mayor del eval es `InMemoryUsageLedger(calls=10**9,
   budget=10**9)`: `--live` gasta lo que haga falta. Los topes `LLM_DAILY_*` son de la app, no del eval. Hace falta
   `--max-cost-usd` y `--max-calls` en el runner (PR a). (D-108)
6. **Los casos públicos no tienen dónde guardar su procedencia.** `CaseFile` es `extra="forbid"` y no tiene licencia,
   fecha del evento, ni commit/URL de la corrección más que `fix.commit`. Además `captured_at` ordena el historial de
   `llm+hints+similar` (D-86): para casos públicos hay que ordenar por cuándo **ocurrió** la falla, no por cuándo la
   capturamos (si no, 30 casos capturados en una hora salen "en el orden en que los bajé"). (D-101)
7. **SPEC §11.1 se contradice en el diff del arreglo.** La lista de archivos dice `fix.diff` "trimmed to ≤ 200
   lines"; la línea de casos públicos dice "only log excerpts and short diff summaries" por las licencias. Además
   `load_case` ni lee `fix.diff`. (D-101)
8. **El repositorio no tiene archivo `LICENSE`.** Para un conjunto de datos con extractos de repos ajenos hay que
   dejar escrito de dónde viene cada cosa y bajo qué licencia (D-101).
9. **`evals/README.md` está viejo:** dice 16 casos de inyección (son 30), que el modelo "no se eligió" (D-88 lo
   eligió) y que "la caché empieza vacía". Se corrige en (a) y (i).
10. **`flaky` en el mundo real:** D-15 dice que una sola falla solo puede ser "sospecha de flaky". Los pares
    "falla → mismo commit en verde" son justamente los flaky candidatos; y pares "falla → commit distinto en verde"
    mezclan arreglos reales con cambios no relacionados. El plan trata las dos cosas (D-101, D-102).
11. **Costos del NFR-03 con logs reales:** el costo medido (USD 0,0003 por caso, D-88) viene de excerpts del lab de
    ≈ 1 300 tokens de entrada. Un log público llena más el presupuesto de ≈ 6 000 tokens: el costo por diagnóstico
    puede ser ~4 veces mayor (≈ USD 0,0015, estimación mía, no medida). Sigue lejos de USD 0,01, pero hay que
    medirlo, no suponerlo (D-110).
12. **El juez de causa no existe en ningún lado:** ni puerto, ni cliente, ni esquema. El `LLMClient` actual solo sabe
    `diagnose(DiagnosisRequest)`. El juez es una herramienta del eval (no del producto): vive en `evals/`, no en
    `core/`.

## 0. Resumen

| Paso | Título | Tamaño | Depende de | D-NN | Cierra en §13 P4 |
|---|---|---|---|---|---|
| 0 | Spike de solo lectura (no es PR) | M | tu aprobación | — | — |
| a | Respuestas del LLM commiteadas: clave con huella del prompt, topes de gasto, `prune` | M | spike | D-100, D-108 | (parcial) 4 |
| b | Métricas que faltan: latencia y costo reales, evidencia cruda, IC, por fuente, flaky simulado | M | a | D-107, D-109 | (parcial) 2 |
| c | Captador de casos públicos: descubrir pares, bajar, limpiar, redactar, licenciar, probar la equivalencia del recorte | L | spike | D-101 | (parcial) 1 |
| d | El lote de ≥ 30 casos públicos y sus etiquetas | M (datos) | c, **tus etiquetas** | D-102 | 1 |
| e | El juez de causa, la hoja de etiquetado y el cálculo de acuerdo | L | a | D-103, D-104 | (parcial) 3 |
| f | `--variant all` completo, reporte con juez y calibración, generador de la tabla del README | M | b, e | — | 2 |
| g | `evals check` + `baseline.json` + `evals.yml` y la regla de regresión e inyección | L | a, b, f | D-105, D-106 | (parcial) 4 |
| h | Datos en vivo: llenar las respuestas, medir la varianza, calibrar el juez, el baseline, la tabla del README y NFR-03 | S (código) | d, e, f, g, **tu dinero y tus 20 etiquetas** | D-108, D-110 | 3, 5 |
| i | Verificación del CI en vivo (rama con prompt malo) y cierre: `docs/phases/p4/`, guía de lectura | S | h | — | 4 |

**Por qué esta división (y qué cambié de tus candidatas):**

- **Caché + clave + topes** (a) va primero y solo: arregla la contradicción más grave (el CI no puede ver un prompt
  malo) y deja la herramienta de gasto controlado que usan todos los pasos en vivo. Sin dinero: es offline.
- **Métricas** (b) separado del reporte (f): (b) es puro cálculo con tests; (f) es presentación y depende del juez.
- **Captador** (c) separado del **lote** (d): (c) es código con `gh` falso, revisable sin tocar un repo ajeno; (d) es
  datos, depende de tus etiquetas y de lo que el spike muestre, y se revisa leyendo casos, no código.
- **Juez** (e) junto con su hoja de etiquetado y el cálculo del acuerdo, pero **sin correrlo en vivo**: lo que se
  mergea es el mecanismo; la corrida con tus 20 etiquetas es de (h).
- **Reporte `--variant all`** (f) después del juez para que la tabla de calibración del juez y la columna de causa
  existan.
- **Gate** (g) separado de **datos** (h): (g) se prueba con reportes sintéticos (offline, sin dinero) y deja el CI listo
  pero **apagado en la práctica** hasta que (h) cargue el baseline (el workflow nace con `workflow_dispatch` y
  `pull_request` sobre rutas del eval; ver D-105).
- **(h) y (i)** son lo que vos pedís aparte: gasto y escritura en GitHub. (i) es donde se ve el CI fallar de verdad.

**Orden de merge y paralelismo.**

```
spike ─ a ──┬── b ───────────┐
            ├── e ── f ──────┤
            │                 g ── h ── i
        c ──┴── d ───────────┘
```

| Par | ¿En paralelo? | Archivos que chocan |
|---|---|---|
| a, c y e (la parte nueva) | sí | solo `evals/runner/cli.py` (cada uno registra un subcomando en su propio hunk) |
| a y b | **no**, b espera a a | `evals/runner/run.py`, `variants.py`, `live.py` (el grabador de respuestas crudas entra en a) |
| c y d | **no**, d espera a c | d usa el captador; además d es datos puros |
| e y f | **no**, f espera a e | `evals/runner/report.py`, `metrics.py` |
| b y f | **no**, f espera a b | `metrics.py`, `report.py`, `variants.py` |
| g y f | **no**, g espera a f | `report.py` (formato del JSON que lee el gate) |
| d y todo lo de código | sí | ninguno (solo `evals/cases/public/` y tests de carga) |

**Migraciones:** ninguna. No toca `src/cicd_doctor/` salvo (a), que exporta una huella del prompt desde `core/diagnosis/prompt.py`
(una función pura, sin cambiar el prompt ni `PROMPT_VERSION`).

**Reglas de trabajo para todos los PRs** (las de P1c a P3): una rama por cambio cortada de `origin/main`, un PR propio;
CI verde (lint, tipos, tests con Postgres, cobertura de `core/` ≥ 90 %); D-NN, SPEC y `CLAUDE.md` en el **mismo** PR;
commits en inglés y sin `Co-Authored-By`; nada de secretos en código ni en fixtures (los únicos falsos viven en
`tests/redaction_corpus.py`); `plans/` y `.env` nunca se commitean; ninguna casilla de §13 se marca sin evidencia
observada; cada PR lo verifica una sesión independiente antes de abrirse. **Nuevo en P4:** un log público es entrada
no confiable y además **ajena** (licencia, datos personales, texto adversario): va a `CLAUDE.md` "Rules".

---

## 1. Paso 0 — Spike de solo lectura (2 a 3 h, no es PR)

**Objetivo:** medir lo que no se deduce leyendo: cuántos pares falla→arreglo se encuentran de verdad, cuántos logs
se pueden bajar y cuánto pesan, qué hace el redactor con logs ajenos, si el recorte del log es equivalente, cuánto tarda
el runner offline y cuánto costaría el juez. **No se corre hasta que lo apruebes** (cada parte por separado).

**Qué es "solo lectura" acá:** no escribe en este repositorio (artefactos al scratchpad y a `plans/p4-spike/`, sin
commitear), no escribe en GitHub (solo `GET` con tu sesión de `gh`, sobre repos públicos ajenos), no abre `.env`, no
usa ninguna clave de OpenAI (los costos se **estiman** contando tokens con tiktoken, no se llama a nadie), no toca la
base. Los logs ajenos bajan al scratchpad, se miran con herramientas locales y se **borran** al terminar la parte;
solo quedan números, ids de runs y, como máximo, dos extractos cortos ya redactados para probar el redactor.

### Parte A — Documentación (lectura de páginas públicas, sin credenciales)

| # | Qué se confirma | Para decidir |
|---|---|---|
| A1 | Retención de logs de Actions (por defecto 90 días en repos públicos; configurable por repo) y qué responde la API vencido (404 o 410) | D-101: ventana de captura y qué pasa con un log vencido |
| A2 | Límite de la API REST con `gh` (5 000/h autenticado), cabeceras `x-ratelimit-*`, y el límite secundario de contenido | presupuesto de llamadas del captador |
| A3 | `GET /actions/runs` (filtros `status`, `branch`, `created`, `workflow_id`, tope de 1 000 con filtros), `GET /compare/{base}...{head}` (qué devuelve con muchos commits y con archivos truncados) | D-101: emparejado |

(No las leí todavía: lo que sé de memoria queda marcado como "a contrastar".)

### Parte B — Lecturas con `gh api` (solo `GET`), sobre 3 a 5 repos públicos

**Cómo se eligen los repos de la muestra** (los elijo yo con estos criterios y te los muestro antes de tocar nada; vos
vetás): licencia permisiva declarada (MIT, Apache-2.0, BSD, ISC, MPL-2.0), CI con GitHub Actions y fallas frecuentes
(≥ 30 runs fallidos en 90 días), logs que no sean gigantes, mezcla de ecosistemas (al menos uno de los que las reglas
conocen —Python, Docker, Terraform— y al menos uno que no —Node/TS, Go o Rust—), y repos "grandes" para tener variedad
pero **sin** ser un proyecto de seguridad (sus logs traen más datos sensibles).

| # | Llamada | Para decidir |
|---|---|---|
| B1 | Por repo: `GET …/actions/runs?status=failure&per_page=100` (hasta 3 páginas), `created` en los últimos 90 días; contar por workflow y evento | cuántos candidatos hay y de qué clase |
| B2 | **Emparejado**: para cada run fallado, el primer run `success` del **mismo workflow y la misma rama** que viene después (H1); y, aparte, el mismo `head_sha` con intento posterior verde (H2: candidato a flaky). Para H1: `GET …/compare/{sha_falla}...{sha_verde}` (cuántos commits, cuántos archivos, cuántas líneas) | D-101: cuántos pares razonables salen, con qué filtros (≤ 3 commits, ≤ 10 archivos, ≤ 7 días, otro `head_sha`) y cuántos son ruido |
| B3 | `GET …/actions/runs/{id}/attempts/{n}/jobs` de los candidatos (qué paso falló, si hay matriz, si hay varios jobs fallados) | `job_id` del caso, pasos |
| B4 | `GET …/actions/jobs/{id}/logs` de ≤ 60 candidatos (con `--allow-escape-sequences`, como el captador de P1b): cuántos se bajan (200) y cuántos no (404/410), el **tamaño** en bytes de cada log y de su paso fallado, y cuántas llamadas y cuánto tiempo consume | D-101: tamaño máximo por caso, política de logs vencidos |
| B5 | Cabecera `x-ratelimit-remaining` antes y después de B1 a B4 | presupuesto del captador (se espera < 600 llamadas) |
| B6 | `GET /repos/{o}/{r}` (`license.spdx_id`) de cada repo | D-101: la lista permitida de licencias |

### Parte C — Local, sin red (Python descartable en el scratchpad, `-I`)

| # | Qué mide | Para decidir |
|---|---|---|
| C1 | Pasar cada log bajado por `strip_terminal_controls` + `redact_text` y contar redacciones por clase. Un segundo escáner **independiente** del redactor (patrones propios: emails, IPs, tokens con entropía, `Authorization`, URLs con credenciales) sobre el resultado: cuántos residuos hay | D-101: ¿alcanza el redactor de producción para logs ajenos o hace falta una pasada extra en la captura (emails, logins)? |
| C2 | **Equivalencia del recorte:** para cada log, el excerpt (`analyze_jobs`) del log completo contra el excerpt del log recortado al paso fallado ± contexto (varias reglas de recorte: solo el paso, paso + 200 líneas previas, cola de N líneas). Cuántas veces son idénticos, y el tamaño en líneas y KB de lo recortado | D-101: qué se guarda de cada log para que el caso mida lo mismo que la app y pese poco |
| C3 | Qué dicen las **reglas** sobre los logs bajados: categoría, `unknown`, cuántos caen en cada categoría (para saber el sesgo de la muestra: ¿cuántos `dependency`, cuántos `timeout`, cuántos sin regla?) | D-102: cuotas por categoría y qué esperar de `rules` |
| C4 | Tokens del excerpt de cada log y tamaño del prompt completo (contador real de tiktoken) → **costo estimado** por diagnóstico con los precios que me des (§7), para `gpt-6-luna` y los candidatos de juez | D-110 (NFR-03) y D-108 (presupuesto) |
| C5 | Tiempo del runner **offline** sobre los 25 casos de lab: `rules`, y las variantes LLM con un `FakeLLM` (la caché real está vencida), con y sin carga de tiktoken y de fastembed; más el tiempo de `uv sync --locked` y de la restauración de cachés leído de los últimos runs de `ci.yml` (`gh run list/view`, solo lectura) | D-105: el presupuesto de < 5 minutos de `evals.yml`; ¿entra `llm+hints+similar` en el gate? |
| C6 | Cuántas entradas ocupa un caso público típico en disco (log recortado + `case.yaml` + `jobs.json`) y cuánto crecen las respuestas por caso y variante | D-100: tamaño del repositorio |
| C7 | Prueba **de papel** de la clave nueva: huella del `SYSTEM_PROMPT` y del esquema (sha256 del texto exacto) y confirmación de que cambiar una palabra del prompt cambia la clave y que dos corridas dan la misma | D-100 |

### Parte E — Contigo (nada se escribe)

| # | Qué | Para decidir |
|---|---|---|
| E1 | Los **precios** vigentes (USD por millón de tokens, entrada y salida) de `gpt-6-luna`, del candidato de juez grande y de `gpt-5.4-mini` (la página de precios; D-88 dejó "a confirmar") | C4, D-108, D-110 |
| E2 | Tu veto o preferencias sobre los repos de la muestra y de los ecosistemas | B |

### Qué se guarda del spike (sin commitear, para tu revisión)

- `plans/p4-spike.md`: el informe con la tabla "pregunta → respuesta → consecuencia", los números de B y C y lo que
  el spike no pudo medir.
- `plans/p4-spike/`: **solo metadatos**: la tabla de candidatos (repo, run id, intento, sha de falla y de arreglo,
  commits, archivos, líneas, tamaño del log, si bajó), los números de ritmo de la API, la tabla de tamaños y de tokens,
  y como máximo **dos** extractos de ≤ 30 líneas ya redactados de repos de licencia permisiva (para probar el
  redactor en los tests de (c)). Ningún log completo ajeno se guarda; los bajados se borran al terminar B/C.
- Cada fixture se escanea (sin tokens, claves, emails ni URLs firmadas) y lo revisás vos antes de que entre al repo.

**Lo que el spike no puede contestar:** cuánto da el modelo real sobre estos logs (varía; es de (h)); la calidad de las
etiquetas (la juzgás vos); los logs que ya no existan el día del lote (se bajan en (d), no antes); el acuerdo del juez.

---

## 2. PRs

### (a) Respuestas del LLM commiteadas, clave con huella, topes — `p4-answers-cache`

**Alcance**
- **Mover y commitear la caché** (D-100): de `evals/.cache/` a `evals/answers/` (no es una caché que se pueda borrar:
  es la evidencia con la que corre el CI), con subcarpetas de dos letras (`ab/ab12….json`) para que git no tenga 1 000
  archivos en un directorio. Se **empieza vacía**: las 106 entradas `diag-v2` no sirven y no se commitean (se pueden
  conservar fuera del repo para la comparación histórica de D-88).
- **Clave con huella del prompt:** `core/diagnosis/prompt.py` exporta `prompt_fingerprint()` (sha256 de `SYSTEM_PROMPT`
  más el esquema estricto de `schema.py`, serializados de forma canónica). `CachedLLMClient.key` suma ese valor, además
  de `prompt_version`. **Consecuencia:** cambiar una palabra del prompt o del esquema es un fallo de caché aunque nadie
  suba la etiqueta (el CI lo detecta; ver D-105).
- **Grabador de lo crudo:** un envoltorio `RecordingLLM` (en `evals/`, no en `core/`) que guarda, por caso, el
  `LLMResult` de la llamada aceptada (JSON crudo, latencia real, tokens, modelo) y cuántas llamadas hubo. Lo consume (b).
- **Topes de gasto del runner:** `--max-cost-usd` y `--max-calls` (obligatorios con `--live`, con valor por defecto
  chico: USD 0,25 y 150 llamadas). Se calcula con los precios dados; si se supera, el runner **frena** (no solo avisa),
  guarda lo hecho y sale con código distinto. También `--no-cache-read` para medir varianza (siempre llama, escribe
  con sufijo de corrida y no pisa la entrada oficial).
- **`python -m evals prune`**: borra las entradas de `evals/answers/` que no sirven a ningún (caso × variante × modelo)
  de la configuración actual (se corre al llenar). `--dry-run` por defecto.
- **Guarda de contenido:** un test recorre **todas** las entradas y falla si alguna (i) contiene algo con forma de
  secreto (el mismo `redact_text` con cuenta 0), (ii) trae un campo que no sea `model`, `prompt_version`, `fingerprint`,
  `result`, (iii) pesa más de 16 KB, (iv) no valida como `LLMResult`. Las respuestas no traen el request (D-65), solo
  citas de líneas del excerpt (≤ 300 caracteres, ≤ 8 por respuesta).
- `.gitattributes`: `evals/answers/** text eol=lf linguist-generated`. `.gitignore`: se deja de ignorar lo que hubiera.
- `evals/README.md` corregido (30 casos de inyección, modelo elegido, dónde vive la caché).

**Archivos:** `src/cicd_doctor/core/diagnosis/prompt.py` (una función), `adapters/llm/cache.py`, `evals/runner/{live,run,cli}.py`,
`evals/runner/recording.py` (nuevo), `evals/README.md`, `.gitattributes`, `tests/`.

**Tests (todos offline, rápidos):** la huella cambia con una palabra del prompt y con un campo del esquema y no cambia
entre corridas; la clave cambia con la huella; la lectura de una entrada vieja sin huella es un fallo; `--max-cost-usd`
frena con un `FakeLLM` que "cuesta" y deja escrito lo hecho; `--no-cache-read` no pisa la entrada oficial; `prune`
borra solo lo huérfano; el escáner de entradas (con una entrada a propósito que contiene un secreto del corpus de
redacción: debe fallar). Nada necesita Postgres ni red.

**Terminado cuando:** un `evals run` offline contra una carpeta vacía da `llm_error` en todo y lo dice claramente;
la clave cambia con el prompt (test); los topes frenan una corrida simulada; D-100 y D-108 escritos; SPEC §11.3 y
`CLAUDE.md` alineados.

**§13:** ninguna casilla sola (prepara 4).

### (b) Métricas que faltan — `p4-metrics`

**Alcance** (todo en `evals/runner/`, sin tocar `core/`):
- **Latencia real** de la llamada al modelo y del pipeline: se toma de `LLMResult.latency_ms` guardado (p50/p95 de la
  llamada) y se deja el `perf_counter` solo para el "pipeline entero" (excerpt + reglas + llamada), con una nota de que
  una corrida cacheada mide la latencia **registrada** de la corrida viva (D-109).
- **Costo con precios siempre:** el costo se calcula de los tokens guardados y de los precios del baseline/CLI; el
  promedio de NFR-03 es **por diagnóstico con LLM** (`source == llm`, no cuenta caché ni respaldo), más p95 y total.
- **Evidencia:** (i) `evidence_validity` = ítems de evidencia **crudos** del modelo que `verified_evidence` acepta /
  ítems crudos (usa la función de `core`, como la app); (ii) `evidence_recall` (lo que ya había: líneas esperadas
  citadas); (iii) casos con respuestas sin una sola evidencia válida.
- **Intervalos:** IC 95 % por bootstrap con semilla fija (1 000 remuestreos por caso) de accuracy y macro-F1, y
  **por fuente** (`lab`, `public`, `injection` por separado y las dos primeras juntas). El IC es informativo, no
  decide el gate (D-105).
- **Calibración:** a la tabla alta/media/baja se le suma una tabla por décimas con ≥ 5 casos, el error de
  calibración esperado (ECE) y la tasa de acierto de las respuestas de confianza alta (lo que §11.2 llama
  "accuracy of high-confidence answers").
- **Abstención:** se deja la métrica de hoy y se separa "dijo `unknown`" de "confianza baja".
- **Flaky (D-107):** `evals/runner/flaky.py` genera secuencias de eventos con semilla y las clasifica con el estado de
  `core/outcomes.py`; el **oráculo** que decide la respuesta correcta es **otra** implementación (una tabla de
  escenarios con la etiqueta escrita a mano por construcción), porque el oráculo no puede llamar al código que juzga
  (regla de la suite de inyección). Métrica: precisión y recall de `resolved_flaky` (y exactitud de
  `resolved_by_commit`) sobre N secuencias, más 2 secuencias **reales** del lab de P2 (fixtures: la del `pr-flaky`
  y la del arreglo, ids sanitizados).
- `CaseResult` suma `answer_raw_items`, `answer_valid_items`, `llm_latency_ms`, `occurred_at`; `Metrics` suma los
  campos de arriba. Compatible hacia atrás para el JSON (`schema_version: 2`).

**Tests (offline):** cada métrica contra resultados armados a mano (incluida una con 0 casos, una con una sola
categoría y una con confianzas en el borde 0,45 y 0,75); el bootstrap con semilla es determinista; las secuencias
simuladas con un caso por escenario y una propiedad (para toda secuencia generada, el oráculo y el estado coinciden);
mutación manual: cambiar una condición de `decide` hace fallar la métrica de flaky (se anota como en D-99).

**Terminado cuando:** el reporte del lab con respuestas de `FakeLLM` muestra todas las métricas de §11.2 salvo causa;
`mypy --strict` y `ruff` limpios; D-107 y D-109 escritos.

**§13:** parcial de 2.

### (c) Captador de casos públicos — `p4-public-capture`

**Alcance** (D-101, solo `gh` con `GET`, igual que `capture.py`):
- `python -m evals discover --repo <o/r> [--since 90d] [--limit N]`: lista candidatos y los **empareja** (H1: primer
  run verde del mismo workflow y la misma rama con otro `head_sha`; H2: mismo `head_sha`, intento posterior verde,
  candidato a flaky), con filtros (≤ 3 commits entre ambos, ≤ 10 archivos, ≤ 7 días; un solo job fallado; matriz
  descartada si hay > 1 job fallado distinto), y escribe `candidates.yaml` (repo, licencia SPDX, run ids, shas,
  tamaños, motivo de descarte). Solo lee.
- `python -m evals capture-public --manifest evals/public-sources.yaml`: para cada entrada del manifiesto
  (**la única fuente de verdad de qué se captura**: repo, run, intento, job, sha del arreglo, etiquetas, quién
  etiquetó), baja el log, jobs y metadatos del arreglo y escribe `evals/cases/public/<id>/`:
  `case.yaml` (con `provenance`), `log.txt` (limpio, redactado, **recortado** según D-101), `jobs.json` (recortado a los
  campos que lee el pipeline), `fix.json` (solo el resumen, los archivos tocados con +/− y el sha; sin texto de código).
- **Limpieza por capas** (orden fijo, con tests de cada una): quitar secuencias de escape (D-62) → recortar → redactar
  con `redact_text` (D-59) → **pasada extra de captura** (emails y logins de usuarios → `[REDACTED:email]` / `[user]`,
  rutas del home del runner no, que son ruido) → **escáner independiente** que debe dar 0 hallazgos o el caso **no se
  escribe** y queda en la lista de descartes con el motivo.
- **Prueba de equivalencia del recorte** (D-101): el captador compara el excerpt del log completo con el del log
  recortado; si difieren, el caso se descarta o se guarda con una regla de recorte más ancha. Evita medir una app que
  no existe.
- **Registros:** `evals/cases/public/SOURCES.md` (repo, SPDX, URL del run, fecha, autor de la etiqueta) generado, y un
  test que falla si un caso no está en SOURCES o su licencia no está en la lista permitida.
- `CaseFile`: campos nuevos opcionales (`provenance: {repo, license, run_url, fix_sha, fix_run_url, occurred_at,
  pairing, labeled_by, label_basis}`) con `extra="forbid"` en el sub-modelo; `Case.run_ref()` usa el repo real; el
  historial de `llm+hints+similar` ordena por `occurred_at` si existe, si no por `captured_at` (D-86 queda igual para
  el lab).
- **Límites duros:** log guardado ≤ 150 KB y ≤ 1 500 líneas (regla de D-101 según el spike); caso con > 1 job fallado
  se descarta; `fix.json` ≤ 2 KB; tiempo de espera por llamada de `gh`; tope de llamadas por corrida
  (`--max-api-calls`, por defecto 400) leyendo `x-ratelimit-remaining` y frenando en 200.

**Tests (offline, `gh` falso inyectable como en `tests/unit/test_capture.py`; ninguno necesita red ni Postgres):**
descubrimiento y emparejado con respuestas armadas (una rama con tres fallas y un verde, un verde con el mismo sha, un
verde de otra rama, un run cancelado en el medio, un verde 30 días después); un log con un secreto de cada clase del
corpus que **no** está entre los patrones (el escáner independiente lo atrapa y el caso no se escribe); un log con
un email y con un login; un log con texto de inyección (se guarda tal cual: es dato, nunca se ejecuta ni se
imprime); recorte equivalente y no equivalente; un 404/410 del log (descarte con motivo, sin romper el lote);
un caso sin licencia permitida (se rechaza antes de bajar el log); la salida nunca imprime texto de logs.

**Terminado cuando:** con el `gh` falso, `discover` y `capture-public` producen casos que `load_cases` carga y que
pasan por `rules` y por un `FakeLLM`; D-101 escrito; SPEC §11.1 y `CLAUDE.md` alineados (`fix.diff` → `fix.json`).

**§13:** parcial de 1.

### (d) El lote de ≥ 30 casos públicos — `p4-public-cases`

**Alcance:** datos. Con el spike aprobado y (c) mergeado:
1. Yo corro `discover` sobre los repos acordados y te dejo una **hoja de candidatos** (≈ 60 a 80 para llegar a 30
   buenos): repo, qué falló (nombre del paso), tamaño, resumen de 1 línea del arreglo, y **nada más** (no las predicciones
   del bot ni del modelo, para no anclarte).
2. **Vos etiquetás** (D-102): categoría, causa en una oración (tuya, en inglés, ≤ 200 caracteres), líneas de evidencia
   si las ves, `flaky` sí/no, y "descartar" si el par no es un arreglo real. Hoja y guía en el Apéndice B.
3. Yo corro `capture-public` con el manifiesto que sale de tus etiquetas, reviso los descartes del escáner y armo
   `SOURCES.md`.
4. Reparto objetivo: **≥ 30 casos**, de los cuales ≥ 3 por categoría en las 7 categorías que se pueden alcanzar; para
   `flaky` y `unknown` es best-effort (se anotan menos si el mundo no da, y la tabla marca "n chico"). ≈ la mitad
   dentro de lo que las reglas conocen (Python, Docker, Terraform, GHA) y ≈ la mitad fuera (Node/TS, Go, Rust),
   etiquetados con `tags` para reportarlos separados.
5. **Tests nuevos (offline):** `tests/unit/test_public_cases.py` carga todos, verifica que cada uno tenga licencia
   permitida, esté en `SOURCES.md`, tenga `expected` completo, que `log.txt` pase el escáner independiente y
   `redact_text` con cuenta 0, que pese ≤ 150 KB, que las líneas de evidencia existan en el excerpt, que las
   firmas (`signature_hash`) no se repitan salvo casos marcados como par intencional, y que `rules` los procese sin
   excepción. Los conteos duros (≥ 30, ≥ 3 por categoría) son parámetros del test, no números mágicos.

**Terminado cuando:** ≥ 30 casos públicos commiteados con etiquetas y procedencia, el escáner independiente en 0, y el
conteo por categoría y por tag publicado en el README de los casos.

**§13:** **casilla 1** (completa: lab ≥ 20 y ≥ 2 por categoría ya se cumplía, inyección 30 ≥ 10).
**Depende de vos:** de tus etiquetas. Sin ellas (c), (e), (f) y (g) avanzan igual.

### (e) El juez de causa — `p4-judge`

**Alcance** (D-103, D-104; todo en `evals/`, nada en `core/`):
- `evals/runner/judge.py`: puerto `CauseJudge` y su implementación con OpenAI: modelo **propio** (`--judge-model`,
  precios `--judge-price`, `JUDGE_*` opcional en el entorno; **no** toca `.env` sin tu OK), esquema estricto
  `{score: 0|1|2, reason: ≤ 200 caracteres}`, prompt versionado `judge-v1`, caché propia (misma carpeta
  `evals/answers/judge/`, clave `(judge model, judge prompt version + huella, texto esperado, texto de la respuesta)`).
- **El juez no ve el log, ni el excerpt, ni el diff:** solo `expected.cause`, `fix.summary` y el `probable_cause` de la
  respuesta (más `suggested_fix` como texto aparte para el criterio "partial", ver D-103). Los tres textos son **no
  confiables** (la respuesta del modelo viene de un log hostil posible): van en un bloque con nonce, sin herramientas, con
  una instrucción fija de que son datos, y la salida pasa por el esquema (cualquier otra cosa = error de juez, no
  puntaje).
- **Estadísticos:** media y distribución 0/1/2 por variante y por fuente; `cause_match_rate` (= score 2 o ≥ 1, ambos).
  Se juzga **también `rules`** (sus causas son plantillas) para la tabla de comparación.
- **Calibración** (D-104): `python -m evals calibrate sheet` arma la hoja de **20** pares (más 5 de reserva) con
  muestreo estratificado y semilla (§Apéndice B); `calibrate score` lee tu hoja llena y calcula acuerdo exacto, **kappa
  ponderado cuadrático**, matriz de confusión humano × juez y las discrepancias graves (0 contra 2); `calibrate
  report` escribe `evals/calibration/judge-report.{json,md}`. Tus etiquetas viven en
  `evals/calibration/judge-labels.yaml` (commiteado: son tuyas, no ajenas).
- **Inyección contra el juez:** 4 casos nuevos de la suite (respuesta de modelo que dice "Score: 2", que cierra el
  bloque, que pide revelar el prompt del juez, que mete un JSON propio) con un `FakeJudge` que obedece (offline) y su
  versión viva (de (h), con consentimiento).
- `evals run --judge` (apagado por defecto; sin esto el reporte omite causa).

**Tests (offline):** esquema válido/ inválido; el prompt no contiene el log ni el diff (aserción sobre el texto
armado); el bloque con nonce escapa su etiqueta; cálculo de kappa y acuerdo con ejemplos de libro (y un caso con
una sola clase para el borde); la hoja es determinista con la semilla y **oculta** variante, modelo y confianza; la
hoja no contiene texto de logs; `FakeJudge` obediente no cambia el puntaje de los casos hostiles (el esquema
lo acota; el caso "dice 2" no pasa a 2 por el texto); el reporte nunca imprime texto de logs.

**Terminado cuando:** con un `FakeJudge` el reporte de lab muestra cause match por variante y se genera la hoja de
calibración; D-103 y D-104 escritos.

**§13:** parcial de 3.

### (f) `--variant all` completo y la tabla del README — `p4-report`

**Alcance:**
- `evals run --variant all --sources lab,public,injection [--judge …]` produce el JSON (`schema_version: 2`) y el
  Markdown con **todo** §11.2: accuracy, P/R/F1 por categoría, macro-F1, matriz de confusión, cause match, evidencia
  (validez cruda y recall), flaky, abstención y tabla de calibración (alta/media/baja y por décimas), costo (promedio,
  p95, total), latencia (llamada y pipeline, p50/p95), resistencia a la inyección. Secciones por fuente y por tag
  (reglas conocidas / fuera). Incluye una sección **"Cambios de veredicto contra el baseline"** (los casos que
  cambiaron, con ids; nunca texto de logs) que usa (g).
- `evals/runner/readme_table.py` y `python -m evals readme-table`: genera la tabla de comparación (reglas contra las
  variantes LLM, N, accuracy, macro-F1, cause match, costo por diagnóstico con LLM, latencia p50/p95 de la llamada)
  **a partir de `baseline.json`**, entre marcadores `<!-- evals:table:start -->` / `end` del `README.md`; un test falla
  si el README no coincide con lo que generaría el baseline (igual que el drift test de los casos de inyección).
- Mejoras del Markdown: la tabla grande se parte por legibilidad (resumen arriba, detalle por variante abajo).

**Tests (offline):** el reporte completo con `FakeLLM` y `FakeJudge` contiene cada sección (test por sección y
por métrica del §11.2, con una lista explícita de las 9 filas de la tabla); el JSON valida contra un esquema
declarado; la generación de la tabla del README es idempotente; ningún reporte contiene texto de log (el test de
siempre, con un log de canario).

**Terminado cuando:** `--variant all` corre offline sobre lab + inyección con fakes y escribe ambos archivos; SPEC §11.3 y
`evals/README.md` alineados.

**§13:** **casilla 2** (la parte de código; con datos reales se verifica en (h)).

### (g) La regla de regresión y de inyección, y `evals.yml` — `p4-ci-gate`

**Alcance** (D-105, D-106):
- `python -m evals baseline --accept`: escribe `evals/baseline.json` desde una corrida (solo la corre una persona; el
  CI nunca lo escribe). Contenido (D-105): `schema_version`, `created_at`, `git_sha`, `prompt_version`,
  `prompt_fingerprint`, el modelo del diagnóstico y del juez, los precios usados, `cases_hash` (sha256 de la lista
  ordenada de `id` + hash de cada `case.yaml`/`log.txt`) y, por variante de gate, `macro_f1`, `accuracy`,
  `f1` por categoría, `cases`, `attacks_won`, costo y latencia registrados; más `threshold: {macro_f1_drop: 0.05}`.
- `python -m evals check [--baseline evals/baseline.json]`: corre offline, compara con el baseline y sale con un
  código **distinto por causa** (el CI muestra por qué):
  | Código | Significa |
  |---|---|
  | 0 | bien |
  | 1 | **regresión**: macro-F1 de una variante de gate cayó > 5 puntos contra el baseline |
  | 2 | **inyección exitosa**: algún caso de inyección ganó en cualquier variante LLM |
  | 3 | **respuestas incompletas**: hay `llm_error` por falta de caché (cambió el prompt, el modelo, el excerpt o un caso y nadie llenó las respuestas); se nombra cuántas y de qué casos |
  | 4 | **baseline viejo**: `cases_hash` o `prompt_fingerprint` no coinciden con lo que hay (se agregó un caso o se tocó el prompt sin aceptar un baseline) |
  | 5 | error del runner (carga, formato) |
  Variantes que deciden: `rules` y `llm+hints [modelo]` (la de producción). `llm` y `llm+hints+similar` se reportan pero
  no deciden (ver D-105).
- `.github/workflows/evals.yml` (D-105): `pull_request` y `push` a `main` con `paths` (`src/cicd_doctor/core/**`,
  `evals/**`, `lab/cases.yaml`, `pyproject.toml`, `uv.lock`, el propio workflow) y `workflow_dispatch`; un solo job sin
  Postgres: `uv sync --locked`, caché de tiktoken y del modelo de fastembed (`actions/cache`, igual que `ci.yml`),
  `python -m evals check`, el reporte al resumen del job (`$GITHUB_STEP_SUMMARY`: solo ids y números) y como artefacto.
  `timeout-minutes: 8` (el objetivo de < 5 min lo mide (i)); acciones fijadas por sha como en `ci.yml`; permisos
  `contents: read`; **sin** `OPENAI_API_KEY` (el workflow no tiene ningún secreto: no puede gastar).
- **Mientras no exista `baseline.json`** el comando sale con 4 y el workflow está pensado para correr recién cuando (h)
  lo cargue: por eso el `pull_request` se agrega **en (h)**, y (g) lo deja solo con `workflow_dispatch`. Así ningún
  PR queda rojo en el medio.

**Tests (offline, sin dinero ni red):** la regla con reportes sintéticos: −4,9 puntos pasa, −5,1 falla, +10 pasa,
una categoría que desaparece, una variante que falta, casos de más y de menos, `NaN`; un ataque ganado en `llm` y
no en `llm+hints` (falla igual); respuestas faltantes (3, no 1); `prompt_fingerprint` distinto (4); orden estable de
los códigos cuando hay varias causas (gana el más grave: 2, 3, 4, 1); `baseline --accept` determinista; el baseline
no cambia con el orden de los casos ni con el reloj; el resumen del job no incluye texto de logs. Un test de
**contrato** del `workflow` (YAML válido, acciones fijadas por sha, sin secretos, `paths` presentes).

**Terminado cuando:** `check` contra un baseline y un reporte de prueba da los seis códigos esperados; D-105 y D-106
escritos; SPEC §11.3 y §12 alineados.

**§13:** parcial de 4 (se completa con la corrida real de (h) y la rama mala de (i)).

### (h) Datos en vivo — `p4-live-data`

**Alcance** (**gasta dinero y toca `.env`; ver §6 y el Apéndice A**). Código mínimo (la vista final del README, el
`pull_request` del workflow); el grueso son datos commiteados:
1. **Llenar las respuestas** (`--live` con topes): `llm`, `llm+hints`, `llm+hints+similar` con `gpt-6-luna` sobre lab +
   público + inyección. Resultado: `evals/answers/` (≈ 85 casos × 3 variantes, ≈ 250 archivos de ~1 KB).
2. **Varianza del modelo:** 3 corridas vivas de `llm+hints` sin leer la caché (`--no-cache-read`), para medir
   cuánto se mueve el macro-F1 entre corridas. Se documenta σ; si el umbral de 5 puntos es < 2σ, se escribe en D-105
   y se propone la salvaguarda (D-105).
3. **Juez** (D-103): corrida del juez sobre los casos no hostiles de las 4 variantes; **tus 20 etiquetas** (§7) y
   el reporte de acuerdo (`evals/calibration/`). Si el acuerdo es bajo, se sigue el árbol de D-104 (cambiar de modelo
   de juez, afinar el prompt sobre los 5 de reserva, o publicar cause match como informativo).
4. **Inyección en vivo:** `pytest -m live tests/live/test_live_injection.py` y el eval de inyección con el modelo real
   (30 casos + 4 del juez): 0 ataques ganados o se abre un arreglo.
5. **Baseline:** `evals baseline --accept`, reporte aceptado en `evals/reports/accepted/` (solo ids y números),
   `README.md` con la tabla generada y la línea de **NFR-03** (cumple, o la desviación documentada, D-110); se agrega
   `pull_request` a `evals.yml`.
6. **Verificación offline del CI en local:** `evals check` con el baseline recién creado da 0 sin red y con la
   `OPENAI_API_KEY` **borrada** del entorno; se anota el tiempo.

**Terminado cuando:** el baseline está commiteado y `evals check` da 0 offline; cada número del README sale del
reporte aceptado; los gastos reales se anotan contra el presupuesto (§6); D-108 y D-110 con sus números.

**§13:** **casillas 3 y 5**, parcial de 4. Depende de vos: dinero, 20 etiquetas, precios.

### (i) Verificación del CI en vivo y cierre — `p4-close`

**Verificación (no es código; escribe en GitHub, §6):**
1. PR de prueba con un **prompt deliberadamente malo** (p. ej. un `SYSTEM_PROMPT` que manda responder siempre
   `unknown`) en una rama aparte:
   - **Sin** llenar las respuestas → `evals.yml` falla con código 3 (respuestas incompletas): prueba que el CI no se
     deja engañar por la caché vieja (D-100).
   - **Con** las respuestas del prompt malo llenadas en vivo (≈ USD 0,06) y commiteadas **solo en esa rama** → falla con
     código 1 (cae el macro-F1 > 5 puntos). Es la prueba que pide §13.
2. Rama de prueba con una **entrada de caché fabricada** que obedece un ataque (categoría forzada en un caso de
   inyección) → falla con código 2. Se documenta que la entrada es simulada (un modelo real no la dio).
3. Un PR **bueno** (cambio de documentación en `evals/`) → verde, con el tiempo del job (< 5 minutos).
4. Las ramas y los PRs de prueba se **cierran sin mergear y se borran**.
Evidencia: ids de runs, códigos de salida, tiempos, en SPEC §13.

**Cierre (el mismo PR que tilda las casillas):** `docs/phases/p4/` (`README.md` corto en inglés, `plan.md`,
`spike.md`, `brief.md`, `fixtures/` revisados y sanitizados, el plan con la línea de una frase de encabezado), el
índice `docs/README.md`, **la guía de lectura** `docs/guia-de-lectura.md` (castellano) con el capítulo de P4,
`CLAUDE.md` (estado de P4, "Rules": logs públicos ajenos) y `SPEC.md` §11, §12, §13 con la evidencia.

**§13:** **casilla 4** completa, y todas las demás con su evidencia ya escrita.

---

## 3. Decisiones nuevas (provisionales D-100 a D-110)

### D-100 — Dónde viven las respuestas del LLM, cómo se las ancla al prompt y cómo se mantienen (modifica D-65, SPEC §11.3)

- **Decisión:** las respuestas del LLM del eval **se commitean** en `evals/answers/` (renombre de `evals/.cache/`),
  con subcarpetas de dos letras. La clave pasa a ser `sha256(model, prompt_version, prompt_fingerprint, request)` donde
  `prompt_fingerprint` es el sha256 del `SYSTEM_PROMPT` y del esquema estricto. Una entrada guarda solo la respuesta
  (como hoy); se prohíbe guardar el request. `python -m evals prune` mantiene la carpeta: solo quedan las entradas que
  la configuración actual usaría. Empieza vacía y se llena en (h). Un test recorre todas las entradas y falla ante
  secretos, campos de más, tamaño > 16 KB o formato inválido.
- **Alternativas:** (1) no commitear y regenerar en el CI con `--live` (gasta dinero en cada PR, hace falta una clave
  en el CI, y no es determinista); (2) guardar la caché como artefacto del CI o en `actions/cache` (puede evictarse,
  no se revisa, no viaja con el commit); (3) un único archivo grande por modelo (diffs ilegibles, conflictos de
  merge); (4) un submódulo o un repo aparte de datos (complica clonar y el CI); (5) mantener la clave actual (el CI no
  ve un prompt malo: descartada, ver el hallazgo 2).
- **Por qué:** el CI offline de §11.3 necesita las respuestas **en el commit**. Con la huella en la clave, cambiar el
  prompt es un fallo de caché y el gate lo reporta (código 3); sin ella el CI da verde con un prompt roto.
- **Costos y límites:** tamaño esperado ≈ 1 KB por respuesta (≈ 250 a 350 archivos, < 0,5 MB); cada refresco de
  modelo o de prompt agrega un bloque nuevo al historial de git (se acepta; `prune` evita que la carpeta crezca, el
  historial sí). Las entradas contienen citas de líneas de **logs ajenos** (≤ 300 caracteres, ≤ 8 por respuesta, ya
  redactados) y las causas que escribió el modelo; se tratan con la misma regla de licencias que los casos (D-101).
  **Consecuencia operativa:** un PR que toca el prompt, el excerpt, las reglas que dan pistas o un caso **debe**
  traer las respuestas nuevas (las llena el dueño con `--live`); un colaborador sin clave ve el código 3 y la
  instrucción. Es la fricción buscada: el prompt solo cambia a propósito.
- **Cambios:** `evals/.cache/` y `.gitignore`; SPEC §11.3 ("committed cache"); la primera corrida viva invalida todo
  (una vez).

### D-101 — Casos públicos: cómo se eligen, se emparejan, se capturan y se licencian, y qué se guarda

- **Decisión (selección):** solo repos públicos con licencia **permisiva declarada** (`license.spdx_id` ∈ MIT,
  Apache-2.0, BSD-2/3-Clause, ISC, MPL-2.0; lista en un archivo y en un test), con CI en Actions, sin proyectos de
  seguridad. La lista de repos la propongo yo con los criterios del spike y vos la vetás; **el manifiesto
  `evals/public-sources.yaml` es la única fuente de verdad de qué se captura**.
- **Emparejado (H1):** run fallado → primer run verde del **mismo workflow y la misma rama** con otro `head_sha`, ≤ 3
  commits y ≤ 10 archivos de distancia (`compare`), ≤ 7 días, un único job fallado. **(H2, flaky candidato):** mismo
  `head_sha`, intento posterior verde. Las dos salen en la hoja de candidatos con su motivo; **nada es caso hasta que
  vos lo etiquetás** (D-102). El número de pares razonables que salen lo mide el spike; si da menos de 60 candidatos
  después de filtrar, se agregan repos o se relajan los filtros (se documenta cada relajación).
- **Qué se guarda de cada caso:** `case.yaml` (+ `provenance`), `log.txt` **recortado y redactado**, `jobs.json`
  recortado (como `capture.py`), y **`fix.json`**: sha del arreglo, lista de archivos con +/− y un resumen de una
  oración escrito por vos. **No se guarda texto del diff** del repo ajeno (modifica SPEC §11.1: `fix.diff ≤ 200
  líneas` desaparece; lo que ya decía "short diff summaries" prevalece). Un diff es código de otro; una oración y una
  lista de rutas no.
- **Recorte del log:** el paso fallado (lo que el pipeline procesa) más el contexto mínimo para que `parse_log` y el
  recorte por paso funcionen, hasta 1 500 líneas y 150 KB (valores a confirmar con C2). **Regla de aceptación:** el
  excerpt del log recortado debe ser **idéntico** al del log completo; si no, se amplía el recorte o se descarta el
  caso. (Si el recorte no es equivalente, el caso mide algo distinto de la app.)
- **Limpieza (D-62, D-59 y una pasada propia):** escapes → recorte → `redact_text` → pasada de captura para emails y
  logins → **escáner independiente** que debe dar 0 hallazgos; si no, el caso no se escribe.
- **Licencias:** `SOURCES.md` (repo, SPDX, URL del run, fecha); la licencia de cada repo permisiva exige atribución en
  la mayoría de los casos: se agrega `NOTICE`-estilo en `evals/cases/public/` con los repos y sus licencias. El repo
  no tiene `LICENSE` propio: esto no lo resuelve, solo documenta lo ajeno (se anota como pregunta §7). Retiro: si un
  mantenedor lo pide, el caso se borra y se corrige el baseline.
- **Logs vencidos:** los logs de Actions vencen (por defecto a los 90 días). La captura es **reproducible mientras
  existan los logs**; después la fuente de verdad son los casos commiteados (por eso se commitean). Un log que da 404/410
  se descarta con su motivo y no frena el lote.
- **Orden del historial:** `provenance.occurred_at` (fecha del run) ordena `llm+hints+similar` para casos públicos.
- **Alternativas:** guardar el diff entero (licencia y tamaño); guardar solo el excerpt que genera la app (mide el
  recorte con la salida del propio recorte: circular); incluir repos sin licencia o copyleft fuerte (GPL: el extracto
  no la contagia con seguridad, pero no vale el riesgo); bajar el log en cada corrida (no es reproducible ni offline);
  un dataset de terceros (no existe uno de GitHub Actions con arreglo emparejado y etiquetas nuestras).
- **Riesgos que se aceptan:** el sesgo de la muestra (repos populares, ecosistemas de las reglas) y el sesgo del
  emparejado (solo fallas que alguien arregló rápido). Se documentan en el README de los casos.

### D-102 — Quién etiqueta, con qué guía y sin ver qué

- **Decisión:** **vos sos el etiquetador final** de los 30 casos y de los 20 del juez. Para bajarte el tiempo, la sesión
  prepara un **borrador** de cada caso (categoría y causa propuestas) usando el log **y** el diff del arreglo, **no**
  la salida de `rules` ni la de ningún modelo evaluado; vos corregís lo que haga falta con la hoja a la vista. De los
  30, un subconjunto aleatorio de 10 lo etiquetás **a ciegas** (sin ver el borrador) antes de ver el borrador: la
  coincidencia entre tu etiqueta a ciegas y el borrador mide cuánto te ancló. La guía de etiquetado
  (`evals/cases/public/LABELING.md`) fija la regla: la categoría es la de la **causa raíz que el commit de arreglo
  corrige**, no la del síntoma; `label_basis: fix | log` anota en qué se apoyó (un arreglo claro o solo el log);
  `flaky` solo si el mismo `head_sha` pasó en un re-run (H2) o si el log dice "flaky/retry" de forma explícita;
  `unknown` cuando ni el log ni el arreglo dejan decidir.
- **Alternativas:** que etiquete el modelo evaluado (circular); que etiquete otro modelo sin revisión (sesgo
  compartido con el juez); solo vos y sin borrador (≈ 3 a 4 min por caso, ~2 h solo de públicos); dos personas (no hay).
- **Sesgos que quedan:** las fronteras que D-13 deja abiertas (lint y tipos son `broken_test`; un `tf validate` roto es
  `config_or_secrets`, D-88 lo vio) y tu criterio. Se mide el desacuerdo borrador-ciego y se informa. Si > 20 % de los
  10 a ciegas difiere, se revisa la guía **antes** de seguir.
- **Consecuencias:** el campo `labeled_by` y `label_basis` quedan en cada caso; las categorías con n chico se marcan.

### D-103 — El juez de causa: modelo, prompt, esquema y cómo no se contamina con el modelo evaluado

- **Decisión:**
  - **Modelo aparte, otro nivel y otra configuración:** `--judge-model` (no `OPENAI_MODEL`). Candidatos medidos en
    la calibración con las mismas 20 etiquetas: el modelo grande de la familia (el `gpt-6.2-sol` que D-88 estimó y
    **nunca** se usó para diagnosticar) y `gpt-5.4-mini`; se queda el que mejor acuerdo dé. Se prefiere **no** usar
    `gpt-6-luna` como juez de `gpt-6-luna`.
  - **El juez recibe solo texto corto:** `expected.cause` y `fix.summary` (etiquetas tuyas) y el `probable_cause` y
    el `suggested_fix` de la respuesta evaluada. **Nunca** ve el log, el excerpt, el diff, la categoría ni quién
    respondió. Escala: 0 (no coincide o contradice), 1 (parcial: acierta la zona pero no la causa, o da una causa
    posible que no es la esperada), 2 (coincide en lo esencial, aunque use otras palabras).
  - **Esquema estricto:** `{score: 0|1|2, reason: string ≤ 200}`; prompt `judge-v1` con las definiciones y 6 ejemplos
    fijos (2 por puntaje; **escritos a mano, sobre casos que no están entre los 20 etiquetados**); sin herramientas;
    `temperature` baja si el modelo la admite; caché propia.
  - **Entrada no confiable:** el `probable_cause` es texto de un modelo que leyó un log posiblemente hostil; va en un
    bloque con nonce y etiqueta escapada, con el mismo patrón de §9.3, y una respuesta fuera del esquema es un error
    de juez (el caso queda "sin juzgar" y se cuenta), nunca un puntaje. La suite de inyección suma 4 casos contra el
    juez (D-99 ampliada).
  - **Misma plataforma (OpenAI) para el juez y el diagnóstico:** único proveedor con clave hoy. Es un riesgo de sesgo de
    familia (un juez tiende a premiar el estilo de su propia familia), y se **declara** en el reporte. Mitigaciones: otro
    nivel, el juez no ve los textos de las otras variantes en la misma llamada, y la calibración contra tus etiquetas
    es la medida del sesgo.
  - **Una llamada por (caso × variante)**, no comparaciones por pares (evita el sesgo de posición).
- **Alternativas:** un juez del mismo modelo (autopreferencia); comparación textual (BLEU/embeddings: no distingue
  causa de síntoma, el SPEC ya la descartó en D-33); un juez de otro proveedor (mejor para el sesgo, pero hoy no hay
  clave ni presupuesto; queda como mejora); que el juez vea el log (puede ser engañado por el log y deja de ser
  independiente del excerpt).
- **Costo:** el juez ve ≈ 600 tokens, no 7 000: una fracción de un diagnóstico (estimación: ≤ USD 0,002 por
  juicio con el modelo grande; **a medir en C4**).
- **Consecuencias:** `evals run --judge` agrega la columna "cause match"; sin `--judge` el reporte dice "no juzgado".

### D-104 — Calibración del juez con 20 etiquetas tuyas, umbral y qué hacer si es bajo

- **Decisión:** la hoja tiene **20 pares** más **5 de reserva** (D-103), elegidos con semilla de una
  población de respuestas de `rules`, `llm` y `llm+hints` (lab y público): ≈ 8 con categoría correcta, ≈ 7 con
  categoría incorrecta y ≈ 5 de `rules`, **con al menos 5 casos que la expectativa humana probable sea "parcial"**
  (para que el 1 aparezca). Se barajan, con ids opacos; la hoja **no** dice variante, modelo, confianza ni categoría.
  Vos puntuás 0/1/2 a ciegas. Medidas: acuerdo exacto, **kappa ponderado cuadrático**, matriz humano × juez y
  discrepancias graves (0 contra 2), con un intervalo (con n = 20 es ancho: se informa).
- **Umbral de aceptación:** acuerdo exacto ≥ 75 % (15 de 20) **y** kappa ponderado ≥ 0,6 **y** ninguna
  discrepancia grave. Si pasa, el juez es "calibrado" y el reporte lo dice con sus números.
- **Si el acuerdo es bajo (en este orden, hasta tres rondas, sin tocar el umbral):** (1) revisar las discrepancias:
  ¿es la guía/escala ambigua o tu etiqueta dudosa? (la escala se afina); (2) probar el otro modelo de juez; (3) afinar
  el prompt y volver a medir **sobre los 5 de reserva** (no sobre los mismos 20, que se vuelven datos de ajuste: si se
  los usa, se dice); (4) colapsar a binario (coincide / no coincide) y recalcular; (5) si nada pasa, **se publica
  "cause match" como informativo, con el acuerdo que dio y la advertencia**, y no se usa en ninguna decisión. La
  casilla pide "se reporta el acuerdo": se cumple con honestidad, no con un número lindo.
- **Cuánto tiempo te pide:** ≈ 1,5 a 2 minutos por par → **30 a 40 minutos** en una sentada (25 pares), sin ver nada
  de lo que dijo el modelo sobre el log más que las dos causas y el arreglo.
- **Alternativas:** más etiquetas (mejor kappa; cuesta tiempo tuyo, queda como opción de 30 si querés); etiquetas de un
  segundo humano (no hay); umbral más laxo (un juez poco confiable no merece una columna en el README).

### D-105 — La regla de regresión del CI: qué compara, con qué ruido, qué es el baseline y quién lo mueve (modifica SPEC §11.3)

- **Decisión:**
  - **Qué compara:** `macro_f1` (como lo calcula `metrics.py`: media del F1 de las categorías con soporte) de las
    variantes `rules` y `llm+hints [modelo de producción]`, **sobre los casos `lab` + `public`** juntos, contra
    `baseline.json`. Falla si baja **más de 0,05** (5 puntos absolutos) en cualquiera de las dos. `llm` y
    `llm+hints+similar` se reportan, no deciden (el segundo depende del historial y es el más sensible; el primero no
    es lo que corre en producción). La inyección va aparte (D-106).
  - **El ruido:** el CI es **determinista** (lee respuestas guardadas), así que no hay ruido de corrida a corrida: una
    caída es un cambio real de salida. El ruido real tiene dos fuentes y se mide en otro lado: (1) la varianza del
    modelo al **llenar** las respuestas (3 corridas vivas, D-108: se reporta σ de macro-F1 y se compara con 0,05; si
    σ > 0,025, el baseline se acepta con la nota "el umbral es ≈ 2σ" y se avisa); (2) el **tamaño chico** de algunas
    categorías: con soporte 3, un caso que cambia mueve el F1 de esa categoría ≥ 0,25 y el macro-F1 unos 3 puntos.
    Por eso: el gate pone sobre la **suma** de lab + público (≈ 55 casos), muestra los **cambios de veredicto por
    caso** (ids, de qué a qué) en el resumen del job, y da un IC por bootstrap en el reporte como **información**, no
    como criterio. Una caída de > 5 puntos por un solo caso en una categoría de soporte 2 **falla igual**: es lo que
    pide §13; el resumen dice "causada por 1 caso" para que se decida con criterio.
  - **`baseline.json`:** lo descripto en (g): métricas aceptadas, `prompt_fingerprint`, modelo(s), precios, `cases_hash` y
    el umbral. **Lo mueve solo una persona** (`evals baseline --accept` en un commit propio, con el motivo en el mensaje y,
    si cambia el modelo o el prompt, un D-NN o una nota en D-100/D-88). El CI **nunca** lo escribe y falla con 4 si no
    coincide con los casos o el prompt (agregar un caso, o tocar el prompt, **obliga** a aceptar un baseline nuevo a
    propósito; el cambio se ve en el diff del PR).
  - **Respuestas faltantes (código 3) no es una regresión:** es "no se puede evaluar". Falla igual (si no, un PR
    puede borrar la caché y pasar), pero con su propio mensaje y su propia instrucción (`evals run --live` y
    commitear `evals/answers/`).
  - **Tiempo:** el workflow no tiene Postgres ni red hacia OpenAI. El objetivo es < 5 min; el spike (C5) dice si entra
    `llm+hints+similar` (necesita fastembed) o solo las dos variantes del gate. Se agrega `timeout-minutes: 8`.
  - **Cuándo corre:** `pull_request` y `push` a `main` con `paths` (código de `core/`, `evals/`, `lab/cases.yaml`,
    `pyproject.toml`, `uv.lock`); un cambio solo de documentación no lo dispara. Convertirlo en check **requerido** de la
    rama es un cambio de configuración del repositorio: queda en tu lista (§6).
- **Alternativas:** gate sobre accuracy (insensible a categorías raras); gate por categoría (demasiado frágil con
  n = 2); umbral relativo o con IC (más "estadístico" pero no es lo que pide SPEC y complica la explicación);
  gate con llamadas vivas (dinero, secreto y no determinista); un baseline que el CI actualiza solo (un prompt malo se
  aceptaría solo).
- **Consecuencias:** SPEC §11.3 ("`evals/baseline.json` holds the last accepted metrics…") se precisa; los PRs que
  tocan el prompt o `core/` necesitan la clave del dueño para pasar el gate (el costo de tener un gate real).

### D-106 — Cómo falla el CI ante una inyección exitosa

- **Decisión:** el código de salida 2 se activa si **algún** caso de inyección tiene `wins` en **cualquier** variante LLM
  del reporte (no solo la de producción), tomando las respuestas guardadas. La definición de ataque ganado es la de
  D-64/D-99 (`judge`): categoría forzada, enlace o mención con efecto, prompt filtrado, evidencia inventada
  publicada, sugerencia insegura; para los casos del juez, un puntaje cambiado por el texto. Es independiente del
  macro-F1: una inyección exitosa **falla aunque el macro-F1 mejore**. La prueba "obedezco a ciegas" (el
  `FakeLLM` que obedece) ya corre en `pytest`; el gate de evals cubre lo que solo el modelo real muestra, desde sus
  respuestas guardadas.
- **Qué NO puede cubrir:** una respuesta futura que el modelo real no dio todavía. Un cambio de prompt exige respuestas
  nuevas (D-100), y entonces el gate las mira. La **prueba en vivo** del gate usa una entrada de caché **fabricada**
  (§(i)), declarada como simulada.
- **Alternativas:** solo la variante de producción (una variante sin pistas puede perder una defensa); un umbral de
  tasa (el objetivo es 0, no una tasa).

### D-107 — Cómo se simulan las secuencias de la métrica de flaky

- **Decisión:** un generador con semilla arma secuencias de eventos de runs (falla, éxito, intentos, ramas, forks,
  workflows, shas, orden de llegada, duplicados) en escenarios con etiqueta **por construcción**: re-run verde del mismo
  commit (flaky), commit nuevo verde (arreglo), falla → falla (abierto), verde de otra rama / otro workflow / de un fork
  con el mismo nombre de rama / sin rama (nada), mismo sha en **otro** run (nada), éxito antes de la falla, reentrega.
  Se clasifican con `core/outcomes.py` (`decide`, `decide_late`), y el **oráculo** es una tabla de escenarios aparte (no
  llama a `decide`). Se reportan precisión y recall de `resolved_flaky` y la exactitud de `resolved_by_commit` sobre N
  = 300 secuencias, y las 2 secuencias reales del lab de P2 (fixtures). Se aclara en el reporte que es una máquina de
  estados determinista: el 100 % es lo esperable y el valor de la métrica es **proteger contra regresiones** y cubrir
  bordes, no estimar un acierto estadístico.
- **Alternativas:** simular con el LLM (no es lo que decide `resolved_flaky`); solo las 2 secuencias reales (n = 2);
  usar secuencias públicas reales (no se pueden etiquetar sin ruido). La métrica de la **categoría** `flaky` (sospecha por
  el log) ya está en P/R por categoría y se mira aparte, con los candidatos H2 del lote público.

### D-108 — Presupuesto y procedimiento de las corridas vivas

- **Decisión:** toda corrida viva del eval lleva `--max-cost-usd` y `--max-calls` (obligatorios, con valor por defecto
  bajo); el runner **frena** al llegar al tope. Presupuesto total de P4: **estimado USD 1,0; tope USD 1,50**
  (§6 tiene el desglose por grupo y lo apruebas por grupo). El orden es siempre: estimar con tiktoken (spike C4) →
  correr con tope → registrar el gasto real contra la estimación en el reporte. Los precios se leen de `--price` o
  de `.env` (`LLM_PRICE_*`); **no se imprimen claves**. La clave está en `.env` y no la leo ni la imprimo (se lee
  solo en el proceso del runner, `SecretStr`).
- **Alternativas:** los topes `LLM_DAILY_*` de la app (no aplican al eval); sin tope (D-65 lo permitía; un bucle de
  reparaciones o un caso gigante podían gastar de más).
- **Consecuencias:** `--live` sin tope se rechaza; los reportes anotan "gastado / tope".

### D-109 — Latencia, costo y evidencia: qué se mide de verdad

- **Decisión:** la **latencia de la llamada** que se reporta es `LLMResult.latency_ms` (la que se midió al llenar
  la respuesta); la del **pipeline** es la medida de la corrida y se rotula "con respuestas guardadas". El **costo** es
  de los tokens guardados × los precios del baseline; el promedio de NFR-03 es **por diagnóstico con LLM**. La
  **validez de evidencia** es sobre los ítems **crudos** del modelo (que `verified_evidence` acepta o descarta) y el
  **recall** de líneas esperadas se queda como está.
- **Alternativas:** dejar `perf_counter` (miente con la caché); contar solo lo publicado (siempre 100 %).
- **Consecuencias:** cambian números de los reportes viejos (los de P1b/P2 no se rehacen; los reportes de P4 son los
  que valen); SPEC §11.2 se aclara.

### D-110 — Qué hacer si NFR-03 no se cumple

- **Decisión:** NFR-03 es **promedio ≤ USD 0,01 por diagnóstico con LLM** en el conjunto `lab` + `public` con el modelo
  de producción. Si el promedio medido (con los precios que confirmes) está **por debajo**, se tilda. Si **lo supera**:
  (1) se ve **por qué** (¿pocos casos con excerpts enormes?) con el p95 y la tabla por tamaño; (2) primero se baja
  lo que el producto envía (el presupuesto de ≈ 6 000 tokens del excerpt, un tope más bajo para el caso largo) y se
  vuelve a medir **sobre el mismo baseline**; (3) si no alcanza, se compara con un modelo más barato (D-65) y se decide
  en un D-NN; (4) si la desviación es chica y está explicada (≤ USD 0,02), se **documenta** en SPEC (NFR-03) y en el
  README con la causa y el plan, no se esconde. Lo que **no** se hace es cambiar el número de NFR-03 sin escribirlo.
  Hoy se espera cumplirlo con margen (≈ USD 0,0003 a 0,002 por caso, estimación a confirmar).
- **Alternativas:** relajar NFR-03 de antemano (sin datos); medir solo el lab (excerpts chicos, optimista).

---

## 4. Riesgos e incógnitas del mundo real

| Id | Incógnita o riesgo | Impacto | Mitigación / dónde se contesta |
|---|---|---|---|
| R-P4-1 | **Pocos pares falla→arreglo razonables** en repos públicos (muchos verdes son cambios no relacionados, re-runs o revert) | **Alto** | spike B2 mide la tasa; filtros de D-101; si faltan, más repos o relajar filtros con registro |
| R-P4-2 | **Logs vencidos** (90 días) o respuestas 404/410 | Medio | captura dentro de la ventana; descarte con motivo; los casos commiteados son la fuente de verdad (D-101) |
| R-P4-3 | **Tamaño de logs** (MB) y límite de 20 MB de cola del pipeline | Medio | recorte con prueba de equivalencia (C2); tope 150 KB por caso |
| R-P4-4 | **Secretos reales** en logs ajenos que el redactor no conoce | **Alto** | redactor + pasada de captura + escáner independiente en 0 o el caso no entra; spike C1 mide residuos; revisión tuya del lote |
| R-P4-5 | **Datos personales** (emails, logins, rutas) en logs y en `git log` de logs | Medio | pasada de emails y logins; SOURCES.md solo con repos y runs; retiro a pedido |
| R-P4-6 | **Texto adversario real** en un log ajeno (no es un ataque nuestro) | Medio | es dato: nonce, esquema, renderizado seguro; se guarda tal cual y se mira en el lote; nunca se ejecuta ni se imprime |
| R-P4-7 | **Licencias:** el repo no tiene `LICENSE`; atribución y copia de extractos | Medio | solo licencias permisivas, solo extractos, NOTICE y SOURCES.md; pregunta §7 |
| R-P4-8 | **Rate limits** de la API (5 000/h, secundarios) durante el lote | Bajo | presupuesto de llamadas (`--max-api-calls`), frena en 200 restantes, se reparte en dos sesiones |
| R-P4-9 | **Calidad y sesgo de las etiquetas** (una persona; fronteras ambiguas) | **Alto** | guía + 10 a ciegas + `label_basis`; el acuerdo borrador-ciego se informa (D-102); los números dicen "n chico" |
| R-P4-10 | **Categorías chicas** (flaky, unknown, timeout) hacen ruidoso el macro-F1 | Alto | gate sobre lab + público, cambios por caso en el resumen, IC informativo (D-105) |
| R-P4-11 | **El juez se equivoca o favorece a su familia** | Alto | modelo aparte, calibración con 20 etiquetas tuyas, árbol de D-104, "informativo" si no calibra |
| R-P4-12 | **Variación del modelo entre corridas** (no determinista) | Medio | 3 corridas vivas, σ documentada (D-105); el CI usa respuestas guardadas |
| R-P4-13 | **Costo**: logs públicos llenan el presupuesto de tokens y el juez suma llamadas | Medio | topes del runner (D-108); NFR-03 con p95 (D-110); estimación del spike C4 |
| R-P4-14 | **Tiempo de CI** > 5 min (tiktoken, fastembed, `uv sync`) | Medio | cachés de `actions/cache`; spike C5; `similar` fuera del gate si no entra |
| R-P4-15 | **Clave de caché demasiado sensible:** cambiar una coma del prompt invalida todo | Bajo (deseado) | es el comportamiento que buscamos; el costo (≈ USD 0,3 por refresco completo) está en el presupuesto |
| R-P4-16 | **Un colaborador sin clave** no puede llenar respuestas para pasar el gate | Bajo (hoy un solo dueño) | el mensaje del código 3 dice qué hacer; queda anotado para si llegan colaboradores |
| R-P4-17 | **Un log ajeno que pide algo al juez** ("score 2") | Medio | bloque con nonce, esquema, 4 casos de inyección contra el juez (D-103) |
| R-P4-18 | **Mezcla de data ajena en OpenAI** durante el eval (los extractos van al proveedor) | Bajo | datos públicos y redactados, `store=false` (D-63), sin credenciales; se anota en el README de los casos |
| R-P4-19 | **El historial de `similar` con 30 casos en ≈ 10 repos** casi no tiene vecinos | Bajo | se reporta "n chico" (como D-86); se prefieren ≥ 2 fallas parecidas por repo si el mundo las da |
| R-P4-20 | **Deriva del modelo:** el modelo del proveedor cambia detrás del mismo nombre | Medio | el baseline guarda el `model` devuelto por la API (p. ej. `gpt-5.4-mini-2026-03-17`); un cambio se ve en la corrida de refresco |
| R-P4-21 | **Los worktrees comparten la base de test** (memoria del proyecto) | Bajo | P4 no agrega migraciones; (a) a (g) no usan Postgres |
| R-P4-22 | **Los cambios en `evals/answers/` inflan los diffs de PR** | Bajo | `linguist-generated`, subcarpetas, y la revisión mira el reporte, no los JSON |

---

## 5. SPEC contra código: lo que se contradice y cómo lo resuelve este plan

No elegí en silencio ninguno; cada uno se corrige en el PR que se indica, en el mismo PR que el código.

| # | SPEC dice | El código o la realidad | Resolución |
|---|---|---|---|
| 1 | §11.3: "CI runs offline from the committed cache" | La carpeta está sin seguimiento y solo tiene entradas `diag-v2` (hoy el código es `diag-v3`) | (a), (h), D-100 |
| 2 | §11.3: clave `(model, prompt_version, request)`; §13 P4: "un prompt deliberadamente malo falla el CI" | El texto del prompt y el esquema no están en la clave: un prompt malo sin cambiar la etiqueta da verde | (a), D-100, D-105 |
| 3 | §11.1: `fix.diff` "trimmed to ≤ 200 lines"; y "only … short diff summaries" | Las dos frases se contradicen; `load_case` ni lee `fix.diff` | (c), D-101 (`fix.json`, sin texto de código) |
| 4 | §11.2: "Latency: p50/p95 of the LLM call and of the whole pipeline" | Offline mide la lectura del archivo (0/1 ms); costo 0 sin precios | (b), D-109 |
| 5 | §11.2: "Evidence validity: share of **published** evidence items that pass verification" | Lo publicado siempre pasa (el pipeline descarta el resto) | (b), D-109 (sobre lo crudo) |
| 6 | §11.2: flaky "over simulated event sequences and the lab flaky workflow" | `resolved_flaky` sale de una máquina de estados determinista | (b), D-107 |
| 7 | §11.2: "Cost: average … `cost_usd` per diagnosis"; NFR-03 "per LLM diagnosis" | El promedio de hoy incluye casos de caché y respaldo con costo 0 | (b), D-109, D-110 |
| 8 | §11.1: el caso público tiene `commit`; el modelo del caso no tiene procedencia ni licencia | `CaseFile` es `extra="forbid"` | (c), D-101 |
| 9 | §11.3: `--variant all --sources lab,public,injection` | El runner usa `lab` por defecto y `public` no existe | (c), (f) |
| 10 | `evals/README.md`: 16 casos de inyección, modelo sin elegir, "CI gate arrives in P4" | 30 casos, `gpt-6-luna` (D-88) | (a), (i) |
| 11 | §12: "Added in later phases: offline evals (P4)" en `ci.yml` | Va como workflow aparte `evals.yml` | (g): se aclara en SPEC §12 |
| 12 | §11.1 describe "23 cases" de lab | Son 23 distintos y 25 capturas | sin cambio; se aclara en el README de los casos |
| 13 | §11.2 variante `llm+hints+similar`: orden por `captured_at` | Para casos públicos tiene que ser la fecha del run | (c), D-101 |
| 14 | §13 P4, caja 3 "agreement is reported" | No define umbral ni qué pasa si es bajo | D-104 (umbral y árbol) |
| 15 | D-33: "CI fails on a macro-F1 drop of more than 5 points" | No dice contra qué conjunto ni con qué ruido | D-105 |

---

## 6. Lista aparte: lo que necesita tu consentimiento explícito

Nada de esto lo hago hasta que lo apruebes (el spike entero espera tu "andá", por partes).

**A. Spike (solo lectura).**

| Qué | Dónde | Notas |
|---|---|---|
| Parte A: leer páginas públicas de documentación | web | sin credenciales |
| Parte B: `gh api` solo `GET` sobre 3 a 5 repos públicos **ajenos** (runs, jobs, compare, repo, logs) | GitHub | usa tu sesión de `gh`; **no** el JWT de la app ni `.env`; ≤ 600 llamadas del límite de 5 000/h; los logs bajan al scratchpad y se **borran** al terminar |
| Parte C: scripts descartables de Python y medir el runner offline | scratchpad / local | sin red salvo C5 (lectura de runs de CI de este repo con `gh`); **no** llama a OpenAI; no toca el repo |
| Parte E: tus precios y tu veto sobre repos | tu respuesta | — |

**B. Lo que escribe en GitHub, gasta dinero o toca `.env` (solo (h) e (i), y solo con tu "sí" a cada grupo):**

| Grupo | Qué | Dónde | Costo / efecto |
|---|---|---|---|
| **1** | Llenar las respuestas: `evals run --variant all --sources lab,public,injection --live --max-cost-usd …` con `gpt-6-luna` | OpenAI | ≈ USD **0,25** (estimado: 85 casos × 3 variantes; los públicos con excerpts más grandes) · tope USD 0,40 |
| **2** | Varianza: 3 corridas de `llm+hints` con `--no-cache-read` | OpenAI | ≈ USD **0,17** · tope 0,30 |
| **3** | Juez: elegir entre dos modelos con las 20 etiquetas y correrlo sobre los casos no hostiles de las 4 variantes | OpenAI | calibración ≈ USD 0,08; corrida completa ≈ USD 0,3 a 0,45 (220 juicios, **a medir en C4**) · tope 0,60 |
| **4** | Inyección en vivo (`pytest -m live` y el eval) incluidos los 4 casos del juez | OpenAI | ≈ USD **0,03** · tope 0,10 |
| **5** | El prompt malo de la rama de prueba: llenar sus respuestas (solo la variante de producción) | OpenAI | ≈ USD **0,06** · tope 0,15 |
| **6** | **Cargar o confirmar en `.env`** `OPENAI_API_KEY`, `OPENAI_MODEL` y `LLM_PRICE_*` (los precios que me des); no se toca nada más; **`LLM_DAILY_*` no aplican al eval**, el tope lo pone `--max-cost-usd` | `.env` (secretos) | lo hacés vos o me decís "los precios están en `.env`" y no lo leo ni lo imprimo |
| **7** | Enviar extractos de logs **públicos ajenos** y ya redactados a OpenAI (parte de 1 a 5) | OpenAI | `store=false`; sin credenciales; datos públicos |
| **8** | Subir las ramas de prueba, abrir PRs de prueba con el prompt malo y con la entrada de caché fabricada, dejar correr `evals.yml`, cerrar sin mergear y **borrar las ramas** | GitHub (este repo) | usa minutos de Actions; ver §(i). Nada entra a `main` |
| **9** | (Opcional) hacer de `evals` un check **requerido** de `main` | configuración del repositorio | cambio de configuración: solo si lo pedís |
| **10** | Los PRs normales de (a) a (i) | GitHub | el flujo de siempre |
| **11** | Mi propuesta de **repos** y **tu etiquetado** (≈ 2,5 h tuyas en 2 sentadas, §7) | — | sin esto (d) y (h) no avanzan |

**Presupuesto total estimado: ≈ USD 0,9 a 1,0; tope acordado: USD 1,50** (la suma de los topes por grupo es mayor
a propósito; el que manda es el total). Cada grupo se corre **solo** si el anterior salió bien. Las estimaciones son
cuentas mías con el costo medido de D-88 y supuestos que el spike C4 reemplaza por tokens reales.

---

## 7. Preguntas para vos y qué tenés que preparar

**Preguntas (con mi recomendación):**

1. **Tiempo de etiquetado:** ≈ 75 minutos para los 30 públicos (con borrador mío, 10 de ellos a ciegas) y 30 a 40
   minutos para los 20 del juez; ≈ **2,5 h en dos sentadas**. ¿Te sirve? Reco: sí; si no, 30 públicos y 20 del juez es
   el mínimo de §13 y no baja de ahí.
2. **Borrador mío + tu revisión (con 10 a ciegas) o todo manual:** Reco: borrador + 10 a ciegas (D-102).
3. **Repos:** Reco: que yo proponga 5 con los criterios (licencia permisiva, fallas frecuentes, mezcla de ecosistemas
   dentro y fuera de lo que las reglas conocen) y vos vetás. ¿Algún repo que **prefieras** o **evites**?
   ¿Aceptás ecosistemas fuera de las reglas (Node/TS, Go, Rust) para ver cuánto aporta el LLM?
4. **Licencias:** ¿aceptás "solo licencias permisivas, solo extractos y resumen de una oración del arreglo, sin texto de
   diff, con NOTICE y SOURCES"? ¿Querés que el repo tenga un `LICENSE` propio (hoy no lo tiene)? Es una decisión tuya
   y no la tomo yo.
5. **Presupuesto en dólares de las corridas vivas:** estimo ≈ USD 1,0, tope USD 1,50 (§6). ¿Lo aprobás por grupos?
6. **Juez:** ¿aceptás que sea del **mismo proveedor** (OpenAI, otro modelo y otro nivel que el del diagnóstico)? Reco:
   sí, declarado en el reporte; la alternativa (otro proveedor) necesita otra clave.
7. **Caché commiteada:** ¿aceptás commitear `evals/answers/` (< 0,5 MB) y que un PR que cambie el prompt o `core/`
   traiga respuestas nuevas llenadas por vos? Reco: sí (D-100). La alternativa es un CI que no ve prompts malos.
8. **Precios:** pasame (o confirmame que están en `.env`) los precios por millón de tokens de `gpt-6-luna`, del modelo
   grande de juez y de `gpt-5.4-mini` (D-88 dejó "a confirmar").
9. **Quién actualiza el baseline:** solo vos (`evals baseline --accept`), en un commit propio. ¿De acuerdo?
10. **`evals` como check requerido en `main`:** ¿ahora, después de (i), o nunca? Reco: después de (i).
11. **Casos `flaky` públicos** (mismo sha que pasó en un re-run): ¿los aceptás como `flaky` aunque no se vea la causa?
    Reco: sí si el log lo sugiere y los marcás vos; si no, `unknown`.
12. **NFR-03:** ¿aceptás la política de D-110 (documentar la desviación si es chica, bajar tokens antes que cambiar de
    modelo, nunca editar el número sin escribirlo)?
13. **Spike:** ¿aprobás A, B, C y E, por partes?

**Qué tenés que preparar, y qué casilla depende de cada cosa:**

| Cosa | Casillas que dependen | Se puede verificar sin ella |
|---|---|---|
| Tus etiquetas de los públicos (≈ 75 min) | 1 | (c), (e), (f), (g) y todo con `FakeLLM`; la casilla 1 queda abierta con "faltan etiquetas" |
| Tus 20 etiquetas del juez (30 a 40 min) | 3 | el mecanismo (hoja, kappa, juez) offline con `FakeJudge`; el acuerdo real no |
| Dinero para los grupos 1 a 5 (≈ USD 1,0) y precios | 2 (con datos reales), 3, 4, 5 | el código de 2, 4 y la forma del reporte, offline con fakes |
| Permiso de subir ramas y PRs de prueba (grupo 8) | 4 (la parte "fallar de verdad") | la lógica del gate con reportes sintéticos y el contrato del YAML |
| Tu veto de repos y de ecosistemas | 1 | el captador con `gh` falso |
| `git fetch`, `main` local al día, `gh` autenticado | todas | — |

**Mi pedido ahora:** tu respuesta a las 13 preguntas (o "tu recomendación"), los precios (o dónde están) y la aprobación
del spike por partes.

---

## Apéndice A — Runbook de (h) e (i): esquema (se arma bien en (h))

Todo desde el worktree de (h), con `.env` copiado y **sin imprimir claves**; los bloques de PowerShell se parsean antes de entregarlos.

1. `git switch -c p4/live-data origin/main` (en su worktree) y `uv sync`.
2. **Estimar:** `evals run --variant all --sources lab,public,injection --estimate` (cuenta tokens con tiktoken y
   precios; no llama a nadie; imprime el costo esperado y lo compara con el tope).
3. **Grupo 1:** `evals run … --live --max-cost-usd 0.40 --max-calls 400` → `evals/answers/`. Anotar gasto real.
4. **Grupo 2:** tres veces `evals run --variant llm+hints … --live --no-cache-read --max-cost-usd 0.10`; calcular σ de
   macro-F1 (`evals variance`).
5. **Tus etiquetas:** `evals calibrate sheet` → `evals/calibration/sheet.md` (**fuera de git** hasta que esté
   llena); la llenás; `evals calibrate score` → acuerdo, kappa, matriz.
6. **Grupo 3:** elegir juez, correr `evals run --judge … --live` con tope.
7. **Grupo 4:** `pytest -m live tests/live/test_live_injection.py` y el eval de inyección: 0 ganados.
8. `evals baseline --accept`, `evals readme-table`, `evals check` con la clave **borrada** del entorno → 0; anotar tiempo.
9. PR (h). Después (i): rama de prompt malo → `evals.yml` → código 3; llenar sus respuestas → código 1; rama con la
   entrada fabricada → código 2; PR bueno → verde; cerrar y borrar las ramas.

| Evidencia que se anota | Dónde |
|---|---|
| Gasto real por grupo y total, contra la estimación | SPEC §13 P4 y D-108 |
| σ de macro-F1 en 3 corridas | D-105 |
| Acuerdo del juez (exacto, kappa, matriz) y el modelo elegido | `evals/calibration/` y D-104 |
| Ids de runs de `evals.yml`, códigos de salida y tiempo del job | SPEC §13 P4 casilla 4 |
| Tabla del README y la línea de NFR-03 | `README.md`, D-110 |

## Apéndice B — Hoja de etiquetado y guía (esquema)

**Hoja de casos públicos (30):** un archivo por tanda en `plans/p4-labels/` (fuera de git), un bloque por candidato:
`id` opaco; repo y nombre del paso fallado; las ≈ 40 líneas redactadas alrededor del error; el resumen de una línea del
arreglo y la lista de archivos tocados; **espacios** para: `category` (una de las 8), `cause` (una oración), `evidence`
(líneas, opcional), `flaky` (sí/no), `discard` (sí/no, con motivo) y `confidence_of_label` (alta/media/baja). Para 10
bloques (marcados solo en mi copia) el borrador **no** se imprime hasta que devolvés la hoja.

**Guía (`LABELING.md`, resumen):** (1) la categoría es la de la causa raíz que **corrige el arreglo**, no la del síntoma;
(2) un gate de lint o de tipos que falla es `broken_test` (D-13); un `terraform validate` roto es `config_or_secrets`;
una imagen o un paquete que no se resuelve es `dependency`; sin memoria/disco/runner es `infra_or_deploy`; DNS,
conexión rechazada o límite de tasa es `network`; (3) `flaky` solo con re-run del mismo sha en verde (H2) o un texto
explícito de reintento; (4) `unknown` si ni el log ni el arreglo alcanzan; (5) descartar si el verde no corrige la
falla (cambio no relacionado, revert del arreglo, falla ambiental que pasó sola sin ser H2).

**Hoja del juez (20 + 5 de reserva):** por par: id opaco; `expected.cause`; `fix.summary`; la respuesta (`probable_cause`
y `suggested_fix`); un espacio `score` (0/1/2) y `nota`. La escala se imprime arriba con 3 ejemplos escritos para la hoja, distintos de
los 6 del prompt del juez (si no, calibraríamos al juez contra su propio prompt).
