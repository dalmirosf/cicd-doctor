> **Original working document, written in Spanish (rioplatense) for the owner and kept untouched below this line, except that the owner's place of residence was removed from two sentences.** It is a planning draft: where it differs from `SPEC.md`, `DECISIONS.md` or the code, those are the authority (see the phase `README.md` for the differences).

# P5 — Deploy y observabilidad: plan (v1)

> Solo plan, sin código. No commiteado (`plans/` queda fuera de git). Escrito el 2026-11-22 sobre `main` en `4abe9bc`.
> Base: SPEC §4 (stack), §9.6, §13 P5, §14 (HTTPS), FR/NFR-08, NFR-10, y DECISIONS D-30, D-32, D-34, D-46, D-52, D-77, D-78.
> Los números D-117 en adelante son provisionales: se asignan en el orden de merge.
> Formato y método: `plans/p4.md` y "How a phase is built" de CLAUDE.md. Nada empieza hasta que apruebes el plan;
> el spike (§2) espera tu "andá", parte por parte. **Nada que cree recursos en AWS corre sin tu consentimiento escrito.**

## Estado (2026-11-24): respuestas del dueño y defaults elegidos

El dueño dijo: sin dominio comprado, cuenta de AWS recién creada y "seguí con lo recomendado".
Lo que sigue es lo que **yo recomendé y quedó adoptado** (a confirmar en el spike donde dice "medir"). Los precios son
**estimaciones mías sin medir**: el spike y la consola de AWS mandan.

| Tema (pregunta de §6) | Default adoptado | Por qué |
|---|---|---|
| 1. Dominio / HTTPS | **ALB + CloudFront con su certificado por defecto** (`*.cloudfront.net`, sin dominio). GitHub pega en la URL de CloudFront; CloudFront reenvía al ALB. Con un secreto en un header, el ALB rechaza lo que no venga de CloudFront. Si más adelante se compra un dominio (~USD 10-15/año), se cambia por ALB + ACM sin tocar la app | El webhook exige HTTPS; ACM sin dominio no existe; App Runner se aparta del diseño de D-46 y de la casilla ("API detrás de un ALB"). El tramo CloudFront → ALB va por HTTP dentro de AWS: es aceptable porque cada webhook va firmado con HMAC (la app ya lo verifica) y se documenta en D-120 |
| 2. Región | **`us-east-1` (N. Virginia)**. La latencia desde la ubicación del dueño no importa (todo es asíncrono: GitHub y OpenAI están en EE. UU.); `sa-east-1` (São Paulo) suele costar bastante más | Medir en el spike B1 que RDS ofrezca PG17 con `vector` ahí |
| 2. Presupuesto | Tope de aviso **USD 20/mes** con alertas al 50 % y 80 % en AWS Budgets, creadas por el dueño **antes** del primer `apply`. Encendido 24/7 el stack costaría del orden de USD 60-80/mes (RDS, ALB, 2 tareas Fargate, IPs públicas). Se prende para verificar y se apaga | Estimación mía; el spike la afina |
| 3. Cuenta | Nueva: sirve para la casilla "fresh account". Ver "Cuenta nueva" abajo | |
| 4. Langfuse | **Cloud** (plan gratuito), cuenta del dueño, claves cargadas por él | Autoalojado agrega servicios y costo; solo viaja texto ya redactado |
| 5. Token de GitHub | Fine-grained, del dueño, mínimo de permisos, por variable de entorno, **nunca por el chat**. Solo hace falta para el PR (g) | |
| 6. Repo del lab | Queda como **(g), al final**; no bloquea el deploy | |
| 7. State de Terraform | **Bucket S3 privado, cifrado y con versionado**, con bloqueo nativo (`use_lockfile`, sin DynamoDB). Se crea una vez, a mano o con un mini-Terraform de bootstrap con state local (con consentimiento); el resto vive en el bucket | |
| 8. Cabos de P4 | Se anotan y se sigue (no bloquean P5) | |
| Uso real (2026-11-24) | **Camino 1**: el uso diario sigue siendo local (api + worker + smee, USD 0). AWS se prende **una sola vez** para verificar las casillas de P5 y después se destruye (`terraform destroy`). La URL de CloudFront cambia si se recrea el stack: no importa, porque se verifica en una sola tanda y el webhook de la app de prod se edita **una vez** (la URL sale de `terraform output`). Regla: no destruir ni recrear el stack a mitad de la verificación. Antes de destruir, copia de lo que se quiera conservar (logs, capturas, ids) en el runbook | Es lo que el dueño eligió para que quede demostrado |
| App de GitHub de prod | La app de prod (D-30) **la crea el dueño** en GitHub; la sesión no la crea. Su clave privada y su secreto de webhook los carga el dueño a mano en Secrets Manager | La sesión pide el momento en que hace falta |
| Red (D-121) | Fargate en **subredes públicas con IP pública, sin NAT**; RDS en subredes privadas sin IP pública; los grupos de seguridad cierran todo salvo lo necesario | Un NAT son ~USD 30+/mes solo por existir. Salvedad de seguridad a documentar |
| Instancias | RDS `db.t4g.micro` (o la mínima con PG17 + `vector`), API 0.25 vCPU / 0.5 GB, worker 0.5 vCPU / 1-2 GB (se mide en A1: fastembed + onnxruntime) | |

