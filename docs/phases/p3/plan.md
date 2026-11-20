> **Original working document, written in Spanish (rioplatense) for the owner and kept untouched below this line.** It is a planning draft: where it differs from `SPEC.md`, `DECISIONS.md` or the code, those are the authority (see the phase `README.md` for the differences).

# P3 — Comandos, sugerencias y configuración por repositorio: plan (v1)

> Solo plan, sin código. No commiteado. Escrito el 2026-11-19 sobre `main` en `7a9f1d0`.
> Base: SPEC §1, §4.3, §5, §6, §7.1, §8, §9.4, §9.6, §9.7, §11, §12, §13 (P3 y P4), §14, FR-19 a FR-21, y DECISIONS
> D-17, D-19, D-21, D-22, D-26, D-56, D-64, D-69 a D-76, D-81 a D-84, D-88. Modelo de formato: `plans/p2.md`.
> Los números D-89 a D-97 son provisionales: se asignan en el orden de merge.
> Nada de esto empieza hasta que apruebes el plan; el spike (§1) espera tu "andá", parte por parte.

## Estado (2026-11-20, v1.1)

- **Respuestas del dueño:** "tu recomendación" a las 10 preguntas de §7: Issues write con tokens separados, config
  inválida apaga el LLM, comando limitado en silencio, 😕 sin runs, sin segunda reacción, sugerencias también en runs
  normales, 3 runs por comando, `comment: false` deja el sticky, `ignore_workflows` ignora también `explain`, spike
  aprobado.
- **Spike corrido (partes B y C):** informe en `plans/p3-spike.md`, fixtures en `plans/p3-spike/github/`. La parte E
  (pantalla de la app) la tiene que mirar el dueño.
- **Cambios al plan por lo medido:** (1) R-P3-5 se resuelve a medias: los runs de un PR de fork **sí** salen por
  `head_sha`, pero con `pull_requests` vacío y a veces `action_required` → D-91 empareja por `head_sha` +
  `head_repository`, nunca por `pull_requests`, y `action_required` no cuenta como fallo. (2) D-95: tope de 16 KB
  antes de parsear, detectar alias/anclas y **claves duplicadas** en el recorrido de eventos, capturar
  `RecursionError`. (3) D-94: el bloque de diff se acota en tokens (≈ 2 000), no solo en líneas; descartar la última
  línea vacía del `patch`. (4) B3 no se puede confirmar con el token de `gh`: los permisos por endpoint se confirman
  en (g).
- **Parte E hecha (2026-11-20, vista por el dueño):** *Issues* = "No access"; *Issue comment* no aparece en
  *Subscribe to events*; ninguna entrega de `issue_comment` en *Recent deliveries*. Confirma A1: sin el permiso de
  Issues, GitHub ni ofrece el evento, así que D-97 es necesario y el cambio de la app es el primer paso de (g).

## Punto de partida (lo que encontré leyendo)

**Lo que ya existe y P3 solo tiene que conectar:**

- `core/policy.py` ya conoce `explain` y `on_demand`; `DiagnosisContext.explain` y `PolicyInput.explain` existen y
  nadie los pone en `True`. `JobKind.EXPLAIN` existe y va al `placeholder_handler`.
- `RepoConfig` (`comment`, `llm`, `ignore_workflows`, `extra="forbid"`) y el puerto `RepoConfigSource.load`
  existen **sin adapter**. `DiagnoseFailedRun` usa `DEFAULT_REPO_CONFIG` fijo (dos lugares: `_comment` y
  `_diagnose_all`).
- La tabla `command_invocations` **ya está creada** en la migración `0001` (PK `(repository_id, comment_id)`, índice
  `(repository_id, pr_number, created_at DESC)`), sin ningún código que la use. P3 probablemente **no necesita
  migración**.
- `Reporter.post_suggestion` y `Reporter.react` están en el puerto y en `GitHubReporter` como
  `NotImplementedError("P3")`. `Diagnosis.suggestion`, el esquema del LLM (`LLMSuggestion`), `checked_suggestion`
  (ruta relativa, sin `..`, ≤ 30 líneas) y el caso de inyección `path-traversal-suggestion` existen; **nada
  renderiza ni publica la sugerencia** (`render.py` no la menciona) y la caché la descarta.
- `tests/fixtures/webhooks/issue_comment_created.json` (sintético) existe; `webhooks.route_event` no maneja
  `issue_comment`.
- Los filtros `NOT r.is_fork` de `similar` y `cached_diagnosis` están en SQL (`store.py` líneas ~202 y ~219) y en
  memoria; hay tests de contrato por fork.

**Lo que encontré y cambia el diseño:**

