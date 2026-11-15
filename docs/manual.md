# Manual de Historia Viva

Este manual es para las personas de la SEU y de la FIE que usan Historia Viva, y para quien lo opera. Las palabras
entre comillas «así» son las que se ven en la pantalla. La versión corta, para tener al lado de la computadora, es la
guía de una página; este manual la amplía.

Cada capítulo termina con **Para quien opera**: el runbook que corresponde cuando algo de ese capítulo hay que
hacerlo desde la PC o el servidor, o cuando falla.

## Contenido

- [Qué es Historia Viva](#que-es-historia-viva)
- [Roles: quién puede qué](#roles-quien-puede-que)
- [Entrar](#entrar)
- [Cargar](#cargar)
- [Validar](#validar)
- [Conversar con el Acervo](#conversar-con-el-acervo)
- [Efemérides y Relatos](#efemerides-y-relatos)
- [Informe de acreditación](#informe-de-acreditacion)
- [Qué no subir](#que-no-subir)
- [Administrar](#administrar)
- [Operación](#operacion)
- [Agente externo](#agente-externo)
- [Lo que todavía está apagado](#lo-que-todavia-esta-apagado)
- [Accesibilidad](#accesibilidad)

## Qué es Historia Viva

Historia Viva es el archivo histórico de la Facultad de Ingeniería del Ejército. Recibe las Piezas del Acervo
(fotografías, actas, resoluciones, convenios, memorias, planillas y videos), propone una Ficha de cada una, y una
persona de la SEU la completa con el Contexto que solo ella conoce y la firma. Lo firmado queda buscable: se le puede
preguntar en lenguaje natural y responde con Citas a las Piezas de donde sale cada dato.

Algunas palabras que se usan en todo el manual:

- **Pieza**: una unidad del Acervo (una foto, un acta, una planilla, un video).
- **Ficha**: el texto que representa a la Pieza: lo que dice o muestra, más el Contexto que agrega una persona.
- **Contexto**: lo que no está escrito en la Pieza y solo sabe una persona: de qué evento es, quién aparece, por qué
  importa.
- **Lote**: un grupo de Piezas con el mismo origen y la misma Fecha, que se cargan juntas y heredan esos datos.
- Los estados de una Pieza: **Recibida** (se está leyendo), **Pendiente** (tiene Ficha propuesta y nadie la firmó),
  **Incompleta** (alguien la trabajó pero falta un dato obligatorio) y **Validada** (firmada con todo completo).
  Solo las Validadas se pueden buscar.
- **Cita**: la referencia al lugar exacto de la Ficha de donde sale una respuesta, con enlace al original.

Nada llega a la búsqueda ni a otro sistema sin que una persona lo haya firmado. Los modelos proponen; las personas
deciden.

**Para quien opera:** arrancar y parar el sistema es el [runbook R1](runbook-r1-arrancar-y-parar.md).

## Roles: quién puede qué

Cada persona tiene un solo rol. La pantalla Administración › Personas muestra, al dar de alta, qué puede el rol
elegido; acá está lo mismo, más lo que **no** puede.

### RGC

El Responsable de Gestión del Conocimiento de la SEU.

- **Puede:** cargar Piezas y crear Lotes, validar Fichas, dar de alta a las personas (menos al Administrador
  técnico), retirar y restaurar Piezas, revisar efemérides y Relatos, y aprobar el Informe de acreditación.
- **No puede:** generar el Informe de acreditación (lo genera el Coordinador de Extensión), ni operar el sistema
  (modelos, Interruptor global, tokens).

### Coordinador de Extensión

- **Puede:** cargar y validar como el RGC, revisar efemérides y Relatos, generar el Informe de acreditación y
  aprobarlo junto con el RGC.
- **No puede:** dar de alta personas, retirar ni restaurar Piezas, ni operar el sistema.

### Director de Carrera

- **Puede:** conversar con Historia Viva, abrir los originales y la Ficha en solo lectura («Ver la Ficha») y leer lo
  ya aprobado: las efemérides y Relatos Aprobados y los Informes de acreditación.
- **No puede:** cargar ni validar. Solo ve Piezas Validadas: la Carrera es un filtro de la búsqueda, no un permiso.

### Administrador técnico

Opera el sistema: modelos, interruptor, tokens de Agentes y comandos. **No firma Fichas.**

- **Puede:** las pestañas «Modelos», «Interruptor», «Tokens» y «Programación de efemérides» de Administración, la
  Conversación y la lectura de lo Validado, y descargar «Mediciones de la sesión».
- **No puede:** validar ni firmar (si lo intenta, el sistema responde «Tu rol no firma Fichas.»), ni decidir sobre el
  contenido del Acervo.
- **Interinato:** mientras no hay un RGC nombrado, el Administrador técnico da de alta a las personas y puede retirar
  Piezas (aparecen las pestañas «Personas» y «Retiradas» con una franja que lo explica). Cada alta queda en la
  auditoría con la marca «interino». Dar de alta al primer RGC termina el interinato. Aun así, nunca valida.

Si alguien intenta algo que su rol no permite, ve «Tu rol no permite esta acción.».

**Para quien opera:** dar de alta a una persona es el [runbook R3](runbook-r3-alta-de-persona.md); darla de baja o
cambiarle el rol, el [runbook R4](runbook-r4-baja-o-cambio-de-rol.md).

## Entrar

1. Abrí la dirección del sistema en el navegador. Es la pantalla de entrada y, después de entrar, la de
   «Mis Piezas».
2. Tocá «Entrar con la cuenta de la FIE» y elegí tu cuenta institucional.
3. Arriba aparece «Entraste como …» con tu nombre y tu rol, el botón «Salir» y los enlaces que tu rol puede usar:
   «Lotes», «Conversar», «Efemérides», «Informes» y «Administración». El enlace «Ayuda» abre este manual.

En la PC de desarrollo, en lugar del botón de Google hay un campo «Tu email» y el botón «Entrar». Lo que se firma así
queda como **firma de ensayo** («Firma de ensayo, identidad no verificada.») y no pasa al servidor de la
universidad.

Si algo sale mal, la pantalla lo dice:

- «Esa cuenta no es de la FIE. Entrá con tu correo institucional (@fie.undef.edu.ar).»
- «Tu cuenta es de la FIE pero todavía no está habilitada. Pedile el alta a la persona que gestiona el acceso (el
  RGC; mientras no haya RGC, el Administrador técnico).»
- «Tu sesión venció por inactividad. Entrá de nuevo.»
- «No pudimos conectar con Google. Probá de nuevo en un rato.»

**Para quien opera:** el alta que habilita a una persona es el [runbook R3](runbook-r3-alta-de-persona.md).

## Cargar

Cargan el RGC y el Coordinador de Extensión, desde la pantalla de entrada.

### Una Pieza suelta

En «Subir una Pieza nueva», arrastrá el archivo a la zona de carga o hacé clic para elegirlo. Se aceptan
«Fotografías, PDF, Word, planillas (.xlsx, .csv, .tsv), texto (.txt) o video (MP4, MOV, WebM, hasta 2 GB y 3 h)».
Una Pieza suelta no hereda nada: su Ficha se completa entera a mano.

### Un Lote

Para varias Piezas con el mismo origen y la misma Fecha, creá un Lote en la sección «Lote»: «Nombre», «Origen»,
«Fecha» (un año, una década, un mes, un día o «Entre dos fechas»), y si los sabés, «Evento», «Lugar», «Propietario»
y «Carreras». Tocá «Crear Lote y cargar en él» y soltá todos los archivos juntos. Cada Pieza hereda esos datos;
cuando el Lote ya tiene Piezas, aparece «Contexto común congelado: el Lote ya tiene Piezas y cada una lo heredó.».

- Un Lote admite hasta 100 Piezas. La pantalla avisa cuántas caben; cuando está lleno dice «Este Lote ya tiene 100
  Piezas.» y hay que crear otro.
- La pantalla «Lotes» lista los Lotes («Mis Lotes» o «Todos») con su estado (Vacío, Procesando, En validación,
  Validado), y desde «Abrir» se ve el detalle de uno, con su progreso y «Cargar más archivos».

### Mientras se sube y después

Cada archivo muestra cómo va («Subiendo», «Recibida», «Ya estaba en este Lote»). Si se rechaza, dice por qué y qué
hacer, por ejemplo «Rechazado: ese tipo de archivo no se admite» o, para un Word con macros, «Es un documento de Word
con macros (.docm). Guardalo como .docx, sin macros, y subilo de nuevo.». Si el archivo ya está en el Acervo dice «Ya
existe en el Acervo» con el enlace «Ver la Pieza».

**No esperes mirando la pantalla.** Una foto tarda minutos en leerse. La Pieza aparece en «Mis Piezas», en el grupo
«Procesando», con «● Recibida, procesando»; cuando tiene su Ficha propuesta pasa a «Para validar». La lista se
actualiza sola.

En «Mis Piezas» las Piezas se agrupan en «Para validar», «Procesando», «Fallaron», «Requieren análisis humano» y
«Validadas, no indexadas». Si una falla («● Recibida · falló. No se pudo procesar este archivo.») tenés dos botones:
«Reintentar» o «Completar a mano», que la deja lista para que una persona escriba la Ficha desde cero, con la marca
«Requiere análisis humano». Los videos siempre llevan esa marca: ningún modelo los mira.

Dos franjas informan, sin impedir trabajar:

- «El sistema de lectura automática no está disponible en este momento.»: se puede seguir subiendo y validando a
  mano; lo subido espera al motor y se lee cuando vuelve.
- «Procesamiento detenido: las Piezas nuevas quedan encoladas y se procesan al reanudarlo.»: alguien frenó el
  procesamiento a propósito.

### Cuándo avisar: «el sistema está lento»

Avisale a la persona de contacto que figura en la guía de una página si ves alguna de estas cosas:

- una Pieza más de 30 minutos en «● Recibida, procesando» o en «esperando al motor»;
- la franja «El sistema de lectura automática no está disponible» o la de «Procesamiento detenido», sin que nadie
  haya avisado que se frenó;
- una pregunta en la Conversación que al minuto todavía no muestra «Fuentes».

**Para quien opera:** una Pieza que no avanza es el [runbook R12](runbook-r12-trabajo-colgado.md); si el disco se
llena, el [runbook R11](runbook-r11-disco-lleno.md).

## Validar

Validan el RGC y el Coordinador de Extensión. Validar es firmar: decir «esta Ficha está bien y completa».

1. En «Mis Piezas», grupo «Para validar», tocá «Validar» en una Pieza «○ Pendiente» («Continuar» si quedó
   «◐ Incompleta»; «Analizar» si requiere análisis humano).
2. Se abre la pantalla partida: a la izquierda el original («Original», con «+» y «−» para acercar y alejar, y
   «Ajustar al ancho»), a la derecha la «Ficha propuesta». La barra del medio se arrastra para dar más lugar a uno u otro.
3. Revisá cada campo. Al lado de cada uno se ve de dónde salió: «🤖 Propuesto, revisá antes de guardar», «Heredado del
   Lote, sin cambios», «Del archivo» o «Editado por vos». La marca 🤖 va solo en lo que escribió un
   modelo: el resumen y la descripción de una foto. El texto de un PDF, un Word, una planilla o un `.txt` sale tal
   cual del archivo y se rotula «Texto del archivo»; en un PDF con páginas escaneadas, el texto de esas páginas lo
   leyó un programa de lectura automática (OCR), que no inventa pero puede leer mal alguna letra.
4. Completá lo marcado. Arriba de la Ficha, «⚠ Falta para validar:» lista los botones «Falta: …» que llevan a cada
   campo que falta; cuando no falta nada dice «✓ Está todo. Podés Validar cuando quieras.».
5. Escribí el Contexto en «🖊 Contexto — solo vos lo sabés»: de qué evento es, quién aparece, por qué importa.
6. Tocá «Validar y firmar». Si está todo, aparece «✓ Ficha Validada» con tu nombre y la hora. Si falta algo, la Ficha
   se guarda igual y queda «● Ficha guardada, queda Incompleta», con lo que falta; la retomás cuando consigas el
   dato. «Guardar como está» guarda sin firmar.
7. «Ir a la siguiente» abre la próxima Pieza para validar.

Los campos obligatorios llevan «*»: «Tipo de Pieza», «Tipo de actividad», «Fecha», «Origen», «Propietario» y
«Resumen». La transcripción o la descripción no es obligatoria, pero sin ella nadie va a encontrar la Pieza
buscando. La Fecha acepta solo el año, el mes y el año, o el
día exacto, y un período con «Desde» y «Hasta (opcional)»; debajo se lee cómo quedó («En 1998», «Década de 1960»).

Avisos que pueden aparecer:

- «⚠ Alerta de contenido sospechoso»: el texto tiene frases que parecen órdenes a un asistente. No se bloqueó nada:
  leelas antes de firmar; al firmar, la pantalla pregunta «¿Firmás igual?».
- «Otra persona guardó esta Ficha mientras la editabas»: lo tuyo no se pierde; elegís «Aplicar mis cambios sobre la
  nueva» o «Descartar los míos».
- «Abierta por …»: alguien más la tiene abierta. Es informativo.

**Video:** el original se ve con su reproductor. La descripción de lo que se ve y se oye se escribe con marcas de
tiempo `[HH:MM:SS]`: el botón «Insertar marca de tiempo», debajo del video, o Alt+M escriben el momento del video
donde está el cursor. Si el navegador no puede reproducirlo, descargá el original y completá la Ficha igual.

**Foto:** la descripción llega como una lista, «🤖 Afirmaciones de la imagen», junto a la foto. Tocá «Aceptar» en
cada una que el original muestra y «Rechazar» en las demás; la decisión se guarda en el momento y podés cambiarla
hasta firmar. Mientras falte decidir alguna, «Validar y firmar» queda deshabilitado y debajo dice cuántas faltan;
«Guardar como está» la deja Incompleta. Al firmar, solo las aceptadas entran al cuerpo de la Ficha; «Ver la Ficha
firmada» lo muestra. El cuerpo de la foto es el «Texto visible en la imagen», que leyó una lectura automática (OCR).
Si aparece «La descripción automática falló», escribí vos en el cuerpo lo que se ve en la foto.
Al rechazar una Afirmación, el resumen se rehace sin ella mientras seguís revisando; «Validar y firmar» lo espera.
Cuando llega, si el resumen era el propuesto lo reemplaza; si lo habías editado, no lo toca y te ofrece «Usar el
resumen nuevo». Si el procesamiento está en pausa o el resumen tarda, «No esperar el resumen nuevo» habilita la
firma con el que hay: revisá que no nombre una Afirmación rechazada.

Cada Afirmación llega cruzada con esa lectura. Arriba van las «Sin respaldo en la lectura automática»: lo que dicen
no está en el texto de la imagen, y son las que hay que mirar contra el original. Debajo, las «Respaldadas por la
lectura automática»: al pasar el mouse o el foco por una, su lugar se marca sobre la foto entera y el ítem dice qué
se lee ahí. «Aceptar las N respaldadas» las acepta juntas; las sin respaldo se deciden de a una, y aceptar una que
nombra una sigla, una fecha o un nombre (o personas, en un cartel o una página) pide que lo confirmes.

Con el teclado se valida sin el mouse (ver «Atajos del teclado» en el bloque de las Afirmaciones). Con el foco en una
Afirmación o en sus botones: **A** la acepta, **R** la rechaza, **↓** o **J** pasa a la siguiente y **↑** o **K**
vuelve a la anterior (primero las de la foto, después las de cada imagen embebida de un documento). **Enter**, con el
foco en la Afirmación, firma la Ficha y abre la siguiente Pieza Pendiente del mismo Lote (o de tus Piezas, si no está
en un Lote); si falta algo (decidir una Afirmación, una imagen que se está describiendo, el resumen que se está
rehaciendo), dice qué, y si no queda ninguna Pieza, también. Aceptar
con **A** pide la misma confirmación que el botón, y «Aceptar las N respaldadas» nunca toma una sin respaldo. Mientras
escribís en un campo las teclas escriben. Las teclas no llevan Ctrl ni Alt, que son del navegador. Con un lector de
pantalla en modo exploración, las letras y las flechas son del lector (NVDA y JAWS usan casi todas para saltar entre
títulos, enlaces o botones): ahí decidí con los botones, o pasá al modo foco y los atajos funcionan.

**Planilla:** se ve por hojas, en pestañas. Lo que se muestra es el contenido extraído, no la planilla original;
para verla tal cual, descargala.

**Quitar una Pieza del Acervo** (solo el RGC): «Acciones › Quitar del Acervo…», con un motivo. Deja de aparecer en la
Conversación y en la API de Agentes, pero no se borra nada y se puede restaurar desde Administración › Retiradas.

**Para quien opera:** el día de una sesión de validación con la SEU es el
[runbook R15](runbook-r15-dia-de-la-validacion.md); una Pieza trabada, el [runbook R12](runbook-r12-trabajo-colgado.md).

## Conversar con el Acervo

Todos los roles pueden conversar. Desde la pantalla de entrada, «Conversar».

La pantalla se usa como un chat: tus preguntas a la derecha, las respuestas a la izquierda, todos los turnos uno
debajo del otro, y la caja para escribir fija abajo.

1. Escribí la pregunta en la caja de abajo y tocá «Enviar» (o Enter; Mayús+Enter agrega un renglón). Mientras
   Historia Viva busca se ve «Buscando…»; cuando empieza a redactar, «Escribiendo…», y la respuesta va apareciendo.
   Hasta que termina no se puede escribir otra pregunta.
2. Si querés acotar, abrí «Filtros»: Fecha («Desde», «Hasta»), «Tipo de Pieza», «Tipo de actividad» y «Carrera». Se
   aplican a la próxima pregunta; con alguno marcado, el botón dice cuántos hay.
3. Debajo de cada respuesta están las Fichas encontradas, en un bloque «Fuentes» que nombra las Fichas y se
   despliega al tocarlo. Cada número [1], [2] de la respuesta es una Cita: abre ese bloque en la Pieza y el lugar de la Ficha
   de donde sale el dato, con «Abrir el original» y «Ver la Ficha».
4. «Detener» (o Esc) corta la respuesta. «Nueva conversación» empieza otra; las anteriores quedan en «Historial». En
   el celular, el historial está plegado: tocá «Mis conversaciones» para verlo.

Historia Viva solo responde con Piezas Validadas. Si no encuentra evidencia lo dice: «No encontré evidencia suficiente
en el Acervo.», y no amplía los filtros por su cuenta. Una cita que no se pudo verificar sale tachada, con el aviso
«Una cita de la respuesta no se pudo verificar: no la tomes como respaldo.».

Quien revisa efemérides (RGC y Coordinador de Extensión) ve, bajo una respuesta completa, «Proponer como relato»: la
respuesta pasa a ser un Relato Propuesto, con sus Citas, para revisar en «Efemérides» › «Relatos».

**Para quien opera:** si la Conversación responde «La conversación no está disponible ahora (el índice no
responde).», es el [runbook R13](runbook-r13-reconstruir-el-indice.md).

## Efemérides y Relatos

Una **efeméride** es un texto breve de difusión («Un día como hoy…», «En octubre…») que Historia Viva redacta a partir
de las Fichas Validadas con esa Fecha. Un **Relato** es un texto sobre un hito que no depende de una fecha, y nace de
una respuesta de la Conversación. Ninguno de los dos sale de Historia Viva sin que una persona lo apruebe.

En «Efemérides», las pestañas «Efemérides» y «Relatos». El RGC y el Coordinador de Extensión filtran por estado
(«Propuestas», «Aprobadas», «Descartadas», «Todas»); los demás roles ven solo lo Aprobado.

1. Abrí una «○ Propuesta». Leé el «Texto» y las «Fichas fuente»: verificá que no invente nada y que siga la
   plantilla. Mientras la SEU no entregue la plantilla institucional, se usa una provisoria (la franja lo avisa).
2. Si hace falta, corregí el texto y tocá «Guardar cambios».
3. «Aprobar» la firma a tu nombre y la deja disponible para el Agente 1. «Descartar…» pide un motivo; una efeméride
   descartada no se vuelve a generar sola.

Si una Ficha fuente tiene una alerta de contenido sospechoso, o se retiró del Acervo, la pantalla lo avisa
(«⚠ Alerta en una Ficha fuente», «⚠ Fuente retirada»).

Las efemérides se generan solas según la programación que fija el Administrador técnico; también puede pedirlas en el
momento con «Generar ahora» (Administración › Programación de efemérides).

**Para quien opera:** frenar la generación junto con todo el procesamiento es el
[runbook R7](runbook-r7-frenar-todo.md).

## Informe de acreditación

El Informe de acreditación reúne, para un período, las Piezas Validadas agrupadas por tipo de actividad, con el
resumen de cada Ficha y sus Citas, para CONEAU Global. Ningún modelo lo escribe: se arma con las Fichas validadas.

1. El Coordinador de Extensión, en «Informes», toca «+ Nuevo informe», elige el «Período» («Desde», «Hasta») y, si
   quiere, «Carreras» (sin marcar, todas), y toca «Generar». Queda un «○ Borrador».
2. Al abrirlo se ve la «Integridad»: qué parte de las Piezas incluidas tiene Fecha, tipo de actividad, origen y
   Propietario, sin duplicados y con el contenido entero (avisa si baja de 95 %). También se ven los «Faltantes»: las Piezas del período que no están Validadas, con «Ver la Pieza».
3. Lo aprueban dos personas distintas: el RGC y el Coordinador de Extensión, cada uno con «Aprobar». Con las dos
   aprobaciones queda «✓ Aprobado».
4. Recién entonces se habilitan «Exportar .docx» y «Exportar .xlsx». La primera exportación lo deja «✓ Exportado».

Si algo cambia después de aprobar (por ejemplo, se retiró una Pieza), el informe lo avisa; se genera un Borrador nuevo
con «Generar de nuevo».

**Para quien opera:** los informes exportados del corpus real se borran al terminar el PPS, con el
[runbook R17](runbook-r17-borrar-el-corpus-real.md).

## Qué no subir

**No subas** material disciplinario, datos de salud ni listados con sueldos. Ante la duda, no lo subas y consultá.

Tampoco hace falta subir lo que no es del Acervo de la FIE. Si algo entró por error, el RGC lo quita con «Acciones ›
Quitar del Acervo…»: deja de verse y de buscarse al instante, y se puede restaurar. Quitar no es borrar: el original y
la Ficha se conservan.

Borrar de verdad una Pieza por un pedido de datos personales es la **supresión**, que todavía está apagada (ver
[Lo que todavía está apagado](#lo-que-todavia-esta-apagado)).

**Para quien opera:** la supresión efectiva es el [runbook R14](runbook-r14-supresion-efectiva.md); el tratamiento de
los datos personales está en [Tratamiento de datos personales](tratamiento-de-datos-personales.md).

## Administrar

«Administración» muestra solo las pestañas del rol de cada persona.

### Del Administrador técnico

- **«Modelos»**: qué modelo hace cada tarea automática. «Editar», «Verificar» y «Guardar»; el cambio rige para los
  trabajos nuevos. Cada cambio queda en «Historial de cambios».
- **«Interruptor»**: dos frenos, «Procesamiento de Piezas» y «API de Agentes», cada uno con «Detener» / «Reanudar» y
  un motivo obligatorio. «Frenar todo…» acciona los dos. Frenar el procesamiento también frena la indexación y la
  generación de efemérides; siguen funcionando la conversación, la validación y la carga (lo subido queda encolado).
- **«Tokens»**: los tokens de los Agentes externos. «Emitir token» lo muestra una sola vez («Copialo ahora. No vas a
  poder volver a verlo.»); «Rotar…» y «Revocar…». Emitir un token no enciende la API de Agentes.
- **«Programación de efemérides»**: «Generación automática», «Periodicidad», «Anticipación (días antes)», «Máximo por
  corrida», y «Generar ahora» para un día o un mes.

### Del RGC

- **«Personas»**: «Dar de alta» con email, nombre y rol; «Cambiar rol», «Dar de baja…» (no se borra lo que firmó y sus
  sesiones se cierran) y «Reactivar». Nadie se cambia ni se da de baja a sí mismo, y no se puede dejar sin nadie a un
  permiso.
- **«Retiradas»**: las Piezas quitadas del Acervo, con quién, cuándo y por qué, y el botón «Restaurar».

**Para quien opera:** rotar el token del Agente 1 es el [runbook R5](runbook-r5-rotar-el-token.md); revocar un token,
el [runbook R6](runbook-r6-revocar-un-token.md); cambiar de modelo, el [runbook R8](runbook-r8-cambiar-de-modelo.md);
frenar todo, el [runbook R7](runbook-r7-frenar-todo.md).

## Operación

La operación del sistema se hace con **runbooks**: páginas con Cuándo, Pasos, Verificación y Si falla, para quien
tiene la PC o el servidor. Se numeran «runbook R1» a «runbook R18».

**«Runbook Rn» no es «rebanada Rn».** Una rebanada es una etapa de la construcción del sistema (por ejemplo, la
rebanada R9 es la de documentación y entrega); un runbook es un procedimiento de operación. El runbook R9 (restaurar un
backup) no tiene nada que ver con la rebanada R9. Por eso siempre se escribe «runbook Rn» o «rebanada Rn», nunca «Rn»
a secas.

| Runbook | Para qué |
|---|---|
| [Runbook R1](runbook-r1-arrancar-y-parar.md) | Arrancar y parar |
| [Runbook R2](runbook-r2-migrar.md) | Migrar la base a una versión nueva |
| [Runbook R3](runbook-r3-alta-de-persona.md) | Alta de persona y primer Administrador técnico |
| [Runbook R4](runbook-r4-baja-o-cambio-de-rol.md) | Baja de una persona o cambio de rol |
| [Runbook R5](runbook-r5-rotar-el-token.md) | Rotar el token del Agente 1 |
| [Runbook R6](runbook-r6-revocar-un-token.md) | Revocar un token |
| [Runbook R7](runbook-r7-frenar-todo.md) | Frenar todo |
| [Runbook R8](runbook-r8-cambiar-de-modelo.md) | Cambiar de modelo |
| [Runbook R9](runbook-r9-restaurar-un-backup.md) | Restaurar un backup |
| [Runbook R10](runbook-r10-ollama-sin-memoria.md) | El motor de modelos se queda sin memoria |
| [Runbook R11](runbook-r11-disco-lleno.md) | Disco lleno |
| [Runbook R12](runbook-r12-trabajo-colgado.md) | Trabajo colgado |
| [Runbook R13](runbook-r13-reconstruir-el-indice.md) | Reconstruir el índice |
| [Runbook R14](runbook-r14-supresion-efectiva.md) | Supresión efectiva de una Pieza |
| [Runbook R15](runbook-r15-dia-de-la-validacion.md) | Día de la validación |
| [Runbook R16](runbook-r16-receta-de-servidor.md) | Receta del servidor de la universidad |
| [Runbook R17](runbook-r17-borrar-el-corpus-real.md) | Borrar el corpus semilla real y dejar constancia |
| [Runbook R18](runbook-r18-sesion-presencial.md) | Encender `/v1` para la sesión presencial con el Agente 1 |

La instalación en la PC y el monitoreo mínimo están en [Despliegue en la PC](despliegue-en-la-pc.md).

**Para quien opera:** empezá siempre por el [runbook R1](runbook-r1-arrancar-y-parar.md).

## Agente externo

Un Agente externo es otro sistema del Proyecto Centenario (por ejemplo, el Agente 1) que usa Historia Viva por la
**API de Agentes** (`/v1`). Se lo trata como a una persona más con reglas fijas:

- Lee solo Piezas Validadas, y solo las efemérides y los Relatos Aprobados.
- Lo que envía (un **Aporte**) entra como Pieza Pendiente, con «Aporte de Agente» en «Mis Piezas» y «⚠ Texto que envió
  un Agente, no una persona» en la Ficha. Una persona lo valida como cualquier otra Pieza; hasta entonces no se busca
  ni se cita.
- Usa un **Token de Agente** propio, que emite el Administrador técnico en Administración › Tokens, con los alcances
  justos: «Buscar fragmentos (fragmentos:leer)», «Leer Piezas (piezas:leer)», «Enviar aportes (aportes:escribir)» y
  «Leer efemérides y relatos (efemerides:leer)».
- La API de Agentes arranca apagada. La enciende a mano el Administrador técnico, en Administración › Interruptor, y
  se apaga al terminar.

### SIU, SISESCA y CONEAU Global

En esta versión Historia Viva **no se conecta** con SIU, SISESCA ni CONEAU Global. Si la FIE decide conectarlos, el
camino es la misma API de Agentes, sin tocar Historia Viva:

1. Un **conector** chico, que corre fuera de Historia Viva, lee el sistema de origen (SIU o SISESCA) con el permiso
   que dé la FIE.
2. El Administrador técnico le emite un Token de Agente propio, solo con «Enviar aportes (aportes:escribir)».
3. El conector envía cada registro como un Aporte (`POST /v1/aportes`), con una clave de idempotencia por registro
   para que reenviarlo no lo duplique.
4. Cada Aporte entra Pendiente y lo valida una persona de la SEU, como cualquier Pieza. Nada se publica solo.

Para CONEAU Global no hace falta conector: el Informe de acreditación se exporta a `.docx` y `.xlsx` y se carga en
CONEAU Global a mano. Mientras tanto, lo que hoy vive en planillas sueltas se carga como Planilla o documento.

**Para quien opera:** encender `/v1` para una sesión con el Agente 1 es el
[runbook R18](runbook-r18-sesion-presencial.md); rotar su token, el [runbook R5](runbook-r5-rotar-el-token.md);
revocarlo, el [runbook R6](runbook-r6-revocar-un-token.md).

## Lo que todavía está apagado

Estas funciones están construidas y probadas, pero **deshabilitadas** hasta que la SEU, la FIE o IT respondan la
consulta que las habilita. Cuando se habilite una, el ticket que la activa actualiza este capítulo y el capítulo donde
se usa.

| Función | Hasta | Qué se ve hoy |
|---|---|---|
| Aprobación en bloque | C29: que la SEU acepte firmar un Lote sin abrir cada Pieza | En el detalle del Lote, «Aprobación en bloque» dice «Deshabilitada hasta que la SEU acepte firmar sin abrir cada Pieza (C29). Mientras tanto, validá de a una.» |
| Supresión de una Pieza y casilla «Publicable para el Agente 1» | C31: que la SEU y la asesoría jurídica decidan sobre los datos personales | La casilla no aparece en la Ficha y la supresión no se puede correr. Para sacar una Pieza, «Quitar del Acervo…» |
| Réplica de los originales en Drive | C14 y C34: las credenciales y la unidad compartida que entrega IT | Nada: los originales quedan solo en el disco del sistema, con sus backups |

**Para quien opera:** la supresión, cuando se habilite, es el [runbook R14](runbook-r14-supresion-efectiva.md).

## Accesibilidad

El objetivo es que **la interfaz** de Historia Viva cumpla WCAG 2.1 AA: se usa entera con teclado, con foco visible,
con lector de pantalla, con zoom al 200 % y en una pantalla angosta. Cada pantalla se revisa con una herramienta
automática (axe-core) y los estados que cambian solos, a mano.

**Contenido archivístico.** Las fotos, los escaneos y los videos de época del Acervo quedan fuera de esa exigencia:
no se pueden rehacer. Su alternativa textual es la **descripción de la Ficha** (la transcripción, la descripción de
la imagen o la descripción de lo que se ve y se oye en un video). Por eso una Ficha sin descripción, además de no
poder encontrarse, deja a la Pieza sin alternativa para quien no la ve.

Algunas ayudas de la pantalla:

- Los estados nunca dependen solo del color: llevan un signo y una palabra («○ Pendiente», «✓ Validada»,
  «⚠ Alerta»).
- Los avisos que aparecen solos (la lista que se actualiza, la respuesta de la Conversación, los errores) se anuncian
  al lector de pantalla.
- Atajos: Enter envía la pregunta en la Conversación (Mayús+Enter agrega un renglón; Esc detiene la respuesta); en
  la pantalla partida, las flechas ← y → mueven la barra del medio; en las Afirmaciones de una foto, A acepta, R
  rechaza, ↓ y ↑ mueven y Enter firma (ver «Atajos del teclado»); con un video abierto, Alt+M inserta la marca de
  tiempo; Esc cierra los diálogos sin hacer nada (el del token nuevo, recién después de marcar «Ya lo guardé»).
- Los diálogos que firman o quitan algo arrancan con el foco en «Cancelar» o «Volver a revisar».

Si algo no se puede usar con teclado o con lector de pantalla, avisale al Administrador técnico: es un error y se
corrige.

**Para quien opera:** antes de una sesión con la SEU, el [runbook R15](runbook-r15-dia-de-la-validacion.md) prepara
la PC; si alguien usa lector de pantalla o zoom, probalo en esa preparación.
