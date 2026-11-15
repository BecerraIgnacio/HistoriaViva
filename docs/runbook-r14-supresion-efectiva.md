# Runbook R14: supresión efectiva de una Pieza

Borra de verdad una Pieza por un pedido de datos personales (Ley 25.326; historia 243, RF-42). **No es el retiro.**
El retiro (pantalla de la Pieza, «Acciones › Quitar del Acervo…») la saca de todo y conserva todo: se puede
restaurar. La supresión borra la Pieza, todas las versiones de su Ficha, sus fragmentos del índice y su original (con el JPEG derivado de una foto HEIC, 153), y
deja un registro **sin contenido ni nombre**. No se deshace.

Entra construida y probada, y **apagada hasta C31**: solo funciona con `HV_DATOS_PERSONALES_HABILITADOS=true`. La
corre `hv_migrador`, el único rol que puede borrar; la aplicación (`hv_app`) no puede.

## Cuándo

- La SEU o la asesoría jurídica de la FIE piden suprimir una Pieza (un titular ejerció su derecho de supresión, o la
  Pieza nunca debió entrar). El pedido llega por escrito y tiene una referencia (número de nota o de expediente).
- No para un error de carga ni para sacarla de la búsqueda mientras se decide: eso es el retiro, o destildar
  «Publicable para el Agente 1» desde Validar si solo no tiene que salir por la API de Agentes.

## Pasos

Los comandos son de la PC, en la raíz del repositorio, con el `.env` de siempre (`D:\hv\.env`). `<HV_ORIGINALES>` es la
carpeta de originales de ese `.env`.

1. **Identificar la Pieza.** El `pieza_id` está en la dirección de Validar (`validar.html?pieza=<pieza_id>`) o en la
   lista de Piezas. Anotarlo con la referencia del pedido.
2. **Retirarla.** Desde la pantalla, «Acciones › Quitar del Acervo…», con la referencia del pedido como motivo. Deja de
   verse y de buscarse al instante, sale de `/v1` y se cancelan sus trabajos (si tuviera uno en curso, la supresión
   no arranca).
3. **Volcado previo.** `uv run python -m historia_viva.respaldo --entorno D:\hv\.env respaldar --motivo manual`
   (anotar en el registro que es el previo a la supresión <referencia>). Sale con 0. Ese snapshot conserva la Pieza hasta que venza (paso 9).
4. **Previa.** No borra nada: dice qué borraría.

   ```
   docker compose --env-file D:\hv\.env run --rm -e HV_DATOS_PERSONALES_HABILITADOS=true ^
     -e HV_ORIGINALES=/data/originales -v "<HV_ORIGINALES>:/data/originales" ^
     migrar suprimir <pieza_id> --motivo "<referencia>" --previa
   ```

   Responde `Previa, no se borró nada. Se borrarían: …` con las filas por tabla. Si dice `Otras Piezas con el mismo
   original`, el pedido seguramente las alcanza también: anotar sus `pieza_id`.
5. **Suprimir.** El mismo comando sin `--previa`. Responde `Pieza <pieza_id> suprimida el …`, el SHA-256 del original,
   `Original borrado.` y que falta avisar a Juan. El motivo es la referencia del pedido: el comando rechaza un motivo
   con un DNI, un CUIL o un CBU, y no se escribe el nombre de quien pidió.
6. **Las otras con el mismo original**, si las hubo y el pedido las alcanza: pasos 2, 4 y 5 con cada una.
7. **Drive.** Si respondió `Tenía réplica en Drive: borrá a mano el archivo <id>`, borrar ese archivo en la carpeta de
   Drive y vaciarlo de la papelera (la réplica llega con 124 y 126).
8. **Avisar a Juan** (responsable del Agente 1): la fecha y el `pza_<pieza_id>`, sin contenido, para que lo borre de
   lo que haya guardado. Avisar a la SEU que quedó hecho, con la fecha.
9. **Backups.** No se tocan a mano: los snapshots viejos vencen por la retención de `respaldar` (`--keep-monthly 3`,
   sección 6 de `despliegue-en-la-pc.md`). Hasta entonces lo suprimido sigue en ellos (**R-DP2**, declarado en
   `tratamiento-de-datos-personales.md`).

Lo que citaba la Pieza pierde la cita, no el texto: las efemérides y los Relatos no descartados quedan con «fuente
retirada» (dejan de salir por `/v1`); los Informes, las Conversaciones y los Aportes que la usaban como fuente pierden
esa fila. Si alguno de esos textos repite el dato personal, se descarta o se corrige desde su pantalla.

## Verificación

- La previa del paso 4, otra vez: responde `No hay una Pieza <pieza_id>: no se borró nada.` y sale con 1.
- El registro, en la base: `SELECT cuando, actor_etiqueta, despues->>'huella_original', motivo FROM
  acervo.auditoria WHERE accion = 'pieza.supresion' ORDER BY cuando DESC LIMIT 1;` da la fecha, quién, el SHA-256 y
  la referencia; nada del contenido ni el nombre del archivo.
- `<HV_ORIGINALES>\<pieza_id>` no existe.
- La búsqueda de las personas y `/v1/fragmentos` no la devuelven.

## Si falla

| Mensaje | Qué pasó | Qué hacer |
|---|---|---|
| `La supresión se habilita cuando … (C31)` (sale con 2) | Falta `-e HV_DATOS_PERSONALES_HABILITADOS=true` o C31 no se decidió | Revisar el comando; sin la decisión de C31 no se suprime |
| `El motivo no lleva DNI, CUIL ni CBU` (sale con 2) | El motivo trae un número personal | Usar la referencia del pedido |
| `La Pieza tiene un trabajo en curso` (sale con 1) | Se saltó el paso 2 | Retirarla y repetir desde el paso 4 |
| `No se pudo borrar el original …` (sale con 1) | La supresión ya quedó registrada; el archivo no se pudo borrar | Borrar a mano `<HV_ORIGINALES>\<pieza_id>` y, si existe, `<HV_ORIGINALES>\.convertidos\<pieza_id>`, y verificar |
| Error de la base | Todo va en una transacción: no se borró nada | Repetir cuando la base responda |

Si alguna vez se **restaura un backup anterior** a una supresión (runbook R9), la Pieza vuelve: después de restaurar,
repetir el paso 5 por cada evento `pieza.supresion` posterior a la fecha del backup.

## Ensayo

| Fecha | Sobre | Cómo | Resultado |
|---|---|---|---|
| 19-oct-2026 | Corpus de prueba (base de pruebas, Pieza Validada e indexada con DNI, CUIL y CBU en el cuerpo, tres versiones de Ficha y réplica en Drive simulada) | `test_datos_personales::test_la_supresion_borra_pieza_fichas_fragmentos_y_original_y_registra_sin_contenido`: el mismo comando `suprimir`, en proceso, como `hv_migrador` | Previa sin cambios; motivo con DNI rechazado; supresión: 0 filas de la Pieza en toda tabla con `pieza_id`, 0 fragmentos, 0 bytes del original, un evento `pieza.supresion` con SHA-256 y motivo, sin contenido; aviso de Drive y de Juan; repetirla sale con 1 |

Sin ensayar todavía: el paso 3 (volcado previo con restic) y el comando por `docker compose run`, que necesitan la PC
con Docker y restic (se ensayan con 128).
