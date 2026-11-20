> **Original working document, written in Spanish (rioplatense) for the owner and kept untouched below this line.** It is a planning draft: where it differs from `SPEC.md`, `DECISIONS.md` or the code, those are the authority (see the phase `README.md` for the differences).

# P1c — Integración con GitHub: plan (v2)

> Solo plan, sin código. No commiteado. Base: SPEC §4.3, §6, §7.1, §7.5, §8, §9.6, §12, §13 (P1c), §14 y
> DECISIONS D-06, D-11, D-12, D-16, D-17, D-18, D-21, D-23, D-28, D-30, D-44, D-47 a D-49, D-54, D-56,
> D-58, D-63 a D-66. Los números D-67 a D-76 son provisionales: se asignan en el orden de merge.

## Cambios respecto de la v1 (por tus respuestas)

1. **Nombre del check run: `CICD Doctor: <workflow>`** (nueva D-76). El nombre del workflow es texto no
   confiable: pasa por `flatten` de `core/render.py` y se corta (ver D-76). Actualiza FR-11, §7.5 y la
   primera casilla de P1c en SPEC §13, y enmienda D-18. El spike confirma qué ve la API con checks del
   mismo nombre; **cómo los dibuja la interfaz** solo se ve escribiendo, así que se prueba en (c)
   (Apéndice B), con tu rama descartable.
2. **(b) se parte en dos:** (b1) HTTP y auth, (b2) `LogSource`.
3. **(d) se parte en dos:** (d1) caso de uso y fakes, (d2) handler, container, e2e y Postgres. Una
   precisión: los fakes (`FakeLogSource`, `FakeReporter`, `FakePullRequests`) van en
   `adapters/memory/github.py`, junto a `FakeLLM` y los demás. `core/` no puede importar `adapters`
   (D-43), así que el caso de uso queda en `core/usecases/` y los fakes fuera.
4. **Spike ampliado** con la verificación de la app de desarrollo (pedida en tu punto 3): una tabla de
   permisos y eventos requeridos vs. reales, y qué falta.
5. **U10 en (c)** sobre una rama descartable del lab, con check runs llamados `CICD Doctor (test)`. Como los
   check runs no se pueden borrar, el plan fija de antemano el conjunto exacto que voy a crear (Apéndice B).
6. **Timeouts de job** quedan como (f), al final y separable: si se complica, pasa a P2 sin bloquear nada.
7. **Runbook de (e)** escrito con comandos copiables de PowerShell 5.1 (Apéndice A): vos hacés las
   escrituras, yo leo con `gh` (solo `GET`).

## Punto de partida (lo que encontré leyendo el código)

- `main` local está **detrás** de `origin/main`: el #8 (P1b 3/3) está mergeado en el remoto pero no en tu
  `main` local. Cada rama de P1c sale de `origin/main` actualizado.
- `httpx2` **ya es dependencia de runtime** (la trae `openai` 3.51); `httpx` y `respx` **no están
  instalados**. El test client de FastAPI también usa `httpx2` (D-44). Esto decide D-67.
- No hay `cryptography` ni `PyJWT` en el lock.
- `.env` tiene `GITHUB_APP_ID`, `GITHUB_PRIVATE_KEY` y `GITHUB_WEBHOOK_SECRET` con valor, y
  `OPENAI_API_KEY`, `OPENAI_MODEL` y los precios vacíos (miré solo los nombres, no los valores).
- Los 23 workflows de `lab/` son **solo `workflow_dispatch`**: ningún run del lab tiene PR asociado. Para
  las casillas de P1c hace falta un workflow del lab disparado por `pull_request` (PR e).
- El esquema no alcanza para **reconstruir** un `Failure` desde la base: `failures` no guarda
  `conclusion`, `started_at`, `completed_at`, `runner_os` ni los límites del step, que `FailedJob` exige. Y
  no hay forma de saber si el análisis de un intento terminó. Hace falta la migración `0003` (PR a).
- `FailureStore.save_diagnosis(failure_id, diagnosis)` no tiene `published` ni uso (tokens, costo,
  latencia), y `failures.id` es un UUID aleatorio con `UNIQUE (run_id, run_attempt, job_id)`: un reintento
  chocaría. `Reporter.complete_check(check_run_id, report)` no recibe el repo (no puede pedir token) y
  obliga al adapter a renderizar. Los puertos cambian (PR a y c).
- `repositories.installation_id` referencia `installations`, y `workflow_runs`/`failures` referencian
  `repositories`: el caso de uso tiene que asegurar esas filas (D-54 lo dejó para P1c), pero
  `DiagnoseRunPayload` no trae `account_login`/`account_type` de la instalación (PR d1).
- El worker no tiene gancho para "último intento" (SPEC §8: "Could not diagnose (internal error)" desde
  P1c). Lo resuelve el handler (D-74).
- El test de arquitectura prohíbe `httpx` en `core` pero no `httpx2`, `jwt` ni `cryptography`.
- Tu consola es PowerShell **5.1**: `ConvertFrom-Json` deja las fechas ISO como texto, y pasar comillas
  dobles dentro de `--jq` a `gh` se rompe. El runbook lo evita.

## 0. Resumen

| Paso | Título | Tamaño | Depende de | D-NN que escribe | Cierra en §13 |
|---|---|---|---|---|---|
| 0 | Spike de solo lectura (no es PR) | S | — | — | — |
| a | Persistencia: `PostgresFailureStore`, `PostgresUsageLedger`, checkpoint, lock de publicación, `0003` | M | — | D-69, D-70 | — |
| b1 | HTTP y auth de GitHub: cliente, JWT, tokens por repo, rate limit | M | spike | D-67, D-68 | — |
| b2 | `LogSource`: jobs paginados, redirect sin auth, `Range`, ring buffer, 404/410 | M | b1 | D-71 | — |
| c | `Reporter` (check y comentario sticky), PR del run con forks, nombre del check, filtro de base | M | a, b1 | D-72, D-76 (+U10 en D-70) | — |
| d1 | Caso de uso `diagnose_failed_run`, cuenta en el payload, fakes de GitHub | M | a, b2, c | D-75 | — |
| d2 | Handler, container, e2e offline, integración y crash en Postgres | M | d1 | D-74 | P1a: "…exactly one check run" |
| e | Verificación real: lab y este repo (el fork queda abierto) | S | d2 | — | 5 de las 6 de P1c |
| f | Timeouts a nivel de job (opción a) | M | d2 | D-73 | pregunta de §14 |

**Orden de merge y paralelismo:** `a` y `b1` no comparten archivos (solo `core/models.py`/`ports.py` los toca
`a`, y `pyproject.toml`/`uv.lock` los toca `b1`): pueden ir en paralelo. Después `b2` (sobre `b1`) y `c`
(sobre `a` y `b1`; `c` también cambia `models.py` y `ports.py`, por eso va después de `a`). `d1` espera a
`a`, `b2` y `c`. `d2` va sobre `d1`. `e` y `f` van sobre `d2`; `f` se puede soltar sin tocar `e`.

Reglas de trabajo para todos los PRs: rama desde `origin/main`, CI verde (lint, types, tests con Postgres,
cobertura de `core/` ≥ 90 %), D-NN y SPEC alineados en el mismo PR, CLAUDE.md actualizado cuando cambia el
estado, commits en inglés sin `Co-Authored-By`, nada de secretos en código ni en fixtures (los únicos
secretos falsos siguen en `tests/redaction_corpus.py`), y ningún log real commiteado sin pasar por la
redacción de la captura. Hasta (d2) el worker sigue con el handler de relleno para `diagnose_run`, así que
`main` funciona igual que hoy después de a, b1, b2, c y d1.

Los tests de adapters de GitHub (b1, b2, c) van en `tests/adapters/`, como los de OpenAI: CI los corre
completos (`uv run pytest`) y no entran en el gate rápido de pre-commit. Los del caso de uso (d1) van en
`tests/unit/` y sí entran.

---

## 1. Paso 0 — Spike de solo lectura (1 a 2 h, no es PR)

**Objetivo:** grabar respuestas reales de la API para que los fixtures de b1, b2 y c tengan la forma real,
verificar la configuración de la app de desarrollo (tu punto 3) y contestar las incógnitas U1 a U13 (§4)
antes de escribir los adapters. **No arranca hasta que apruebes este plan.**

**Cómo:** un script descartable en el scratchpad (fuera del repo), corrido desde el repo con
`uv run --with "pyjwt[crypto]" python <scratchpad>\github_spike.py` (la dependencia extra vive solo en esa
ejecución, no entra en `pyproject.toml`). Lee `GITHUB_APP_ID` y `GITHUB_PRIVATE_KEY` de `.env` con
`Settings` (`SecretStr`) y nunca los imprime.

**Solo lectura, impuesto por el script, no por buena voluntad:** una lista blanca de pares (método, plantilla
de ruta); cualquier otra llamada aborta antes de salir a la red. Todo es `GET` salvo
`POST /app/installations/{id}/access_tokens`, que crea un token de una hora, no toca ningún repositorio y no
cambia nada visible. Las cabeceras `Authorization`, los tokens y las URLs firmadas no se imprimen ni se
guardan: de la URL firmada solo el host y el path.