**Cuenta nueva: lo que hace el DUEÑO antes del primer comando** (la sesión no toca esto ni pide claves):
1. MFA en el usuario raíz, y no usar el raíz para trabajar.
2. Un usuario administrador con **IAM Identity Center (SSO)**, para entrar con `aws sso login` y credenciales de corta vida
   (nada de claves de acceso largas en la máquina).
3. Una alerta en AWS Budgets (USD 20, avisos al 50 % y 80 %) a su correo.
4. Mirar en la consola qué créditos o plan gratuito tiene la cuenta y qué servicios restringe el plan gratuito (en cuentas
   nuevas puede limitar servicios; hay que **verificarlo en la consola**, no lo doy por sabido).
5. Elegir `us-east-1` en la consola y confirmar que ahí se ve RDS.

## 1. Punto de partida (lo que encontré leyendo)

**Ya existe:**
- Un solo paquete con dos entradas: `cicd-doctor api` y `cicd-doctor worker` (`app/container.py`). El worker se niega a
  arrancar sin `GITHUB_APP_ID` y `GITHUB_PRIVATE_KEY`; el cliente de OpenAI solo existe con `OPENAI_API_KEY` y `OPENAI_MODEL`.
- `/health` y `/ready` en la API (sirven de health check del ALB).
- Migraciones Alembic `0001` a `0006` y `docker/compose.yaml` con Postgres + pgvector (`pgvector/pgvector:0.8.7-pg17`, D-79).
- `FastEmbedEmbedder` con carga en hilo de fondo y `EMBEDDING_CACHE_DIR` (D-77, D-78): el modelo ya se puede hornear en la imagen.
- `.env.example` ya lista `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` y `settings.py` los lee.
- CI en `ci.yml` y `evals.yml`; no hay `cd.yml`, ni `Dockerfile`, ni `infra/`.

**Lo que NO existe y la SPEC da por hecho (hallazgos):**
1. **No hay logs JSON.** `container.py:76` dice "plain-text logs on stderr until structlog arrives". Los logs son texto con
   `logger.info("webhook %s ...", delivery_id)`. La casilla de §13 ("seguir un diagnóstico por `delivery_id` en CloudWatch")
   y NFR-10 piden JSON correlacionado por `delivery_id`, `job_id`, `run_id`, `installation_id`, `repository`. Hay que
   construirlo, no solo configurarlo.
2. **No hay tercera pasada de redacción.** SPEC §9 dice que la redacción corre tres veces y que la tercera es "the
   logging pipeline (today: `jobs.last_error`; structlog in P5)". Cuando haya structlog, un procesador debe redactar.
3. **Langfuse no está implementado.** Solo están las tres variables de entorno. Falta un puerto de trazas en `core/`
   (`core/` no puede importar `langfuse`, lo impone `test_architecture.py`), un adaptador y la prueba del token falso.
4. **No hay `Dockerfile`, ni imagen, ni `docker compose up` del stack completo**, solo el servicio `db`.
5. **HTTPS (§14):** un ALB necesita un certificado ACM, y ACM necesita un dominio. Hoy no hay dominio. Es la decisión
   que más cambia el diseño (ver §6, pregunta 1).