1. **La app de desarrollo hoy no recibe `issue_comment`.** El fixture real `tests/fixtures/github/app.json` (spike de
   P1c) dice: `events: ["workflow_run"]` y permisos `actions: read, checks: write, contents: read, metadata: read,
   pull_requests: write`. Falta suscribir el evento y, según la documentación de GitHub (leída hoy, ver §1 parte A),
   **`issue_comment` exige el permiso Issues (read)** y **crear una reacción en un comentario exige Issues (write)**,
   no Pull requests. D-21 y SPEC §9.6 suponían lo contrario ("no Issues"; "las reacciones las cubre Pull
   requests"). Es la contradicción más grande del plan (D-97). `contents: read` **ya está** en la app.
2. **Un intento analizado es final** (D-69: el primer escritor gana). Un run de fork ya tiene su checkpoint con
   reglas (`fork_policy`) cuando llega el comando. `explain` no puede "analizar de nuevo": tiene que **mejorar** el
   diagnóstico publicado de un intento ya guardado, o crear el checkpoint si todavía no existe (D-93).
3. **El excerpt redactado ya está en la base** (`failures.excerpt`). `explain` puede llamar al LLM sin volver a bajar
   el log (más rápido, y funciona aunque el log haya vencido), reconstruyendo el `JobAnalysis` desde lo guardado.
4. **`issue_comment` no trae la cabeza del PR.** Hay que pedir `GET /pulls/{n}` (head sha, repo de la cabeza,
   estado) y listar los runs por `head_sha` (D-91).
5. **El LLM no ve el código.** Hoy propone `suggestion {path, start_line, end_line, replacement}` solo mirando el
   log: reemplazaría líneas que nunca leyó. Para que una sugerencia tenga sentido hay que darle las líneas del diff
   del PR (un bloque más no confiable, `diag-v3`; D-94).
6. **El flujo de fork cambia dos frases de SPEC:** §9.3 dice "A fork run never reaches the model" y D-81 dice que un
   run de fork no se sirve de la caché "en las dos direcciones". Con `explain` el primero deja de ser cierto, y hoy
   el código **sí leería la caché** para un fork con `explain=True` (la guarda `_cached` solo mira la política).
   D-92 lo cierra.
7. **Configuración inválida: D-26 dice "reemplazada por los valores por defecto".** Un `llm: of` (error de
   tipeo) volvería a `auto` y mandaría logs a OpenAI justo en el repo que quiso apagarlo. Propongo fallar cerrado
   para el LLM (D-95); modifica D-26.
8. **Fallar en silencio es mala UX, pero SPEC §9.7/D-22 lo piden** para los rechazos. Distingo "rechazado por
   autorización" (silencio total, como pide D-22) de "aceptado pero no hay nada que hacer" (D-91).

## 0. Resumen

| Paso | Título | Tamaño | Depende de | D-NN | Cierra en §13 P3 |
|---|---|---|---|---|---|
| 0 | Spike de solo lectura (no es PR) | S | tu aprobación | — | — |
| a | Permisos y eventos de la app (docs + tu cambio manual) | S | spike A/E | D-97 | (parcial) casilla 5 |
| b | `RepoConfigSource`: archivo en la rama por defecto, política, `comment`, `ignore_workflows` | M | spike C | D-95, D-96 | casilla 4 (offline) |
| c | Webhook `issue_comment`, autorización, límite 1/5 min, reacción 👀 | M | a (para vivo) | D-89, D-90 | casilla 2 (offline) |
| d1 | `explain` en el caso de uso y mejora del diagnóstico publicado (core + store) | L | b | D-92, D-93 | — |
| d2 | Job `explain`: PR, runs de la cabeza, runs hijos, handler y cableado | M | c, d1 | D-91 | casilla 1 (offline) |
| e | Sugerencias: diff del PR, `diag-v3`, validación, review comment | L | d2 | D-94 | casilla 3 (offline) |
| f | Inyección nueva (comentarios, config, diff, sugerencias) y su oráculo | M | e | (D-64 ampliada) | — |
| g | Verificación en vivo, workflows del lab y runbook | S | todo | — | casillas 1 a 5 en vivo |

**Por qué esta división (y qué cambié de tus candidatas):**

- *Permisos* (a) lo saqué como paso propio y primero: bloquea todo lo vivo y es la única pieza que depende de que
  vos cambies la app. Es un PR casi solo de documentos (D-21, D-97, SPEC §9.6) más el `ping` de verificación.
- *Configuración* (b) va antes del comando porque `explain` tiene que respetar `llm: off` y `ignore_workflows`
  desde el primer día; si no, el comando nacería saltándose la política.
- *Webhook + límite + reacción* (c) lo separé del *job de explain* (d): (c) se puede mergear solo (la app no recibe
  el evento hasta que vos lo suscribas, y el job termina con 👀 y nada más) y se revisa sin tocar el flujo de
  diagnóstico.
- *explain* lo partí en d1 (core y store: el cambio delicado, "mejorar lo publicado", sin GitHub) y d2 (el
  cableado a GitHub: PR, listado de runs, jobs hijos). Es el corte que dejé previsto como en P2 (e2).
- *Sugerencias* (e) dependen de explain para poder probarse de punta a punta, y tocan prompt, esquema, renderer,
  reporter y el adapter de pulls: es el más grande y el que más superficie de ataque agrega.
- *Inyección* (f) es un PR propio porque es lo que más te interesa revisar; cada PR anterior trae sus tests
  mínimos, y (f) consolida la suite y el oráculo.

**Orden de merge y paralelismo.**

```
spike ─ a ───────────────────────────────┐
        b ── d1 ──┐                      │
        c ────────┴── d2 ── e ── f ── g ─┘
```

| Par | ¿En paralelo? | Archivos que chocan |
|---|---|---|
| a y el resto | sí | solo docs (`DECISIONS.md`, `SPEC.md` §9.6, `CLAUDE.md`) |
| b y c | sí | `core/models.py`, `core/ports.py` (dos puertos distintos, hunks separados), `app/container.py` |
| b y d1 | **no**, d1 espera a b | los dos reescriben `DiagnoseFailedRun` (`_comment`, `_diagnose_all`, la carga de config) |
| c y d2 | **no**, d2 espera a c | `worker/handlers.py`, `app/container.py`, `adapters/github/webhooks.py` |
| d1 y c | sí | solo `core/models.py` y `core/ports.py` (hunks separados) |
| e y d2 | **no** | `diagnose_failed_run.py`, `core/ports.py`, `adapters/github/reporter.py`, `pulls.py` |
| f | después de e | `evals/runner/injection.py`, `markdown_threats.py`, `tests/unit/test_injection.py` |

**Migraciones.** Ninguna prevista: `command_invocations` ya existe, la mejora del diagnóstico usa las columnas de
`diagnoses` que hay (`published`, `cached_from`) y la configuración vive en memoria. Si (d1) descubre que la tabla
necesita otra columna, es la `0007` y se avisa antes (los worktrees comparten la base de test: memoria del proyecto).

**Reglas de trabajo para todos los PRs** (las de P1c y P2): una rama por cambio cortada de `origin/main`, un PR
propio; CI verde (lint, tipos, tests con Postgres, cobertura de `core/` ≥ 90 %); D-NN, SPEC y `CLAUDE.md` en el
**mismo** PR; commits en inglés, sin `Co-Authored-By`; nada de secretos en código ni en fixtures (los fixtures de
GitHub se sanitizan y los revisás vos antes de entrar); `plans/` y `.env` nunca se commitean; ninguna casilla de §13
se marca sin evidencia observada. **Nuevo en P3:** el cuerpo de un comentario de PR y el archivo de configuración
son entrada no confiable, igual que un log (va a `CLAUDE.md` "Rules"). Hasta (c) el webhook contesta 204 a
`issue_comment`, así que `main` funciona igual que hoy después de cada paso.

---

## 1. Paso 0 — Spike de solo lectura (2 a 3 h, no es PR)

**Objetivo:** contestar con mediciones lo que no se deduce leyendo, y grabar los fixtures que los tests de (b), (c),
(d2) y (e) necesitan. **No se corre hasta que lo apruebes** (y cada parte por separado).

**Qué es "solo lectura" acá:** no escribe en el repositorio (artefactos al scratchpad y a `plans/p3-spike/`, sin
commitear), no escribe en GitHub (solo `GET` con tu sesión de `gh`), no abre `.env`, no usa el JWT ni la clave de
la app, no usa ninguna clave de OpenAI, no toca la base. Lo que ya leí de la documentación pública (parte A) **ya lo
hice** mientras planificaba: son lecturas de páginas públicas, sin credenciales.

### Parte A — Documentación de GitHub (ya leída; falta contrastarla con la realidad)

| # | Qué dice la documentación (leída el 2026-11-19) | Consecuencia | Cómo se contrasta |
|---|---|---|---|
| A1 | El evento `issue_comment` exige **Issues: read** (solo nombra Issues). Acciones: `created`, `edited`, `deleted`, `pinned`, `unpinned` | La app necesita un permiso que hoy no tiene (D-97); solo se actúa en `created` (D-89) | E1 (pantalla de la app) y la primera entrega real en (g) |
| A2 | `POST …/issues/comments/{id}/reactions` exige **Issues: write** (fila verbatim, token de instalación). Valores: `+1 -1 laugh confused heart hooray rocket eyes`; 200 si ya existía, 201 si se creó | El token de explain necesita `issues: write` (D-97) | B3 (`x-accepted-github-permissions` del `GET` hermano) y (g) |
| A3 | `POST …/pulls/{n}/comments` exige Pull requests: write; `GET …/pulls/{n}/files` Pull requests: read (de resúmenes, **no verbatim**) | Ya cubierto por `pull_requests: write` | B3 |
| A4 | `GET …/actions/runs` acepta `head_sha`, `status` (también valores de conclusión), `per_page ≤ 100`, tope de 1 000 resultados con filtros | Alcanza para D-91 | B1 |
| A5 | `GET …/pulls/{n}/files`: máximo 3 000 archivos, 100 por página; `patch` opcional (la página no dice cuándo falta) | Un `patch` ausente = sin sugerencia en ese archivo (D-94) | B2 |
| A6 | `GET …/contents/{path}` **sin `ref` usa la rama por defecto**; ≤ 1 MB trae el contenido; `type` es `file`/`symlink`/`submodule`; un directorio devuelve lista | El adapter de configuración no manda `ref` nunca: la garantía de "solo la rama por defecto" es estructural (D-95) | B4 |
| A7 | Límite secundario de contenido: 80 por minuto y 500 por hora; la reacción no tiene límite propio documentado | Un explain son ~3 escrituras + reacciones: lejos del tope | — |
| A8 | **No dice** si los runs de PRs de fork aparecen en el listado de la base, ni qué pasa en una instalación existente cuando la app suma un permiso o un evento | Se mide en B1 (parcial: sin fork) y se resuelve en (g) con tu cuenta/organización | E1, (g) |
| A9 | Un review comment con la línea fuera del diff: la página solo lista 403 y 422 ("validation failed… spammed"); **no menciona `suggestion`** | El texto exacto del 422 solo se ve escribiendo: se mide en (g) y el adapter trata cualquier 422 como "solo texto" (D-94) | (g) |

### Parte B — Lecturas con `gh api` (solo `GET`), sobre el lab y este repositorio

| # | Llamada | Para decidir |
|---|---|---|
| B1 | `GET /repos/{o}/{r}/actions/runs?head_sha=<sha de un PR existente del lab>&per_page=100`, con y sin `status=failure`; comparar los campos del ítem con el `workflow_run` del webhook (`id`, `name`, `run_attempt`, `workflow_id`, `head_sha`, `head_branch`, `event`, `conclusion`, `head_repository`, `pull_requests`, `updated_at`) | D-91: ¿el ítem alcanza para armar el `RunRef` y el `DiagnoseRunPayload`? ¿trae `run_attempt` del último intento? ¿un run de un solo workflow con varios intentos aparece una vez? |
| B2 | `GET /repos/{o}/{r}/pulls/{n}` (head.sha, head.repo, state, merged, `draft`) y `GET …/pulls/{n}/files?per_page=100` de 3 a 5 PRs reales de este repo (uno con renombre si existe, uno grande); guardar los `patch` | D-94: forma del `patch`, hunks, `\ No newline at end of file`, renombres, cuándo falta `patch` |
| B3 | `gh api -i` de `GET …/issues/comments/{id}/reactions`, `GET …/pulls/{n}/comments` y `GET …/pulls/{n}/files`, leyendo `x-accepted-github-permissions` | D-97: confirmar el permiso que GitHub dice aceptar por endpoint (el header es por endpoint, no depende del token) |
| B4 | `GET …/contents/.github/cicd-doctor.yml` (esperado 404: forma del error), `GET …/contents/.github/workflows/ci.yml` (forma: `type`, `encoding`, `size`, `content` con saltos de línea) y `GET …/contents/.github` (lista) | D-95: forma de lo que parseamos, tamaños y `type` |
| B5 | `GET /repos/{o}/{r}/pulls/{n}/comments` de un PR sin comentarios y de uno con comentarios de revisión (forma, `user.login` del bot, `commit_id`, `original_line`) | D-94: la búsqueda por marcador entre los comentarios de revisión del bot |
| B6 | Cuántos PRs de fork hay en repos públicos de ejemplo (solo lectura de un repo público ajeno) y la forma de su `head.repo`/runs | A8: el ítem de run de un PR de fork (sin escribir nada: lectura pública) |

### Parte C — Local, sin red (Python descartable en el scratchpad)

| # | Qué mide | Para decidir |
|---|---|---|
| C1 | `yaml.safe_load` contra: una "bomba" de alias (≤ 16 KB), etiquetas `!!python/object`, claves duplicadas, un archivo de 1 MB, un documento múltiple, BOM y CRLF. Tiempo y memoria; y si un recorrido por **eventos** (`yaml.parse`, sin construir nada) detecta alias y cuenta nodos antes de cargar | D-95: tope de tamaño y regla "sin alias" |
| C2 | Un prototipo (en el scratchpad, no en el repo) del conjunto de líneas del lado nuevo que dibuja un `patch`, corrido sobre los 3-5 `patch` reales de B2 más casos sintéticos: hunk único, varios hunks, línea sin salto final, renombre, archivo agregado | D-94: la regla "[start, end] dentro de **un** hunk" y su costo |
| C3 | Tamaño del prompt de `diag-v3` con un bloque de diff de 40 / 80 líneas, con el contador real de tiktoken | D-94: tope del bloque del diff y del costo (NFR-03) |

### Parte E — Contigo (lectura de pantalla, nada se escribe)

| # | Qué | Para decidir |
|---|---|---|
| E1 | En la página de la app de desarrollo → *Permissions & events*: qué eventos ofrece GitHub **hoy** (sin tocar nada) y cuáles se habilitan con "Issues: read" / "Issues: write" (se lee, no se cambia). Y en *Advanced → Recent deliveries*: ¿llegan entregas de `issue_comment` hoy? (esperado: no) | D-97: confirmar A1 con la pantalla real |

### Fixtures que se guardan (sin commitear, para tu revisión)

- `plans/p3-spike/github/`: las respuestas de B1, B2, B4 y B5 sanitizadas con el sanitizador de P1c (dueño →
  `acme`, ids y SHAs consistentes, sin logins reales, sin URLs firmadas). Los `patch` reales se guardan solo si son
  de este repo (sin secretos); pasan a `tests/fixtures/github/` en (e) y (b) si los aprobás.
- `plans/p3-spike.md`: el informe con la tabla "pregunta → respuesta → consecuencia", los números de B, C y la
  lista de lo que el spike no pudo medir.

**Lo que el spike no puede contestar:** el 422 de una línea fuera del diff y la forma del bloque `suggestion` en la
UI (escribir en GitHub); los runs y la cabeza de un PR de **fork** (no hay organización); cuándo GitHub entrega
`issue_comment` a una instalación que todavía no aceptó el permiso; la latencia de punta a punta (se mide en (g));
cuánto sugiere el modelo real (varía).

---

## 2. PRs

### (a) Permisos y eventos de la app — `p3-permissions`

**Alcance**
- Documentación y decisión, **sin código de producción**: D-97 (D-21 modificado), SPEC §9.6 y §13 (el texto de la
  casilla 5), `CLAUDE.md`.
- `adapters/github/auth.py`: dos conjuntos de permisos nuevos, `EXPLAIN_PERMISSIONS` (`actions: read`,
  `checks: write`, `pull_requests: write`, `issues: write`) y `CONFIG_PERMISSIONS` (`contents: read`). El de
  diagnóstico **no cambia**: un permiso que la instalación todavía no aceptó no puede romper los diagnósticos que
  hoy funcionan (un token pedido con un permiso que la instalación no tiene da 422, `_TOKEN_REFUSED`).
- `lab/README.md`: la sección de preparación de (g) con los pasos manuales de la app (tu parte).

**Tests (offline, rápidos):** la solicitud del token lleva exactamente el conjunto pedido (`tests/adapters/test_github_auth.py`);
un 422 del token de explain se mapea a `InstallationUnavailable`-like con el mensaje "el permiso no está
aceptado" sin afectar al token de diagnóstico.

**Terminado cuando:** D-97 escrito con la evidencia de B3/E1 y la entrega real marcada como pendiente de (g);
SPEC §9.6 alineado; los dos conjuntos de permisos probados.

**§13:** casilla 5 **solo parcialmente** (se tilda en (g) con la entrega observada). Tu cambio en la app (Issues
write + suscripción a `issue_comment`) es la única acción manual y está en la lista de consentimientos (§6).

### (b) Configuración por repositorio — `p3-repo-config`

**Alcance**
- Adapter `adapters/github/repo_config.py::GitHubRepoConfigSource` (puerto `RepoConfigSource`, que pasa a devolver
  `LoadedConfig(config, state, problems)`; `state ∈ {default, file, invalid, unreadable}`, `problems` = rutas de
  campos, nunca valores). `GET /repos/{o}/{r}/contents/.github/cicd-doctor.yml` **sin `ref`** y con
  `CONFIG_PERMISSIONS`; 404 → `default`; tamaño > 16 KB, `type != file`, `encoding != base64` → `invalid`; 403/422
  por permiso no aceptado → `unreadable` (se comporta como `default`, con un WARNING); 5xx y límites →
  `TransientError` (el job reintenta; **no** se cae a los valores por defecto en silencio).
- Parseo en `core/repo_config.py` (puro): recorrido de eventos de PyYAML (`yaml.parse`) que rechaza alias, anclas
  y más de N nodos antes de `yaml.safe_load`; luego `RepoConfig` con `extra="forbid"`, `ignore_workflows` ≤ 50
  nombres de ≤ 120 caracteres, sin controles. Archivo vacío = `default`.
- Caché en proceso con vencimiento (`CONFIG_TTL_S`, 300 s; 404 se cachea 60 s) por `repo_id`, con el reloj
  inyectado; una lectura por job aunque el job la use varias veces. No hay invalidación por `push` (D-95).
- **Cableado en `DiagnoseFailedRun`:** la configuración se carga al principio (después de `_ensure_rows`, antes de
  cualquier escritura de GitHub). `ignore_workflows` (nombre exacto del workflow): el job termina `done` sin check,
  sin comentario, sin checkpoint, sin reserva (como el gate de D-73). `llm` entra a `DiagnosisContext.repo_llm`.
  `comment: false` salta `_comment`. Un archivo `invalid` → `repo_llm = off` y una línea en el check ("la
  configuración del repo es inválida: <campos>; LLM apagado"). La reescritura de resolución de `RecordRunOutcome`
  (D-84) también respeta `comment: false` y `ignore_workflows`.
- Un motivo nuevo `on_demand` para `FallbackReason` (hoy el texto de `config_off` dice "LLM disabled by the
  repository configuration", engañoso con `llm: on_demand`): "LLM on demand: comment `/cicd-doctor explain`".
  `fallback_reason` es `text` en la base: sin migración.
- `app/settings.py` y `.env.example`: `CONFIG_TTL_S`. `app/container.py`: el adapter y la fábrica.

**Archivos:** `core/models.py` (`LoadedConfig`, `FallbackReason`), `core/ports.py`, `core/repo_config.py` (nuevo),
`core/render.py` (texto), `core/usecases/diagnose_failed_run.py`, `core/usecases/record_run_outcome.py`,
`adapters/github/repo_config.py` (nuevo), `adapters/memory/` (un `FakeRepoConfig` programable), `app/{settings,container}.py`,
`.env.example`, `tests/github_fake.py` (ruta de `contents`), fixtures de B4.

**Tests**
- **Offline, rápidos (`unit`):** parseo (válido, vacío, claves desconocidas, tipos mal, alias, etiquetas,
  duplicadas, enorme, BOM/CRLF, nombres con caracteres raros); `LoadedConfig` por estado; política: `invalid` →
  `config_off`; `on_demand` sin explain → motivo `on_demand`; `comment: false` no escribe comentario pero sí el
  check; `ignore_workflows` no escribe nada y no reserva presupuesto; la resolución no reescribe un comentario con
  `comment: false`; caché con `FakeClock` (vence, 404 corto); un error transitorio no cae a defaults.
- **Adapter (`tests/adapters`, `FakeGitHub`):** la petición **no lleva `ref`** (se afirma sobre lo grabado); un
  archivo en la cabeza de un PR de fork distinto del de la rama por defecto no cambia el resultado (el fake sirve
  contenidos distintos por `ref` y el test falla si el adapter llegara a pedir uno); tokens: `contents: read` y
  nada más; 404, 403, 422 de token, 5xx, tamaño, `type=dir`; el cuerpo de un error nunca llega a un mensaje.
- **Postgres:** ninguno (no hay tabla). **Contrato:** no aplica.
- **Offline (fixtures reales):** `tests/adapters/test_github_fixtures.py` carga los de B4.

**Terminado cuando:** CI verde; el worker arranca sin archivo en ningún repo igual que hoy (defaults); D-95 y D-96
escritos; SPEC §2 (FR-21), §4.3, §5.2, §9.1 (fila "Malicious config"), §9.4 y §13 alineados; `CLAUDE.md`.

**§13:** casilla 4 (`comment: false`, y "el mismo archivo cambiado solo en la cabeza de un PR no tiene efecto") —
**solo la parte offline**; la viva en (g).

### (c) Webhook `issue_comment`, autorización, límite y 👀 — `p3-command-ingest`

**Alcance**
- `adapters/github/webhooks.py`: `issue_comment` con `action == "created"` y `issue.pull_request` presente se
  normaliza a un `NewJob(kind=EXPLAIN)` con `ExplainPayload` (modelo nuevo: `installation_id`, `repo_id`,
  `repo_full_name`, `private`, `default_branch`, `account_login/type`, `pr_number`, `comment_id`, `actor_login`,
  `received_at`; **nunca** el cuerpo del comentario ni JSON crudo, D-56). Clave de dedupe
  `explain:{comment_id}`. El webhook decide **solo con lo que trae el payload**: primera línea exactamente
  `/cicd-doctor explain` (D-89), `author_association ∈ {OWNER, MEMBER, COLLABORATOR}`, el autor no es un bot
  (`comment.user.type != "Bot"`), `action = created`. Todo lo demás → 204 sin tocar la base (`edited`, `deleted`,
  issues que no son PRs, comandos en bloques citados/código, otras apps). No hay llamadas a GitHub en el camino de
  la petición (NFR-01).
- Puerto nuevo `CommandLog` (`claim(repo_id, pr_number, comment_id, actor, now, window) -> bool`) sobre la tabla
  `command_invocations`: transacción con `pg_advisory_xact_lock` por `(repo, pr)`; si ya hay fila de **ese**
  `comment_id` → `True` (un reintento del mismo comando); si hay otra fila del mismo PR dentro de la ventana →
  `False`; si no, inserta con la hora del `Clock` inyectado y devuelve `True`. Implementaciones en memoria y
  Postgres, con contrato en las dos (D-51). La purga (D-85) suma a `command_invocations` una retención (30 días).
- `Reporter.react(comment: CommentRef, reaction)`: `CommentRef` gana `repo_id` (el token es por repositorio);
  `GitHubReporter.react` hace `POST …/issues/comments/{id}/reactions` con `EXPLAIN_PERMISSIONS`; 200/201 son éxito; 404 = comentario borrado
  (`CommentGone`, no es un error del job); un 403 por permiso no aceptado → `PermanentError` con el motivo.
- `worker/handlers.py::explain_handler` y `core/usecases/explain_command.py` (primera versión): valida el payload
  (inválido = `PermanentError`), **carga la configuración** (`llm: off` o `LLM_ENABLED=false` → no reserva ni
  reacciona más de lo que dice D-91), hace `claim`; si es `False` termina `done` sin efectos (silencio, D-90);
  si es `True` reacciona 👀 y termina (el trabajo real llega en d2).
- `app/settings.py`: `COMMAND_WINDOW_S` (300). `app/container.py`: el registro de `explain`.

**Archivos:** `adapters/github/webhooks.py`, `core/models.py`, `core/ports.py`, `core/usecases/explain_command.py` (nuevo),
`adapters/postgres/command_log.py` (nuevo), `adapters/memory/command_log.py` (nuevo), `adapters/github/reporter.py`,
`worker/handlers.py`, `app/{settings,container}.py`, `core/usecases/purge.py`, `tests/fixtures/webhooks/` (casos
nuevos, sintéticos).

**Tests**
- **Offline, rápidos:** matriz del webhook (tabla: primera línea exacta, con espacios, con texto detrás,
  en segunda línea, dentro de ``` o `>`, mayúsculas, caracteres de ancho cero y homoglifos; las seis asociaciones;
  `Bot`; `edited`/`deleted`; issue que no es PR; sin `installation`; payload sin campos); `ExplainPayload` no
  contiene el cuerpo; ventana de 5 minutos con `FakeClock` (4:59 rechaza, 5:01 acepta); reintento del mismo
  comando acepta; el comando rechazado no reacciona; `llm: off`.
- **Contrato (`tests/contract/test_command_log.py`, memoria y Postgres):** los casos de arriba más aislamiento por
  repositorio y por PR, y orden estable.
- **Postgres (`integration`):** dos engines reclamando a la vez el mismo PR: exactamente uno gana; 40 reclamos
  concurrentes sobre 4 PRs dejan 4 filas; `test_webhook_postgres.py` (un `issue_comment` firmado crea un job y un
  duplicado no); la purga borra filas de más de 30 días.
- **Adapter (`FakeGitHub`):** `react` (200, 201, 404, 403 con permiso nombrado, 5xx); el cuerpo de un error no se
  filtra; el token lleva `issues: write`.
- **E2E offline:** webhook firmado → worker → `FakeGitHub`: una reacción `eyes`, ningún check ni comentario.

**Terminado cuando:** CI verde; con la app sin la suscripción nada cambia; D-89 y D-90 escritos; SPEC §2 (FR-19),
§6, §9.7, §12 alineados.

**§13:** casilla 2 ("no reaction, no job" y "un segundo comando dentro de 5 minutos se ignora") — parte offline.

### (d1) `explain` en el caso de uso y mejora de lo publicado — `p3-explain-core`

**Alcance**
- `DiagnoseRunPayload` gana `trigger: Literal["automatic", "explain"] = "automatic"`, `pr_number` y
  `command_comment_id` opcionales (los jobs viejos siguen validando). `explain=True` entra al `DiagnosisContext`.
- `DiagnoseFailedRun.run` en modo explain (D-93):
  1. guardas: igual que hoy (intento más nuevo, filas), más `ignore_workflows` y la configuración;
  2. si el intento **ya tiene checkpoint**: cada falla cuyo diagnóstico publicado es `rules` se reanaliza **desde
     lo guardado** (`JobAnalysis` reconstruido del `excerpt`, la firma y las reglas recomputadas del excerpt: un
     helper nuevo `core/analysis.py::analysis_from_stored`), con `explain=True`; las que ya tienen `llm`/`cache`
     **no gastan nada**;
  3. si no hay checkpoint: el flujo normal de `_analyze` con `explain=True` (descarga logs) y el checkpoint lo
     escribe el explain;
  4. el resultado se guarda con un método nuevo del store, `FailureStore.upgrade_published(run_id, attempt,
     upgrades)`: en **una** transacción, por falla, pone `published = false` en la fila vigente, inserta la fila
     `llm` con `published = true` y su uso (el costo vive en la fila publicada, D-69) y devuelve el registro leído de
     nuevo. Idempotente: una falla que ya tiene una fila publicada `llm`/`cache` no cambia;
  5. check run y comentario se republican **desde lo guardado**, bajo `publication_lock`, con la guarda
     `(run_id, attempt)` (el mismo intento no cuenta como "más nuevo"); el comentario usa `payload.pr_number`
     (el PR del comando) cuando el run no tenía PR guardado.
- **Política para fork + explain (D-92):** la guarda `_cached` **no lee la caché** si `run.is_fork`; el prompt de
  un explain de fork va **sin** `similar` (el reporte sí las muestra, como hoy, D-82); la política no cambia
  (`explain` ya salta la regla 3 y la 4; `llm: off` y el interruptor siguen mandando).
- `handlers.diagnose_run_handler`: con `trigger = explain`, **no** publica "Could not diagnose" al rendirse
  (pisaría un diagnóstico bueno); solo registra y relanza.
- `app/settings.py`: `EXPLAIN_MAX_RUNS` (3).

**Archivos:** `core/models.py`, `core/ports.py` (`upgrade_published`), `core/analysis.py`,
`core/usecases/diagnose_failed_run.py`, `core/usecases/report.py`, `adapters/postgres/store.py`,
`adapters/memory/store.py`, `worker/handlers.py`, `tests/contract/test_failure_store.py`, `tests/factories.py`.

**Tests**
- **Contrato (memoria y Postgres):** `upgrade_published` deja exactamente una fila publicada por falla; conserva la
  de reglas como no publicada; el costo queda en la fila publicada y `SUM(cost_usd)` es el gasto real; un segundo
  llamado no cambia nada; una falla inexistente → `KeyError`; la fila nueva de un run de fork **no** aparece en
  `cached_diagnosis` ni en `similar` de otro run (el test de envenenamiento de §4, R-P3-3); `run_analysis` vuelve
  igual a sí misma después de mejorar.
- **Offline, rápidos:** un fork con checkpoint de reglas + explain → una llamada al LLM por falla, el diagnóstico
  publicado pasa a `llm`, el check y el comentario cambian, **sin** bajar logs; sin checkpoint → baja logs; ya `llm`
  → cero llamadas; `budget` agotado → sigue `rules` con su motivo y no se rompe nada; `llm: off` → no llama; una
  carrera automático/explain (los dos intentan el checkpoint) deja un solo registro; un fork explain no lee la caché
  (contador) y su prompt no lleva `similar`; el reintento después de `upgrade_published` no vuelve a llamar al LLM
  ni a reservar; el give-up de un explain no pisa el check.
- **Postgres (`integration`):** `upgrade_published` bajo concurrencia (dos explain del mismo intento: una sola
  llamada al LLM gana, usando el ledger real y `FakeLLM` con contador); el flujo completo en `test_diagnose_postgres.py`
  con un run de fork; el índice de publicadas rechaza dos publicadas.
- **E2E offline:** `FakeGitHub` + `FakeLLM`: run de fork con reglas → explain → el comentario existente (mismo id)
  muestra el LLM.

**Terminado cuando:** CI verde; D-92 y D-93 escritos; SPEC §4.3, §5.2, §7.1, §9.3, §9.4 y §12 alineados (la frase
"A fork run never reaches the model" se corrige).

**§13:** ninguna sola (es la mitad de la casilla 1).

### (d2) Job `explain`, PR y runs de la cabeza — `p3-explain-job`

**Alcance**
- Puerto nuevo `PullRequestSource.get(repo, n) -> PullRequestInfo` (estado, `draft`, `head_sha`,
  `head_repo_full_name`, `head_branch`, `base_repo_id`, `merged`) y un puerto nuevo `RunSource.failed_runs(pr) ->
  list[RunSummary]` (o ampliación de `LogSource`; se decide en el PR): `GET /pulls/{n}` y `GET
  /actions/runs?head_sha=<sha>&per_page=100` (una sola página), con `EXPLAIN_PERMISSIONS`.
- `ExplainCommand.run` completo (D-91): `claim` → `react 👀` → PR (cerrado/mergeado → termina) → runs de la cabeza
  del PR → se queda con los de conclusión `failure`/`timed_out` cuyo `head_sha` y `head_repository` coinciden con la
  cabeza actual del PR → **el último run por workflow** (id mayor) → los **`EXPLAIN_MAX_RUNS` más recientes** →
  un `diagnose_run` hijo por run (`trigger = explain`, dedupe `explain:{comment_id}:{run_id}:{attempt}`, distinto
  de `diagnose:{run}:{attempt}` para que no lo trague el job automático). Sin runs fallados: reacciona
  `confused` y termina. Runs todavía en curso: se ignoran (el comando no espera; se vuelve a pedir).
- El `RunRef` de cada hijo sale del ítem del listado con `installation_id`/`repo` del comando; `is_fork =
  head_repository.id != repo_id` (cabeza borrada = fork, como el webhook).
- `explain_handler` y `container` cableados; el hijo usa el `diagnose_run_handler` con el flujo de (d1).
- Registro por comando de los tiempos (`seconds_since_command`) para medir el objetivo de 90 s.

**Archivos:** `core/ports.py`, `core/models.py`, `core/usecases/explain_command.py`, `adapters/github/pulls.py`,
`adapters/github/runs.py` (nuevo), `adapters/memory/github.py` (`FakePullRequests` y un `FakeRunSource`),
`worker/handlers.py`, `app/container.py`, `tests/github_fake.py`.

**Tests**
- **Offline, rápidos:** selección de runs (tabla: varios workflows, varios intentos, runs en curso, runs de otra
  cabeza, tope de 3, determinismo del orden); sin runs; PR cerrado; comentario borrado entre el claim y el 👀;
  reintento del job (no duplica hijos ni reacciones); hijos con claves distintas de las automáticas.
- **Adapter (`FakeGitHub`):** `GET /pulls/{n}` y listado con paginación forzada, 404, 5xx, límite de tasa; la
  consulta lleva `head_sha`; ítems de run reales (fixture B1) producen un `RunRef` igual al del webhook.
- **E2E offline:** webhook firmado → `explain` → 3 hijos → check y comentario editados; una **segunda orden dentro
  de 5 minutos** no crea nada; un fork con checkpoint de reglas; `FakeLLM` con contador (≤ 9 llamadas).
- **Postgres (`integration`):** el recorrido completo en `test_explain_postgres.py` con dos workers; un hijo muerto
  no afecta a los demás.

**Terminado cuando:** CI verde; D-91 escrito; SPEC §2 (FR-19), §7, §8 y §9.7 alineados; `CLAUDE.md`.

**§13:** casilla 1 ("👀 y un diagnóstico con LLM en 90 s, también en un fork") y, junto con (c), la 2 — **offline**.

### (e) Sugerencias — `p3-suggestions`

**Alcance**
- **El modelo ve las líneas que puede tocar (D-94).** Un puerto nuevo `PullRequestSource.diff(pr) -> PullDiff`
  (`GET /pulls/{n}/files`, hasta 30 páginas; por archivo: `filename`, `previous_filename`, `status`, `patch`). Un
  módulo puro `core/diff.py` convierte cada `patch` en hunks con el **conjunto de líneas del lado nuevo** y su
  texto. El prompt `diag-v3` suma un bloque `<untrusted_diff nonce="…">` (mismo nonce y mismo escapado que los
  otros dos bloques) con solo los hunks de **los archivos que el excerpt nombra** (rutas que aparecen en líneas de
  error y existen en el diff), con tope de líneas y de tokens (C3). Sin diff (no hay PR, un push a `main`, archivo
  sin `patch`, diff truncado) no hay bloque y `suggestion` es `null`. `PROMPT_VERSION = diag-v3`: la caché y la de
  los evals empiezan vacías a propósito (como en D-82).
- **Validación contra el diff** (`core/suggestions.py`, pura): la ruta del modelo debe ser **exactamente** el
  `filename` actual de un archivo del diff (un renombre se sugiere sobre el nombre nuevo; `previous_filename` no
  vale); `start..end` completo dentro de **un** hunk del lado nuevo; ≤ 30 líneas; `start ≤ end`; `status` no
  `removed`; el `replacement` redactado y ≤ 2 000 caracteres (ya es regla de `checked_suggestion`); y el `head_sha`
  del PR **hoy** es el del run. Si algo falla → no se publica, queda `suggested_fix` como texto (D-19).
- **Publicación:** `Reporter.post_suggestion(target, suggestion)` pasa a recibir un `ReviewSuggestion` validado
  (PR, `commit_id` = el sha del run, ruta, líneas, texto). El cuerpo es una explicación corta (ya por `inline`) y
  un bloque `suggestion` con una cerca más larga que cualquier racha de backticks del reemplazo, más un marcador
  oculto `<!-- cicd-doctor:v1 suggestion pr=N failure=<uuid> -->`. Se busca por marcador entre los comentarios de
  revisión **del bot** (`GET /pulls/{n}/comments`) y se edita o se crea, bajo el `publication_lock` del
  comentario sticky de ese (PR, workflow) (sin tabla nueva). Un 422 (línea fuera del diff, spam) es "solo texto",
  no un error del job. A lo sumo una sugerencia por falla y `EXPLAIN_MAX_SUGGESTIONS` (3) por comando.
- **Dónde corre:** tras publicar el diagnóstico, en el camino automático de runs **no fork** con LLM y en el
  explain (también de fork: el que aplica la sugerencia es un humano sobre la rama del PR). La caché (`cache`)
  nunca trae sugerencia (D-81).
- `render.py`: el check y el comentario dicen "Suggested change posted as a review comment" (un texto fijo) cuando
  se publicó, y el motivo en una línea cuando no ("no cabe en el diff").

**Archivos:** `core/diff.py`, `core/suggestions.py` (nuevos), `core/diagnosis/{prompt,schema,validate}.py`,
`core/models.py`, `core/ports.py`, `core/render.py`, `core/usecases/diagnose_failed_run.py`,
`adapters/github/{pulls,reporter}.py`, `adapters/memory/github.py`, `tests/github_fake.py` (`FakeRepoWrites`:
rutas de `pulls/{n}/files` y de comentarios de revisión con el 422), fixtures de B2 y B5.

**Tests**
- **Offline, rápidos:** el parser de `patch` (fixtures reales de B2 y sintéticos: un hunk, varios, sin salto final,
  renombre, agregado, borrado, vacío, binario sin `patch`); la regla "un solo hunk" (inclusive en el borde); ruta
  distinta, con `..`, absoluta, de un archivo que no está en el diff, de un archivo renombrado (vieja y nueva);
  `head_sha` movido; diff de 3 000 archivos y paginación; el prompt `diag-v3` (el bloque tiene nonce, escapa las
  tres etiquetas, respeta el tope y falta cuando no hay diff); un reemplazo con ``` y con `@mention` y con
  `<!--`: la cerca crece y el marcador no se puede falsificar; idempotencia (dos corridas, un solo comentario); el
  texto de respaldo cuando se descarta.
- **Adapter (`FakeGitHub`):** `files` con paginación, 404, límites; `post_suggestion` con 201, 422 (→ texto), 403
  (permiso nombrado), 5xx; el cuerpo exacto de la petición (fixture), solo el bot edita sus comentarios.
- **E2E offline:** PR con un diff y un `FakeLLM` que sugiere dentro y fuera de las líneas → un review comment y un
  solo texto; el mismo caso en un fork explain.
- **Evals:** `evals run` offline sigue verde con la caché vacía por `diag-v3` (se anota en el informe); nada se
  corre en vivo en este PR.
- **Postgres:** ninguno nuevo (no hay tabla); el flujo del caso de uso con `FakeLLM` ya corre en
  `test_diagnose_postgres.py`.

**Terminado cuando:** CI verde; D-94 escrito; SPEC §2 (FR-20), §5.1 (regla 3), §9.3, §9.5, §4.3 y §7.5 alineados;
`diag-v3` documentado.

**§13:** casilla 3 ("dentro del diff → review comment con `suggestion`; fuera → solo texto") — offline.

### (f) Inyección y superficies nuevas — `p3-injection`

**Alcance**
- Casos nuevos en `evals/runner/injection.py` (generados, con el comando `write-injection`) y su juez ampliado:
  - **Comentarios:** comando con carga detrás de la primera línea, comando en segunda línea, dentro de una cita o
    de un bloque de código, con caracteres de ancho cero y homoglifos, de un bot, con `edited`; el cuerpo del
    comentario **nunca** llega a un payload ni a un prompt (el test lo afirma inspeccionando `ExplainPayload` y
    los mensajes).
  - **Configuración:** bomba de alias, `!!python/object`, claves desconocidas, `llm` mal escrito (verifica que
    queda **apagado**), `ignore_workflows` con nombres de control/Unicode/markdown (el nombre del workflow se
    compara y nunca se renderiza), archivo gigante.
  - **Diff:** un PR cuyo diff contiene instrucciones, etiquetas de cierre de los tres bloques, un nonce falso y
    líneas con imágenes/enlaces/menciones.
  - **Sugerencias:** ruta con `..`, ruta de un archivo que no está en el diff, líneas fuera del diff, un
    reemplazo con cercas de backticks, con `@equipo`, con un marcador oculto falso, con `curl … | sh` (no se
    puede impedir el contenido, pero se afirma que va dentro de la cerca y con la leyenda de "revisá antes de
    aplicar").
- `evals/runner/markdown_threats.py`: el oráculo acepta exactamente el bloque `suggestion` y el marcador oculto
  propios y nada más; un review comment se escanea con las mismas reglas que el sticky.
- Un caso de **segundo orden** nuevo: una respuesta de un explain de fork no aparece jamás en la caché ni en las
  parecidas de un run normal (el envenenamiento, R-P3-3), también con un fork explain seguido de un run no fork con
  la misma firma.
- `tests/live/test_live_injection.py` suma los casos nuevos (se corre una vez, con tu consentimiento, §6).

**Archivos:** `evals/runner/{injection,markdown_threats}.py`, `evals/cases/injection/*` (generados),
`tests/unit/test_injection.py`, `tests/unit/test_render.py`, `tests/unit/test_github_webhooks.py`,
`tests/live/test_live_injection.py`.

**Tests:** todo **offline** y rápido (con el modelo obediente `obeys_injection` de D-64 y mutación manual de las
defensas nuevas, una por una, como se hizo en P1b); el live aparte.

**Terminado cuando:** CI verde; los casos nuevos están en el repositorio y un test falla si se desvían del
generador; la tabla de mutaciones documentada en D-94/D-95; SPEC §9.1 y §11.1 actualizados (número de casos).

**§13:** ninguna casilla de P3 propia; sostiene NFR-07 (0 ataques exitosos) con las superficies nuevas.

### (g) Verificación en vivo — `p3-live`

**Alcance:** como (h) de P2: workflows en el lab (`pr-suggest.yml`: un fallo determinista en una línea que el PR
cambió; `pr-fork-modnf` reutiliza `pr-py-modnf`), un archivo de configuración de prueba (en una rama del lab, nunca
en `main` salvo en el paso de `comment: false`), el runbook en `lab/README.md` (sección "Live verification (P3)",
bloques de PowerShell cortos y parseados antes de entregarlos, sin variables compartidas entre terminales), SPEC §13
con la evidencia observada, DECISIONS (verificaciones al pie), `CLAUDE.md`. Sin código de producción; si algo sale
distinto en vivo vuelve como un PR de arreglo con su D-NN.

**Terminado cuando:** cada casilla tildada con ids, tiempos y filas; las que no se pudieron medir quedan abiertas
con el motivo.

**§13:** casillas 1 a 5 en vivo (ver Apéndice A).

---

## 3. Decisiones nuevas (provisionales D-89 a D-97)

### D-89 — Quién puede disparar el comando y cómo se verifica

- **Decisión:** se acepta el comando solo si **todo** esto vale, evaluado en el webhook con lo que trae el payload,
  sin llamadas a GitHub: evento `issue_comment`, `action == "created"`, `issue.pull_request` presente, la primera
  línea del cuerpo (después de quitar `\r`) es **exactamente** `/cicd-doctor explain` (sin espacios de más, sin
  texto, sin variantes de ancho cero), `comment.author_association ∈ {OWNER, MEMBER, COLLABORATOR}` y
  `comment.user.type != "Bot"`. Cualquier otra cosa: 204, nada guardado. `edited` y `deleted` se ignoran: el comando
  vale por lo que decía al crearse (un comentario editado después para meter el comando no cuenta; uno borrado antes
  de que el worker llegue es "comando retirado": la reacción da 404 y el job termina sin gastar). El job guarda el
  `comment_id` y el autor, **nunca el cuerpo**.
- **Alternativas:** consultar `GET /collaborators/{user}/permission` (más preciso para roles de triage, una llamada
  y un permiso más; D-22 la rechazó por costo, sigue valiendo); aceptar `edited`; aceptar el comando en cualquier
  línea o con argumentos (`explain --all`: más superficie sin necesidad); aceptar al autor del PR.
- **Por qué alcanza `author_association`:** viene en el payload firmado, no necesita permiso y es lo que D-22 ya
  decidió. **Límites conocidos:** un colaborador de triage queda fuera (aceptado, D-22); `author_association` es el
  del momento del comentario; un `MEMBER` con "visibilidad oculta" de una organización puede figurar como
  `CONTRIBUTOR` (se rechaza: falla cerrada). El autor del PR de fork nunca es `COLLABORATOR` por serlo.
- **Consecuencias:** SPEC §9.7 y FR-19 no cambian de fondo; se agrega "bots" y "solo `created`". Los homoglifos se
  rechazan por comparación exacta (no hay normalización Unicode).

### D-90 — Límite de 1 comando por PR cada 5 minutos

- **Decisión:** el estado vive en `command_invocations` (ya existe). Lo decide el **worker**, no el webhook (el
  webhook sigue sin lógica de estado): `CommandLog.claim` toma un `pg_advisory_xact_lock(repo, pr)`, y bajo ese
  candado (1) si ya hay una fila de **ese** `comment_id` devuelve `True` (idempotencia de reintentos y de
  reentregas), (2) si hay otra fila del PR con `created_at > now − 5 min` devuelve `False`, (3) si no, inserta con la
  hora del `Clock` y devuelve `True`. Ventana **fija** desde el último comando **aceptado**; los rechazados no
  extienden la ventana. Un comando ignorado termina `done`, sin reacción (silencio, D-22/§9.7). La retención de la
  tabla la limpia la purga (30 días).
- **Alternativas:** limitar en el webhook (obliga a mezclar la cola con la tabla en una transacción del camino
  de la petición); ventana deslizante que se extiende con cada intento (castiga al que insiste); un contador en
  memoria del worker (varios workers no lo comparten); responder con una reacción 😕 a los limitados (informa, pero
  revela política y suma una escritura: queda como pregunta §7).
- **Consecuencias:** un colaborador que comenta 100 veces crea 100 jobs baratos (una consulta cada uno): aceptado.
  Un comando aceptado cuyo trabajo falla igual consumió la ventana (el costo ya pudo gastarse); el reintento del
  mismo comentario no la vuelve a gastar. **Por PR, no por usuario ni por repositorio:** el tope global lo ponen el
  presupuesto y las 30 llamadas diarias por instalación (D-16).

### D-91 — Qué runs explica `explain`, y qué hace cuando no hay

- **Decisión:** el job pide el PR (`GET /pulls/{n}`), y si no está abierto termina. Lista los runs del repositorio con
  `head_sha = PR.head_sha` (una página de 100), conserva solo los **completados** con conclusión `failure` o
  `timed_out` cuya `head_sha` y repositorio de la cabeza coinciden con los del PR **ahora**, toma el **último run
  por workflow** (id mayor, y su último intento) y de esos los **3 más recientes** (`EXPLAIN_MAX_RUNS`). Cada uno
  es un job hijo `diagnose_run` con `trigger = explain`. Un run `cancelled` no se mira (su regla de timeout, D-73,
  cuesta llamadas y el automático ya lo cubre). Los runs en curso se ignoran: el comando no espera. **Sin runs
  fallados:** se reacciona `confused` (😕) y el job termina; sin texto. Con `llm: off` o el interruptor apagado el
  comando se acepta (👀) y termina sin llamar al LLM; el check dice ya "LLM disabled…".
- **Alternativas:** diagnosticar todos los runs (el autor de un fork controla cuántos workflows fallan: gasto
  ilimitado, D-17); esperar a los runs en curso (jobs largos con lease ocupada); un comentario de respuesta "no hay
  nada" (un tipo de comentario nuevo con su propio marcador y su lock; queda como mejora si lo pedís, §7); no
  reaccionar nada (el usuario ve 👀 y nada).
- **Consecuencias:** el costo máximo de un comando es 3 runs × 3 jobs = 9 llamadas ≈ USD 0,003 con `gpt-6-luna`;
  el tope diario de la instalación lo frena antes si hace falta. Un PR sin runs aún (workflows con aprobación
  pendiente, `action_required`) da 😕. Si el listado no mostrara los runs de un fork (A8) el comando fallaría para
  forks: se mide en (g) y el plan B es listar por `event=pull_request&branch=<rama>` filtrando por `head_repository`.

### D-92 — PRs de fork con LLM: qué entra al prompt, quién controla el gasto, y la caché y las parecidas

- **Decisión:** `explain` en un fork **sí** llama al LLM (D-17) con estas fronteras:
  1. **Qué entra al prompt:** el excerpt redactado y acotado (≤ 6 000 tokens por run, igual que siempre), las
     pistas de reglas, y —desde (e)— el bloque del diff acotado y con nonce. **No** entra el cuerpo del comentario,
     ni el título/descripción del PR, ni `similar`.
  2. **Quién controla el gasto:** el autor del fork no puede disparar nada; el comando lo da un colaborador. El
     autor solo controla el contenido de un excerpt que ya tiene tope. Topes duros: 3 runs × 3 jobs por comando,
     1 comando por PR cada 5 minutos, 30 llamadas diarias por instalación y el presupuesto USD global, todos
     existentes. Un explain que choca con el presupuesto cae a reglas con `budget`, como el automático.
  3. **Caché, las dos direcciones:** una respuesta de un run de fork **no se escribe a la caché ni a las
     parecidas** (los filtros `NOT is_fork` de D-79 y D-81, ahora con test de envenenamiento propio) y un run de
     fork **no la lee** (se cierra el agujero actual: `_cached` hoy solo mira la política, y con `explain=True` un
     fork la leería). Costo de no leerla: una llamada de ≈ USD 0,0003 por falla de fork en un explain.
  4. **Parecidas:** en el reporte de un fork se siguen mostrando (D-82: son del historial no-fork); **al prompt de
     un fork no entran**: ni el autor del fork elige qué vecinos lee el modelo ni nada que él escribió se mezcla
     con historia del repo.
- **Alternativas:** servir la caché a un explain de fork (más barato; rompe la simetría de D-81 y deja que el autor
  fuerce un acierto con una firma copiada de un log público); pasar `similar` también (valor real, pero más
  superficie y poco ahorro); responder solo con reglas a los forks (rompe FR-19).
- **Consecuencias:** SPEC §9.3 ("A fork run never reaches the model") y §9.4 regla 5 se reescriben; una falla de
  fork que se mejora con explain queda con fila `llm` **no cacheable** por construcción. Los cambios de
  `is_fork` con la cabeza borrada (se trata como fork) quedan cubiertos. Pendiente de verificar (spike/(g)): el
  `is_fork` de un run `pull_request_target`, donde `head_repository` puede ser el repositorio base.

### D-93 — La respuesta de `explain` mejora el diagnóstico publicado; no es otro comentario

- **Decisión:** `explain` **actualiza en el lugar** el check run (el mismo id; ya está `completed`/`neutral`, solo
  cambia el `output`) y el comentario sticky de su (PR, workflow), reescritos desde lo guardado bajo
  `publication_lock` y la guarda `(run_id, attempt)`, igual que el cierre de un fix (D-84). El diagnóstico nuevo se
  guarda con `FailureStore.upgrade_published` (una transacción por falla: la fila de reglas queda como no
  publicada, la `llm` pasa a publicada y lleva el costo). Una falla que ya tiene `llm` o `cache` publicada **no se
  vuelve a analizar** (nunca se gasta dos veces para un intento); el reintento del job no llama al modelo. El
  comando no crea comentarios nuevos; la señal es 👀 y la edición del sticky.
- **Alternativas:** un comentario nuevo por comando (ruido y rompe D-18/"nunca un segundo comentario por
  (PR, workflow)"); responder al comentario del comando (los comentarios de issue no tienen hilos); guardar la
  mejora en tabla aparte (otra fuente de verdad para el check y el comentario); reanalizar siempre (gasto sin
  necesidad).
- **Consecuencias:** modifica D-69 en un punto: un intento analizado es final **salvo** la mejora de reglas a LLM
  por `explain`. `run_analysis` sigue siendo la única fuente. Un `explain` hijo que se rinde no pisa el check (D-74
  no aplica). Si el run no tiene comentario sticky aún, el explain lo crea con el PR del comando.

### D-94 — Sugerencias: de dónde salen las líneas, cómo se validan contra el diff, y por qué una mal ubicada no se publica

- **Decisión:** (1) el modelo solo puede sugerir líneas que **se le mostraron**: `diag-v3` agrega un bloque
  `<untrusted_diff>` con los hunks (lado nuevo) de los archivos del diff del PR que el excerpt nombra; sin ese
  bloque `suggestion` es `null`. (2) La sugerencia se valida en `core/suggestions.py` contra el `GET /pulls/{n}/files`
  **actual**: la ruta es exactamente el `filename` de un archivo con `patch` que no está `removed`; las líneas
  `[start, end]` están enteras en un solo hunk del lado nuevo; ≤ 30 líneas; el `head_sha` del PR es el del run. (3) Si
  pasa, se publica **un** review comment con bloque `suggestion` (cerca más larga que cualquier backtick del
  contenido, marcador oculto propio, leyenda fija "generado desde logs no confiables; revisá antes de aplicar"),
  hallado por marcador entre los comentarios de revisión del bot. (4) Si no pasa o GitHub contesta 422, **no se
  publica nada** y `suggested_fix` queda como texto: un `suggestion` mal ubicado o sobre un archivo que cambió se
  aplicaría sobre otro código (un clic de un humano que no ve la diferencia) o daría un error inútil. (5) Un diff
  truncado (más de 3 000 archivos, `patch` ausente) → solo texto.
- **Alternativas:** dejar que el modelo adivine sin ver el código (hoy: reemplaza líneas desconocidas); leer el
  archivo completo con `contents: read` (para un fork la app, con token de un solo repo, no puede leer el repo de
  la cabeza; y es más texto no confiable); `position` de la API vieja (cerrándose); permitir sugerencias que cruzan
  hunks (GitHub las rechaza); publicar siempre y dejar que GitHub falle.
- **Consecuencias:** modifica SPEC §5.1 regla 3 (suma la validación contra el diff), §9.3 (tercer bloque no
  confiable) y D-19 (se concreta). El costo sube por el bloque (C3 lo mide); `diag-v3` invalida la caché a
  propósito. Los renombres usan el nombre nuevo. Una sugerencia sobre un PR que recibió un push entre el run y la
  publicación no se postea (se evita el "outdated" instantáneo).

### D-95 — Esquema, valores por defecto y validación de `.github/cicd-doctor.yml`; solo la rama por defecto

- **Decisión:** `comment: bool = true`, `llm: auto | on_demand | off = auto`, `ignore_workflows: list[str] = []`
  (≤ 50 nombres, ≤ 120 caracteres, sin caracteres de control), `extra="forbid"`. **Solo la rama por defecto, por
  construcción:** el adapter hace `GET …/contents/.github/cicd-doctor.yml` **sin parámetro `ref`** (la API usa la
  rama por defecto del repositorio) y su firma ni siquiera acepta uno; un PR (de fork o no) nunca pide su propio
  archivo, y un test afirma que ninguna petición lleva `ref`. Seguridad del parseo: ≤ 16 KB, `type == file`,
  recorrido de eventos que rechaza alias y anclas, `yaml.safe_load`, validación estricta. **Estados:** ausente (404)
  → defaults; **inválido** (YAML roto, clave desconocida, tipo mal, demasiado grande, con alias) → defaults **con
  `llm = off`** y una línea en el check con los **nombres de campos** que fallan (nunca valores); sin permiso
  (`unreadable`: 403/422 de token) → defaults y un WARNING; error transitorio → el job reintenta, nunca defaults.
  Caché en proceso 300 s (ausente 60 s); un cambio en la rama por defecto aplica en hasta 5 minutos; sin
  invalidación por `push` (no se suscribe otro evento).
- **Modifica D-26** (que decía "reemplazado por los valores por defecto cuando falta o es inválido"): para el LLM
  falla cerrado, porque un tipeo no puede mandar logs a un tercero en el repo que quiso apagarlo.
- **Alternativas:** inválido → defaults (D-26 original: más simple y fail-open para la privacidad); reconocer campo
  por campo y ignorar solo el roto (respeta mejor la intención, pero un tipeo en `llm` volvería al comportamiento
  que el dueño quiso evitar); leer de la cabeza del PR (D-26 lo descartó); invalidar por `push`.
- **Consecuencias:** SPEC §2 (FR-21), §4.3 (el puerto devuelve `LoadedConfig`), §9.1 y §13 cambian. Un repo con un
  tipeo pierde el LLM hasta corregirlo y lo ve en el check. Una instalación sin `contents: read` aceptado funciona
  con defaults.

### D-96 — `comment: false` e `ignore_workflows` frente a lo ya publicado y a la resolución de P2

- **Decisión:** `comment: false` → no se crea ni se edita **ningún** comentario sticky (el check run se sigue
  publicando, D-18); un comentario que ya existía **queda como está** (no se borra ni se reescribe: es el último
  estado que dijo la verdad) y la reescritura de resolución (D-84) también se salta. `ignore_workflows` (nombre
  exacto del workflow, sensible a mayúsculas) → ese workflow no produce **nada**: ni check, ni comentario, ni
  checkpoint, ni LLM, ni reserva; vale también para `explain` (sacarlo de la lista lo reactiva) y no impide que
  `record_outcome` registre el éxito (barato, y mantiene el orden de P2) pero sí el comentario. Se decide en el
  caso de uso, no en el webhook (el webhook no hace I/O).
- **Alternativas:** borrar el sticky al apagar (necesita permiso de borrado y es destructivo); reescribirlo con
  "comentarios apagados" (ruido, y un cambio de configuración editaría un montón de comentarios viejos); ignorar
  por patrón o regex (superficie de ReDoS, sin pedido).
- **Consecuencias:** un comentario viejo puede quedar mostrando "Likely cause" aunque ya se arregló (se lo dice
  la documentación). Cambiar la configuración no reescribe nada hacia atrás.

### D-97 — Permiso y evento para `issue_comment` y para las reacciones (modifica D-21)

- **Evidencia hoy:** el fixture real `app.json` (2026-11-16) da `events: [workflow_run]` y permisos sin Issues;
  la documentación de GitHub (leída el 2026-11-19) dice que `issue_comment` exige **Issues: read** y que crear una
  reacción en un comentario exige **Issues: write**. D-21 ("no Issues") y SPEC §9.6 ("Pull requests write cubre
  las reacciones") estaban supuestos, no medidos.
- **Decisión (propuesta, a confirmar con B3/E1 y con la primera entrega real):** la app suma **Issues: write**
  (incluye read) y se suscribe a `issue_comment`; las instalaciones tienen que **aceptar** el permiso nuevo (hasta
  entonces conservan el anterior). Los tokens quedan separados por caso de uso (D-68): `DIAGNOSIS_PERMISSIONS` no
  cambia, `EXPLAIN_PERMISSIONS` suma `issues: write`, `CONFIG_PERMISSIONS` es `contents: read`. Si GitHub aceptara la
  reacción con Pull requests write (se verifica con el header `x-accepted-github-permissions` y en (g)), se pide
  solo Issues: read para el evento y se usa lo mínimo.
- **Alternativas:** no reaccionar (rompe FR-19); consultar los comentarios por polling en vez de webhook (más
  lento, más permisos de lectura); Issues write en todos los tokens (más privilegio del necesario).
- **Consecuencias:** el principio "mínimo privilegio" (NFR-06) se mantiene por token; la app gana acceso de
  escritura a issues (comentarios y reacciones de issues, no solo de PRs), que se documenta en SPEC §9.6 y D-21.
  La verificación real (entrega con el permiso aceptado, y sin aceptarlo) es la casilla 5.

---

## 4. Riesgos e incógnitas del mundo real

| Id | Incógnita o riesgo | Impacto | Mitigación / dónde se contesta |
|---|---|---|---|
| R-P3-1 | La app no recibe `issue_comment` (evento no suscrito y permiso Issues ausente) | **Bloquea lo vivo** | (a) y tu cambio manual; E1 lo lee antes |
| R-P3-2 | Las instalaciones no aceptan el permiso nuevo y el evento no llega | Alto | `new_permissions_accepted` llega como evento `installation`; (g) lo observa; hasta entonces todo sigue como hoy |
| R-P3-3 | Una respuesta LLM de un explain de fork contamina la caché o las parecidas de runs normales | **Alto** (inyección de segundo orden) | filtros `NOT is_fork` + tests de contrato y de envenenamiento de punta a punta (d1, f) |
| R-P3-4 | `is_fork` de un run `pull_request_target` o de una cabeza borrada | Medio | cabeza borrada = fork (webhook); `pull_request_target` se mide en (g); si falla, el run se trata como fork por la dirección segura |
| R-P3-5 | (Medido en el spike) los runs de un PR de fork sí salen por `head_sha`, con `pull_requests` vacío y a veces `action_required` | Medio | emparejar por `head_sha` + `head_repository`; confirmar en (g) con la organización |
| R-P3-6 | Un explain gasta de más (el autor fabrica workflows que fallan) | Medio | tope 3 runs × 3 jobs, 1/5 min/PR, 30/día/instalación, USD global; test con contador de llamadas |
| R-P3-7 | Un comentario editado o borrado cambia lo que se aceptó | Bajo | solo `created`; el cuerpo nunca se guarda; el 404 de la reacción retira el comando |
| R-P3-8 | Dos workers aceptan dos comandos del mismo PR a la vez | Medio | candado transaccional en `claim` + test de dos engines |
| R-P3-9 | Un PR de 3 000+ archivos o un `patch` ausente | Bajo | solo texto; paginación con tope |
| R-P3-10 | Un `suggestion` aplicado sobre código que cambió | Medio | solo si `head_sha` coincide; un hunk; marcador; el 422 es "solo texto" |
| R-P3-11 | El contenido de un reemplazo (`curl … | sh`) lo aplica un humano de buena fe | Medio, **inherente** | redacción, ≤ 30 líneas y 2 000 caracteres, leyenda fija, siempre un clic humano; aceptado (D-19) |
| R-P3-12 | Bomba YAML o archivo gigante en la rama por defecto | Bajo | 16 KB, sin alias, `safe_load` (C1) |
| R-P3-13 | Un tipeo en `llm:` enciende el LLM | Medio | D-95: inválido apaga el LLM |
| R-P3-14 | Latencia: objetivo 90 s del comando al diagnóstico | Medio | sin bajada de logs cuando hay checkpoint; LLM p50 3,5 s (D-88); `seconds_since_command` en el log; se mide en (g). No hay p95 con una muestra |
| R-P3-15 | Límites de tasa (80 escrituras/min, 500/h) | Bajo | un explain ≈ 3 a 8 escrituras |
| R-P3-16 | `GitHubReporter.react` para un comentario recién creado puede dar 404 por consistencia | Bajo | un 404 retira el comando; si en (g) se ve en comentarios reales, se reintenta una vez |
| R-P3-17 | Los worktrees comparten la base de test | Medio | sin migraciones previstas; si aparece una, `TEST_DATABASE_URL` por worktree |
| R-P3-18 | Una cantidad grande de comandos spamea la cola | Bajo | solo colaboradores; cada uno es una consulta |
| R-P3-19 | `diag-v3` mueve los números de evals y vacía la caché de P1b/P2 | Bajo | se anota; `llm+hints` se vuelve a correr con tu consentimiento (§6) |

---

## 5. SPEC contra código: lo que se contradice y cómo lo resuelve este plan

No elegí en silencio ninguno; cada uno se corrige en el PR que se indica, en el mismo PR que el código.

| # | SPEC dice | El código o la realidad | Resolución |
|---|---|---|---|
| 1 | §9.6 / D-21: sin Issues; "reacciones: Pull requests write" | Documentación: `issue_comment` pide Issues read; reacciones, Issues write; `app.json`: evento no suscrito | (a), D-97 |
| 2 | §9.3: "A fork run never reaches the model" | Con `explain` sí llega | (d1), D-92 |
| 3 | §9.4 regla 5 / D-81: un fork no se sirve de la caché "en las dos direcciones" | `_cached` solo mira la política: un fork con `explain=True` leería la caché | (d1), D-92 |
| 4 | D-69: "un intento analizado es final" | `explain` mejora lo publicado de un intento ya analizado | (d1), D-93 |
| 5 | §4.3: `RepoConfigSource.load -> RepoConfig`; D-26: inválido = defaults | Fail-open para el LLM | (b), D-95 |
| 6 | §4.3: `post_suggestion(target, suggestion)`, `react(comment, reaction)` | `CommentRef` no tiene `repo_id` (token por repo); la sugerencia necesita `commit_id`, PR y diff | (c) y (e) cambian las firmas |
| 7 | §5.1 regla 3 (ruta y span) | No valida contra el diff; el modelo no ve el código | (e), D-94, `diag-v3` |
| 8 | §6: `command_invocations` en "P3" | Ya está creada en `0001`, sin código | (c): se usa, sin migración |
| 9 | §9.7: "primera línea exactamente el comando" | El payload no trae la cabeza del PR, `edited`/bots no están dichos | (c), D-89 |
| 10 | §9.4 regla 4: `on_demand` → `config_off` | El texto del check dice "LLM disabled by repo configuration" | (b): motivo `on_demand` |
| 11 | `DEFAULT_REPO_CONFIG` en `DiagnoseFailedRun` ("P1c has no repository config") | Obsoleto | (b) |
| 12 | §8: sin fila para el comando | Nuevo comportamiento ante comentario borrado, 404, 422 | (c), (e) |
| 13 | §11.1 "16 casos de inyección" | Se amplía | (f) |
| 14 | `JobKind.EXPLAIN` sin handler | Placeholder | (c) y (d2) |
| 15 | §13 P3 caja 3: "dentro del diff → review comment" | El LLM puede no sugerir en un caso dado: la caja solo se puede ver con un fallo determinista y un modelo que acierte | (g): se documenta qué evidencia se acepta |

---

## 6. Lista aparte: lo que necesita tu consentimiento explícito

Nada de esto lo hago hasta que lo apruebes (el spike entero espera tu "andá", por partes).

**A. Spike (solo lectura).**

| Qué | Dónde | Notas |
|---|---|---|
| Parte B: `gh api` solo `GET` (runs, PRs, files, contents, comentarios de revisión, reacciones con `-i`) | lab y este repositorio, y un repo público ajeno para B6 | usa tu sesión de `gh`; **no** el JWT de la app ni `.env` |
| Parte C: scripts descartables de Python | scratchpad | sin red; no tocan el repo |
| Parte E: que mires la pantalla de la app (*Permissions & events* y *Recent deliveries*) | GitHub (tu navegador) | solo lectura |

**B. Lo que escribe en GitHub, gasta dinero o toca `.env` o la configuración de la app (solo (g), nunca antes, y
solo con tu "sí" a cada grupo):**

| Qué | Dónde | Motivo / costo |
|---|---|---|
| **Cambiar la app de desarrollo:** permiso Issues → write y suscribir `issue_comment` | configuración de la app | casilla 5; lo hacés vos |
| **Aceptar el permiso nuevo** en las instalaciones (lab y este repo) | configuración de la instalación | lo hacés vos; sin eso el evento no llega |
| Publicar `pr-suggest.yml` y un workflow de configuración en el lab (push) | lab | casillas 3 y 4 |
| Abrir PRs y empujar commits a ramas del lab | lab | casillas 1 a 4 |
| Comentar `/cicd-doctor explain` en PRs del lab (con tu cuenta; escribe un comentario) | lab | casilla 1; la reacción y la edición del comentario las escribe el worker con la app |
| Un comando de una cuenta **sin permisos** (ver §7) | lab | casilla 2 |
| Un comando extra dentro de 5 minutos | lab | casilla 2 (límite) |
| Un archivo `.github/cicd-doctor.yml` con `comment: false` en la rama por defecto del lab, y su reverso | lab | casilla 4 (escribe y borra un archivo de `main` del lab) |
| Un cambio de ese archivo solo en la rama de un PR | lab | casilla 4 (sin efecto) |
| Cargar `OPENAI_API_KEY`, `OPENAI_MODEL` y `LLM_PRICE_*` en `.env` y bajar `LLM_DAILY_BUDGET_USD` y `LLM_DAILY_CALLS_PER_INSTALLATION` para la prueba; **restaurarlos a mano al terminar** | `.env` (secretos) | gasto de la verificación: ≈ 5 comandos × hasta 9 llamadas de ~7 000 tokens ≈ **USD 0,02**, con `LLM_DAILY_BUDGET_USD=0.10` |
| `pytest -m live tests/live/test_live_injection.py` con los casos nuevos | OpenAI | ≈ USD 0,01 (los 16 casos costaban ≈ USD 0,005) |
| `evals run --variant llm+hints --live` con `diag-v3` (para refrescar la caché de evals) | OpenAI | ≈ USD 0,02 (41 casos a ≈ USD 0,0003); opcional |
| Arrancar API, worker y smee **desde el worktree de (g)** con el `.env` real copiado | local | todas |
| Una organización con un fork del lab | GitHub | casilla de fork (§7) |

---

## 7. Preguntas para vos y qué tenés que preparar

**Preguntas (con mi recomendación):**

1. **Permiso:** ¿aceptás sumar *Issues: write* a la app (D-97)? Reco: sí, con tokens separados por caso de uso.
   Alternativa: *Issues: read* solo y sin reacción 👀 (rompe FR-19).
2. **Configuración inválida:** ¿LLM apagado hasta que se corrija (D-95, cambia D-26)? Reco: sí.
3. **Comando limitado por la ventana:** ¿silencio total (SPEC/D-22) o una reacción 😕? Reco: silencio.
4. **Sin runs fallados / sin LLM:** ¿una reacción 😕 alcanza o querés un comentario corto? Reco: 😕 en P3, un
   comentario de aviso como mejora si lo extrañás.
5. **¿Señal de "terminé"?** Hoy: 👀 y la edición del sticky. ¿Una segunda reacción al final (🚀)? Reco: no.
6. **Sugerencias automáticas:** ¿también en runs normales con LLM (no solo con `explain`)? Reco: sí (FR-20 no pide el
   comando); se apaga con `comment: false`.
7. **Tope de runs por comando:** 3 (`EXPLAIN_MAX_RUNS`). ¿Te sirve?
8. **`comment: false` con un sticky ya publicado:** dejarlo como está (D-96). ¿Preferís borrarlo?
9. **`ignore_workflows` ignora también `explain`:** ¿de acuerdo?
10. **Spike:** ¿aprobás A a E, por partes?

**Qué tenés que preparar, y qué casilla depende de cada cosa:**

| Cosa | Casillas que dependen | Se puede verificar sin ella |
|---|---|---|
| Cambiar la app (Issues write + `issue_comment`) y aceptar el permiso en las instalaciones | 1, 2, 5 en vivo | todo offline |
| **Una cuenta de GitHub sin permisos de escritura** (cualquier cuenta que no sea colaboradora del lab, que pueda comentar en un PR del lab público; o la organización con un miembro externo) para el caso de rechazo | 2 en vivo (la mitad "no reaction, no job") | el rechazo por `author_association` y el límite 1/5 min se prueban offline; en vivo el límite sí se puede con tu cuenta |
| **La organización para el fork** (la misma que pide P1c) | 1 en fork (en vivo), `pull_request_target`, R-P3-5 | un PR del mismo repositorio (rama) cubre explain, sugerencias y configuración; el fork queda abierto con su motivo |
| Clave de OpenAI en `.env` y tope bajo | 1, 3 en vivo (LLM real) | offline con `FakeLLM` |
| Un PR del lab cuyo diff incluya la línea que falla (`pr-suggest.yml`) | 3 en vivo | offline; el modelo real puede no sugerir (se documenta) |
| `git fetch`, `main` local actualizado, `docker compose up -d db`, canal de smee | todas | — |
| El cambio de configuración **solo en una rama de un PR** | 4 (la parte "sin efecto") | no necesita fork: una rama del mismo repo prueba lo mismo |

**Mi pedido ahora:** tu respuesta a las 10 preguntas (o "tu recomendación") y la aprobación del spike por partes.

---

## Apéndice A — Runbook de (g): verificación en vivo de P3 (esquema)

Mismo reparto que P1c y P2: **vos** corrés las acciones (escrituras en GitHub y cambios en `.env`); **yo** leo
los resultados con `gh` (solo `GET`). El runbook con comandos copiables de PowerShell 5.1 (líneas cortas, variables
que no se comparten entre terminales, parseados antes de entregarlos) se escribe en `lab/README.md` dentro del PR
(g). Se corre desde el worktree de (g), con API, worker y smee **de esa rama**, `.env` copiado y los procesos viejos
detenidos.

**Casilla 5 — Permiso y evento (primero).**
- Acción (vos): suscribir `issue_comment` y sumar Issues; aceptar el permiso en las instalaciones.
- Verificación (yo): el evento `installation` con `new_permissions_accepted` en las entregas recientes; una entrega
  `issue_comment` del lab con respuesta 202 (`gh api` de las entregas necesita el JWT: **lo leés vos de la pantalla**
  *Recent deliveries* y me pasás el estado, o lo veo en el log del worker). Documentar qué pasó con una instalación
  que **no** aceptó (si hay una).

**Casilla 1 — Comando de un colaborador → 👀 y diagnóstico con LLM en ≤ 90 s.**
- Acción (vos): PR del lab con `pr-py-modnf` fallando; comentar `/cicd-doctor explain` con tu cuenta.
- Verificación (yo): reacción `eyes` del bot en el comentario (`GET …/reactions`); el check y el sticky (mismo id
  que antes) con fuente `LLM gpt-6-luna · prompt diag-v3`; la fila publicada `llm` con costo; `llm_usage.calls`; el
  tiempo del comentario al sticky editado (`seconds_since_command` del log). **En un PR de fork** (con la
  organización): lo mismo, y la fila `llm` de ese run **no** aparece en `cached_diagnosis` de un run normal con la
  misma firma (consulta a la base).

**Casilla 2 — No colaborador → sin reacción, sin job; segundo comando < 5 min → ignorado.**
- Acción (vos): el comando desde la cuenta sin permisos; luego dos comandos seguidos desde la tuya.
- Verificación (yo): cero reacciones y cero filas `jobs` de tipo `explain` para el primero (la fila de
  `webhook_deliveries` no existe si el router devolvió 204); para el segundo, un job `done` sin reacción nueva y una
  sola fila en `command_invocations`.

**Casilla 3 — Sugerencia dentro del diff → review comment con `suggestion`; fuera → solo texto.**
- Acción (vos): PR con `pr-suggest.yml` (el fallo apunta a una línea que el PR cambió) y `/cicd-doctor explain`;
  luego un PR donde la línea del fallo **no** está en el diff.
- Verificación (yo): un review comment del bot con bloque `suggestion` y el marcador, sobre las líneas correctas;
  en el segundo caso, ningún review comment y `suggested_fix` en el sticky. Si el modelo no sugiere, la casilla
  queda abierta con el motivo (el modelo varía) y se reintenta; **no** se tilda con un `FakeLLM`.

**Casilla 4 — Configuración.**
- Acción (vos): `comment: false` en `.github/cicd-doctor.yml` de la rama por defecto del lab; un PR que falla; y,
  aparte, un PR cuya **rama** cambia ese archivo (`comment: true`, `llm: off`).
- Verificación (yo): con `comment: false` el check existe y **no** hay comentario nuevo; el archivo cambiado solo en
  la rama no cambia nada (el check sigue con el comportamiento de la rama por defecto); un archivo inválido en
  `main` del lab muestra "configuración inválida" y LLM apagado. Se restaura el archivo.

**Limpieza (vos):** PRs y ramas del lab cerrados; el archivo de configuración de prueba borrado de `main`; `.env`
sin la clave y con los topes restaurados **a mano**; procesos detenidos; worktree retirado (pasos de `CLAUDE.md`).