| # | Llamada | Para contestar |
|---|---|---|
| 1 | `GET /app` (JWT) | slug (el bot es `<slug>[bot]`), id, **permisos y eventos reales** de la app de desarrollo |
| 2 | `GET /app/installations` (JWT) | instalaciones, `repository_selection`, `suspended_at`, y los **permisos y eventos de cada instalación** (pueden ir por detrás de los de la app si falta que el dueño acepte un cambio) |
| 3 | `GET /app/hook/config` y `GET /app/hook/deliveries?per_page=5` (JWT) | la URL del webhook (¿apunta a smee?), y estado y evento de las últimas entregas; el secreto sale enmascarado y las entregas no traen el payload |
| 4 | `POST …/access_tokens` con `repository_ids: [lab]` y `{actions: read, checks: write, pull_requests: write}` | forma de la respuesta (`expires_at`, `permissions`, `repositories`); se guarda **sin** el campo `token` |
| 5 | `GET /installation/repositories` (token de instalación) | en qué repos está instalada la app: ¿el lab **y** `cicd-doctor`? |
| 6 | `GET /repos/{o}/{lab}/actions/runs?status=failure&per_page=5` y `GET …/runs/{id}` | forma de `pull_requests` en runs de `workflow_dispatch` |
| 7 | `GET …/runs/{id}/attempts/{n}/jobs?per_page=100` de un run con 2 intentos (los flaky) | U4: ¿el intento 2 lista los jobs reutilizados del 1? |
| 8 | `GET …/actions/jobs/{job_id}/logs` sin seguir redirects | U1: status y host del `Location` |
| 9 | `GET` al `Location`, sin `Authorization`, en streaming | U2: `Content-Length`, `Content-Encoding`, `Accept-Ranges`, `Content-Type`; los 2 primeros bytes (¿gzip?); el cuerpo no se guarda |
| 10 | el mismo blob con `Range: bytes=1000-` y con `Range: bytes=-1000` | U3: ¿206 con `Content-Range`? ¿rango por sufijo? |
| 11 | el mismo `Location` 2 y 10 minutos después | U5: vencimiento de la URL firmada |
| 12 | `GET …/actions/jobs/1/logs` (no existe) y `GET …/actions/secrets` con el token sin ese permiso | forma del 404 y del 403 "Resource not accessible by integration" |
| 13 | `GET /repos/{o}/{lab}/check-runs/{job_id}/annotations` del run de `job-timeout` | U7: texto exacto de la anotación y que el job id sea el id del check run |
| 14 | `GET /repos/{o}/{r}/commits/{sha}/check-runs?filter=all` en commits del lab y de este repo | U8: `external_id`, `app.id`, `app.slug`, `output`; U12 (parte API): busca un commit con **dos check runs del mismo nombre** (jobs de Actions de igual nombre en distintos workflows) y registra cómo los lista la API |
| 15 | `GET /repos/{o}/{r}/pulls?head=…&state=open` y `GET …/issues/{n}/comments` si existe algún PR | U9: `head.sha`, `head.repo`, `base.repo.id`, `user.type`, `user.login` de un bot |
| 16 | `GET /rate_limit` y los headers `x-ratelimit-*` de cualquier respuesta | nombres reales y `x-ratelimit-resource` |

**Verificación de la app de desarrollo (tu punto 3).** El script imprime una tabla "requerido vs. real" con
la app y con cada instalación, y una lista "Falta configurar":

| | Requerido (§9.6) | Qué compara el script |
|---|---|---|
| Actions | read | `permissions.actions` de la app y de la instalación |
| Checks | write | ídem |
| Contents | read | ídem |
| Metadata | read | ídem |
| Pull requests | write | ídem |
| Eventos | `workflow_run` (más `issue_comment` en P3) | `events` de la app y de la instalación |
| Webhook | URL de smee, activo | `GET /app/hook/config` |
| Instalada en | lab y `cicd-doctor` | `GET /installation/repositories` |

Todo lo que falte lo tenés que cambiar vos en la configuración de la app; el script solo lo informa.

**Salida:** crudo en `<scratchpad>\github-spike\raw\` (fuera del repo). Un segundo paso escribe copias
**sanitizadas** en `plans\p1c-spike\sanitized\` (sin commitear, para que las puedas abrir y revisar; pasan a
`tests/fixtures/github/` recién cuando arranque b1, b2 o c, y solo las que hayas revisado): owner →
`acme`, logins y nombres inventados, ids, SHAs y `node_id` reemplazados de forma consistente, sin emails,
sin URLs firmadas, sin tokens. Antes de escribir cada archivo, un escaneo busca en el texto sanitizado todo
lo que el script vio en crudo (logins, emails, SHAs, ids, los tokens y el JWT de esa ejecución) y no escribe
el archivo si encuentra algo. **Vos las revisás antes de que entren a ningún PR**, como pediste. Las
respuestas a U1–U13 van a `plans\p1c-spike.md` (sin commitear) y de ahí a D-67, D-70 a D-72 y D-76.

**Lo que el spike no puede contestar** (requiere escribir): si un check run `completed` puede volver a
`in_progress` (U10), cómo dibuja la interfaz dos checks del mismo nombre (U12, parte visual), si el nombre
se muestra como texto plano (U12), y crear un check run sobre el `head_sha` de un fork (U11). U10 y U12 se
prueban en (c) con el Apéndice B; U11, en la casilla 3 de (e).

---

## 2. PRs

### (a) Persistencia — `p1c-persistence`

**Alcance**
- `PostgresFailureStore` con todos los métodos del puerto salvo `similar` (P2: necesita embeddings; sus
  tests de contrato quedan solo en memoria, con la razón escrita).
- Checkpoint por intento (D-69, D-70): `save_run_analysis(record) -> RunAnalysisRecord` atómico, el primero
  que escribe gana y el segundo recibe lo guardado; `run_analysis(run_id, attempt) -> RunAnalysisRecord | None`.
- Lock de publicación: `publication_lock(key)` (context manager async). Postgres: advisory lock de sesión
  sobre una conexión dedicada, con espera máxima de 30 s → `TransientError`. Memoria: un `asyncio.Lock` por
  clave.
- Guarda de intento más nuevo en SQL: `ON CONFLICT … DO UPDATE … WHERE (last_run_id, last_attempt) <=
  (EXCLUDED…)`, devuelve si actualizó.
- `PostgresUsageLedger` con la consulta de §6 (`pg_advisory_xact_lock(hashtext('llm_budget'))`), el día UTC
  del `Clock` inyectado (como D-48) y los límites por constructor.
- Migración `0003`: en `failures`, `conclusion`, `runner_os`, `started_at`, `completed_at`,
  `step_started_at`, `step_completed_at` (la tabla está vacía en todos lados, así que `conclusion`,
  `started_at` y `completed_at` pueden ser `NOT NULL`); en `run_attempts`, `analyzed_at timestamptz` (marca
  del checkpoint); `CREATE UNIQUE INDEX diagnoses_published_idx ON diagnoses (failure_id) WHERE published`.
- Modelos nuevos en `core/models.py`: `DiagnosisUsage` (versión congelada del `LLMUsage` del pipeline),
  `StoredFailure` (failure, published, rules, usage), `RunAnalysisRecord` (run, attempt, items) y
  `failure_id_for(run_id, attempt, job_id)` (UUIDv5 determinista).

**Archivos:** `adapters/postgres/store.py`, `adapters/postgres/ledger.py`,
`adapters/postgres/migrations/versions/0003_….py`, `adapters/memory/store.py`, `core/models.py`,
`core/ports.py`, `core/diagnosis/pipeline.py` (convierte `LLMUsage` → `DiagnosisUsage`),
`tests/contract/test_failure_store.py`, `tests/contract/test_usage_ledger.py`,
`tests/integration/test_migrations.py`, `tests/integration/test_ledger_concurrency.py`,
`tests/integration/test_publication_lock.py`, `tests/factories.py`.

**Tests** (los de Postgres llevan la marca `integration`, D-51)
- Contrato `FailureStore` en memoria **y Postgres**: lo existente, más checkpoint (ida y vuelta exacta de
  un `RunAnalysisRecord` con 3 fallas, segundo `save_run_analysis` devuelve el primero, `run_analysis` de
  un intento sin analizar es `None`, intento con 0 fallas es un checkpoint válido), un solo `published` por
  falla, uso en la fila publicada, guarda de publicación por `(run_id, attempt)`, aislamiento por repo
  (D-10).
- Contrato `UsageLedger` en memoria **y Postgres**: los 10 tests actuales (leen la fila con un helper por
  adapter).
- **Postgres**: `test_ledger_concurrency.py`: dos engines (como dos procesos), 40 `reserve` concurrentes;
  con presupuesto para exactamente 7 → exactamente 7 reservas y `reserved_usd ≤ budget`; con tope de 30
  llamadas por instalación → exactamente 30; `settle` concurrente no pierde montos.
- **Postgres**: `test_publication_lock.py`: el segundo dueño espera al primero; lo obtiene apenas se
  libera; lo obtiene si la conexión del primero muere; pasada la espera máxima es `TransientError`.
- **Postgres**: `test_migrations.py` verifica `0003` (columnas, índice parcial, downgrade).

**Terminado cuando:** CI verde; las dos suites de contrato pasan en ambos adapters; nada cableado (el worker
no cambia); SPEC §4.3 (puerto), §6 (columnas de `0003`, consultas del store, checkpoint) y §12 (fila
Contract) alineados; D-69 y D-70 escritos.

**§13:** ninguna casilla. Deja listos el checkpoint y el presupuesto bajo concurrencia que usan P1a y P1c.

### (b1) HTTP y auth de GitHub — `p1c-github-auth`

**Alcance**
- Dependencias: `httpx2` explícita en runtime (ya está en el lock), `pyjwt[crypto]` (trae `cryptography`).
- `adapters/github/http.py`: `GitHubHttp` sobre `httpx2.AsyncClient` (`base_url`, `Accept:
  application/vnd.github+json`, `X-GitHub-Api-Version` fijo en la versión que confirme el spike,
  `User-Agent: cicd-doctor`, timeouts de 5 s de conexión y 30 s de lectura, sin seguir redirects);
  paginación por `Link: rel="next"` con `per_page=100` y tope de páginas; el mapeo de errores de D-67 en un
  solo lugar, incluido el rate limit.
- `adapters/github/auth.py`: carga de la clave (contenido PEM o ruta), JWT de la app, tokens de
  instalación por `(instalación, repo, permisos)` con caché, renovación a 5 minutos del vencimiento, una
  sola petición por clave aunque haya concurrencia (single-flight), invalidación tras un 401;
  `app_identity()` (`GET /app`: slug e id, en caché); conjuntos de permisos por caso de uso (D-68).
- `configure_logging()` baja los loggers de `httpx2` y `httpcore2` a WARNING: a INFO escriben la URL de cada
  pedido, y la de un blob lleva la firma en la query.
- `tests/unit/test_architecture.py`: prohíbe `httpx2`, `jwt` y `cryptography` en `core`.

**Archivos:** `pyproject.toml`, `uv.lock`, `adapters/github/{http,auth}.py`, `app/container.py` (solo
logging), `tests/github_fake.py` (FakeGitHub: router con estado sobre `httpx2.MockTransport`; registra cada
pedido con sus headers; fallas programables; en este PR solo las rutas de la app y de los tokens, y las
siguientes PRs le suman las suyas), `tests/fixtures/github/{app,installation_token}.json` (del spike,
sanitizados), `tests/adapters/test_github_{http,auth}.py`, `tests/unit/test_architecture.py`.

**Tests** (offline, en `tests/adapters/`; ninguno usa Postgres)
- JWT: con un par RSA generado en la sesión de tests (nunca commiteado), `iss` = app id, `iat` = ahora −
  60 s, `exp` ≤ ahora + 10 min, RS256; la clave se carga desde contenido y desde ruta (`tmp_path`).
- Token (NFR-06): el cuerpo lleva `repository_ids: [repo]` y exactamente el subconjunto de permisos;
  reutilización en caché; renovación a `expires_at` − 5 min con `FakeClock`; 10 pedidos concurrentes →
  un solo POST; 401 → invalida, pide uno nuevo y reintenta una vez; segundo 401 → `TransientError`; 403
  (suspendida), 404 (desinstalada), 422 (permiso no otorgado) al pedir token → `PermanentError`.
- Paginación: 3 páginas por `Link`, tope de páginas.
- Rate limit: `x-ratelimit-remaining: 0` → `retry_after` hasta `x-ratelimit-reset`; secundario con
  `retry-after` → ese valor; secundario sin header → 60 s; 403 de permiso → `PermanentError`.
- Mapeo de errores (D-67): 5xx, timeout y error de conexión → `TransientError`; 404/410 → permanente; 422 →
  permanente.
- Ningún mensaje de error ni línea de log contiene el token, la query de una URL ni texto del cuerpo (un
  canario en el cuerpo falso y `caplog`).

**Terminado cuando:** CI verde; nada cableado; SPEC §4.2, §8 y §12 (respx → `httpx2.MockTransport`)
alineados; D-67 y D-68 escritos; CLAUDE.md nombra el adapter de auth.

**§13:** ninguna.

### (b2) `LogSource` — `p1c-github-logs`

**Alcance**
- `adapters/github/logs.py`: `GitHubLogSource` (`LogSource`): `jobs()` (jobs del intento, paginados,
  parseados con `jobs.py`, que ya existe) y `job_log()` según D-71: redirect sin `Authorization` con un
  cliente aparte, `Range` por offset, `TailBuffer` (ya existe), tope de 100 MB, decodificación UTF-8 con
  reemplazo.
- `core/errors.py`: `LogsUnavailable(PermanentError)` (404/410: logs vencidos o borrados). El caso de uso
  de d1 aplica la regla de gracia; el adapter solo informa.
- `core/ports.py`: la docstring de `LogSource.job_log` documenta la excepción.
- `tests/github_fake.py` suma las rutas de jobs, logs y blobs, con el host de blobs aparte.

**Archivos:** `adapters/github/logs.py`, `core/errors.py`, `core/ports.py`, `tests/github_fake.py`,
`tests/fixtures/github/{jobs_run_attempt,log_redirect,log_blob_headers}.json` (del spike, sanitizados),
`tests/adapters/test_github_logs.py`.

**Tests** (offline, en `tests/adapters/`)
- Jobs: parseo del fixture real, 3 páginas.
- El pedido al blob **no** lleva `Authorization` ni ningún header de GitHub (asertado sobre lo que registró
  FakeGitHub); solo `https`.
- `Content-Length` > máximo → segundo pedido con `Range: bytes=N-`, resultado truncado que empieza en una
  línea completa; sin `Content-Length` → ring buffer, `truncated = true`; 100 MB de stream → corta y no lee
  más; el servidor ignora el `Range` (200 en vez de 206) → se queda con la cola igual.
- `Content-Encoding` según lo que diga el spike (U2).
- 404 y 410 → `LogsUnavailable`; 5xx, timeout y error de conexión → `TransientError`; 403 →
  `PermanentError`.
- Ningún mensaje de error ni log contiene la query firmada (canario).

**Terminado cuando:** CI verde; nada cableado; SPEC §7.1 paso 5 y §8 alineados; D-71 escrito con las
respuestas reales del spike.

**§13:** ninguna.

### (c) Escritura, PR del run y nombre del check — `p1c-github-write`

**Alcance**
- Puertos (D-70, D-72): `Reporter.start_check(run, known_id) -> int`,
  `Reporter.complete_check(run, check_run_id, output: CheckOutput)`, `Reporter.upsert_comment(target, body,
  known_id) -> int`; `CheckOutput` pasa de `core/render.py` a `core/models.py`; puerto nuevo
  `PullRequestSource.find(run) -> int | None`.
- `core/render.py`: `check_name(workflow_name) -> str` (D-76) y el nombre del workflow en el encabezado del
  comentario (`### CICD Doctor: <workflow>`, con `inline`), porque hay un comentario por (PR, workflow) y sin
  el nombre dos comentarios no se distinguen. **Esto último lo agrego yo**: decime si lo sacás.
