> **Original working document, written in Spanish (rioplatense) for the owner and kept untouched below this line.** It is a planning draft: where it differs from `SPEC.md`, `DECISIONS.md` or the code, those are the authority (see the phase `README.md` for the differences).

# P3 — Informe del spike (solo lectura), 2026-11-20

Corrí las partes B y C del plan (§1). La parte E (pantalla de la app) la tenés que mirar vos. No escribí nada en
GitHub, no abrí `.env`, no usé el JWT ni la clave de OpenAI. Solo `GET` con tu sesión de `gh`. Fixtures sanitizados
(dueño → `acme`, email → `dev@acme.example`) en `plans/p3-spike/github/`: `b1_all`, `b1_fail`, `b2_pr2`, `b2_pr3`,
`b2_pr4`, `b2_pr34`, `b2_pr36`. Sin commitear.

## Pregunta → respuesta → consecuencia

| # | Pregunta | Respuesta medida | Consecuencia |
|---|---|---|---|
| B1 | ¿`actions/runs?head_sha=` alcanza para armar el `RunRef`? | Sí: trae `id`, `name`, `run_attempt`, `workflow_id`, `head_sha`, `head_branch`, `event`, `conclusion`, `head_repository`, `pull_requests`, `updated_at`. Un run con un intento aparece una vez; `status=failure` filtra bien. | D-91 se puede implementar con una sola llamada por comando. |
| B6 | ¿Los runs de un PR de **fork** salen en el listado de la base? (A8) | **Sí**, con `head_sha` del fork (probado en un PR público ajeno). `head_repository` es el fork y `pull_requests` viene **vacío (0)**. `conclusion` puede ser `action_required` (sin aprobar). | **Cambia D-91:** el match es por `head_sha` (y `head_repository`), **nunca** por `pull_requests`. `action_required` no es fallo: no se explica (se cuenta como "sin runs fallados"). |
| B2 | ¿Cuándo falta `patch`? | Falta en archivos generados grandes (`uv.lock`, 477 cambios) y en renombres sin cambios (`status=renamed`, `previous_filename`, 0 cambios). Un archivo `removed` sin contenido tampoco lo trae. 184 archivos revisados. | D-94 confirmado: `patch` ausente = sin sugerencia en ese archivo; renombre usa el nombre nuevo. |
| B3 | ¿`x-accepted-github-permissions` confirma los permisos por endpoint? | **No sirve con tu token de `gh`**: el header solo aparece con tokens de app. Solo vi `X-Accepted-Oauth-Scopes: repo`. | A2/A3 siguen siendo de la documentación. Se confirman en (g) con el token de instalación (o mirando el 403 si falta el permiso). |
| B4 | ¿Forma de `contents`? | Archivo inexistente: **404** `{"message":"Not Found"}`. Archivo: `type=file`, `encoding=base64`, `size`, `content`. Directorio: lista. | El adapter trata 404 como "sin configuración"; chequea `type == "file"` y `size` antes de decodificar. |
| B5 | ¿Comentarios de revisión? | Lista vacía en ambos repos (no hay ninguno todavía). Forma no medida. | Se mide en (g). Los tests de (e) usan la forma de la documentación hasta entonces. |

## Parte C (local)

| # | Resultado |
|---|---|
| C1 | `yaml.safe_load`: 1 MB de YAML tarda **5,5 s y 64 MB** → hace falta un tope de tamaño (propongo 16 KB, antes de parsear). El recorrido por **eventos** (`yaml.parse`) detecta alias y anclas sin construir nada (bomba de alias: rechazada por "anchor"). Etiquetas `!!python/*`: `safe_load` las rechaza solo (`ConstructorError`). Documento múltiple: `ComposerError`. Anidado 500 niveles: **`RecursionError`** (hay que capturarla). **Claves duplicadas: se aceptan en silencio (gana la última)** → hay que detectarlas a mano en el recorrido de eventos. BOM y CRLF: sin problema. Raíz que no es un mapa: hay que validarla. |
| C2 | Prototipo del conjunto de líneas del lado nuevo: 184 patches reales, 15 589 líneas, 11,7 ms en total; el conteo coincide con los encabezados `@@` en todos. Ojo: el `patch` real no termina en salto de línea; si el texto lo tiene hay que descartar la última línea vacía (me dio una línea de más en un caso sintético). Las líneas `\ No newline at end of file` se saltean. |
| C3 | Costo del bloque de diff con tiktoken `o200k_base`, sobre líneas de Markdown/Python de este repo: 40 líneas ≈ 4 700 tokens, 80 ≈ 5 400, 160 ≈ 6 800 (las líneas de documentación son larguísimas). | 

Consecuencia para D-94: el tope del bloque debe ser en **tokens** (propongo 2 000) además de en líneas; si no, un
archivo con líneas largas se come el presupuesto.

## Lo que el spike no pudo medir

El 422 por línea fuera del diff; la forma de un review comment con `suggestion`; permisos reales del token de app
(B3); qué pasa en una instalación vieja cuando la app suma un permiso; la latencia (se mide en (g)). Parte E:
pendiente de vos.
