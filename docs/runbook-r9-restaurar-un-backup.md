# Runbook R9: restaurar un backup

Comandos de PowerShell, en la raíz del repositorio. `dc` abrevia `docker compose --env-file D:\hv\.env`. Las
rutas de ejemplo (`D:\hv\volcados`, `E:\hv-restauracion`) son las del `.env` de la PC
(`HV_VOLCADOS`, `HV_ORIGINALES`, `RESTIC_REPOSITORY`), siempre fuera del OneDrive (DEC-144).

## Cuándo

- La base no arranca, quedó corrupta o perdió datos; un original falta o cambió de hash
  (`verificar-originales`); se borró algo por error.
- Como prueba: antes de cargar el corpus semilla real y antes de la Validación SEU nº 2 (spec
  §2.3, historia 230). La prueba es solo el paso 2.

## Pasos

1. **Volcado del estado actual, aunque esté roto.** Siempre primero: si la restauración sale
   mal, se vuelve a esto.

   ```powershell
   uv run python -m historia_viva.respaldo --entorno D:\hv\.env respaldar --motivo manual
   ```

   Si `pg_dump` falla porque la base no arranca, copiar el volumen tal como está, con `postgres`
   detenido y la imagen que ya está en la PC:

   ```powershell
   dc stop postgres
   New-Item -ItemType Directory -Force D:\hv\rotos
   dc run --rm --no-deps -v D:\hv\rotos:/copia --entrypoint tar postgres `
     czf /copia/pgdata-$(Get-Date -Format yyyyMMddTHHmm).tgz -C /var/lib/postgresql/data .
   ```

2. **Restaurar en el proyecto aparte** `historiaviva-restauracion` (otros volúmenes, la base en
   `127.0.0.1:54330`, la web en `127.0.0.1:8081`, sin Caddy). El script restaura el último
   snapshot de restic, verifica los seis criterios y deja un informe:

   ```powershell
   uv run python -m historia_viva.respaldo.restauracion restaurar --entorno D:\hv\.env `
     --destino E:\hv-restauracion --quien "Ignacio Becerra"
   ```

   `--destino` es una carpeta vacía, con ruta absoluta y fuera del OneDrive. Usa los modelos del
   origen (`hvmvp_modelos`, de solo lectura) para embeber las 3 consultas de referencia: si el
   volumen no existe (otra PC), antes `dc up -d ollama` y `dc run --rm modelos`.
   Sale con 0 si pasan los seis criterios. Si solo era la prueba, se anota y se termina acá.
3. **Restaurar en la PC.** Con el informe del paso 2 en `PASA`:
   1. Parar la app: `dc stop trabajador-procesamiento trabajador-indexacion trabajador-efemerides web caddy`.
   2. Solo si `postgres` no arranca y el paso 1 dejó su copia: `dc rm -sf postgres` y
      `docker volume rm hvmvp_pgdata`. Es la única excepción al runbook R1 fuera del R17.
   3. `dc up -d --wait postgres`.
   4. Los globales y la base, del volcado que restauró el paso 2 (`<nombre>` es el del
      informe, sin `.dump`):

      ```powershell
      $volcados = "E:\hv-restauracion\snapshot\D\hv\volcados"   # HV_VOLCADOS dentro del snapshot
      $postgres = docker compose --env-file D:\hv\.env ps -q postgres
      docker cp "$volcados\<nombre>.globales.sql" "${postgres}:/tmp/globales.sql"
      docker cp "$volcados\<nombre>.dump" "${postgres}:/tmp/base.dump"
      dc exec -T postgres psql -X -q -U historia -d historia_viva -f /tmp/globales.sql
      dc exec -T postgres pg_restore --clean --if-exists -U historia -d historia_viva /tmp/base.dump
      ```

      El binario va con `docker cp`: la tubería de PowerShell 5.1 lo reescribe como texto. Los
      errores de `psql` por roles que ya existen son esperables (el bootstrap los creó). Si el
      paso 3.2 recreó el volumen, `pg_restore` va **sin** `--clean --if-exists`: la base nueva no
      tiene `acervo` y la limpieza de `v_lote_resumen` falla sin el schema (lo mismo hace el
      script del paso 2).
   5. Los originales que falten, sin pisar los que están (robocopy sale con 1 cuando copió algo):
      `robocopy E:\hv-restauracion\snapshot\D\hv\originales D:\hv\originales /E /XC /XN /XO`.
   6. `dc run --rm migrar` y `dc up -d`.
4. **Bajar el proyecto aparte:** `docker compose --env-file D:\hv\.env -f compose.yml -f compose.restauracion.yml --project-name historiaviva-restauracion down -v`
   (sus volúmenes son `hvrestauracion_*`; `down -v` no toca los de la PC).

## Verificación

Los criterios de la restauración probada (spec §2.3), los mismos que verifica el paso 2. En la
PC, después del paso 3:

| Criterio | Cómo | Esperado |
|---|---|---|
| Conteo de Piezas por estado y de Fichas por versión | `uv run python -c "from historia_viva.respaldo import sql_de_referencia; print(sql_de_referencia())" \| dc exec -T postgres psql -X -A -t -U historia -d historia_viva` | Igual al `conteos` del `<nombre>.referencia.json` |
| `verificar-originales` | `dc exec -T web python -m historia_viva.respaldo.restauracion verificar-originales` | Sale con 0: 0 faltantes; los huérfanos se explican |
| 20 Piezas al azar | Las del informe del paso 2; en la PC, abrir 3 en la pantalla partida | Todas abren |
| 3 consultas de referencia, por la búsqueda | `dc run --rm -v "${volcados}:/volcados:ro" web consultas-de-referencia --comparar /volcados/<nombre>.referencia.json` | «Las 3 consultas de referencia dan lo esperado.» |
| Roles de base | `dc exec -T postgres psql -U historia -d historia_viva -c "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'acervo' AND c.relkind IN ('r','p') AND has_table_privilege('hv_app', c.oid, 'DELETE')"` | Solo `data_fragmento` (el índice, DEC-143) |
| Tiempo total | Desde el paso 1 | Se anota; si pasa de 4 h se redefine el RTO |

Si el `.referencia.json` dice `"estable": false` o trae `error`, conteos y consultas no se
comparan: se anota «no comparable». `/api/salud` en `http://localhost` vuelve a `"estado": "ok"`.

| Fecha | Quién | Volcado | Tiempo total | Resultado | Desvíos |
|---|---|---|---|---|---|
| | | | | | |

## Si falla

- **Un paso del script** (sale con 1 y el informe dice «Paso de la restauración»): se corrige lo
  que nombra (Docker, restic, `POSTGRES_PASSWORD`) y se repite el paso 2.
- **Faltantes en `verificar-originales`:** buscarlos en un snapshot anterior
  (`restic snapshots`, `restic restore <id> --target ... --include <ruta>`) o en el USB.
- **Las consultas no dan lo mismo:** si el índice vino incompleto, runbook R13 y volver a
  comparar. Si la búsqueda no responde, `ollama-embeddings` o su modelo (`dc logs ollama-embeddings`).
- **El último snapshot no sirve:** restaurar uno anterior a mano (`restic restore <id>`) y
  repetir desde el paso 3 con sus volcados.
- **Todo falla:** queda la copia del paso 1. No borrar nada más y avisar al Director.