- `adapters/github/reporter.py`: `GitHubReporter`.
  - `start_check`: con id conocido → `PATCH` a `in_progress` ("Analyzing failure… (attempt N)"), y también
    actualiza `name` por si renombraron el workflow; 404 o sin id → busca en
    `GET /repos/{o}/{r}/commits/{sha}/check-runs?app_id=…&filter=all` el que tenga
    `external_id = "cicd-doctor:run:{run_id}"` (por `external_id`, no por nombre: el nombre es texto
    editable); si no hay, `POST` con `name`, `head_sha`, `status: in_progress`, `external_id` y
    `details_url` (la URL del run).
  - `complete_check`: `PATCH` con `status: completed`, `conclusion: neutral`, `completed_at`, `output`.
  - `upsert_comment`: id conocido → `PATCH /issues/comments/{id}`; 404 o sin id → recorre los comentarios
    del PR (paginados, tope de 10 páginas) y toma solo los de `user.login == "<slug>[bot]"` cuya primera
    línea es el marcador de ese (PR, workflow) → `PATCH`; si no hay → `POST`. **Nunca** edita un
    comentario que no escribió el bot.
- `adapters/github/pulls.py`: `GitHubPullRequests.find(run)` según D-72.
- `adapters/github/webhooks.py`: `pull_requests[]` se filtra por `base.repo.id == repository.id`.
- Prueba manual de U10 y U12 (Apéndice B), con tu rama descartable. Su resultado decide el paso 2 de D-70 y
  si D-76 necesita escapar el nombre.

**Archivos:** `core/models.py`, `core/ports.py`, `core/render.py`, `adapters/github/{reporter,pulls,webhooks}.py`,
`tests/github_fake.py`, `tests/fixtures/github/{check_run,issue_comments,pulls}.json` (sanitizados, del
spike), `tests/fixtures/webhooks/workflow_run_completed_foreign_pr.json` (sintético),
`tests/adapters/test_github_{reporter,pulls}.py`, `tests/unit/test_render.py`,
`tests/unit/test_github_webhooks.py`.

**Tests** (offline, sin Postgres)
- Nombre del check (D-76): propiedades con Hypothesis: nunca contiene saltos de línea, caracteres de
  control ni invisibles; nunca pasa de 80 caracteres; es determinista; un secreto plantado del corpus no
  sale; vacío o solo espacios → `CICD Doctor`; el prefijo siempre es `CICD Doctor`. Casos fijos: nombre
  largo cortado con `…`, workflow con HTML, markdown, `@` y URL. Sin escapes de markdown (la API toma texto
  plano), salvo que U12 diga lo contrario.
- Encabezado del comentario: golden actualizado; el nombre pasa por el escáner de amenazas de
  `evals/runner/markdown_threats.py` con ataques fijos y Hypothesis.
- Check run: `POST` con `external_id`, `details_url` e `in_progress`; con id conocido → `PATCH`, ningún
  `POST`; id conocido que da 404 → búsqueda por `external_id` → `PATCH`; id perdido y nada encontrado →
  `POST`; `complete` → `neutral` y la salida dentro de los límites; el token pedido es el de diagnóstico;
  workflow renombrado → el `PATCH` actualiza el nombre.
- Comentario: id conocido → `PATCH`; id que da 404 → búsqueda por marcador → `PATCH`; el marcador copiado
  en un comentario de **otro usuario** se ignora (seguridad); el marcador de otro workflow se ignora;
  paginación; tope de páginas.
- PR: número del payload; fork → búsqueda por `head`, coincide `head.sha` y `head.repo.full_name`; sin
  coincidencia → `None`; varios → el número más bajo; fork borrado (`head_repo_full_name` vacío) → `None`.
- Webhook: un PR de otro repo en `pull_requests` no se toma como PR del run.

**Terminado cuando:** CI verde; nada cableado; SPEC FR-11, §4.3, §7.1 paso 2, §7.5 y la primera casilla de
P1c en §13 alineados; D-18 enmendada; D-72 y D-76 escritos; U10 y U12 registrados (en D-70 y D-76).

**§13:** ninguna (el texto de la primera casilla cambia, sin marcarla).

### (d1) Caso de uso y fakes — `p1c-diagnose-usecase`

**Alcance**
- `core/usecases/diagnose_failed_run.py`: el flujo de D-70 (guarda → check `in_progress` → checkpoint o
  análisis → check completo → comentario). El análisis lista los jobs, selecciona (`select_failed_jobs`),
  baja los logs, llama a `analyze_jobs` y corre `DiagnoseJob` por job **en paralelo** (`gather`, máximo 3;
  el ledger es seguro bajo concurrencia). Sin `RepoConfigSource` en P1c: se usan los valores por defecto
  (`comment: true`, `llm: auto`), así que no se pide `contents: read`.
- La regla de gracia de los logs (D-71): `LogsUnavailable` dentro de los 10 minutos posteriores a
  `completed_at` → `TransientError(retry_after=30 s)`; después → "Could not diagnose" (`logs_expired`) y
  `PermanentError`.
- Filas de instalación y repo (D-75): `DiagnoseRunPayload` gana `account_login` y `account_type`
  (opcionales, para los jobs ya encolados) y el webhook los llena; `InstallationStore` gana "insertar si
  falta", en ambos adapters y en el contrato. El caso de uso inserta la instalación si falta (sin pisar
  `suspended_at`/`deleted_at`) y hace upsert del repo con `default_branch` y `private`, después de obtener
  el token (prueba de que el repo sigue instalado).
- `adapters/memory/github.py`: `FakeLogSource`, `FakeReporter`, `FakePullRequests` (grabadores,
  programables) para los unit tests.
- Una línea de log por publicación con `run_id`, `attempt` y los segundos desde `completed_at`, para medir
  NFR-02 en (e) sin structlog.

**Archivos:** `core/usecases/diagnose_failed_run.py`, `core/models.py`, `core/ports.py` (InstallationStore),
`adapters/github/webhooks.py`, `adapters/memory/{github,installations}.py`,
`adapters/postgres/installations.py`, `tests/unit/test_diagnose_failed_run.py`,
`tests/contract/test_installation_store.py`, `tests/unit/test_github_webhooks.py`.

**Tests** (unit, con fakes en memoria; entran en el gate rápido)
- Camino feliz: un check creado y completado `neutral`, un comentario con el marcador, ids guardados.
- Sin PR → sin comentario. Fork → búsqueda del PR y reglas con `fork_policy`.
- Presupuesto 0 → `budget` y el texto del límite en el check. Error del LLM → `llm_error`. Sin LLM
  configurado → `disabled`.
