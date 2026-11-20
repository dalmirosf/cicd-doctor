> **Original working document, written in Spanish (rioplatense) for the owner and kept untouched below this line.** It is a planning draft: where it differs from `SPEC.md`, `DECISIONS.md` or the code, those are the authority (see the phase `README.md` for the differences).

# P1c — Resultado del spike de solo lectura

> Sin commitear. Ejecutado el 2026-11-16 contra la app de desarrollo, con el script
> `github_spike.py` (scratchpad, fuera del repo). Crudo fuera del repo; copias sanitizadas en
> `plans/p1c-spike/sanitized/` (19 archivos, **para que las revises**: no entran en ningún PR hasta
> que lo hagas).

## Qué se hizo, y qué no

- Solo `GET`, más `POST /app/installations/{id}/access_tokens` (3 veces: un token de solo `metadata:
  read` para listar repos, y un token por repo con `actions: read`, `checks: write`, `pull_requests:
  write`). Una lista blanca dentro del cliente rechaza cualquier otra llamada antes de salir a la red;
  la autoprueba lo verificó con `DELETE`, `PATCH`, `PUT` y `POST` a check runs y comentarios.
- Nada se imprimió ni se guardó de la clave, el JWT, los tokens ni las URLs firmadas (de la URL
  firmada, solo host, plantilla de ruta y **nombres** de parámetros). Los cuerpos de log se contaron y
  se descartaron.
- **Un fallo del propio sanitizador, corregido y re-verificado.** El primer pase marcó 6 de 19
  fixtures como bloqueados, y de los 13 que pasaron, `github` (dueño de la app `github-actions`) se
  había tratado como un usuario más y reescribía `api.github.com`. Corregí el sanitizador (cuentas
  públicas conservadas, literales iguales a su reemplazo ignorados, ids que solo viven en URLs
  también se mapean), borré los 13 y regeneré los 19 desde el crudo. Verificación independiente
  (otra herramienta, sobre los archivos finales): 0 coincidencias de login, repos ajenos, emails,
  tokens, PEM/JWT, firmas de URL, avatares reales o canal de smee; 0 de los 25 SHAs reales y 0 de los
  152 ids reales sobreviven.
- No se pudo comprobar (requiere escribir): U10, U11 y la parte visual de U12. U4 se cerró después, con
  el re-run que hiciste vos y lecturas mías (abajo).

## Configuración de la app de desarrollo (U13): no falta nada

| | Requerido (§9.6) | App | Instalación |
|---|---|---|---|
| Actions | read | read ✔ | read ✔ |
| Checks | write | write ✔ | write ✔ |
| Contents | read | read ✔ | read ✔ |
| Metadata | read | read ✔ | read ✔ |
| Pull requests | write | write ✔ | write ✔ |
| Permisos de más | ninguno | ninguno | ninguno |
| Evento `workflow_run` | sí | suscrita ✔ | ✔ |
| Evento `issue_comment` | P3 | no suscrita (esperado) | — |

- Instalación única, de tipo `User`, `repository_selection: selected`, sin suspender, con **dos** repos:
  `cicd-doctor` y `cicd-doctor-lab` (los dos que hacen falta).
- Webhook: host `smee.io` con ruta de canal, `content_type: json`, `insecure_ssl: 0`; el secreto sale
  enmascarado (existe, su valor no es visible). Las últimas 5 entregas: 1 `installation_repositories`
  (`added`), 3 `workflow_run` `completed` y 1 `workflow_run` `in_progress`, todas con `status OK` y
  `status_code 200` (llegaron a smee).
- La app recibe **todas** las acciones de `workflow_run` (también `in_progress`): el router ya contesta
  204 a las que no son `completed` (D-56). No hace falta tocar nada.
- Tokens de instalación con alcance a un repo: concedidos exactamente lo pedido más `metadata: read`,
  `repository_selection: selected`, `repositories: 1`, vencen en 60 minutos.
- JWT: RS256 con `iss` como **string** (el app id) aceptado a la primera. API version seleccionada:
  `2022-11-28`. Desfase de reloj local vs. servidor: 0,9 s.

## Respuestas a las incógnitas