6. **Los secretos y Terraform:** si Terraform escribe el valor de un secreto, ese valor queda en el state. La clave
   privada de la app y el secreto del webhook no deben pasar por ahí (ver §4, regla de secretos).
7. **El orden del huevo y la gallina:** el servicio ECS necesita una imagen en ECR, y ECR lo crea Terraform. Hace falta
   un primer `apply` parcial (o `desired_count = 0`) y después el CD.

## 2. Spike (antes de implementar; cada parte con tu "andá")

**A. Local y de solo lectura (sin consentimiento especial):**
- A1. `docker build` de una imagen de prueba en un directorio temporal: tamaño, tiempo, que el modelo de fastembed
  quede horneado y cargue con la red apagada. Sale de ahí la memoria mínima de la tarea Fargate (el modelo son ~67 MB,
  pero onnxruntime sube el uso).
- A2. Versiones: `terraform version`, `aws --version`, qué providers `hashicorp/aws` y `integrations/github` se pueden fijar.
- A3. Arrancar la API y el worker de la imagen contra el compose local, con el modo de logs JSON de prueba.

**B. Lee tu cuenta de AWS (solo lectura, con tu sí):**
- B1. `aws sts get-caller-identity` y `aws rds describe-db-engine-versions --engine postgres` para confirmar que **RDS
  ofrece PostgreSQL 17 con `vector` en la región elegida** y qué clases de instancia hay. Sin imprimir claves.

**C. Escribe o gasta (cada uno con consentimiento aparte, no entra en el spike por defecto):**
- C1. Un `terraform plan` real contra la cuenta (lee, no crea).
- C2. El primer `terraform apply` (crea y cobra). Va en la verificación en vivo (§5, PR g), no en el spike.

## 3. PRs (cada uno mergeable solo, con `main` verde y la app funcionando)

| PR | Qué | Toca | Necesita Postgres | AWS | Casilla de §13 |
|---|---|---|---|---|---|
| a | Logs JSON con structlog: correlación por `delivery_id`, `job_id`, `run_id`, `installation_id`, `repository`; procesador que redacta; `LOG_FORMAT=json\|text` (texto por defecto en local) | `app/`, `api/`, `worker/`, tests | no | no | parte de la 6.ª |
| b | Trazas de LLM: puerto `Tracer` en `core/ports.py`, adaptador Langfuse, `NoopTracer` si no hay claves; tokens, costo, latencia, versión del prompt, **solo texto redactado** | `core/`, `adapters/`, `container.py`, tests | no | no | parte de la 6.ª |
| c | `Dockerfile` multi-etapa (usuario no root, modelo horneado), servicios `api`, `worker` y `migrate` en compose; `docker compose up` levanta todo | `Dockerfile`, `docker/`, `README.md` | sí | no | 1.ª |
| d | Terraform base: backend remoto (S3 + bloqueo), red, ECR, RDS PG17 + `vector`, Secrets Manager (solo los contenedores), IAM mínimos, clúster ECS | `infra/terraform/` | no | **solo `validate` y `fmt`** | 2.ª (código) |
| e | Terraform: servicios ECS (API tras ALB, worker count 1), tarea de migración, OIDC de GitHub y rol de deploy | `infra/terraform/` | no | solo `validate` | 2.ª (código) |
| f | `cd.yml`: build, push a ECR, migración como tarea única, y recién entonces actualizar servicios; sin claves largas | `.github/workflows/cd.yml` | no | no (no corre hasta g) | 4.ª (código) |
| g | Terraform provider de GitHub: crea el repo del lab desde `lab/` (ojo: el `main` del lab tiene un merge commit, ver "Publish" en `lab/README.md`) | `infra/terraform/github/` | no | token de GitHub | 3.ª (código) |
| h | **Verificación en vivo**: `apply`, `plan` sin cambios, CD real, webhook de la app de prod, flujo de aceptación de P1c, CloudWatch, Langfuse con token falso, `destroy` o apagado | `lab/README.md` (runbook), SPEC, docs | no | **sí, gasta** | 2.ª a 6.ª |
| i | Archivo de fase en `docs/phases/p5/`, README y SPEC/CLAUDE alineados | docs | no | no | cierre |