- Sin jobs fallidos → `no_failed_job`. 404 de logs dentro de la gracia → `TransientError`; después o 410 →
  `logs_expired` y `PermanentError`. 403 → `permission`.
- **Reintento después de un fallo al publicar → ninguna** llamada nueva al LLM ni descarga de logs, mismos
  ids de check y comentario (D-70).
- Intento viejo con uno más nuevo ya publicado → no publica nada. Intento 2 → `PATCH` del mismo check y
  comentario con "Attempt 2".
- Los secretos plantados (de `tests/redaction_corpus.py`) en un log falso no aparecen en ninguna llamada al
  reporter ni en lo guardado.
- Instalación sin fila → se inserta; con `deleted_at` → no se pisa.

**Terminado cuando:** CI verde; nada cableado (el handler sigue de relleno); SPEC §4.2 y §7.1 alineados; D-75
escrito.

**§13:** ninguna.

### (d2) Handler, container, e2e y Postgres — `p1c-diagnose-wiring`

**Alcance**
- `worker/handlers.py`: `diagnose_run_handler`: payload inválido → `PermanentError`; en el último intento, o
  ante `PermanentError`, publica "Could not diagnose" con su motivo (best effort, con su propio timeout) y
  re-lanza (D-74).
- `app/container.py`: construye `GitHubHttp`, auth, `GitHubLogSource`, `GitHubReporter`,
  `GitHubPullRequests`, `PostgresFailureStore`, `PostgresUsageLedger`, `TiktokenCounter`,
  `OpenAILLMClient` solo si hay clave **y** modelo (si no, `None` → `disabled`), `CostModel` con los
  precios, `DiagnoseJob`. El worker no arranca sin `GITHUB_APP_ID`/`GITHUB_PRIVATE_KEY` (el mensaje nombra la
  variable, nunca el valor). Tamaño del pool = consumidores + heartbeats + locks.
- `.env.example` y `app/settings.py`: la validación de arranque; el test que mantiene iguales las listas de
  variables sigue verde.

**Archivos:** `worker/handlers.py`, `app/container.py`, `app/settings.py`, `tests/unit/test_worker_handlers.py`,
`tests/e2e/test_diagnose_e2e.py`, `tests/integration/test_diagnose_postgres.py`,
`tests/integration/test_diagnose_crash.py`.

**Tests**
- Unit: el handler (payload inválido, último intento publica `internal_error` y re-lanza, `PermanentError`
  publica su motivo, un error al publicar el "Could not diagnose" no tapa el original).
- E2E offline (`tests/e2e/`, sin Postgres): `POST /webhooks/github` con el fixture firmado (TestClient, cola
  en memoria) → `Worker.run_one` con los **adapters reales de GitHub** sobre FakeGitHub + store en memoria +
  `FakeLLM`; asertan la secuencia exacta: token con alcance, jobs, log 302 + blob sin `Authorization`,
  `POST` del check `in_progress`, `PATCH` `completed`/`neutral`, `POST` del comentario con marcador.
  Además: entrega duplicada → ningún job nuevo; intento 2 → solo `PATCH`; dos runs del mismo PR y workflow
  en paralelo → **un** `POST` de comentario; `POST` del comentario exitoso pero `save_publication` falla
  (falla inyectada) → reintento → búsqueda por marcador → `PATCH`, un solo comentario.
- **Postgres** `test_diagnose_postgres.py`: el camino feliz y el del intento 2 con `PostgresJobQueue`,
  `PostgresFailureStore` y `PostgresUsageLedger`.
- **Postgres** `test_diagnose_crash.py`: el handler del worker A se cancela mientras está bloqueado en la
  descarga del blob (después del `POST` del check), el `FakeClock` pasa el lease, el worker B lo toma y
  termina; FakeGitHub registra **exactamente un** `POST` de check run y el job queda `done` una vez. Es en
  proceso: el crash con un proceso real matado ya existe para la cola, pero el fake de GitHub no cruza
  procesos.

**Terminado cuando:** CI verde con e2e e integración; `uv run cicd-doctor worker` arranca con el `.env`
actual; SPEC §4.2 (implementado), §4.4, §7.1 (flujo con checkpoint) y §8 (muerto → check) alineados; D-74
escrito; CLAUDE.md dice que el worker diagnostica de verdad.

**§13:** P1a "… and exactly one check run is created for it (integration test with the fake reporter)". Las
casillas de P1c quedan cubiertas offline, pero se marcan recién en (e).

### (e) Verificación real — `p1c-live`

**Alcance**
- Lab: un workflow nuevo `lab/.github/workflows/pr-py-modnf.yml` (`on: pull_request`, el caso `py-modnf`),
  sin tocar los 23 existentes. Lo publicás en el repo del lab.
- El runbook del Apéndice A pasa a `lab/README.md` (sección "Live verification (P1c)"), siguiendo la
  convención de la captura de P1b.
- Cada casilla de §13 se marca solo con la evidencia que leí con `gh` (URLs de runs, ids, tiempos), como en
  P1b.
- Lo que aparezca (por ejemplo U11) se arregla en PRs chicos aparte y se registra como D-NN.

**Archivos:** `lab/.github/workflows/pr-py-modnf.yml`, `lab/README.md`, `SPEC.md` §13, `CLAUDE.md`, `README.md`.

**Tests:** los de las casillas son manuales y de lectura con `gh`. No agrego tests `-m live` de GitHub:
dependen de estado (PRs abiertos, forks) que un test no puede preparar sin escribir en GitHub.

**Terminado cuando:** las casillas 1, 2, 4, 5 y 6 están marcadas con evidencia verificada. La casilla 3
(fork) queda abierta en SPEC §13 con el motivo "falta una organización para el fork; se verifica al final de
P2 o P3", como las abiertas de P1b, y su cobertura offline (tests de d1 y d2) queda dicha en el mismo texto.

**§13:** P1c, 5 de 6 (la 3 queda abierta).

### (f) Timeouts a nivel de job (opción a de §14) — `p1c-job-timeouts`

**Separable:** si se complica, pasa a P2 sin bloquear (e).

**Alcance:** el webhook también encola los `workflow_run` `cancelled` como `diagnose_run`
(`conclusion: "cancelled"`, misma clave de dedupe); para esos runs el caso de uso lista los jobs y, para
cada job `cancelled`, lee las anotaciones de su check run (método nuevo del `LogSource`). Solo si alguna dice
`has exceeded the maximum execution time` el job se trata como `timed_out` (dispara GHA-TIMEOUT por
`on_conclusion`) y sigue el camino normal. Si no, termina sin publicar nada: **para los runs cancelados el
check run se crea recién después de esa decisión**. `evals capture` acepta el caso `job-timeout`.

**Tests:** unit (anotación presente → `timeout`; ausente → silencio, ninguna llamada al reporter; mezcla de
jobs), adapter (endpoint de anotaciones, paginación), webhook (`cancelled` encolado), y en vivo con el
workflow `job-timeout` del lab → diagnóstico `timeout`, y una cancelación manual → nada.

**Terminado cuando:** CI verde; FR-04, FR-06, §7.1 y §14 alineados; D-73 escrito; el caso `job-timeout`
capturado si el lab lo produce.

**§13:** ninguna casilla nueva; resuelve la pregunta de §14 y destraba el caso `job-timeout` mencionado en
la primera casilla de P1b.

---

## 3. Decisiones nuevas

### D-67 — Cliente HTTP de GitHub: `httpx2`, probado con `httpx2.MockTransport` y un FakeGitHub propio

- **Decisión:** el adapter de GitHub usa `httpx2.AsyncClient`, que ya está en runtime por `openai` 3.x (se
  declara explícita). Dos clientes: uno para `api.github.com` (headers de la API, sin seguir redirects) y
  otro para los blobs de logs, sin headers por defecto ni credenciales. Los tests inyectan
  `httpx2.MockTransport` con `tests/github_fake.py`: un FakeGitHub con estado (check runs, comentarios,
  PRs), que registra cada pedido con sus headers y tiene fallas programables. **No se usa respx.** Sin
  reintentos dentro del cliente: los reintentos son del job (D-23) y, con el checkpoint de D-70, repetir un
  job después de analizar solo repite `PATCH`s baratos. La única excepción es el 401 (renovar el token una
  vez). Mapeo de errores en un solo lugar: 401 → renovar y reintentar una vez, después transitorio; 403 o
  429 de rate limit → `TransientError` con `retry_after` (`x-ratelimit-reset`, `retry-after`, o 60 s para
  el secundario sin header); otros 403 → `PermanentError`; 404/410 → permanente (con la gracia de D-71 para
  logs); 422 → permanente; 5xx, timeout y conexión → transitorio. Los mensajes nombran método, plantilla
  del endpoint y status, nunca el cuerpo, la URL firmada ni el token (como D-63). **Excepción medida en el
  spike:** el 403 de permiso trae `x-accepted-github-permissions` (por ejemplo `secrets=read`), una cabecera
  de GitHub que dice qué permiso faltó; se copia al mensaje solo si cumple `^[a-z_=,; ]{1,100}$`. Los loggers de
  `httpx2`/`httpcore2` quedan en WARNING.
- **Alternativas:** `httpx` + `respx` (D-28): dos pilas HTTP en el mismo proceso, `respx` parchea
  globalmente y su soporte de `httpx2` es incierto e innecesario; `aiohttp` + `aioresponses` (una tercera
  pila, otra API de tests que la de OpenAI); `vcrpy` con cassettes grabados (registran tokens y URLs
  firmadas, y el replay esconde justo los headers que hay que asertar, como la falta de `Authorization`);
  `githubkit` (descartado en D-28).
- **Consecuencias:** enmienda D-28 y D-44; SPEC §12 cambia "respx" por `httpx2.MockTransport`; el test de
  arquitectura prohíbe `httpx2` en `core`. Un 502 aislado de GitHub cuesta un reintento del job (backoff de
  hasta 30 s): aceptable para NFR-02, que es un p95.

### D-68 — Autenticación de la app: PyJWT, tokens por repo y por permiso, caché en memoria