| Id | Respuesta | Consecuencia |
|---|---|---|
| U1 | `GET …/jobs/{id}/logs` → **302** a `productionresultssa<N>.blob.core.windows.net`, ruta `/actions-results/<uuid>/workflow-job-run-<uuid>/logs/job/job-logs.txt`, con una firma SAS en la query (`sig`, `se`, `sp`, `sv`, `sr`, `st`, `spr`, `rsct`, `skoid`, `sktid`, `skt`, `ske`, `sks`, `skv`). El número `<N>` del host puede variar. | D-71: no fijar el host; exigir `https`. Recomiendo permitir los sufijos `.blob.core.windows.net`, `.githubusercontent.com` y `.github.com`, y ante otro host seguir sin credenciales pero registrar el host en WARNING (sin la query) |
| U2 | **Sin `Content-Encoding`**, ni con el `Accept-Encoding` por defecto ni con `identity`. `Content-Length` presente, `Accept-Ranges: bytes`, `Content-Type: text/plain`. Los primeros bytes son `EF BB` (**BOM UTF-8**), no gzip. | El `Range` sobre bytes es válido. El BOM ya lo tolera `parse_log` (`removeprefix("﻿")`); el adapter decodifica con `utf-8` y reemplazo, sin `utf-8-sig` |
| U3 | `Range: bytes=N-` → **206** con `Content-Range: bytes 10740-21480/21481`. `Range: bytes=-1000` (sufijo) → **200 con el cuerpo entero**: el blob lo ignora. | D-71 confirmado: siempre por offset (`len − max`). Además, **una respuesta 200 a un pedido con `Range` es un cuerpo completo** y pasa por el `TailBuffer`; hay que probarlo |
| U4 | **Resuelta para lo que el diseño necesita; un caso sin observar.** Medido sobre el run `flaky-timing` que re-ejecutaste con `--failed` (ahora `run_attempt = 2`, `failure`): `attempts/1/jobs` → 1 job (`run_attempt: 1`, `failure`, sus fechas del 14/11); `attempts/2/jobs` → 1 job **con otro id** (`run_attempt: 2`, `failure`, fechas del 16/11); **ningún id de job se repite entre intentos**. `GET /runs/{id}/jobs` (sin intento, `filter=latest`) devuelve solo el del intento 2; con `filter=all` devuelve los dos. El intento 1 sigue consultable después del re-run, con sus jobs intactos. Cada job lleva su propio campo `run_attempt`. **Sin observar:** si el listado de un intento incluye jobs que **no** se re-ejecutaron. Los 23 workflows del lab tienen un solo job, y con un solo job el re-run lo ejecuta de nuevo. | Se usa siempre `attempts/{n}/jobs` con el intento del `workflow_run` (nunca el listado sin intento, que cambiaría con un re-run posterior). Cada intento tiene sus propios job ids, así que los logs y las fallas (`UNIQUE (run_id, run_attempt, job_id)`) no chocan entre intentos. El caso sin observar (un re-run de **un** job cuando hay otro fallido que no se re-ejecuta) se cubre con un fixture sintético en b2 y se puede observar con un workflow del lab de dos jobs; ver "Qué sigue abierto" |
| U5 | La URL firmada **seguía válida a los 2 minutos** (206) y **estaba vencida a los 10** (403). El valor exacto entre 2 y 10 minutos no se midió. | El segundo pedido (el `Range`) sale apenas termina el primero, en segundos: no hay problema. Si un reintento del pedido del blob tardara minutos, hay que pedir el redirect de nuevo, no reusar la URL: D-71 lo dice |
| U6 | No observada (necesita un run recién terminado). | Se mide en (e) con la gracia de D-71 |
| U7 | **Resuelta.** El run de `job-timeout` termina `cancelled`, igual que su único job (`run`). `GET /repos/{o}/{r}/check-runs/{job_id}/annotations` → 200 (el id del job **es** el id del check run) con dos anotaciones de nivel `failure`: `The job has exceeded the maximum execution time of 1m0s` y `The operation was canceled.` | D-73 (opción a) es viable tal cual; el texto coincide con el patrón de GHA-TIMEOUT |
| U8 | Los check runs de Actions: `app.slug = github-actions`, `external_id` = **UUID**, `name` = nombre del job, `output.title/summary/text = null`, más `annotations_count` y `annotations_url`. `GET /commits/{sha}/check-runs?per_page=100&filter=all` funciona con el token del repo (`x-accepted-github-permissions: checks=read`). | El filtro por `app_id` (D-70, D-76) no se probó: se prueba en (c), Apéndice B |
| U9 | **Sin respuesta.** No hay comentarios de bots en ninguno de los dos repos (el lab no tiene PRs; este repo tiene 8, ninguno con comentario de bot). | Se confirma en (e) con el primer comentario real. Para los fixtures de (c) la forma es la documentada (`user.login = "<slug>[bot]"`, `user.type = "Bot"`) y se fabrica a mano |
| U10 | No comprobable en solo lectura. | Apéndice B, en (c) |
| U11 | No comprobable (no hay fork). | La casilla 3 queda abierta con su motivo |
| U12 | **API:** el commit de `main` del lab tiene **30 check runs que se llaman igual** (`run`, todos de `github-actions`, uno por workflow) y la API los lista como 30 objetos distintos, con `id` y `external_id` propios. **Interfaz y texto plano del nombre:** no comprobable en solo lectura. | Apéndice B. El hallazgo confirma que el nombre no identifica un check: lo hace `external_id` (D-70, D-76) |
| U13 | Ver la tabla de arriba: **nada falta**. | — |

## Otros hallazgos útiles para los PRs

