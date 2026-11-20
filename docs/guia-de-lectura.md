# Guía de lectura de CICD Doctor

Esta guía está en castellano rioplatense a propósito: es la única excepción a la regla "todo en inglés" del
repositorio. Sirve para leer el proyecto entero, despacio, cuando la app esté terminada. No reemplaza a los
documentos en inglés: los resume y te dice en qué orden leerlos. Si algo de acá contradice a `DECISIONS.md`,
`SPEC.md` o al código, tienen razón ellos y esta guía está mal (avisá).

## 1. Qué es CICD Doctor

Es una GitHub App. Cuando falla un run de GitHub Actions, la app lee el log, busca la causa y la explica en el
pull request: un *check run* (la fila de la lista de checks) y un comentario. Siempre intenta primero con reglas
fijas (29 patrones conocidos: falta un módulo, venció un timeout, etc.). Si está permitido, además le pregunta a un
modelo de lenguaje (OpenAI, hoy `gpt-6-luna`), y no se fía de lo que contesta: le pide una forma estricta, verifica
que las líneas de evidencia existan de verdad en el log y muestra todo como texto inerte (sin links ni menciones
que actúen sobre quien lo lee).

La idea que atraviesa todo el proyecto: **el log de CI es entrada no confiable**. Cualquiera puede escribir lo
que quiera en un log (o en un comentario de PR, o en un archivo de configuración), así que nada de eso se ejecuta,
nada se obedece, y todo lo que sale hacia GitHub pasa por un renderizador seguro. Por eso hay una suite de
"inyección" que ataca a la app a propósito.

Otras ideas que se repiten: cada decisión importante queda escrita (`DECISIONS.md`, entradas `D-NN`); una casilla
de `SPEC.md` §13 solo se tilda con evidencia observada (ids, horas, filas); y todo lo que escribe en GitHub o gasta
plata lo hacés vos, con tu consentimiento explícito.

## 2. En qué orden leer

1. `README.md` de la raíz y `SPEC.md` §1 a §3: qué es el producto.
2. Esta guía, entera.
3. `docs/README.md`: el índice de la carpeta `docs/`.
4. Las fases, en orden. Para cada una: primero su `README.md` (en inglés, cuenta la historia); si querés el detalle,
   después `spike.md` y `plan.md` (en castellano, tal como se escribieron):
   - `docs/phases/p0-p1b/`: cimientos, cola, webhook, pipeline sin red, reglas, camino del LLM. No tiene plan ni spike
     (es una reconstrucción hecha a partir de los PRs y de `DECISIONS.md`).
   - `docs/phases/p1c/`: el GitHub de verdad.
   - `docs/phases/p2/`: la memoria.
   - `docs/phases/p3/`: comandos, sugerencias y configuración.
5. `DECISIONS.md` de a tramos, siguiendo las fases (cada `README.md` de fase lista qué `D-NN` produjo).
6. `SPEC.md` §13 con las casillas tildadas y su evidencia; y `lab/README.md`, que tiene los runbooks de cada
   verificación en vivo.
7. Al final, `CLAUDE.md`: cómo trabajan las sesiones de Claude en este repo.

Autoridad cuando dos documentos discrepan: el código y sus tests, después `DECISIONS.md`, después `SPEC.md`. Los
planes son intención; el `README.md` de cada fase dice dónde difieren del código final.

## 3. La historia, fase por fase

### P0, P1a y P1b: construir sin GitHub y sin OpenAI (13 al 16 de noviembre de 2026)

Primero se armó el esqueleto: Python 3.13, `uv`, `ruff`, `mypy` estricto, tests, `pre-commit`, Docker Compose con
Postgres y la extensión de vectores, y CI. La arquitectura es *hexagonal*: el centro (`core/`) contiene las reglas
del negocio y no puede importar nada de afuera (un test lo comprueba); los *adapters* hablan con Postgres, GitHub
y OpenAI.

En P1a se construyó la entrada: un webhook que verifica la firma de GitHub y no hace nada más que encolar un
trabajo en Postgres, y un *worker* que consume esa cola (con leases y reintentos, para que un worker que se muera
a mitad de un trabajo no pierda ni duplique nada).