- **Decisión:** `pyjwt[crypto]` firma RS256 con `iat` = ahora − 60 s (deriva de reloj), `exp` = ahora +
  9 min (GitHub acepta hasta 10) e `iss` = `GITHUB_APP_ID`, con el tiempo del `Clock`. La clave sale de
  `GITHUB_PRIVATE_KEY` (contenido PEM, o ruta a un archivo leído al arrancar) y vive en un `SecretStr`.
  Los tokens se piden con `repository_ids: [repo_id]` y el subconjunto del caso de uso (diagnóstico:
  `actions: read, checks: write, pull_requests: write`; P2 y P3 suman `contents: read` donde haga falta), se
  guardan en memoria por `(instalación, repo, permisos)` hasta 5 minutos antes de `expires_at`, con una sola
  petición en vuelo por clave. Un job usa un solo token para todo. Al pedir token: 403 (suspendida), 404
  (desinstalada) y 422 (permiso no otorgado) son permanentes. Las claves de test se generan con
  `cryptography` en cada sesión: nunca hay un PEM commiteado (`*.pem` sigue ignorado) y no se toca el corpus
  de redacción.
- **Alternativas:** RS256 a mano con `cryptography` (una dependencia menos, pero código criptográfico
  propio sin ganancia real); `joserfc`/`authlib` (más grandes y generales); token de instalación sin
  alcance por repo (caché más simple, pero viola NFR-06 y D-21).
- **Consecuencias:** `cryptography` entra en runtime (tiene wheels para Windows y Linux con Python 3.13).
  El app id sigue siendo el `iss`; si querés el Client ID (lo que GitHub recomienda hoy), es una variable
  nueva y otra D-NN.

### D-69 — Store y ledger en Postgres: checkpoint por intento, ids deterministas, uso en la fila publicada

- **Decisión:**
  - `PostgresFailureStore` implementa el puerto salvo `similar` (P2).
  - `save_run_analysis` guarda en **una** transacción el run, el intento, las fallas y sus diagnósticos, y
    pone `run_attempts.analyzed_at`. El primero que escribe gana: el segundo (un worker cuyo lease venció
    mientras terminaba) recibe lo ya guardado. `run_analysis(run_id, attempt)` lo reconstruye entero.
  - `failures.id = uuid5(namespace, "{run_id}:{attempt}:{job_id}")`: el mismo id en memoria y en Postgres,
    en cualquier reintento.
  - Diagnósticos: siempre la fila `rules`; la fila `llm` solo si el LLM devolvió una respuesta válida
    (D-35); **una sola** fila `published` por falla (índice parcial único); el uso del LLM (modelo,
    llamadas, tokens, costo, latencia) va en la fila publicada, sea `llm` o el fallback de reglas que pagó
    llamadas fallidas, así `SUM(cost_usd)` es el gasto real.
  - `0003` agrega lo necesario para reconstruir `FailedJob` y la marca `analyzed_at`.
  - Ledger: la consulta de §6, el día UTC del `Clock` (D-48). Una reserva de un worker muerto antes de
    `settle` queda retenida hasta que termina el día UTC: conservador.
  - Lock de publicación: `pg_advisory_lock(hashtextextended(clave, 0))` de sesión, en una conexión del pool
    reservada mientras dura, con espera máxima de 30 s (`TransientError`); se libera al salir o si la
    conexión muere. Una colisión de hash solo serializa de más.
- **Alternativas:** métodos por fila (`save_run`, `save_failure`, `save_diagnosis`) sin transacción común
  (checkpoints a medio escribir); ids aleatorios con `ON CONFLICT … RETURNING` (funciona en Postgres, pero
  el store en memoria tendría que imitarlo); una tabla `checkpoints` con un JSON (simple, pero duplica el
  esquema que P2 consulta por columnas); `SELECT … FOR UPDATE` sobre la fila de publicación (no existe antes
  de la primera publicación).
- **Consecuencias:** cambia el puerto `FailureStore` (SPEC §4.3) y §6. Los métodos por fila quedan para
  P2 (`record_outcome`).

### D-70 — Frontera entre persistir y publicar; "exactamente un comentario"

- **Decisión:** el diagnóstico se calcula una sola vez por intento y se publica tantas veces como haga
  falta, siempre desde lo guardado.
  1. **Guarda:** si el check de `(repo, run)` ya fue publicado por un intento mayor, el job termina sin
     hacer nada (intento viejo).
  2. **Check `in_progress`**, bajo el lock de la clave del check: id guardado → `PATCH`; si no, búsqueda
     por `external_id` (y `app_id`); si no, `POST`. El id se guarda **en el acto**, antes de analizar.
  3. **Checkpoint:** si `run_analysis(run, attempt)` existe, se usa; si no, jobs → logs → análisis →
     `DiagnoseJob` → `save_run_analysis`. El commit es el checkpoint. El LLM solo se llama antes del
     checkpoint.
  4. **Check completo**, bajo el lock: guarda → `PATCH` `completed`/`neutral` → `save_publication`.
  5. **Comentario**, bajo el lock de `(repo, PR, workflow)`: guarda por `(run_id, attempt)` → upsert (id
     guardado → marcador entre los comentarios del bot → `POST`) → `save_publication`.
  - **Qué se reintenta si publicar falla después de persistir:** solo 2, 4 y 5, que son idempotentes
    (`PATCH` del mismo contenido), desde lo guardado: ninguna descarga de logs, ninguna llamada al LLM,
    ninguna reserva nueva. El tope "una llamada por job por intento" (FR-13) vale entre reintentos del job.
  - **Exactamente un comentario:** (i) el lock serializa a todos los que escriben una misma clave, de
    cualquier worker y de cualquier run (dos pushes seguidos que fallan en el mismo PR); (ii) dentro del
    lock, el id guardado o, si se perdió (crash entre el `POST` y `save_publication`), la búsqueda por
    marcador encuentra el comentario que creamos; (iii) solo cuentan los comentarios de `<slug>[bot]`, así
    que un usuario que copia el marcador no puede secuestrarlo ni bloquearlo. La guarda dentro del lock
    impide que un intento viejo pise a uno nuevo. El check run tiene la misma garantía con `external_id =
    "cicd-doctor:run:{run_id}"`.
  - Residuos aceptados: que la lista de comentarios de GitHub no muestre uno creado hace menos de un
    segundo **y** que justo ahí muera el worker; PRs con más de 1.000 comentarios (tope de páginas); un
    crash entre la respuesta del LLM y el commit del checkpoint, que repite una llamada (el ledger la acota).
  - Puertos: los cambios de `Reporter` de (c) y el `publication_lock` de (a).
- **Alternativas:** publicar y después persistir (un crash pierde el registro y el reintento llama otra
  vez al LLM); un *outbox*: persistir y encolar un job `publish` aparte (separación más limpia, pero otro
  tipo de job, su dedupe y su orden; queda como refactor posible); sin lock, solo la guarda (carrera entre
  leer y escribir: un intento viejo puede pisar el comentario nuevo, y dos runs pueden hacer `POST` los dos);
  "reclamar" la fila de `comment_publications` antes del `POST` (exige `comment_id` nullable y que el reclamo
  venza si el worker muere: es reimplementar un lock).
- **Consecuencias:** el lock ocupa una conexión durante una llamada a GitHub (~1 s): el pool suma una por
  consumidor. U10 decide el paso 2 para un intento nuevo: si GitHub no deja volver a `in_progress` un check
  completado, el paso 2 solo actualiza el `output` ("Analyzing attempt N…") y el check sigue `completed`.

### D-71 — Descarga de logs

- **Decisión:** `GET …/jobs/{id}/logs` sin seguir redirects; el `Location` se pide con el cliente de blobs,
  sin `Authorization` ni headers de GitHub, solo `https`. El blob se pide en streaming: si `Content-Length`
  > `LOG_MAX_BYTES`, se cierra y se pide `Range: bytes={len − max}-`; si no hay `Content-Length` o el
  servidor ignora el `Range`, todo pasa por `TailBuffer` con el tope de 100 MB. Bytes → texto con UTF-8 y
  reemplazo (el BOM del principio lo quita `parse_log`).
  **Confirmado por el spike (2026-11-16):** el blob es de Azure (`productionresultssa<N>.blob.core.windows.net`,
  el número varía), sin `Content-Encoding`, con `Content-Length` y `Accept-Ranges: bytes`. `bytes=N-`
  devuelve 206 con `Content-Range`; **`bytes=-N` (sufijo) lo ignora y devuelve 200 con el cuerpo entero**,
  así que se usa siempre el offset, y **un 200 a un pedido con `Range` se trata como cuerpo completo** (pasa
  por el `TailBuffer`; test propio). La URL firmada seguía válida a los 2 minutos y estaba vencida a los 10
  (403): se usa apenas llega el redirect, y si hubiera que repetir el pedido del blob se pide el redirect de
  nuevo, no se reusa la URL. Host: `https` obligatorio; los sufijos `.blob.core.windows.net`, `.githubusercontent.com` y
  `.github.com` son los esperados, y otro host se sigue sin credenciales pero se registra en WARNING (solo el
  host, sin la query). 404 o 410 → `LogsUnavailable`; el caso de uso
  aplica la gracia: un 404 dentro de los 10 minutos posteriores a `completed_at` es transitorio (los logs
  pueden tardar en estar), después es "Could not diagnose" (`logs_expired`); 410 es siempre permanente. La
  URL firmada nunca se loguea ni entra en un error. Si algún día el blob trajera `Content-Encoding: gzip`
  (hoy no lo trae, U2), el `Range` sobre bytes comprimidos no serviría: se descartaría el `Range` y el tope
  contaría bytes descomprimidos; un test con ese header fabricado lo cubre.
- **Alternativas:** `HEAD` y después `GET` (un pedido más siempre); seguir el redirect con el cliente de la
  API (filtra el token, §9.1); el zip del run entero (D-11 lo descartó).
- **Consecuencias:** enmienda §7.1 paso 5 (la gracia del 404) y §8. Memoria pico por job: hasta 3 logs de
  20 MB, más su texto, porque `analyze_jobs` reparte el presupuesto de tokens entre todos; con 4
  consumidores, del orden de 250 a 500 MB en el peor caso (a tener en cuenta al dimensionar en P5).

### D-72 — El PR de un run, forks incluidos

- **Decisión:** en el webhook, `workflow_run.pull_requests` se filtra por `base.repo.id == repository.id`
  (GitHub puede listar PRs de otro repo con la misma rama) y se toma el primero. Si no queda ninguno y el
  run es de fork o de un evento `pull_request*`, `PullRequestSource.find` busca
  `GET /repos/{o}/{r}/pulls?head={dueño_head}:{rama}&state=open` y acepta solo los que tienen `head.sha ==
  run.head_sha` y `head.repo.full_name == run.head_repo_full_name`; con varios, el número más bajo. Fork
  borrado o sin coincidencia → sin comentario (el check se publica igual). El número encontrado se guarda en
  `workflow_runs.pr_number`.