**Orden y choques de archivos.** a y b chocan en `container.py` y `settings.py`: van **en serie** (a, luego b). c es
independiente de a y b y puede ir en paralelo con ellos. d y e chocan entre sí (`infra/terraform/`): en serie, y pueden ir
en paralelo con a, b, c. f necesita saber los nombres que fijan d y e. g es independiente. h va al final y espera a todos.
La regla de CLAUDE.md sobre sesiones aplica: dos escritores no tocan los mismos archivos.

## 4. Decisiones a redactar (borrador; alternativas en cada una)

- **D-117 Logs.** structlog con salida JSON a stderr; el procesador de redacción reutiliza `core/redaction.py`. Alternativa:
  `logging` estándar con un formateador JSON propio (menos dependencias, menos procesadores listos).
- **D-118 Trazas.** Puerto `Tracer` en `core/`; el adaptador Langfuse es opcional y falla en silencio (una traza caída nunca
  rompe un diagnóstico). Alternativa: OpenTelemetry (más estándar, más piezas para lo que se pide).
- **D-119 Imagen.** Una sola imagen para `api` y `worker` (el comando decide), usuario no root, modelo horneado con
  `EMBEDDING_CACHE_DIR` apuntando adentro, `uv sync --frozen --no-dev`.
- **D-120 Entrada HTTPS.** Según tu respuesta a la pregunta 1: ALB + ACM + dominio, o App Runner / una URL sin dominio.
- **D-121 Red y costo.** Fargate en subredes públicas con IP pública (sin NAT) y RDS privado, o subredes privadas con NAT
  (~USD 30+/mes solo de NAT, estimación mía sin medir). Recomiendo la primera para un proyecto de aprendizaje, anotando
  la salvedad de seguridad.
- **D-122 Secretos.** Terraform crea los contenedores de los secretos; **los valores** (clave privada de la app, secreto
  del webhook, `OPENAI_API_KEY`, claves de Langfuse) los carga **el dueño** a mano, fuera de Terraform y del chat, con
  `ignore_changes` sobre el valor. Así no entran al state.
- **D-123 CD.** Rol de deploy por OIDC limitado a `repo:<owner>/cicd-doctor:ref:refs/heads/main`; la migración corre como
  tarea ECS única y el deploy espera su código de salida 0 antes de tocar los servicios.
- **D-124 Teardown.** Cómo se apaga lo que cuesta (RDS, ALB, NAT) cuando no se usa, y qué se conserva.

**Regla de secretos para toda la fase.** Nunca imprimir ni leer en el chat `GITHUB_PRIVATE_KEY`, el secreto del webhook,
`OPENAI_API_KEY`, claves de AWS ni tokens. No se commitea `*.tfstate`, `*.tfvars` con valores, ni `.env`. El `.gitignore`
cubre eso **antes** del primer `terraform init`.

## 5. Verificación en vivo (PR h): qué observar para tildar cada casilla

| Casilla de §13 P5 | Evidencia que se anota |
|---|---|
| Dockerfile + `docker compose up` | tamaño de imagen, usuario, tiempo de arranque, `/ready` en `ready` |
| `terraform apply` en cuenta nueva y segundo `plan` sin cambios | recursos creados, salida del segundo `plan` ("No changes") |
| Repo del lab creado con el provider | URL del repo y contenido igual a `lab/` |
| Merge a `main` → CD → migración → servicios, sin claves largas | id del run de `cd.yml`, código de la tarea de migración, versión de la tarea nueva |
| Webhook de la app de prod al HTTPS público; flujo de P1c pasa | entregas 200 en GitHub, un check run y un comentario reales, tiempo hasta el comentario |
| CloudWatch con JSON seguible por `delivery_id`; Langfuse con una traza por llamada | consulta de Logs Insights, traza con tokens, costo, latencia y versión del prompt, y el token falso plantado **no** aparece |

Una casilla se tilda solo con lo que se observó. Lo que no se pudo verificar queda abierto con su motivo.

## 6. Preguntas para vos (cambian el plan)