1. **403 de permiso**: `Resource not accessible by integration`, y la cabecera
   `x-accepted-github-permissions: secrets=read` dice qué permiso habría hecho falta. Recomiendo que el
   `PermanentError` de D-67 incluya ese valor (es una cabecera de GitHub, no del cuerpo; se valida con
   `^[a-z_=,; ]{1,100}$` antes de copiarla). El 404 de un job que no existe trae `actions=read`.
2. **El 404 y el 403 traen el mismo cuerpo de forma**: `message`, `documentation_url`, `status`. Ninguno
   se copia a un mensaje de error (D-63, D-67).
3. **Rate limit**: el token de instalación tiene 5000 por hora (`x-ratelimit-resource: core`), con las
   cinco cabeceras `x-ratelimit-{limit,remaining,used,reset,resource}`. No hubo 429 ni `retry-after`:
   esas ramas de D-67 quedan cubiertas solo por tests con respuestas fabricadas.
4. **`workflow_run.pull_requests` está vacío en un `workflow_dispatch`** y el repo de cabeza es el mismo
   que el base (R1 confirmada para este caso; el caso de otro repo con la misma rama sigue sin probar).
5. **`GET /repos/{o}/{r}/pulls?head={owner}:{rama}&state=open`** devuelve 200 (vacío) con la forma de
   consulta de D-72; el caso de un PR de fork no se pudo ver.
6. **`GET /app/hook/deliveries`** funciona con el JWT y no trae el payload: sirve para diagnosticar
   entregas sin exponer datos.
7. **El workflow `name` de los runs del lab es el nombre del archivo** (`flaky-timing`), así que los checks
   de la casilla 1 se llamarán `CICD Doctor: pr-py-modnf`.
8. **El objeto `app` de un check run de Actions trae una lista enorme de permisos**: al armar los
   fixtures de (c) conviene recortarla.

## Qué sigue abierto

| Id | Qué falta | Cómo se cierra |
|---|---|---|
| U4 (un caso) | ¿Un re-run de **un** job, con otro job fallado que no se re-ejecuta, lista ese segundo job en el intento 2 (con su `run_attempt` viejo)? | Si pasa, el intento 2 volvería a diagnosticar un job del intento 1. Opciones, a decidir en b2 con el fixture sintético: aceptarlo (es lo que realmente falló en ese intento) o filtrar por `job.run_attempt == run_attempt`. Para observarlo hace falta un workflow del lab de **dos** jobs, que hoy no existe (23 de 23 tienen uno): se puede sumar en (e) |
| U9 | El primer comentario real del bot | (e) |
| U10, U12 visual | Escribir un check run de prueba | Apéndice B, en (c) |
| U11 | Un check run sobre el `head_sha` de un fork | Casilla 3, abierta |

---

Totales del script: 39 llamadas a la API, **3 `POST`** (los tres a `access_tokens`), y ninguna fuera de
la lista blanca.

## Apéndice B ejecutado (U10, U12 API y U8), 2026-11-16

Sobre el commit descartable `7c2b1af` de la rama `scratch/p1c-checks` del lab, con un token limitado al
lab y a `checks: write` (concedido: `checks: write` y `metadata: read`). 3 check runs creados y 3
`PATCH`, en el límite acordado; ninguna llamada fuera de la lista blanca.

| Id | Resultado |
|---|---|
| **U10** | **Reabrir no se puede, pero GitHub no lo rechaza.** Un `PATCH` con `status: in_progress` sobre un check `completed` contesta **200** y el check **sigue `completed` / `neutral`**. En el mismo `PATCH`, `name` y `output` **sí se aplican** (el título pasó a "Analyzing failure (attempt 2)" y el nombre cambió). El 422 que el reporter espera como señal **no ocurre**: el comportamiento real es el del plan B de D-70 (el check queda completado y solo cambia el texto), pero sin error. |
| **U12, API** | Dos check runs con el **mismo nombre** se crean (201) y se listan como objetos distintos. Un nombre con `<b>`, `**`, backticks y un link se guarda **tal cual**: la API no lo interpreta ni lo escapa. Falta la parte visual (la ve el usuario en la página del commit). |
| **U8** | El filtro `app_id` de `GET /commits/{sha}/check-runs` **funciona** (3 de 3 con el id de la app). |

**Parte visual de U12 (la vio el usuario en la página del commit):** el desplegable dice "All checks have
passed · 3 neutral checks" y lista las tres filas. El nombre con `<b>`, `**`, backticks y link se muestra
**como texto literal**, sin negrita ni link: D-76 queda confirmada (texto plano, sin escape). Los `neutral`
cuentan como "passed" en el resumen. **Sin observar:** dos filas con el mismo nombre, porque la prueba de
U10 renombró el primer check (`CICD Doctor (test) renamed`) antes de mirar; es inofensivo, la identidad es
el `external_id`.

Consecuencias para el código ya mergeado: el reporter funciona tal cual (el `PATCH` es inofensivo), pero su
test y la nota de D-70 dicen que GitHub "puede rechazar con 422"; hay que corregir la documentación y
considerar el caso "200 pero `status` sigue `completed`" como el comportamiento normal, sin reintentos.
