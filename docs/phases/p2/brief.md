> **Original brief given to the planning session, written in Spanish. The owner's local absolute paths were replaced by `<repo>`; nothing else was changed.** It is context for the plan, not an authority: `SPEC.md`, `DECISIONS.md` and the code win.

Soy el dueño de CICD Doctor, un proyecto en este repositorio. Quiero que planifiques la fase P2. SOLO UN PLAN, sin código. Hablame en español (identificadores, nombres de archivo y código en inglés); lo que después vaya al repositorio (SPEC, DECISIONS) va en inglés.

## Contexto

CICD Doctor es una GitHub App (Python 3.13, uv, arquitectura hexagonal: `core/` tiene los puertos y `adapters/` los implementa) que explica por qué falló un run de GitHub Actions. P0, P1a, P1b y P1c están mergeadas en `main`: el worker recibe un webhook firmado, diagnostica un run fallado (reglas siempre, LLM cuando está permitido) y publica un check run neutral `CICD Doctor: <workflow>` más un único comentario sticky en el PR. Se verificó en vivo contra la app de desarrollo el 2026-11-16 y 2026-11-17.

La siguiente fase es P2 (SPEC.md §13): memoria. Embeddings, caché por firma exacta, fallas parecidas, seguimiento de flaky y de commits que arreglan, purga por retención y la variante de evals `llm+hints+similar`.

`CLAUDE.md` del repo lista las reglas fijas: `core/` nunca importa adapters ni librerías de IO, sin secretos en el código, los logs de CI son entrada no confiable, las decisiones van a `DECISIONS.md` como D-NN, SPEC.md debe quedar alineado, sin `Co-Authored-By`.

## Entregable

Escribí el plan en `<repo>/plans/p2.md`. Esa carpeta no está commiteada a propósito: no la commitees ni la agregues a ningún PR. Después frená.

## Leé primero

`SPEC.md` (§1, §4.3, §5, §6, §7.1, §7.2, §8, §9.4, §11, §12, §13 P2 y P3, §14), `DECISIONS.md` (sobre todo D-08, D-09, D-10, D-15, D-16, D-24, D-34, D-35, D-47 a D-49, D-54, D-60, D-65, D-66, D-69, D-70, D-71, D-73, D-74, D-75, D-76), `CLAUDE.md`, `src/cicd_doctor/core/ports.py`, `core/models.py`, `core/policy.py`, `core/usecases/diagnose_failed_run.py`, `core/diagnosis/pipeline.py`, `adapters/postgres/store.py` y `migrations/`, `adapters/memory/store.py` y `embedder.py`, `adapters/github/webhooks.py`, `worker/runner.py`, `worker/handlers.py`, `app/container.py`, `app/settings.py` y `evals/runner/`.

Usá `<repo>/plans/p1c.md` (solo lectura) como MODELO de formato y nivel de detalle: es el plan que siguió la fase anterior. `<repo>/plans/p1c-spike.md` muestra cómo se corrió e informó un spike de solo lectura.

Hechos de P1c a tener presentes:
- GitHub NO reabre un check run completado: un `PATCH` a `in_progress` contesta 200 y el check sigue `completed`, aunque `name` y `output` sí se aplican.
- El comentario sticky se puede editar.
- Ambos se escriben bajo `publication_lock` con la guarda de intento más nuevo (D-69, D-70).
- El diagnóstico se calcula una vez por intento del run y se guarda como checkpoint antes de publicar.

## Qué entregar, en este orden