- **Alternativas:** `GET /repos/{o}/{r}/commits/{sha}/pulls` (devuelve PRs por commit, pero para forks
  depende de que el commit esté en el repo base, comportamiento menos documentado); confiar en
  `pull_requests[0]` sin filtrar (comentaría en un PR equivocado).
- **Consecuencias:** un cambio chico en el payload normalizado (D-56) y un fixture sintético nuevo.

### D-73 — Timeouts a nivel de job: opción (a)

- **Decisión:** opción (a) de §14, implementada en el PR (f): los runs `cancelled` se encolan, se leen las
  anotaciones de los check runs de sus jobs cancelados, y se diagnostica solo cuando una anota el timeout;
  las cancelaciones manuales y las de `concurrency: cancel-in-progress` quedan en silencio (sin check run).
- **Por qué (a) y no (b):** el timeout es una de las 8 categorías, y el `timeout-minutes` a nivel de job es
  la forma más común de tenerlo; con (b), GHA-TIMEOUT no dispara nunca en un run real y la categoría
  depende solo de los timeouts de step. El costo es bajo y acotado: un listado de jobs y una llamada de
  anotaciones por job cancelado, sin logs ni LLM, y el permiso Checks ya está.
- **Alternativas:** (b), documentar el hueco (cero trabajo, pero deja una categoría casi muerta); leer el
  log buscando `The operation was canceled.` (no distingue un timeout de una cancelación manual).
- **Consecuencias:** enmienda FR-04 y FR-06; repos con mucha cancelación por concurrencia generan jobs que
  terminan sin publicar (baratos, pero visibles en la cola). **U7 confirmada por el spike:** un run con
  `timeout-minutes` de job termina `cancelled`; `GET /check-runs/{job_id}/annotations` devuelve 200 (el id del
  job es el del check run) con `The job has exceeded the maximum execution time of 1m0s` y `The operation was
  canceled.`, y el texto coincide con el patrón de GHA-TIMEOUT. Separable: si se complica pasa a P2.

### D-74 — Publicar "Could not diagnose" y los jobs muertos

- **Decisión:** el handler de `diagnose_run`, no el worker, publica el check "Could not diagnose" con su
  motivo: 403 → `permission`; `LogsUnavailable` fuera de la gracia → `logs_expired`; sin jobs fallidos →
  `no_failed_job` (eso no es un error: el job termina `done`); cualquier error en el último intento
  (`job.attempts >= job.max_attempts`) → `internal_error`. Es best effort (con timeout propio y sin dejar
  escapar sus errores), respeta la guarda de intento más nuevo y después re-lanza para que el worker haga
  `failed` o `dead` (D-49). Instalación suspendida o desinstalada: no hay token, así que no se publica nada.
- **Alternativas:** un callback `on_give_up` en el `Worker` (más general, pero el worker no sabe nada de
  checks y hoy no lo necesita ningún otro tipo de job); no publicar nada (el check quedaría `in_progress`
  para siempre).
- **Consecuencias:** un job enterrado por el barrido de leases (el worker murió en el último intento) no
  pasa por el handler y su check queda `in_progress`. Se acepta; un barrido de checks colgados puede llegar
  con la purga de P2.

### D-75 — El payload de `diagnose_run` trae la cuenta; el caso de uso asegura instalación y repo

- **Decisión:** `DiagnoseRunPayload` gana `account_login` y `account_type` (de `repository.owner`),
  opcionales para los jobs ya encolados. Después de obtener el token, el caso de uso inserta la instalación
  si falta (sin pisar `suspended_at` ni `deleted_at`) y hace upsert del repo con `default_branch` y
  `private`. Completa D-54.
- **Alternativas:** `GET /app/installations/{id}` con el JWT (una llamada más por job para un dato que el
  webhook ya trae); exigir que exista el evento `installation` (las instalaciones previas a P1a no tienen
  fila).
- **Consecuencias:** `InstallationStore` gana un "insertar si falta"; el contrato lo cubre en ambos
  adapters.

### D-76 — El check run se llama `CICD Doctor: <workflow>`, con el nombre sanitizado y cortado

- **Decisión:** el check run de un run se llama `CICD Doctor: <workflow>`. Lo construye
  `core/render.py::check_name(workflow_name)`:
  1. el nombre pasa por `flatten` (redacta secretos, quita caracteres de control, invisibles y bidi, une en
     una línea);
  2. se corta a 60 caracteres, con `…` si se cortó;
  3. si queda vacío, el nombre es `CICD Doctor` a secas.
  El resultado nunca pasa de 80 caracteres. **No se le aplica escape de markdown:** el campo `name` de la
  Checks API es texto plano, y escapar mostraría barras invertidas. U12 lo confirma en (c) con un nombre
  cargado de markdown y HTML; si la interfaz lo interpreta, se agrega `escape` aquí y en el test.
  - La **identidad** del check no es el nombre sino `external_id = "cicd-doctor:run:{run_id}"`, junto con
    el id guardado en `check_publications`. Renombrar un workflow (o un nombre que colisiona tras el corte)
    no duplica ni pierde checks: el `PATCH` actualiza el nombre.
  - El comentario sticky, que ya es uno por (PR, workflow), lleva el nombre en su encabezado
    (`### CICD Doctor: <workflow>`, con `inline`).
  - SPEC: FR-11, §7.5 y la primera casilla de P1c en §13 dicen `CICD Doctor: <workflow>`; D-18 queda
    enmendada.
- **Por qué:** FR-11 decía `CICD Doctor` para todos, así que dos workflows fallando en el mismo commit
  producían dos checks idénticos que no se distinguen en la lista. El nombre del workflow lo controla quien
  escribe el repo (en un fork, un atacante), así que es texto no confiable como cualquier otro del run.
- **Alternativas:** nombre fijo `CICD Doctor` y distinguir solo por el título (los dos checks siguen
  indistinguibles en la lista); el nombre sin cortar (GitHub no documenta un máximo y un nombre enorme rompe
  la interfaz); escapar el markdown sin saber si hace falta (barras invertidas visibles si no la hace).
- **Consecuencias:** quien use "checks requeridos" por nombre no puede requerir `CICD Doctor` (el check es
  `neutral` y no bloquea merges, D-18: es lo esperado). U12 puede mostrar que la interfaz agrupa o
  desambigua checks homónimos; si no, el sufijo del workflow es lo que los distingue.

---

## 4. Riesgos e incógnitas de la API real

**Estado tras el spike (2026-11-16, detalle en `plans/p1c-spike.md`):**

| Estado | Incógnitas |
|---|---|
| Resueltas | U1, U2, U3 (`Range` por offset; el sufijo se ignora), U4 (cada intento tiene sus propios job ids y `attempts/{n}/jobs` lista solo los suyos; ver el caso sin observar), U5 (válida a los 2 min, vencida a los 10), U7 (anotación de timeout), U8, U13 (la app y la instalación coinciden con §9.6, nada falta) |
| Parcial | U4 (un caso: jobs no re-ejecutados, el lab no tiene workflows de dos jobs), U12 (la API lista los homónimos como objetos distintos; falta la interfaz) |
| Abiertas, dependen de que escribas | U9 (el primer comentario real del bot) |
| No comprobables en solo lectura | U10, U11 (casilla 3, abierta), U12 visual (Apéndice B) |
| Sin cambios | U6 (se mide en (e)) |

La tabla de abajo es la de antes del spike; las filas resueltas se conservan por su contexto.

| Id | Incógnita o riesgo | Impacto | Mitigación / dónde se contesta |
|---|---|---|---|
| U1 | Status y host del redirect de logs | Bajo | Spike 8; los tests no dependen del host |
| U2 | `Content-Length` y `Content-Encoding` del blob | **Alto** para el `Range`: con gzip, el rango sobre bytes comprimidos no sirve | Spike 9; D-71 tiene la salida (sin `Range`, tope sobre bytes descomprimidos) |
| U3 | ¿El blob acepta `Range` por offset? ¿Y por sufijo? | Medio | Spike 10; D-71 usa offset y cae a `TailBuffer` si se ignora |
| U4 | ¿El listado de jobs del intento N incluye los jobs reutilizados del intento 1? | Medio: podría re-diagnosticar jobs viejos o perder los nuevos | Spike 7 sobre un run flaky re-ejecutado |
| U5 | Vencimiento de la URL firmada | Bajo: el segundo pedido con `Range` es inmediato | Spike 11; si vence, se vuelve a pedir el redirect |
| U6 | Logs no disponibles justo después de `completed` (404 transitorio) | Medio: sin gracia sería "logs expirados" falso | La gracia de D-71; se observa en (e) |
| U7 | Texto exacto de la anotación de timeout y job id = check run id | Bloquea (f) | Spike 13 |
| U8 | Forma de `external_id` y filtros de `check-runs` por commit | Medio | Spike 14 |
| U9 | `user.login` de comentarios de una app | Medio: es el filtro de seguridad del marcador | Spike 15 si hay comentarios de bots; si no, se confirma en (e) |
| U10 | ¿Un check run `completed` puede volver a `in_progress`? | Medio: cambia el paso 2 de D-70 | Prueba manual en (c), Apéndice B; D-70 tiene la alternativa |
| U11 | Crear un check run sobre el `head_sha` de un fork desde el repo base | Medio | Casilla 3 de (e); está documentado como posible, falta verlo |
| U12 | Cómo se ven checks del mismo nombre, y si `name` se muestra como texto plano | Medio: decide si D-76 escapa | Spike 14 (API) y Apéndice B (interfaz) |
| U13 | La app de desarrollo y sus instalaciones tienen los permisos, eventos y repos de §9.6 | **Bloquea** (e), y b1 para el token | Spike 1 a 5: tabla "requerido vs. real" |
| R1 | `workflow_run.pull_requests` lista PRs de otros repos, y está vacío para forks y para `workflow_dispatch` | Alto si no se filtra | D-72; fixture sintético; el lab gana un workflow `pull_request` |
| R2 | La URL firmada (con la firma en la query) llega a un log por el logger de `httpx2` o a `last_error` | Alto (fuga de credencial temporal) | Logger en WARNING, errores con plantilla del endpoint, test con canario |
| R3 | Dos workflows que fallan en el mismo commit crean checks que no se distinguen | UX | Resuelto por D-76 (sujeto a U12) |
| R4 | Límites secundarios al crear contenido (comentarios por minuto y hora) | Bajo a esta escala | Mapeo a `retry_after` (D-67) |
| R5 | Reloj de la laptop desfasado → JWT rechazado (`iat` en el futuro) | Bajo | `iat` − 60 s; el 401 de `/app/...` se loguea claro |
| R6 | NFR-02 (p95 < 90 s): smee, el poll de 5 s, 3 jobs con LLM (30 s de timeout, 1 s y 4 s de reintentos) | Medio con LLM, bajo sin él | Diagnóstico de los jobs en paralelo; se mide en (e) con la línea de log de (d1) |
| R7 | Memoria pico por job (D-71) | Bajo en dev, a medir en P5 | Documentado; `WORKER_CONCURRENCY` |
| R8 | Primer PR de un fork: GitHub puede pedir aprobación para correr workflows | Bloquea la casilla 3 hasta aprobar | Lo aprobás vos en la pestaña Actions del lab (Apéndice A) |
| R9 | Instalar la app de desarrollo en este repo hace que todo fallo de CI genere jobs mientras el worker local corre | Bajo (es la app de desarrollo) | Esperado (§12: se diagnostica a sí misma) |
| R10 | Con la casilla 4, el excerpt redactado del lab sale hacia `api.openai.com` aunque la clave sea inválida (la petición viaja; el 401 llega después) | Bajo: es contenido nuestro del lab | Documentado en el runbook; la casilla 6 corre sin clave |
| R11 | Los check runs de prueba de U10/U12 no se pueden borrar | Bajo | Conjunto fijo y mínimo en una rama descartable (Apéndice B) |