En P1b se construyó todo el camino "de un log crudo a un diagnóstico", sin red: elegir los jobs que fallaron,
quedarse con la cola del log, limpiarlo, **redactar secretos** antes de guardar o enviar cualquier cosa, armar un
*excerpt* de hasta 6.000 tokens, calcular una *firma* del error (para reconocer el mismo error otra vez), las
29 reglas, el camino del LLM con un modelo falso, el renderizador seguro y la suite de inyección. También nació
el repositorio de laboratorio (`lab/`): 23 workflows que fallan a propósito, de donde se capturaron logs reales
para los tests. Los logs reales corrigieron suposiciones: hacía falta limpiar secuencias de escape de terminal,
y la redacción se comía la máscara `***` que pone GitHub.

### P1c: el GitHub de verdad (16 y 17 de noviembre)

Primera fase con plan, spike y verificación en vivo. El spike (solo lecturas) midió cosas que no se podían
adivinar: la URL del log es un redirect firmado que vence en minutos; un `Range` por final no funciona pero uno por
inicio sí; cada intento de un run tiene sus propios ids de job; un job que se pasa de `timeout-minutes` termina
`cancelled` y la frase "exceeded the maximum execution time" está en una anotación, no en el log. Con eso se
construyó la autenticación (la app firma un JWT y pide tokens de instalación por repositorio y por permiso), la
descarga de logs, el reporter (un check run por workflow y un comentario "sticky" por PR) y el caso de uso que
los ata. Se verificó en vivo: un PR del lab recibió su check y su comentario 9,4 segundos después de terminar
el run; un re-run editó el mismo check y el mismo comentario; una clave inválida y un presupuesto en cero cayeron a
las reglas con el texto correcto; la app diagnosticó su propio CI roto. Una sorpresa: GitHub no reabre un check
completado (contesta 200 y no hace nada), distinto de lo que el plan esperaba. **Quedó abierta** la casilla del PR
desde un fork, porque hace falta un fork de otra cuenta.

### P2: la memoria (17 y 18 de noviembre)

La app aprende de lo que ya vio, solo dentro de cada repositorio. Cada falla se convierte en un *embedding* (un
vector de 384 números que permite medir "se parece a"). Si llega la misma falla exacta (misma firma) y ya hay una
respuesta del modelo verificada de hace menos de 7 días, se reutiliza sin llamar al modelo (la *caché por firma*).
Si llega una falla *parecida* (puntaje 0,85 o más), el reporte la lista con el commit que la arregló. Si un
mismo commit falla y después pasa en un re-run, se marca *flaky*; si pasa en otro commit de la misma rama, esa
falla queda "arreglada por" ese commit, y el comentario lo dice. Hay una purga diaria. El spike cambió dos
decisiones antes de escribir código: el modelo de embeddings se calienta en segundo plano (la primera descarga
tardó hasta 166 segundos) y la búsqueda de parecidas es exacta por repositorio (el índice aproximado perdía
resultados). En vivo se vio todo menos el fork. De paso se eligió el modelo de OpenAI (`gpt-6-luna`) con una
mini-evaluación medida.

### P3: comandos, sugerencias y configuración (20 de noviembre)

Un colaborador comenta `/cicd-doctor explain` en un PR: la app reacciona con 👀 y mejora el diagnóstico ya
publicado usando el modelo, incluso en un PR de fork (con límites: sin caché y sin fallas parecidas, para que un
atacante no pueda envenenar la memoria). Solo se acepta un comando por PR cada 5 minutos, y los rechazos son
silenciosos. El modelo ahora ve el diff del PR y puede proponer un cambio, que se publica como *review comment*
con bloque `suggestion` solo si cae dentro de un único tramo del diff (si no, queda como texto). La configuración
`.github/cicd-doctor.yml` se lee **solo de la rama por defecto** (un PR no puede cambiar su propia política) y un
archivo inválido apaga el LLM en vez de usar los valores por defecto. El spike descubrió que la app ni recibía el
evento `issue_comment` (hacía falta el permiso Issues) y que los runs de un PR de fork salen sin `pull_requests`.
En vivo: 👀 en 3 segundos y diagnóstico editado en 11. **Sigue abierto** el fork (otra vez), y la ruta "fuera del
diff" con una respuesta nueva del modelo (esa vez contestó la caché).

### Lo que viene

