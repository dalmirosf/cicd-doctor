> **Original brief given to the planning session, written in Spanish. The owner's local absolute paths were replaced by `<repo>`; nothing else was changed.** It is context for the plan, not an authority: `SPEC.md`, `DECISIONS.md` and the code win.

Soy el dueño de CICD Doctor, un proyecto en este repositorio. Quiero que planifiques la fase P3. SOLO UN PLAN, sin código. Hablame en español, en criollo y sin jerga innecesaria (identificadores, nombres de archivo y código en inglés); lo que después vaya al repositorio (SPEC, DECISIONS) va en inglés.

## Contexto

CICD Doctor es una GitHub App (Python 3.13, uv, arquitectura hexagonal: `core/` tiene los puertos y `adapters/` los implementa) que explica por qué falló un run de GitHub Actions. P0, P1a, P1b, P1c y P2 están mergeadas en `main` y verificadas en vivo (la única casilla abierta es la del fork de P1c, que espera una organización de GitHub). El worker recibe un webhook firmado, diagnostica un run fallado (reglas siempre, LLM cuando está permitido, modelo `gpt-6-luna`, D-88) y publica un check run neutral `CICD Doctor: <workflow>` más un único comentario sticky en el PR. P2 agregó la memoria: embeddings, caché por firma exacta, fallas parecidas, flaky y "arreglado por un commit", y la purga diaria.

La siguiente fase es P3 (SPEC.md §13): comandos, sugerencias y configuración por repositorio (FR-19, FR-20, FR-21):
- `/cicd-doctor explain` en un PR, por `OWNER`, `MEMBER` o `COLLABORATOR`: reacción 👀 y diagnóstico con LLM de los últimos runs fallados del head del PR, también en PRs de fork; un comando cada 5 minutos por PR.
- Sugerencias: si el LLM propone un cambio cuyas líneas están dentro del diff del PR, se publica como review comment con bloque `suggestion`; si no, solo como texto.
- Configuración por repositorio en `.github/cicd-doctor.yml` **leída de la rama por defecto** (`comment`, `llm: auto|on_demand|off`, `ignore_workflows`); un cambio hecho solo en el head de un PR de fork no tiene efecto.
- Verificar qué permiso hace falta para recibir `issue_comment` en PRs (Pull requests o Issues: read) y anotarlo en DECISIONS.md (D-21).

`CLAUDE.md` del repo lista las reglas fijas y el método de fases ("How a phase is built"): `core/` nunca importa adapters ni librerías de IO, sin secretos en el código, los logs de CI y ahora también los comentarios de los PRs son entrada no confiable, las decisiones van a `DECISIONS.md` como D-NN, SPEC.md debe quedar alineado, sin `Co-Authored-By`.

## Entregable

Escribí el plan en `<repo>/plans/p3.md`. Esa carpeta no está commiteada a propósito: no la commitees ni la agregues a ningún PR. Después frená.

## Leé primero

`SPEC.md` (§1, §4.3, §5, §6, §7.1, §8, §9.4, §11, §12, §13 P3 y P4, §14, y los FR-19, FR-20, FR-21), `DECISIONS.md` (sobre todo D-17, D-21, D-24, D-25, D-64, D-69 a D-76, D-81, D-83, D-84, D-88), `CLAUDE.md`, `src/cicd_doctor/core/ports.py` (en particular `RepoConfigSource`, `PullRequestSource`, `Reporter`), `core/models.py`, `core/policy.py`, `core/render.py`, `core/usecases/diagnose_failed_run.py`, `core/usecases/report.py`, `core/diagnosis/pipeline.py`, `core/diagnosis/validation.py`, `adapters/github/webhooks.py`, `adapters/github/reporter.py`, `adapters/github/pulls.py`, `adapters/github/http.py`, `adapters/postgres/` y `migrations/` (la última migración es la `0006`), `worker/runner.py`, `worker/handlers.py`, `app/container.py`, `app/settings.py`, `evals/runner/injection.py` y `lab/README.md`.

Usá `<repo>/plans/p2.md` (solo lectura) como MODELO de formato y nivel de detalle: es el plan que siguió la fase anterior, con su tabla de decisiones cerradas, su lista de consentimientos y su runbook. `<repo>/plans/p2-spike.md` muestra cómo se corrió e informó un spike de solo lectura. `<repo>/plans/p2-brief.md` es el encargo que se le dio a P2.

Hechos de fases anteriores a tener presentes:
- GitHub NO reabre un check run completado; el comentario sticky sí se puede editar; ambos se escriben bajo `publication_lock` con la guarda de intento más nuevo (D-69, D-70).
- El diagnóstico se calcula una vez por intento del run y se guarda como checkpoint antes de publicar.
- La política (SPEC §9.4) ya conoce el comando `explain` y el modo `on_demand`; la caché de P2 (D-81) deja fuera los runs de fork y marcó `is_fork = false` desde ya porque P3 es el primer camino por el que un log controlado por un atacante llega al LLM de un fork. Revisá qué implica eso para la caché y para las "parecidas".
- El texto de un comentario de PR lo escribe cualquiera: es tan no confiable como un log. Un `/cicd-doctor explain` en un fork no puede dejar que el autor del PR elija qué se manda al modelo ni cuánto gasta (D-17).
- La app de desarrollo está instalada en `dalmirosf/cicd-doctor-lab` y en este repositorio; el runbook en vivo de cada fase vive en `lab/README.md` (los bloques de PowerShell se parsean antes de entregarlos; líneas cortas; las variables de una terminal no existen en otra).
- Hoy hay clave de OpenAI y el modelo está elegido: un spike o una verificación en vivo pueden usar el LLM real, pero cada gasto va en la lista de consentimientos con su costo estimado. Los topes de `.env` para pruebas (`LLM_DAILY_BUDGET_USD`, `LLM_DAILY_CALLS_PER_INSTALLATION`) se bajan y se restauran a mano.

