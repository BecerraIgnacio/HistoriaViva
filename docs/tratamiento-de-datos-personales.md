# Historia Viva: tratamiento de datos personales del Acervo

Insumo para la asesoría jurídica de la FIE (Ley 25.326). **No es asesoramiento legal**: ordena el problema para que
lo decidan quienes corresponden. La base legal, la inscripción y el tratamiento de datos sensibles los confirma la
asesoría jurídica.

## 1. Finalidad

Preservar y consultar la memoria institucional de la FIE: documentos, fotografías, planillas y videos de actividades
de extensión y de las Carreras se cargan como Piezas, una persona de la SEU valida su Ficha y el Acervo validado se
consulta conversando, se usa para efemérides, relatos e informes de acreditación, y se entrega al Agente 1 por la
API `/v1`. No se usa para evaluar, sancionar ni perfilar personas.

## 2. Datos personales por clase de Pieza

| Clase | Qué datos personales puede traer | Riesgo |
|---|---|---|
| Fotografía (`imagen`) | Rostros; la descripción que propone el modelo puede nombrar o inferir personas | Derecho a la imagen. Identificar personas en imágenes queda fuera del MVP (D2) |
| Documento | Nombres, DNI, domicilios; a veces sanciones, salud o afiliación (posibles datos sensibles, art. 7) | El texto completo entra a la Ficha y al índice de búsqueda |
| Planilla | Listas de personal, legajos, DNI, sueldos, fechas de nacimiento | Concentración: una planilla reúne cientos de personas y se indexa entera |
| Video | Imagen y voz. No pasa por modelos: lo describe una persona | Datos biométricos potenciales; copias en los backups |
| Aporte (texto del Agente 1) | Lo que escriba el Agente 1 | Entra Pendiente y una persona lo valida; una vez Validado vuelve por `/v1` |
| Conversaciones | Las preguntas libres pueden contener nombres u otros datos | Se guardan por persona y solo las ve su dueña; su contenido no va a logs ni a la auditoría |
| Efemérides, relatos e informes | Derivan de Fichas Validadas y pueden repetir nombres | Lo Aprobado sale por `/v1` y el Agente 1 lo difunde: la revisión humana es el filtro |
| Metadatos y logs | Correo de quienes operan, identificador de la persona | Se minimizan: los logs no llevan contenido |

**Política de admisión** (figura en la guía de una página y en la pantalla de carga): no se sube material
disciplinario, datos de salud ni listados con sueldos, hasta que la SEU y la asesoría fijen un criterio (Q9).

## 3. Accesos por rol

Se entra con la cuenta institucional de Google. Los roles controlan qué se puede hacer, no qué se ve: **todo rol que
consulta ve todas las Piezas Validadas** y la Carrera es un filtro de búsqueda, no un permiso (DEC-125).

| Rol | Qué puede hacer |
|---|---|
| Responsable de Gestión del Conocimiento (RGC) | Cargar, validar, consultar, gestionar personas, retirar Piezas, revisar efemérides y relatos, aprobar informes |
| Coordinador de Extensión | Cargar, validar, consultar, revisar efemérides y relatos, generar y aprobar informes |
| Director de Carrera | Solo consultar: Piezas Validadas, efemérides y relatos Aprobados, informes Aprobados o Exportados |
| Administrador técnico | Consultar y operar el sistema (modelos, interruptor, tokens, programación). Ve también las Piezas no Validadas, sin poder validarlas. Nunca valida. Gestiona personas y retira Piezas solo en forma interina, hasta que la SEU nombre al RGC, con cada acción auditada |
| Agente 1 (sistema externo) | Con un token, lee por `/v1` las Fichas Validadas y lo Aprobado, y deja Aportes que entran Pendientes |

Quien solo consulta (el Director de Carrera) no ve Piezas sin validar; el Administrador técnico sí las ve.

## 4. Medidas presentes

- Autenticación institucional; cada operación exige su permiso.
- La aplicación se conecta a la base con un usuario sin permiso de borrar (`hv_app` sin DELETE), salvo el índice de
  búsqueda, que es derivado y se reconstruye; conversaciones y citas son de solo agregado.