P4 (evaluaciones a escala con casos públicos), P5 (despliegue en AWS con Terraform y observabilidad) y P6 (extras:
una app "Fixer" que abre PRs y Kubernetes local). Están en `SPEC.md` §13 con sus casillas vacías.

## 4. Glosario

- **GitHub App / instalación:** la app es el programa registrado en GitHub; una *instalación* es esa app puesta en
  una cuenta o repositorio concreto. Los permisos se aceptan por instalación.
- **Token de instalación:** credencial temporal (una hora) que la app pide a GitHub firmando un JWT con su clave
  privada. Se pide uno por repositorio y solo con los permisos que ese caso de uso necesita.
- **JWT:** un token firmado que prueba que quien llama es la app; se usa solo para pedir tokens de instalación.
- **Webhook:** GitHub avisa por HTTP que pasó algo (un run terminó, alguien comentó). Va firmado con un secreto
  compartido; si la firma no coincide, la app contesta 401 y no guarda nada.
- **Entrega (delivery):** un aviso concreto del webhook, con id propio; sirve para no procesar dos veces lo mismo.
- **Cola de trabajos / worker / lease:** el webhook solo encola; el worker toma trabajos de Postgres. Un *lease* es
  un alquiler con vencimiento: si el worker muere, el trabajo vuelve a quedar disponible.
- **Idempotencia:** hacer algo dos veces da el mismo resultado que hacerlo una (clave para reintentos).
- **Run / intento (attempt):** un run es una ejecución de un workflow; cada "Re-run" es un intento nuevo del
  mismo run, con ids de job propios.
- **Check run:** la fila `CICD Doctor: <workflow>` que aparece entre los checks del commit. Queda `neutral`
  (la app informa, no bloquea). GitHub no permite reabrir uno ya completado.
- **Comentario sticky:** un único comentario por PR y workflow que la app edita en vez de crear otro. Se
  reconoce por un marcador oculto y solo entre los comentarios del propio bot.
- **Excerpt:** el recorte del log (hasta 6.000 tokens) que se analiza y se manda al modelo, ya redactado.
- **Redacción:** reemplazar secretos (tokens, claves, contraseñas) por `[REDACTED:...]` antes de guardar, enviar
  o publicar.
- **Firma (signature):** texto normalizado del error (sin horas, rutas ni ids) y su hash; dos fallas iguales dan la
  misma firma aunque los runs sean distintos.
- **Checkpoint:** el análisis de un intento se calcula una vez y se guarda; si el trabajo se reintenta, se publica
  desde lo guardado (no se vuelve a llamar al modelo ni a bajar el log).
- **Publication lock (candado de publicación):** un candado de Postgres que hace que, aunque haya varios workers,
  solo uno escriba a la vez un check o un comentario (así hay exactamente uno de cada).
- **Presupuesto (budget):** antes de cada llamada al modelo se reserva su costo contra un tope diario; si no
  alcanza, se cae a las reglas.
- **Fallback (caída a las reglas):** si el modelo falla, está apagado, se acabó el presupuesto o devuelve algo
  inválido, se publica el diagnóstico de las reglas con el motivo.
- **PR de fork:** un pull request que viene de una copia del repositorio en otra cuenta. Su código y sus logs los
  controla otra persona, así que la app solo usa reglas automáticamente y el modelo únicamente si un colaborador
  lo pide con `explain`.
- **Nonce:** una cadena aleatoria que delimita el bloque no confiable dentro del prompt (`<untrusted_log nonce=…>`),
  para que el contenido del log no pueda "cerrar" el bloque y hacerse pasar por instrucciones.
- **Inyección de prompt:** intentar que el modelo obedezca instrucciones escondidas en el log, el diff o un
  comentario. La suite de inyección ataca a la app a propósito; "ataque ganado" es que el modelo obedezca.
- **Oráculo:** el verificador de los tests que decide si un ataque ganó; no puede llamar al código que juzga, para
  no darle la razón por construcción.
- **Mutación:** romper a mano una defensa y comprobar que algún test falla; así se prueba que el test sirve.
- **Embedding:** vector numérico (384 dimensiones) de la firma; la distancia entre vectores mide parecido.
- **Caché por firma:** reutilizar una respuesta verificada del modelo para la misma firma exacta, en el mismo repo,
  con el mismo prompt y de menos de 7 días; nunca desde un fork.