## Qué entregar, en este orden

1. **División en PRs chicos**, cada uno mergeable solo, con `main` verde y la app funcionando. CADA PR es una subsección `### (x) Título: nombre-de-rama`, como en p2.md. Piezas candidatas (cambialas con razones, juntá o partí):
   - el webhook `issue_comment`: parseo y verificación del comando, normalización a un job (`DiagnoseRunPayload` con origen "comando", nunca JSON crudo, D-56), filtro por `author_association`, límite de 1 comando por PR cada 5 minutos (cómo se persiste y se hace idempotente con varios workers) y la reacción 👀;
   - el job de `explain`: cómo encuentra los últimos runs fallados del head del PR, cómo reutiliza el checkpoint y el LLM (modo `on_demand`), qué cambia para forks y cómo se publica;
   - las sugerencias: pedirle al LLM un cambio, validar que las líneas estén dentro del diff del PR (`PullRequestSource`), el review comment con bloque `suggestion`, y el texto de respaldo; revisar el esquema estricto de D-63 y la validación de rutas (hay un caso de inyección de path traversal);
   - la configuración `.github/cicd-doctor.yml` (`RepoConfigSource`): lectura desde la rama por defecto, validación estricta, caché y su vencimiento, y cómo `comment`, `llm` e `ignore_workflows` entran a la política sin romper los chequeos existentes;
   - el permiso `issue_comment` y los eventos suscritos de la app (qué se verifica a mano en la configuración de la app de desarrollo y qué se documenta);
   - los casos de inyección nuevos (comentarios y archivo de configuración como superficie de ataque) y su verificación offline;
   - la verificación en vivo con un runbook como el de P2 (PR (h) de P2).
   Decidí el orden, qué se puede hacer en paralelo (y qué archivos chocan) y qué bloquea a qué.
2. **Por PR:** alcance, archivos, tests (decí exactamente cuáles corren en Postgres y cuáles son offline; las suites de contrato corren en memoria Y Postgres, D-51), criterio de terminado y qué casilla de §13 P3 cierra (ninguna se marca sin verificación). Cada PR tiene que poder revisarse.
3. **Decisiones nuevas como D-NN** (el próximo número libre es D-89), cada una con alternativas, tu recomendación y consecuencias. Como mínimo:
   - quién puede disparar el comando y cómo se verifica (`author_association` viene en el payload: ¿alcanza o hay que consultar la API?, ¿qué pasa con un comentario editado o borrado?, ¿y con un bot?);
   - el límite de 1 por PR cada 5 minutos: dónde vive el estado, qué ventana, qué se hace con los comandos ignorados (¿silencio o aviso?);
   - cómo se resuelve "los últimos runs fallados del head del PR" y qué hace `explain` si no hay ninguno o si el run sigue en curso;
   - fork PRs con LLM: qué entra al prompt, cómo se evita que el autor del fork controle el gasto, cuál es la interacción con la caché y con las parecidas de P2 (D-81, D-82) y con el presupuesto (D-17);
   - cómo se calculan y validan las sugerencias contra el diff (los archivos, las líneas, los renombres, los diffs truncados por la API), y por qué un `suggestion` mal ubicado no se publica;
   - el formato y el lugar de la respuesta de `explain` (¿otro comentario, el sticky existente, una respuesta al comentario?) sin romper `publication_lock` ni el comentario sticky;
   - el esquema, los valores por defecto y la validación del archivo de configuración, qué se hace con un archivo inválido y con claves desconocidas, y cómo se garantiza que se lee solo de la rama por defecto;
   - cómo `comment: false` y `ignore_workflows` se combinan con lo ya publicado (¿qué pasa con un comentario sticky existente?) y con la edición de resolución de P2 (D-84);
   - el permiso necesario para `issue_comment` y cómo se verifica sin adivinar.
4. **Riesgos y lo que no sabemos del mundo real:** la API de GitHub (reacciones, review comments con `suggestion`, diff de un PR, límites de tamaño), la entrega de `issue_comment` según el permiso, la latencia de punta a punta (objetivo: diagnóstico en menos de 90 s), forks, rate limits, comentarios editados o repetidos, la superficie de inyección nueva. Proponé un spike CORTO, de SOLO LECTURA y local donde se pueda, para correr antes de implementar, con la lista exacta de lo que haría y mediría y qué se guardaría como fixtures sanitizados. NO lo corras: esperá mi aprobación. Todo lo que escriba en GitHub (comentarios, reacciones, ramas, PRs de prueba), gaste dinero o toque mi `.env`, la configuración de la app o secretos va en una lista aparte, porque necesita mi consentimiento explícito.
5. **Preguntas para mí** y qué tengo que preparar (por ejemplo: la organización para el fork, una cuenta de prueba que no sea colaboradora para el caso del rechazo, los permisos de la app). Decí qué casillas de P3 dependen de cada cosa y cuáles se pueden verificar sin eso.

## Reglas

Nada de código, commits, pushes, PRs ni ramas. No corras el worker, la API ni ningún comando `gh` que escriba. Podés leer el repositorio libremente y correr comandos de solo lectura; el esquema real de la base solo a través de las migraciones del repo. Nunca leas ni imprimas `<repo>\.env`, ni una clave ni un token. Si algo de SPEC contradice el código, decilo en el plan en lugar de elegir en silencio. Si necesitás archivos temporales, usá tu scratchpad. Cuando termines, dame un resumen corto en español (la división, las decisiones, los riesgos principales, el spike propuesto y las preguntas) y frená.
