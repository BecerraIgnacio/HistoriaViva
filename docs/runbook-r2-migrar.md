# Runbook R2: migrar

Comandos de PowerShell, en la raíz del repositorio, con la pila arriba (runbook R1). `dc` y `sql` como en el
runbook R1:

```powershell
function dc { docker compose --env-file D:\hv\.env @args }
function sql { dc exec -T postgres psql -U historia -d historia_viva -c @args }
```

«Runbook R2» es de operación; no es la rebanada R2 de construcción (R-37).

## Cuándo

Hay código nuevo en `main` (un ticket integrado), con o sin migraciones nuevas en
`src/historia_viva/migraciones/sql/`. Nunca durante la carga de un Lote ni en una sesión con la
SEU: se hace con la cola tranquila.

## Pasos

1. **Parar los trabajadores** (terminan el trabajo en curso, runbook R1 paso 4). `web` queda
   arriba: el respaldo del paso 4 corre las consultas de referencia en `web`.

   ```powershell
   dc stop trabajador-procesamiento trabajador-indexacion trabajador-efemerides
   ```

2. **Traer el código:** `git pull` (en la raíz del repositorio). `git status` sin cambios propios.
3. **Construir la imagen:** `dc build`.
4. **Respaldar y migrar en un solo comando** (el volcado previo es obligatorio: si el respaldo
   falla, no migra):

   ```powershell
   uv run python -m historia_viva.respaldo --entorno D:\hv\.env migrar
   ```

   Hace un volcado con motivo `antes-de-migrar` y su snapshot de restic y recién entonces
   `docker compose run --rm migrar` con la imagen nueva. Cada migración se aplica en su propia
   transacción y como `hv_migrador`.
5. **Levantar todo con la imagen nueva:** `dc up -d` (recrea `web` y los trabajadores).

## Verificación

- El paso 4 sale con 0 y lista las migraciones aplicadas (`  0038_supresion_efectiva`, …) y
  «Base al día: N migración(es) aplicada(s).».
- Repetido, no aplica nada: `dc run --rm migrar` → «Base al día: 0 migración(es) aplicada(s).».
- `sql "SELECT version, nombre, aplicada_el FROM acervo.schema_migracion ORDER BY version DESC LIMIT 3;"`:
  la última es la del código nuevo.
- `uv run python -m historia_viva.respaldo --entorno D:\hv\.env estado` sale con 0: el snapshot
  `antes-de-migrar` es el último.
- La verificación del runbook R1 (todos `running`, `/api/salud` en `ok`).

## Si falla

- **«Falló: …» en el paso 4 antes de migrar** (sale con 1): el respaldo no se hizo y la base no
  se tocó. Ver `respaldo … estado` (restic, `HV_VOLCADOS`, disco: runbook R11), corregir y repetir
  el paso 4. Si falló la poda después de un snapshot bueno, también sale con 1 sin migrar: el
  snapshot está; corregir restic y repetir.
- **Sale con 2:** el `.env` o `HV_ORIGINALES` están dentro del OneDrive o falta una variable: el
  mensaje dice cuál.
- **`migrar` rechaza** («cambió el checksum de una migración ya aplicada» o «su número es menor
  que la última aplicada»): el código está mal integrado, no la base. No se edita
  `schema_migracion`: volver al commit anterior (`git checkout <commit>`, `dc build`, `dc up -d`)
  y avisar.
- **`migrar` rechaza con «la base tiene migraciones que este código no trae»:** la base es más
  nueva que el código (un respaldo restaurado de una versión posterior, o un `git checkout` a un
  tag anterior). Volver al commit anterior no sirve: actualizar el código a la versión de la base
  (`git checkout main`, `dc build`) y repetir el paso 4.
- **Una migración falla a mitad:** esa se revierte entera (su transacción); las anteriores de la
  misma corrida quedan aplicadas. Si la app no arranca así, restaurar el volcado `antes-de-migrar`
  con el runbook R9 y volver al commit anterior.
- **`web` no arranca después del paso 5:** `dc logs --tail 100 web`. Un error de configuración de
  modelos (`configuracion-invalida`) se corrige en la pantalla de modelos (runbook R8).

## Registro de ejecución

Lo ejecuta en la PC alguien que no lo escribió. Lo que falló se corrige en el texto de arriba.

| Fecha | Quién | Commit | Desvíos | Corregido en el texto |
|---|---|---|---|---|
| 2026-10-20 | Implementador del ticket 115 (ensayo sin Docker) | `c6a31d8` | `migrar` en proceso contra una base vacía de pgserver: aplicó 0001 a 0038 y repetido dio «Base al día: 0 migración(es) aplicada(s).». El respaldo previo y `dc build` no se corrieron (sin Docker ni restic); el orden respaldo → `migrar` lo prueba `test_respaldo::test_migrar_respalda_antes_y_no_migra_sin_respaldo`. No cuenta como ejecución en la PC | — |
| | | | | |
