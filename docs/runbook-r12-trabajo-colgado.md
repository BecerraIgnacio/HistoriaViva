# Runbook R12: trabajo colgado

Comandos de PowerShell, en la raíz del repositorio. `dc` abrevia `docker compose --env-file D:\hv\.env`, y `sql`
abrevia `dc exec -T postgres psql -U historia -d historia_viva -c` (solo para mirar).

## Cuándo

Una Pieza lleva mucho más que lo normal sin pasar a Pendiente (una foto tarda 135 a 170 s;
un video o una planilla grande, más), o una Validada no termina de indexarse, y en la cola hay un
trabajo `en_curso` que no avanza:

```powershell
sql "SELECT trabajo_id, pieza_id, tipo, generaciones, tope_generaciones, tomado_por,
            iniciado_el, arriendo_vence_el
     FROM acervo.trabajo_procesamiento WHERE estado = 'en_curso' ORDER BY iniciado_el;"
```

Cada toma deja un arriendo (`arriendo_vence_el` = tope de tiempo del trabajo más un margen). Si
el trabajador muere a mitad, la próxima toma de un trabajador del mismo tipo lo recupera sola
(historia 232): vuelve a `encolado` sin gastar otra generación, o queda `fallido` si ya gastó las
dos.

## Pasos

1. **Esperar al arriendo.** Si `arriendo_vence_el` todavía no pasó, el trabajo puede estar
   corriendo: no se toca. Mirar `dc logs --tail 50 trabajador-procesamiento` (o
   `trabajador-indexacion`) por el `trabajo_id`.
2. **Que haya un trabajador vivo de ese tipo.** El arriendo vencido lo recupera la toma
   siguiente: con el trabajador caído no lo recupera nadie. En `/api/salud`,
   `trabajadores.<tipo>.vivo`; si está en `false`, `dc up -d trabajador-procesamiento` (o el que
   sea) y esperar una vuelta (unos segundos).
3. **Si no se retoma solo, devolverlo a `encolado` con `hv_migrador`**, solo con el arriendo ya
   vencido y generaciones disponibles:

   ```powershell
   dc exec -T postgres psql -U hv_migrador -d historia_viva -c "
   UPDATE acervo.trabajo_procesamiento
      SET estado = 'encolado', tomado_por = NULL, arriendo_vence_el = NULL
    WHERE trabajo_id = '<trabajo_id>' AND estado = 'en_curso'
      AND arriendo_vence_el < now() AND generaciones < tope_generaciones;"
   ```

   Tiene que decir `UPDATE 1`. Anotarlo en la bitácora con el `trabajo_id`.
4. **Si se repite con la misma Pieza, marcarlo fallido y anotar:**

   ```powershell
   dc exec -T postgres psql -U hv_migrador -d historia_viva -c "
   UPDATE acervo.trabajo_procesamiento
      SET estado = 'fallido', terminado_el = now(),
          error = 'Marcado fallido a mano (runbook R12): se cuelga al procesarse'
    WHERE trabajo_id = '<trabajo_id>'
      AND (estado = 'encolado' OR (estado = 'en_curso' AND arriendo_vence_el < now()));"
   ```

   Un `en_curso` con el arriendo vigente no se toca: puede estar corriendo.

   La Pieza queda Recibida con ese error a la vista. Se revisa el original (¿un PDF de miles de
   páginas, un video roto?) antes de reprocesarla.
5. **Reprocesar.** `dc run --rm web reprocesar --fallidos` (o `--lote <ID>`, o
   `--tipo indexacion`). **Tope de 6 reprocesos por Pieza, para todos (DEC-137):** también para
   el Administrador técnico y para este comando. Las Piezas que ya lo alcanzaron se saltean y se
   listan. Superarlo exige `--forzar`, que queda auditado (`pieza.reproceso` con la marca de
   forzado y quién lo pidió):

   ```powershell
   dc run --rm web reprocesar --fallidos --lote <ID> --forzar
   ```

   Se fuerza solo con el motivo anotado (qué se cambió para que esta vez no falle).

## Verificación

- La consulta de arriba ya no muestra el trabajo; la Pieza llegó a Pendiente (o la Validada
  tiene fragmentos: `/api/salud` → `indice.pendientes` en 0).
- Nadie sumó una generación de más: `generaciones` ≤ `tope_generaciones` (la base lo exige con un
  `CHECK`).
- Un `--forzar` aparece en la auditoría: `sql "SELECT cuando, actor_etiqueta, objeto_id, motivo FROM acervo.auditoria WHERE accion = 'pieza.reproceso' AND despues->>'forzar' = 'true' ORDER BY cuando DESC LIMIT 5;"`.

## Si falla

- **`UPDATE 0`:** el trabajo ya no está `en_curso`, el arriendo no venció o no le quedan
  generaciones (entonces la toma siguiente lo deja `fallido`: paso 5).
- **Se cuelgan muchos a la vez:** el motor no responde o no tiene memoria: runbook R10.
- **Con `--forzar` vuelve a fallar:** no se insiste; se anota y la Pieza se carga a mano o se
  retira.