---

## 5. Estado de tus respuestas

| # | Respuesta | Qué hice |
|---|---|---|
| 1 | Nombre `CICD Doctor: <workflow>`, sanitizado, y que el spike confirme los checks homónimos | D-76; spike 14 (API) y Apéndice B (interfaz) |
| 2 | No se prueba ahora: la casilla 3 de (e) queda **abierta** con el motivo "falta una organización para el fork; se verifica al final de P2 o P3". Cubierto offline mientras tanto | (e) cierra 5 de 6 casillas; el fork queda cubierto por los tests de d1 y d2 (fork → búsqueda del PR por `head`, solo reglas, `fork_policy`). U11 sigue abierta |
| 3 | `.env` tiene `GITHUB_APP_ID` y `GITHUB_PRIVATE_KEY`; permisos configurados antes; la app está instalada en el lab y en `cicd-doctor`; que el spike lo verifique y diga qué falta | Spike 1 a 5 y la tabla "requerido vs. real" |
| + | Nombre del workflow en el comentario sticky: aceptado | (c), D-76 |
| + | "Arrancá el spike" | Autorizado: solo `GET`, más `POST …/access_tokens` |
| 4 | Spike sí; fixtures sanitizados, los revisás | Lista blanca impuesta en el script; revisión tuya antes de cada PR |
| 5 | U10 sí, en (c), rama descartable, "CICD Doctor (test)" | Apéndice B; conjunto fijo de 3 check runs |
| 6 | Opción (a) como PR (f), separable | (f) al final; D-73 |
| 7 | Casillas 4 y 5: cargás y sacás las variables | Apéndice A, casillas 4 y 5 |
| 8 | Escrituras vos, lecturas yo | Apéndice A: cada casilla tiene "Acción (vos)" y "Verificación (yo, solo lectura)" |
| 9 | `pyjwt[crypto]` OK | b1 |
| + | Partir (b) y (d) | b1/b2 y d1/d2 |

## 6. Qué tenés que preparar