- **Flaky:** falla intermitente: el mismo commit pasa en un re-run sin cambiar código.
- **`explain`:** el comando `/cicd-doctor explain` en un PR.
- **Review comment / bloque `suggestion`:** un comentario sobre líneas del diff; con el bloque, GitHub muestra un
  botón "Commit suggestion". Solo se publica si cae dentro de un tramo (hunk) del diff.
- **Diff / hunk:** el diff son los cambios del PR; un hunk es cada tramo continuo de líneas cambiadas.
- **Spike:** una exploración corta y de solo lectura, antes de programar, para medir lo que no se puede deducir
  leyendo.
- **Fixture:** dato de prueba guardado en el repositorio; los de GitHub se *sanitizan* (se reemplazan nombres, ids
  y secretos) y nunca se editan a mano.
- **Lab:** el repositorio `cicd-doctor-lab`, con workflows que fallan a propósito.
- **Casilla (box):** un criterio de aceptación de `SPEC.md` §13; se tilda solo con evidencia observada.
- **D-NN:** una entrada de `DECISIONS.md`.

## 5. Cómo se trabaja: el método de una fase

El método está en `CLAUDE.md`, sección "How a phase is built". En resumen:

1. **Un plan por fase**, escrito antes del código: se parte la fase en PRs chicos (cada uno se puede mergear solo
   con `main` verde), con sus archivos, sus tests y la casilla que cierran. Las decisiones nuevas se redactan como
   `D-NN` provisionales (los números se asignan al mergear) y los riesgos y las preguntas para vos quedan listados.
   Vos respondés las preguntas y aprobás.
2. **Un spike de solo lectura**, que vos aprobás parte por parte antes de que corra. Mide lo que no se puede
   deducir (cómo contesta la API de verdad, cuánto tarda una librería). Lo que escribe en GitHub, gasta plata o
   toca `.env` se lista aparte y necesita tu consentimiento. Las respuestas guardadas se sanitizan y las revisás vos
   antes de que entren al repo.
3. **Una sesión y una rama por PR**, con un encargo autocontenido. Una sesión *autora* escribe el código y los
   tests. No hace commit ni PR si no se lo piden.
4. **Un verificador independiente**: *otra* sesión, que no escribió el código, corre las puertas de calidad (`ruff`,
   `mypy`, tests rápidos y completos con Postgres), lee el diff buscando código que se pasó de alcance y código
   crítico para la seguridad (credenciales, redacción, texto no confiable, idempotencia), y revisa que los
   documentos coincidan con el código. El informe del autor no cuenta como prueba. Los PRs de P3 muestran lo que
   esto encuentra: un `suggestion` con caracteres bidireccionales que pasaba, una ruta con barra invertida, un
   oráculo que aceptaba un comentario sin sugerencia.
5. **Los documentos van en el mismo PR que el código:** el `D-NN`, `SPEC.md` y `CLAUDE.md`.
6. **Verificación en vivo al final de la fase**, contra la app de desarrollo y el lab, siguiendo el runbook de
   `lab/README.md`. Vos hacés las escrituras; las lecturas se hacen con `gh`. Una casilla se tilda solo con lo
   que se observó; lo que no se pudo ver queda abierto con su motivo (por eso el fork sigue abierto).

La carpeta `plans/` (borradores de trabajo) sigue fuera de git. Cuando una fase se verifica, su plan revisado, su
informe de spike y sus fixtures se copian a `docs/phases/<fase>/` en el mismo PR que cierra los documentos de la
fase, y se escribe un `README.md` corto de la fase. Eso es lo que hizo el PR `docs-phases` con P1c, P2 y P3.

## 6. Cosas que conviene tener presentes al leer

- Los planes están en castellano y usan números `D-NN` provisionales; en P3 todos cambiaron al mergear. Usá la tabla
  del `README.md` de P3.
- Los números de PR salen del orden de merge, no del orden del plan.
- Lo que el plan esperaba y la realidad contradijo (por ejemplo, GitHub no reabre un check completado, el HNSW pierde
  resultados con repos grandes, la app no recibía `issue_comment`) está en la sección "Where the plan and the final
  code differ" de cada fase: es la parte más instructiva de leer.
- Pendientes abiertos a la fecha (20 de noviembre de 2026): el PR desde un fork (P1c y P3), la ruta "fuera del diff"
  con una respuesta fresca del modelo, y todo P4 a P6.