1. **¿Tenés un dominio?** Con dominio: ALB + ACM (lo que dice D-46). Sin dominio: App Runner o ALB sin HTTPS propio (el
   webhook de GitHub exige HTTPS). Recomiendo decidirlo antes de d.
2. **Región de AWS y presupuesto mensual tope.** Lo que más cuesta prendido es RDS, el ALB y (si hay) NAT. ¿Cuánto
   estás dispuesto a gastar y por cuánto tiempo?
3. **¿Cuenta de AWS nueva o una que ya usás?** La casilla dice "fresh account". Una cuenta existente puede tener cosas que
   chocan con los nombres.
4. **Langfuse: ¿cloud (cuenta tuya) o autoalojado?** Cloud es lo más simple; autoalojado agrega servicios y costo.
5. **¿Quién crea el token de GitHub para el provider (PR g)?** Es tuyo, con el mínimo de permisos, y no pasa por el chat.
6. **¿Dejamos el repo del lab para el final (g) o lo sacás de P5?** No bloquea el deploy.
7. **Backend del state:** S3 con bloqueo creado a mano una vez (bootstrap), o state local solo para esta fase.
8. **Los dos cabos sueltos de P4** (verificación independiente de #67 y #69; tope de gasto del juez): ¿se anotan y siguen,
   o se cierran antes? Yo los anoto y sigo.

## 7. Riesgos y desconocidos

- Que RDS no ofrezca la versión de `pgvector` que usa el repo (0.8.x) en esa región: el spike B1 lo mide.
- Que la tarea Fargate necesite más memoria que la esperada por fastembed + onnxruntime: el spike A1 lo mide.
- Costo: todo lo que prenda queda facturando hasta el `destroy`. Se pide tope y se documenta el apagado.
- El state de Terraform guarda datos sensibles aunque los secretos no estén: bucket privado, cifrado, sin versionar en git.
- Webhook de prod y de dev son apps distintas (D-30): no se mezclan las claves.
- Una migración que falla a mitad de un deploy deja la base a medias: la tarea de migración debe ser idempotente (Alembic
  lo es) y el deploy se detiene si sale con código distinto de 0.

---

## Apéndice A — Brief para la sesión de P5 (copiar y pegar)

> **Tarea:** P5 (deploy y observabilidad) del repo `cicd-doctor`, siguiendo `plans/p5.md` (leelo entero) y el método de
> "How a phase is built" de CLAUDE.md.
>
> **No hagas todavía:** ningún comando que cree, cambie o cobre recursos en AWS o GitHub; ningún `terraform apply`; no
> leas ni imprimas `.env`, claves, tokens ni `GITHUB_PRIVATE_KEY`; no commitees ni abras PR sin que te lo pida.
>
> **Paso 1.** Las preguntas de §6 ya tienen respuesta: están en la sección "Estado (2026-11-24)" del plan (adoptá esos defaults, sin dominio, `us-east-1`, ALB + CloudFront). Respondé con el spike propuesto (§2) parte por parte, marcando qué defaults pensás medir, y listá solo las preguntas que sigan abiertas; esperá mi "andá". La parte A del
> spike (local, solo lectura) podés correrla cuando te lo diga. La B (lee AWS) y la C (escribe) piden consentimiento aparte.
>
> **Paso 2.** Con las respuestas, ejecutá **un PR por sesión y por rama** cortada de `origin/main`, empezando por (a) logs
> JSON, (c) Dockerfile y (d) Terraform base, que no chocan. Cada PR: tests, `ruff`, `mypy`, suite rápida y la completa con
> Postgres arriba; D-NN y SPEC/CLAUDE alineados en el mismo PR; verificación por otra sesión antes de abrirlo.
>
> **Reglas que no se rompen:** `core/` no importa `langfuse`, `boto3` ni nada de IO (lo impone `test_architecture.py`);
> los logs nunca llevan texto crudo de un log de CI ni secretos; los valores de los secretos los carga el dueño, no
> Terraform; nada de claves de AWS largas (OIDC); código, comentarios y commits en inglés; sin líneas `Co-Authored-By`.
>
> **Hecho cuando:** cada casilla de SPEC §13 P5 está tildada con evidencia observada (ids, tiempos, filas) o abierta con
> su motivo, y `docs/phases/p5/` tiene el plan, el spike y el README de la fase.