- Nada pendiente de la pregunta 2: la casilla 3 queda abierta hasta que haya una organización para el fork.
- `git fetch` y actualizar `main` local (está detrás del #8). La rama `p1b-llm` ya está mergeada y se puede
  borrar cuando quieras.
- `docker compose up -d db` para los tests de integración de (a) y (d2).
- Para el spike: nada más (la clave y el app id están en `.env`). Si la tabla del spike muestra que falta un
  permiso o evento, lo cambiás vos en la app de desarrollo.
- Antes de (c): crear la rama descartable del Apéndice B y pasarme su SHA.
- Para (e): el canal de smee corriendo; publicar en el lab el workflow `pr-py-modnf.yml`; la clave inválida y el presupuesto 0 en `.env` en las casillas 4 y 5, y sacarlos después.
- Nada de OpenAI con valor real: ninguna casilla de P1c lo necesita. Las casillas `-m live` de P1b siguen
  abiertas hasta que tengas la clave.

---

## Apéndice A — Runbook de (e): verificación en vivo de P1c

**Quién hace qué.** Vos corrés las **acciones** (escrituras en GitHub y cambios en `.env`). Yo leo los
resultados con `gh` (solo `GET`). Los bloques **Verificación** son de solo lectura y también los podés correr
vos. Todo está pensado para **PowerShell 5.1**: sin `&&`, sin comillas dobles dentro de `--jq` y con
`[datetimeoffset]` para las fechas (5.1 deja las fechas ISO como texto).

### A.0 Variables y arranque (una vez por sesión de PowerShell)

Reemplazá `OWNER`, `SLUG` y `ORG`. `SLUG` sale del spike (`GET /app`); `ORG` es la organización del fork.

```powershell
$LAB = "OWNER/cicd-doctor-lab"
$ME  = "OWNER/cicd-doctor"
$BOT = "SLUG[bot]"
$ORG = "ORG"
```

Arranque de la base y el esquema:

```powershell
docker compose up -d db
uv run alembic upgrade head
```

Cada uno en su terminal:

```powershell
uv run cicd-doctor api
```

```powershell
uv run cicd-doctor worker
```

```powershell
npx smee-client --url https://smee.io/CANAL --target http://127.0.0.1:8000/webhooks/github
```

Comprobación (debe decir `ready`):

```powershell
Invoke-RestMethod http://127.0.0.1:8000/ready
```

Clon del lab para trabajar (una vez):

```powershell
gh repo clone $LAB "$env:TEMP\lab-p1c"
```

Precondición: `lab/.github/workflows/pr-py-modnf.yml` (el workflow nuevo de (e)) ya está en `main` del lab.

### A.1 Casilla 1 — un PR del lab: check `neutral` en ≤ 90 s y un comentario sticky

**Acción (vos):**

```powershell
Set-Location "$env:TEMP\lab-p1c"
git switch main
git pull
git switch -c p1c/box1
Add-Content -Path p1c-scratch.txt -Value "box 1" -Encoding ascii
git add p1c-scratch.txt
git commit -m "p1c box 1"
git push -u origin p1c/box1
gh pr create --repo $LAB --base main --head p1c/box1 --title "P1c box 1" --body "Live check of P1c"
```

Guardá las referencias (si `$RUN` queda vacío, esperá 10 s y repetí esa línea):

```powershell
$PR  = gh pr view p1c/box1 --repo $LAB --json number --jq .number
$SHA = git rev-parse HEAD
$RUN = gh run list --repo $LAB --branch p1c/box1 --workflow pr-py-modnf.yml --limit 1 --json databaseId --jq '.[0].databaseId'
gh run watch $RUN --repo $LAB
```

**Verificación (yo, solo lectura):**

```powershell
$checks = gh api "repos/$LAB/commits/$SHA/check-runs?per_page=100" | ConvertFrom-Json
$mine = @($checks.check_runs | Where-Object { $_.name -like 'CICD Doctor*' })
$mine | Select-Object id, name, status, conclusion, started_at, completed_at
$runInfo = gh run view $RUN --repo $LAB --json updatedAt | ConvertFrom-Json
([datetimeoffset]$mine[0].completed_at - [datetimeoffset]$runInfo.updatedAt).TotalSeconds
$comments = gh api "repos/$LAB/issues/$PR/comments?per_page=100" | ConvertFrom-Json
@($comments | Where-Object { $_.user.login -eq $BOT }).Count
```

**Criterio:** un check llamado `CICD Doctor: <workflow>`, `status = completed`, `conclusion = neutral`; los
segundos entre el final del run y el `completed_at` del check, **≤ 90**; exactamente **1** comentario del
bot. Anotá `$mine[0].id` y el id del comentario para la casilla 2:

```powershell
$id1  = $mine[0].id
$cid1 = ($comments | Where-Object { $_.user.login -eq $BOT }).id
```

### A.2 Casilla 2 — "Re-run failed jobs": se edita, no se duplica

**Acción (vos):**

```powershell
gh run rerun $RUN --repo $LAB --failed
gh run watch $RUN --repo $LAB
```

**Verificación (yo):**

```powershell
$checks2 = gh api "repos/$LAB/commits/$SHA/check-runs?per_page=100" | ConvertFrom-Json
$mine2 = @($checks2.check_runs | Where-Object { $_.name -like 'CICD Doctor*' })
$mine2 | Select-Object id, name, status, conclusion, completed_at
$comments2 = gh api "repos/$LAB/issues/$PR/comments?per_page=100" | ConvertFrom-Json
$botComments = @($comments2 | Where-Object { $_.user.login -eq $BOT })
$botComments.Count
$botComments | Select-Object id, updated_at
$botComments[0].body.Split("`n") | Select-Object -First 4
```

**Criterio:** `$mine2[0].id` es igual a `$id1`; `$botComments.Count` es **1** y su `id` es `$cid1`; el
encabezado del comentario incluye `Attempt 2`.

### A.3 Casilla 3 — PR desde un fork: solo reglas, "LLM not used for fork PRs"

**Esta casilla queda abierta en P1c:** "falta una organización para el fork; se verifica al final de P2 o
P3". Mientras tanto la cubren los tests offline de d1 y d2 (fork → búsqueda del PR por `head`, solo reglas,
`fork_policy`, y el check publicado igual). Los comandos quedan escritos para cuando haya una organización.
Necesita un fork en una cuenta u organización distinta del dueño del lab.

**Acción (vos):**

```powershell
gh repo fork $LAB --org $ORG --clone=false
gh repo clone "$ORG/cicd-doctor-lab" "$env:TEMP\lab-p1c-fork"
Set-Location "$env:TEMP\lab-p1c-fork"
git switch -c p1c/box3
Add-Content -Path p1c-scratch.txt -Value "box 3" -Encoding ascii
git add p1c-scratch.txt
git commit -m "p1c box 3"
git push -u origin p1c/box3
$URL3 = gh pr create --repo $LAB --base main --head "${ORG}:p1c/box3" --title "P1c box 3 (fork)" --body "Fork PR live check of P1c"
$PR3  = ([string]($URL3 | Select-Object -Last 1)).Split('/')[-1]
$SHA3 = git rev-parse HEAD
```

Si GitHub pide aprobación (pestaña Actions del lab → "Approve and run"), aprobá el run. Después:

```powershell
$RUN3 = gh run list --repo $LAB --branch p1c/box3 --workflow pr-py-modnf.yml --limit 1 --json databaseId --jq '.[0].databaseId'
gh run watch $RUN3 --repo $LAB
```

**Verificación (yo):**

```powershell
$checks3 = gh api "repos/$LAB/commits/$SHA3/check-runs?per_page=100" | ConvertFrom-Json
$mine3 = @($checks3.check_runs | Where-Object { $_.name -like 'CICD Doctor*' })
$mine3 | Select-Object id, name, status, conclusion
$mine3[0].output.summary -match 'LLM not used for fork PRs'
$comments3 = gh api "repos/$LAB/issues/$PR3/comments?per_page=100" | ConvertFrom-Json
@($comments3 | Where-Object { $_.user.login -eq $BOT }).Count
```

**Criterio:** check `neutral` sobre el commit del fork (esto contesta U11), el resumen contiene `LLM not used
for fork PRs` (`True`), y hay 1 comentario del bot en el PR.

### A.4 Casilla 4 — clave de OpenAI inválida → reglas con `llm_error`

**Aviso (R10):** con una clave inválida, el excerpt redactado del lab viaja a `api.openai.com` y vuelve un
401. Es contenido nuestro; no cuesta nada.

**Acción (vos):**

1. Abrí `.env` **a mano en el editor** (no lo reescribas con PowerShell) y poné:
   `OPENAI_API_KEY=invalid-key-for-p1c-box4` y `OPENAI_MODEL=model-for-p1c-box4`.
2. En la terminal del worker: Ctrl+C y `uv run cicd-doctor worker`.
3. Disparar un análisis nuevo (nuevo push al PR de la casilla 1):

```powershell
Set-Location "$env:TEMP\lab-p1c"
git switch p1c/box1
Add-Content -Path p1c-scratch.txt -Value "box 4" -Encoding ascii
git commit -am "p1c box 4"
git push
$SHA4 = git rev-parse HEAD
Start-Sleep -Seconds 10
$RUN4 = gh run list --repo $LAB --branch p1c/box1 --workflow pr-py-modnf.yml --limit 1 --json databaseId --jq '.[0].databaseId'
gh run watch $RUN4 --repo $LAB
```

**Verificación (yo):**

```powershell
$checks4 = gh api "repos/$LAB/commits/$SHA4/check-runs?per_page=100" | ConvertFrom-Json
$mine4 = @($checks4.check_runs | Where-Object { $_.name -like 'CICD Doctor*' })
$mine4 | Select-Object id, name, status, conclusion
$mine4[0].output.summary -match 'LLM unavailable'
docker compose exec -T db psql -U cicd -d cicd_doctor -c "SELECT source, fallback_reason, published, created_at FROM diagnoses WHERE published ORDER BY created_at DESC LIMIT 3;"
```

(Ajustá `-U` y `-d` si cambiaste `POSTGRES_USER` o `POSTGRES_DB`.)

**Criterio:** check `neutral`, el resumen dice `LLM unavailable`, y la fila publicada más reciente tiene
`source = rules` y `fallback_reason = llm_error`.

### A.5 Casilla 5 — `LLM_DAILY_BUDGET_USD=0` → `budget` y el texto del límite

**Acción (vos):**

1. En `.env`, a mano: `LLM_DAILY_BUDGET_USD=0` (dejá la clave inválida de la casilla 4).
2. Reiniciá el worker (Ctrl+C y `uv run cicd-doctor worker`) y empujá otro commit:

```powershell
Set-Location "$env:TEMP\lab-p1c"
Add-Content -Path p1c-scratch.txt -Value "box 5" -Encoding ascii
git commit -am "p1c box 5"
git push
$SHA5 = git rev-parse HEAD
Start-Sleep -Seconds 10
$RUN5 = gh run list --repo $LAB --branch p1c/box1 --workflow pr-py-modnf.yml --limit 1 --json databaseId --jq '.[0].databaseId'
gh run watch $RUN5 --repo $LAB
```

**Verificación (yo):**

```powershell
$checks5 = gh api "repos/$LAB/commits/$SHA5/check-runs?per_page=100" | ConvertFrom-Json
$mine5 = @($checks5.check_runs | Where-Object { $_.name -like 'CICD Doctor*' })
$mine5 | Select-Object id, name, status, conclusion
$mine5[0].output.summary -match 'LLM daily limit reached'
docker compose exec -T db psql -U cicd -d cicd_doctor -c "SELECT source, fallback_reason, published, created_at FROM diagnoses WHERE published ORDER BY created_at DESC LIMIT 3;"
docker compose exec -T db psql -U cicd -d cicd_doctor -c "SELECT * FROM llm_usage ORDER BY day DESC LIMIT 3;"
```

**Criterio:** el resumen dice `LLM daily limit reached`; la fila publicada más reciente tiene
`fallback_reason = budget`; en `llm_usage` no sube `calls` con esta corrida.

**Después de la casilla 5 (vos):** en `.env`, a mano, dejá `OPENAI_API_KEY=`, `OPENAI_MODEL=` y
`LLM_DAILY_BUDGET_USD=2.00`, y reiniciá el worker.

### A.6 Casilla 6 — un test roto en una rama de este repo: se diagnostica su propio CI

El test va en `tests/integration/` para que el gate de pre-commit (que solo corre `unit` y `contract`) no
lo vea, y falla en CI, que corre todo.

**Acción (vos):**

```powershell
Set-Location C:\dev\cicd-doctor
git fetch origin
git switch -c p1c/box6 origin/main
@'
"""Deliberate failure for the P1c live check. Delete this file."""


def test_p1c_deliberate_failure() -> None:
    assert 1 == 2, "deliberate failure for the P1c live check"
'@ | Set-Content -Path tests/integration/test_p1c_deliberate_failure.py -Encoding ascii
git add tests/integration/test_p1c_deliberate_failure.py
git commit -m "Add a deliberately failing test for the P1c live check"
git push -u origin p1c/box6
gh pr create --repo $ME --base main --head p1c/box6 --title "P1c box 6 (do not merge)" --body "Deliberate CI failure to check that CICD Doctor diagnoses its own CI."
$PR6  = gh pr view p1c/box6 --repo $ME --json number --jq .number
$SHA6 = git rev-parse HEAD
Start-Sleep -Seconds 10
$RUN6 = gh run list --repo $ME --branch p1c/box6 --workflow ci.yml --limit 1 --json databaseId --jq '.[0].databaseId'
gh run watch $RUN6 --repo $ME
```

**Verificación (yo):**

```powershell
$checks6 = gh api "repos/$ME/commits/$SHA6/check-runs?per_page=100" | ConvertFrom-Json
$mine6 = @($checks6.check_runs | Where-Object { $_.name -like 'CICD Doctor*' })
$mine6 | Select-Object id, name, status, conclusion
$mine6[0].output.title
$comments6 = gh api "repos/$ME/issues/$PR6/comments?per_page=100" | ConvertFrom-Json
@($comments6 | Where-Object { $_.user.login -eq $BOT }).Count
```

**Criterio:** check `CICD Doctor: CI`, `neutral`, con título `Likely cause: broken_test (…)`, y 1 comentario
del bot en el PR.

### A.7 Limpieza (vos)

```powershell
gh pr close $PR6 --repo $ME --delete-branch
gh pr close $PR --repo $LAB --delete-branch
gh pr close $PR3 --repo $LAB --delete-branch
```

Los check runs que quedaron no se pueden borrar; cuelgan de commits de ramas descartables.

---

## Apéndice B — Prueba manual de U10 y U12 en (c)

**Objetivo:** contestar U10 (¿un check run `completed` puede volver a `in_progress`?) y U12 (cómo se ven
checks del mismo nombre y si el nombre se muestra como texto plano).

**Preparación (vos).** Una rama descartable con **un commit propio** (no uno de `main`: los check runs no se
borran y quedarían pegados a un commit real). Un `push` de rama no dispara ningún workflow del lab
(solo `workflow_dispatch` y, desde (e), `pull_request`).

```powershell
gh repo clone OWNER/cicd-doctor-lab "$env:TEMP\lab-scratch"
Set-Location "$env:TEMP\lab-scratch"
git switch -c scratch/p1c-checks
git commit --allow-empty -m "scratch: P1c check run lifecycle test"
git push -u origin scratch/p1c-checks
git rev-parse HEAD
```

Pasame el SHA que imprime la última línea.

**Lo que hago yo (con la app de desarrollo, un token con alcance solo a ese repo y `checks: write`), y nada
más que esto:**

| # | Llamada | Para qué |
|---|---|---|
| 1 | `POST /check-runs`: nombre `CICD Doctor (test)`, `external_id = cicd-doctor:test:1`, `in_progress` | crear |
| 2 | `PATCH` del 1: `completed`, `neutral` | completar |
| 3 | `PATCH` del 1: `status: in_progress` | **U10**: ¿lo acepta? ¿con qué status y qué queda? |
| 4 | `POST /check-runs`: el **mismo nombre** `CICD Doctor (test)`, `external_id = cicd-doctor:test:2`, `completed`, `neutral` | **U12**: dos checks homónimos |
| 5 | `POST /check-runs`: nombre `CICD Doctor (test): <b>x</b> **b** \`c\` [l](https://example.com)`, `external_id = cicd-doctor:test:3`, `completed`, `neutral` | **U12**: ¿el nombre se muestra como texto plano? |

Total: **3 check runs permanentes** (el 1, el 2 y el 3 de la tabla, o sea los pasos 1, 4 y 5) y hasta 3
`PATCH`. Después de cada llamada anoto status y forma de la respuesta, y leo `GET …/commits/{sha}/check-runs`
para ver cómo los lista la API.

**Lo visual (vos o yo):** abrí `https://github.com/OWNER/cicd-doctor-lab/commit/<SHA>` y mirá el desplegable
de checks. Si preferís, lo leo yo en el navegador del panel, con tu sesión, previa autorización tuya.
Decime: ¿se ven dos filas `CICD Doctor (test)` o una?, ¿el nombre del paso 5 sale como texto literal?

**Limpieza (vos):** `git push origin --delete scratch/p1c-checks`. Los check runs quedan en el commit.

**Qué decide cada resultado:**

- U10 acepta volver a `in_progress` → D-70 paso 2 queda como está. Si no, el paso 2 solo actualiza el
  `output` del check y este sigue `completed` durante un intento nuevo.
- Nombre del paso 5 se ve literal → D-76 no escapa. Si se interpreta, D-76 agrega `escape` y su test.
- Dos filas separadas → D-76 queda como está. Si la interfaz las agrupa u oculta una, se revisa.