1. **División en PRs chicos**, cada uno mergeable solo, con `main` verde y la app funcionando. CADA PR es una subsección `### (x) Título: nombre-de-rama`, como en p1c.md. Piezas candidatas (cambialas con razones, juntá o partí):
   - el adapter de embeddings (fastembed, D-08) y su cableado para que una falla reciba su embedding;
   - la búsqueda `similar` en Postgres con pgvector y el índice HNSW, y `cached_diagnosis`;
   - la caché por firma exacta en la política (SPEC §9.4 paso 5, `source = cache`, `llm_usage.calls` sin cambios);
   - las fallas parecidas en el prompt como contexto no confiable y en el reporte renderizado (SPEC §5, §7.5, D-64: todo texto es no confiable);
   - el camino de éxito (un `workflow_run` con conclusión `success` pasa a ser un job `record_outcome`, FR-17) y la máquina de estados en `core/outcomes.py` (`resolved_flaky`, `resolved_by_commit` con `fix_commit_sha`, `stale` a los 30 días, D-24), incluida la edición del comentario sticky al resolverse;
   - el job de purga y su programación diaria por el worker (FR-18, D-34);
   - la variante de evals `llm+hints+similar`;
   - la verificación en vivo con un runbook como el de P1c (PR (e) de P1c).
   Decidí el orden, qué se puede hacer en paralelo (y qué archivos chocan) y qué bloquea a qué.
2. **Por PR:** alcance, archivos, tests (decí exactamente cuáles corren en Postgres y cuáles son offline; las suites de contrato corren en memoria Y Postgres, D-51), criterio de terminado y qué casilla de §13 P2 cierra (ninguna se marca sin verificación). Cada PR tiene que poder revisarse.
3. **Decisiones nuevas como D-NN** (el próximo número libre es D-77), cada una con alternativas, tu recomendación y consecuencias. Como mínimo:
   - dónde y cuándo se calcula el embedding respecto del checkpoint del análisis, y qué se reintenta;
   - cómo `similar` filtra por repositorio (D-10) y qué funciones de pgvector necesita (iterative index scans, comentario de SPEC §6);
   - la clave de la caché exacta y su interacción con el presupuesto del LLM y con los runs de fork (D-17);
   - cómo entran las fallas parecidas al prompt y al reporte de forma segura;
   - cómo el camino de éxito encuentra las fallas abiertas del mismo workflow y `branch_key` (`FailureStore.open_failures`, forks), y cuándo un run cuenta como flaky o como arreglado por un commit (D-24), incluidas las carreras entre un webhook de falla tardío y un éxito anterior;
   - cómo y cuándo se reescribe el comentario sticky al resolverse sin romper `publication_lock`;
   - cómo se programa la purga y se la hace idempotente con varios workers (D-49);
   - cómo se calibra `SIMILAR_MIN_SCORE` = 0.85 contra el modelo ONNX cuantizado que sirve fastembed (SPEC §14);
   - si el modelo de embeddings se carga en el proceso del worker (arranque en frío de unos 7,5 s medido, tamaño de la imagen, D-08).
4. **Riesgos y lo que no sabemos del mundo real:** API de GitHub, pgvector en la imagen Compose `pgvector/pgvector:pg17`, fastembed en Linux y en CI, la descarga del modelo en la primera corrida, memoria del worker, tiempo de construcción del índice. Proponé un spike CORTO, de SOLO LECTURA y local, para correr antes de implementar, con la lista exacta de lo que haría y mediría y qué se guardaría como fixtures. NO lo corras: esperá mi aprobación. Todo lo que escriba en GitHub, gaste dinero o toque mi `.env` o secretos va en una lista aparte, porque necesita mi consentimiento explícito.
5. **Preguntas para mí** y qué tengo que preparar. Pendientes conocidos: una organización para la casilla del fork de P1c y la clave de OpenAI (las casillas `-m live` de P1b). Decí si P2 depende de ellas.

## Reglas

Nada de código, commits, pushes, PRs ni ramas. No corras el worker, la API ni ningún comando `gh` que escriba. Podés leer el repositorio libremente y correr comandos de solo lectura; el esquema real de la base solo a través de las migraciones del repo. Nunca leas ni imprimas `<repo>\.env`, ni una clave ni un token. Si algo de SPEC contradice el código, decilo en el plan en lugar de elegir en silencio. Si necesitás archivos temporales, usá tu scratchpad. Cuando termines, dame un resumen corto en español (la división, las decisiones, los riesgos principales, el spike propuesto y las preguntas) y frená.