- El motor de modelos es local y no expone puertos: el contenido no sale a servicios externos de IA.
- Nada publicado a internet: el sistema corre en una PC y toda interacción con la SEU es presencial (DEC-119).
- Trazabilidad: cada Ficha queda firmada por quien la validó, con su historial de versiones.
- Logs sin contenido.
- Previsto al desplegar: backups cifrados (restic y disco externo con BitLocker) fuera del OneDrive, y la PC con
  BitLocker.
- Corpus semilla real limitado (sección 7).

## 5. Procedimiento de derechos (acceso, rectificación, supresión)

1. El pedido llega a la persona que designe la SEU (pendiente: sección 6), que lo registra con fecha.
2. **Acceso:** se buscan las Piezas que mencionan a la persona titular conversando con el Acervo y en la lista de
   Piezas; se le informa qué hay y dónde.
3. **Rectificación:** una persona con permiso de validar corrige la Ficha; la versión anterior queda en el historial.
4. **Retiro:** quien tiene permiso de retirar (la RGC, o el Administrador técnico durante el interinato) quita la Pieza del Acervo con motivo: sale de la búsqueda y de `/v1`, pero el original y las Fichas
   quedan y se puede restaurar. El retiro **no es supresión**.
5. **No publicar:** el RGC destilda «Publicable para el Agente 1» en Validar. Queda una versión nueva de la Ficha y
   la Pieza sale de `/v1` y de las efemérides y Relatos nuevos; las personas la siguen viendo y buscando.
6. **Supresión efectiva:** con el comando `suprimir`, que corre `hv_migrador` (el único rol que puede borrar), se
   borran la Pieza, todas las versiones de su Ficha, sus fragmentos del índice y su original, después de un volcado
   previo; queda un registro **sin contenido ni nombre** (fecha, quién, SHA-256 del original y la referencia del
   pedido) y se avisa al responsable del Agente 1. Lo que la citaba pierde la cita, no el texto: las efemérides y los
   Relatos quedan con «fuente retirada». El paso a paso es el runbook R14 (`runbook-r14-supresion-efectiva.md`).
   Lo suprimido sigue en los backups hasta que vencen, unos tres meses (R-DP2).

**Al validar**, la pantalla avisa si lo guardado parece tener un DNI, un CUIL o un CBU. El aviso no bloquea ni se
guarda: es para que quien valida decida si el dato tiene que quedar.

`publicable`, la supresión y el aviso están construidos y probados (tickets 123 y 125) y **apagados hasta C31**
(`HV_DATOS_PERSONALES_HABILITADOS`): los enciende la decisión de la SEU y de la asesoría jurídica (ticket 128).

## 6. Qué decide cada parte

| Decide la SEU | Decide el docente de seguridad (C7) | Decide la asesoría jurídica o responsable de la base |
|---|---|---|
| La finalidad y la política de admisión; quién atiende los pedidos de acceso o supresión; si se aceptan planillas con personal y videos con personas | Si las medidas técnicas alcanzan (art. 9); si se habilita identificar personas en imágenes (D2) | La base legal y el consentimiento de personas vivas; la inscripción ante la AAIP; el aviso a titulares; el tratamiento de datos de personas fallecidas |

Fuera del MVP: control de acceso por Pieza y registro de lecturas de Fichas. La inscripción ante la AAIP queda como
deuda a trasladar a la institución.

## 7. Corpus semilla real

De 30 a 50 Piezas reales, **sin datos personales**, en la PC del alumno, solo con el aval escrito del Director y de
la SEU (C49) y con los backups y la restauración probados. Se declara desvío de soberanía: la PC no es
infraestructura de la facultad (DEC-120, DEC-100). Al terminar el PPS se borran la base, los originales, las
conversaciones, los volcados y todos los backups, y queda una constancia firmada (runbook R17). Hasta tener el aval,
solo se usa corpus de prueba.

## 8. Riesgos declarados

| Riesgo | Descripción |
|---|---|
| R-DP1 | Sin control de acceso por Pieza: todo rol que consulta ve todo lo Validado (DEC-125, interpretación de DIR-35 a avalar por el Director, C43) |
| R-DP2 | Los backups conservan lo suprimido hasta que vencen |
| R-DP3 | El Agente 1 puede usar un modelo de lenguaje externo con lo que lee por `/v1` (Q7) |
| R-DP4 | Una PC personal o corporativa es la sede de datos reales |
| R-DP5 | El corpus real está en la PC hasta el borrado final (runbook R17) |
